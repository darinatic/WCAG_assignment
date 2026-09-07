"""Grounding retrieval - two paths that answer two different questions.

**Anchor (deterministic).** axe-core already tags every rule with the success
criterion it enforces (`wcag111` -> SC 1.1.1), so "which criterion applies" is a
lookup, not a guess - no embeddings, no similarity threshold, no chance of
confidently citing the wrong criterion. It is the highest-stakes fact in the
system and, conveniently, the cheapest to make exact.

**Semantic (hybrid BM25 + dense, fused with RRF).** Deliberately narrow scope:
this path never decides which criterion applies. It handles the genuinely fuzzy
follow-ups, where the query is prose rather than a rule id - "what if I use
aria-label instead?", "will this break announcement order?"

Local embeddings (bge-small on CPU) rather than a hosted API: no second key, and
the eval suite stays reproducible offline. The encoder is lazily loaded and
degrades to BM25-only if it cannot be loaded, because the normative half of the
corpus never depended on it.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from functools import lru_cache
from typing import Any

import numpy as np

from app.config import (BM25_TOP_K, CHUNKS_PATH, EMBEDDINGS_PATH, EMBED_MODEL,
                        HYBRID_TOP_K, RRF_K, SEMANTIC_TOP_K)

log = logging.getLogger(__name__)


# ==========================================================================
# Anchor - deterministic criterion lookup
# ==========================================================================

_TAG_RE = re.compile(r"^wcag(\d)(\d)(\d+)$")


def tag_to_sc(tag: str) -> str | None:
    """'wcag111' -> '1.1.1';  'wcag2410' -> '2.4.10'."""
    m = _TAG_RE.match(tag.strip().lower())
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}.{m.group(3)}"


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    if not CHUNKS_PATH.exists():
        raise RuntimeError(
            f"Corpus not built. Run `python scripts/build_corpus.py` first ({CHUNKS_PATH})."
        )
    with CHUNKS_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


@lru_cache(maxsize=1)
def _by_sc() -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for c in _load():
        if c.get("sc_id"):
            out.setdefault(c["sc_id"], []).append(c)
    return out


@lru_cache(maxsize=1)
def _by_id() -> dict[str, dict]:
    return {c["id"]: c for c in _load()}


def normative_for_sc(sc_id: str) -> dict | None:
    """The exact normative text of one success criterion."""
    for c in _by_sc().get(sc_id, []):
        if c["flavour"] == "normative" and c["id"] == f"sc:{sc_id}":
            return c
    return None


def failures_for_sc(sc_id: str, limit: int = 4) -> list[dict]:
    """Failure techniques ('this specific markup fails SC X').

    The most directly actionable grounding available - it describes the mistake
    rather than the ideal, which is what the developer is actually looking at.
    """
    out = [c for c in _by_sc().get(sc_id, [])
           if c.get("technique_class") == "failure"]
    return out[:limit]


def sufficient_for_sc(sc_id: str, limit: int = 6) -> list[dict]:
    out = [c for c in _by_sc().get(sc_id, [])
           if c.get("technique_class") == "sufficient" and c["id"].startswith("assoc:")]
    return out[:limit]


def anchor_retrieve(sc_tags: list[str]) -> dict:
    """Entry point. Given axe's wcag tags, return the grounding that is certain.

    Returns normative criteria (never dropped downstream) plus the associated
    failure and sufficient techniques (advisory, dropped first under budget).
    """
    sc_ids, seen = [], set()
    for tag in sc_tags:
        sc = tag_to_sc(tag)
        if sc and sc not in seen:
            seen.add(sc)
            sc_ids.append(sc)

    normative, advisory = [], []
    for sc in sc_ids:
        n = normative_for_sc(sc)
        if n:
            normative.append(n)
        advisory.extend(failures_for_sc(sc))
        advisory.extend(sufficient_for_sc(sc))

    return {
        "sc_ids": sc_ids,
        "normative": normative,
        "advisory": advisory,
        "method": "deterministic-anchor",
    }


def get_chunk(chunk_id: str) -> dict | None:
    return _by_id().get(chunk_id)


def known_ids() -> set[str]:
    """Every citable identifier in the corpus.

    The citation verifier's membership check (design spec §5.4) uses this: a
    criterion the model names that is not in here was invented, regardless of
    whether it happens to exist in the real WCAG.
    """
    ids: set[str] = set()
    for c in _load():
        if c.get("sc_id"):
            ids.add(c["sc_id"])
        if c.get("technique_id"):
            ids.add(c["technique_id"])
    return ids


# ==========================================================================
# Semantic - hybrid BM25 + dense, RRF fusion
# ==========================================================================

_WORD = re.compile(r"[a-z0-9]+")


@lru_cache(maxsize=1)
def _chunks() -> list[dict]:
    with CHUNKS_PATH.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _tokenize(text: str) -> list[str]:
    return _WORD.findall(text.lower())


@lru_cache(maxsize=1)
def _bm25():
    from rank_bm25 import BM25Okapi
    corpus = [_tokenize(f"{c['title']} {c['text']}") for c in _chunks()]
    return BM25Okapi(corpus)


_ENCODER: Any = None
_ENCODER_STATE = "cold"          # cold | loading | ready | failed
_ENCODER_LOCK = threading.Lock()


def _load_encoder() -> None:
    """Actually load bge-small. Measured at ~22s on CPU, which is why nothing that
    a developer is waiting on may call this synchronously."""
    global _ENCODER, _ENCODER_STATE
    try:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer(EMBED_MODEL)
        model.max_seq_length = 256
        with _ENCODER_LOCK:
            _ENCODER, _ENCODER_STATE = model, "ready"
        log.info("dense encoder ready")
    except Exception as exc:  # noqa: BLE001
        with _ENCODER_LOCK:
            _ENCODER, _ENCODER_STATE = None, "failed"
        log.warning("dense encoder unavailable (%s); BM25-only retrieval", exc)


def warm_encoder() -> None:
    """Start loading the encoder in the background. Safe to call repeatedly."""
    global _ENCODER_STATE
    with _ENCODER_LOCK:
        if _ENCODER_STATE in {"loading", "ready", "failed"}:
            return
        _ENCODER_STATE = "loading"
    threading.Thread(target=_load_encoder, name="encoder-warm", daemon=True).start()


def ensure_encoder(timeout: float = 120.0) -> bool:
    """Block until the encoder is ready. For the eval suite and offline scripts,
    where determinism matters more than latency - a run that silently used BM25-only
    because the encoder had not finished loading would not be comparable to one that
    did not."""
    warm_encoder()
    deadline = time.time() + timeout
    while time.time() < deadline:
        with _ENCODER_LOCK:
            if _ENCODER_STATE in {"ready", "failed"}:
                return _ENCODER_STATE == "ready"
        time.sleep(0.2)
    return False


def encoder_state() -> str:
    with _ENCODER_LOCK:
        return _ENCODER_STATE


def _embedder():
    """The encoder if it is ready, otherwise None - never a blocking load.

    Returning None rather than blocking (or raising) is deliberate. Loading bge-small
    costs ~22s on CPU, and it was being paid by whichever developer clicked "explain
    and fix" first after a restart: a 22-second spinner on the one turn the design
    promises is a single fast round trip. Lexical-only retrieval is an acceptable
    degradation for the *advisory* half of the corpus, and the normative half never
    depended on embeddings at all - that is the point of the deterministic anchor -
    so neither a cold encoder nor a failed one can affect which criterion is cited.
    """
    with _ENCODER_LOCK:
        if _ENCODER_STATE == "ready":
            return _ENCODER
        if _ENCODER_STATE == "failed":
            return None
    warm_encoder()
    return None


@lru_cache(maxsize=1)
def _embeddings() -> np.ndarray:
    if not EMBEDDINGS_PATH.exists():
        raise RuntimeError(
            f"Embeddings not built. Run `python scripts/embed_corpus.py` ({EMBEDDINGS_PATH})."
        )
    return np.load(EMBEDDINGS_PATH)


def _rrf(rankings: list[list[int]]) -> dict[int, float]:
    """Reciprocal rank fusion. Scores from BM25 and cosine are not comparable,
    so fuse on rank rather than trying to normalise two different scales."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (RRF_K + rank + 1)
    return scores


