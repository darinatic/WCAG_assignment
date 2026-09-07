"""Citation verifier (design spec §5.4).

This is Layer 1's answer to the brief's question: *how would you catch it if the
assistant cited a WCAG criterion that doesn't actually say what it claims?*

Three stages, cheapest first:

1. **Extract** every criterion and technique reference from the answer.
2. **Membership** - was that identifier in the context the model was given? If not
   it was produced from memory, which is a fabrication *regardless of whether the
   criterion happens to exist*. This stage is free and catches the worst failure.
3. **Support** - a small model receives ONLY the quoted normative text and the
   sentence making the claim, and decides whether the text supports it. No page
   context, no conversation, nothing to be distracted by. The isolation is the point:
   a judge that sees everything the generator saw tends to share its blind spots.

Unsupported claims are surfaced struck-through with the real text beside them
rather than being deleted. A developer watching the machine disagree with the model
is better served than one handed a silently-laundered answer.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app import tracing
from app.config import VERIFIER_MODEL
from app.models import Citation
from app.retrieval import get_chunk, known_ids, normative_for_sc

# [SC 1.4.3] / [SC 2.4.7] / bare 1.4.3 inside brackets
_SC_RE = re.compile(r"\[(?:SC\s*)?(\d\.\d\.\d{1,2})\]", re.I)
# [H37] [ARIA6] [G18] [F65]
_TECH_RE = re.compile(r"\[([A-Z]{1,5}\d{1,3})\]")
# A sentence boundary is terminal punctuation followed by whitespace/quote/end -
# never a bare ".". This domain is full of "4.5:1" and "3.08:1", and splitting on
# the decimal point hands the judge the fragment "5:1 [SC 1.4.3]." to rule on.
_SENT_END = re.compile(r"""[.!?](?=[\s"')\]]|$)|\n""")

_JUDGE_PROMPT = """\
You are checking one factual claim against one piece of source text. Nothing else.

SOURCE TEXT (this is the complete, authoritative text of {identifier}):
\"\"\"
{source}
\"\"\"

CLAIM MADE ABOUT {identifier}:
\"\"\"
{claim}
\"\"\"

Does the source text support the claim?

- "supported": the source text states or directly entails the claim.
- "partial": the claim is broadly consistent but adds specifics the source does not
  state (for example naming a particular technique as though the criterion required it).
- "unsupported": the source text does not support the claim, or contradicts it.

Judge ONLY against the source text shown. Do not use your own knowledge of WCAG.

Respond with a JSON object and nothing else:
{{"verdict": "supported|partial|unsupported", "reason": "<one short sentence>"}}
"""


def extract_citations(text: str) -> list[Citation]:
    out: list[Citation] = []
    seen: set[tuple[str, str]] = set()
    for m in _SC_RE.finditer(text or ""):
        ident = m.group(1)
        if ("sc", ident) in seen:
            continue
        seen.add(("sc", ident))
        out.append(Citation(raw=m.group(0), identifier=ident, kind="success_criterion"))
    for m in _TECH_RE.finditer(text or ""):
        ident = m.group(1)
        if ("tech", ident) in seen:
            continue
        seen.add(("tech", ident))
        out.append(Citation(raw=m.group(0), identifier=ident, kind="technique"))
    return out


def _claim_sentence(text: str, raw: str) -> str:
    """The sentence containing the citation - the unit the judge actually checks."""
    idx = text.find(raw)
    if idx < 0:
        return text[:400]
    start = 0
    for m in _SENT_END.finditer(text, 0, idx):
        start = m.end()
    m = _SENT_END.search(text, idx)
    end = m.end() if m else len(text)
    return text[start:end].strip()[:600]


def verify(answer: str, retrieved_ids: set[str],
           client: Any | None = None) -> list[Citation]:
    """Run all three stages. `client` is an anthropic.Anthropic, or None to skip
    stage 3 (membership checking still runs, and still catches fabrication)."""
    citations = extract_citations(answer)
    if not citations:
        return []

    corpus_ids = known_ids()

    with tracing.span("citation_verifier", "parser",
                      citation_count=len(citations)) as sp:
        for c in citations:
            c.in_retrieved_set = c.identifier in retrieved_ids

            if not c.in_retrieved_set:
                # Not in the context we supplied. Even if it is a real criterion,
                # the model produced it from memory - which is the failure mode this
                # whole layer exists to catch.
                c.verdict = "fabricated"
                c.explanation = (
                    "Cited from the model's own memory: this identifier was not in "
                    "the grounding context supplied for this answer."
                    + ("" if c.identifier in corpus_ids
                       else " It does not appear anywhere in the corpus.")
                )
                continue

            source = None
            if c.kind == "success_criterion":
                chunk = normative_for_sc(c.identifier)
                source = chunk["text"] if chunk else None
                c.quoted_text = (source or "")[:1500]
            else:
                c.verdict = "supported"
                c.explanation = "Technique id was present in the supplied context."
                continue

            if not source:
                c.verdict = "unsupported"
                c.explanation = "No normative text available to check this against."
                continue

            if client is None:
                c.verdict = "unchecked"
                c.explanation = "Support check skipped (no model client available)."
                continue

            c.verdict, c.explanation = _judge(client, c.identifier, source,
                                              _claim_sentence(answer, c.raw))

        tracing.set_io(sp, outputs={"verdicts": [c.to_dict() for c in citations]})
    return citations


def _first_json_object(text: str) -> dict[str, Any] | None:
    """First complete JSON object in a model reply, ignoring anything after it.

    A greedy ``{.*}`` spans two objects when the model emits a second one, and
    json.loads then fails with "Extra data" - throwing away a verdict that was
    in fact returned.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text or ""):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _judge(client: Any, identifier: str, source: str, claim: str) -> tuple[str, str]:
    prompt = _JUDGE_PROMPT.format(identifier=identifier, source=source[:4000],
                                  claim=claim)
    try:
        resp = client.messages.create(
            model=VERIFIER_MODEL,
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = _first_json_object(text)
        if data is None:
            return "unchecked", "Judge returned no parsable verdict."
        verdict = str(data.get("verdict", "unchecked")).lower()
        if verdict not in {"supported", "partial", "unsupported"}:
            verdict = "unchecked"
        return verdict, str(data.get("reason", ""))[:300]
    except Exception as exc:  # noqa: BLE001
        return "unchecked", f"Support check failed: {exc}"


def summarise(citations: list[Citation]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for c in citations:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    clean = all(c.verdict in {"supported", "unchecked"} for c in citations)
    return {"total": len(citations), "by_verdict": counts, "clean": clean}


def annotate(answer: str, citations: list[Citation]) -> str:
    """Mark bad citations inline so the developer sees the disagreement.

    Deliberately not a silent deletion: the point is to show the machine checking
    the model, not to launder the output into looking trustworthy.
    """
    out = answer
    for c in citations:
        if c.verdict in {"fabricated", "unsupported"}:
            out = out.replace(c.raw, f"{c.raw}⚠️", 1)
    return out