def semantic_retrieve(query: str, top_k: int = HYBRID_TOP_K,
                      exclude_ids: set[str] | None = None,
                      flavours: set[str] | None = None) -> list[dict]:
    """Hybrid retrieval over the corpus.

    exclude_ids lets the caller drop anything the anchor already supplied, so the
    two paths never spend budget on the same chunk twice.
    """
    chunks = _chunks()
    exclude_ids = exclude_ids or set()

    lex_scores = _bm25().get_scores(_tokenize(query))
    lex_rank = list(np.argsort(lex_scores)[::-1][:BM25_TOP_K * 3])

    model = _embedder()
    if model is not None:
        qv = model.encode([query], normalize_embeddings=True)[0]
        sims = _embeddings() @ qv
        dense_rank = list(np.argsort(sims)[::-1][:SEMANTIC_TOP_K * 3])
        fused = _rrf([lex_rank, dense_rank])
        method = "hybrid-rrf"
    else:
        sims = np.zeros(len(chunks), dtype=np.float32)
        fused = _rrf([lex_rank])
        # Distinguish "still warming" from "broken" - they need different responses
        # from whoever is reading the inspector.
        method = ("bm25-only (dense encoder still loading)"
                  if encoder_state() in {"cold", "loading"}
                  else "bm25-only (dense encoder unavailable)")

    out: list[dict] = []
    for idx in sorted(fused, key=lambda i: fused[i], reverse=True):
        c = chunks[int(idx)]
        if c["id"] in exclude_ids:
            continue
        if flavours and c["flavour"] not in flavours:
            continue
        out.append({**c,
                    "score": round(float(fused[idx]), 5),
                    "bm25": round(float(lex_scores[idx]), 3),
                    "cosine": round(float(sims[idx]), 3),
                    "method": method})
        if len(out) >= top_k:
            break
    return out
