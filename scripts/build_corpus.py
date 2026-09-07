"""Build the grounding corpus.

Run once: `python scripts/build_corpus.py`. Fetched pages are cached under
corpus_data/raw/ and the emitted chunks.jsonl is committed, so retrieval - and
therefore the eval suite - needs no network at run time.

Flavour is the load-bearing field (design spec §5.2). It decides how a chunk is
*presented* to the model, not just whether it is retrieved:

    normative     the requirement itself. Quoted verbatim, never paraphrased.
    understanding W3C-authored explanation of intent. Informative.
    technique     a documented way to satisfy (or fail) a criterion. Informative.
    community     widely-used practice from outside W3C. Lowest authority.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import CORPUS_DIR, CHUNKS_PATH  # noqa: E402

RAW = CORPUS_DIR / "raw"
RAW.mkdir(parents=True, exist_ok=True)

WCAG_JSON = CORPUS_DIR / "wcag22.json"
UNDERSTANDING_BASE = "https://www.w3.org/WAI/WCAG22/Understanding"
TECHNIQUES_BASE = "https://www.w3.org/WAI/WCAG22/Techniques"

# The success criteria our recipes actually cover. Fetching Understanding pages
# for all 87 would be theatre (design spec §5.3).
IN_SCOPE_SC = ["1.1.1", "1.3.1", "1.4.3", "2.4.4", "4.1.2"]

# Techniques worth full text rather than just a title. Failure techniques (F-codes)
# earn their place: "this specific markup fails SC X" is the most directly
# actionable grounding in the whole corpus.
FULL_TEXT_TECHNIQUES = [
    ("H37", "html"),      # alt on img
    ("H67", "html"),      # null alt for decorative images
    ("H30", "html"),      # link text describing purpose
    ("H91", "html"),      # HTML form controls and links (accessible name)
    ("H44", "html"),      # label associated with form control
    ("ARIA6", "aria"),    # aria-label
    ("ARIA10", "aria"),   # aria-labelledby for non-text content
    ("ARIA16", "aria"),   # aria-labelledby for form controls
    ("G18", "general"),   # 4.5:1 contrast
    ("G145", "general"),  # 3:1 contrast (large text)
    ("G148", "general"),  # not specifying background/foreground
    ("F65", "failures"),  # failure: img with no alt
    ("F68", "failures"),  # failure: form control with no label
    ("F89", "failures"),  # failure: null alt on an image that is the only link content
]

OTHER_SOURCES = [
    ("https://www.w3.org/TR/html-aria/", "html-aria", "normative",
     "ARIA in HTML (W3C Recommendation)"),
    ("https://web.dev/articles/optimize-lcp", "web.dev", "community",
     "Optimize Largest Contentful Paint"),
    ("https://web.dev/articles/lcp", "web.dev", "community",
     "Largest Contentful Paint (LCP)"),
]

TECH_PATH = {"general": "general", "html": "html", "aria": "aria", "css": "css",
             "failure": "failures", "failures": "failures", "smil": "smil",
             "text": "text", "flash": "flash", "silverlight": "silverlight",
             "script": "client-side-script", "client-side-script": "client-side-script",
             "server-side-script": "server-side-script", "pdf": "pdf"}


def fetch(url: str, cache_name: str) -> str | None:
    """Fetch with an on-disk cache so re-runs are offline and reproducible."""
    path = RAW / cache_name
    if path.exists():
        return path.read_text(encoding="utf-8")
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True,
                      headers={"User-Agent": "a11y-assistant-corpus-builder/1.0"})
        if r.status_code != 200:
            print(f"  ! {url} -> HTTP {r.status_code}", file=sys.stderr)
            return None
        path.write_text(r.text, encoding="utf-8")
        time.sleep(0.3)  # be polite to w3.org
        return r.text
    except Exception as exc:  # noqa: BLE001
        print(f"  ! {url} -> {exc}", file=sys.stderr)
        return None


def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "nav", "header", "footer"]):
        bad.decompose()
    text = soup.get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def split_sections(html: str, max_chars: int = 1400) -> list[tuple[str, str]]:
    """Split a W3C page into (heading, text) pairs, then hard-cap each piece."""
    soup = BeautifulSoup(html, "lxml")
    for bad in soup(["script", "style", "nav", "footer"]):
        bad.decompose()
    body = soup.find("main") or soup.find("body") or soup
    out: list[tuple[str, str]] = []
    current_head, buf = "Introduction", []

    for el in body.find_all(["h1", "h2", "h3", "h4", "p", "li", "pre", "dd", "dt"]):
        if el.name in ("h1", "h2", "h3", "h4"):
            if buf:
                out.append((current_head, " ".join(buf)))
                buf = []
            current_head = el.get_text(" ", strip=True)
        else:
            t = el.get_text(" ", strip=True)
            if t:
                buf.append(t)
    if buf:
        out.append((current_head, " ".join(buf)))

    chunks: list[tuple[str, str]] = []
    for head, text in out:
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 40:
            continue
        while len(text) > max_chars:
            cut = text.rfind(". ", 0, max_chars)
            cut = cut + 1 if cut > max_chars // 2 else max_chars
            chunks.append((head, text[:cut].strip()))
            text = text[cut:].strip()
        if text:
            chunks.append((head, text))
    return chunks


def walk_techniques(node, acc: list[dict], klass: str) -> None:
    """wcag.json nests techniques in situations/groups; flatten them."""
    if isinstance(node, list):
        for item in node:
            walk_techniques(item, acc, klass)
        return
    if not isinstance(node, dict):
        return
    if "id" in node and "title" in node and "technology" in node:
        acc.append({"id": node["id"], "title": node["title"],
                    "technology": node.get("technology", "general"), "class": klass})
    for key in ("techniques", "groups", "and", "or"):
        if key in node:
            walk_techniques(node[key], acc, klass)


def main() -> None:
    wcag = json.loads(WCAG_JSON.read_text(encoding="utf-8"))
    chunks: list[dict] = []

    # ---- 1. normative success criteria -------------------------------------
    sc_index: dict[str, dict] = {}
    for principle in wcag["principles"]:
        for guideline in principle["guidelines"]:
            for sc in guideline["successcriteria"]:
                sc_index[sc["num"]] = sc
                chunks.append({
                    "id": f"sc:{sc['num']}",
                    "flavour": "normative",
                    "standard": "wcag22",
                    "sc_id": sc["num"],
                    "level": sc["level"],
                    "title": f"SC {sc['num']} {sc['handle']} (Level {sc['level']})",
                    "text": html_to_text(sc["content"]),
                    "source_url": f"https://www.w3.org/TR/WCAG22/#{sc['id']}",
                })
    print(f"normative success criteria: {len(chunks)}")

    # ---- 2. glossary terms (normative definitions) --------------------------
    n_terms = 0
    for term in wcag.get("terms", []):
        body = html_to_text(term.get("definition", ""))
        if not body:
            continue
        chunks.append({
            "id": f"term:{term['id']}",
            "flavour": "normative", "standard": "wcag22", "sc_id": None, "level": None,
            "title": f"Definition: {term['name']}",
            "text": body,
            "source_url": f"https://www.w3.org/TR/WCAG22/#dfn-{term['id']}",
        })
        n_terms += 1
    print(f"glossary terms: {n_terms}")

    # ---- 3. technique associations (free, from wcag.json) -------------------
    n_assoc = 0
    for num, sc in sc_index.items():
        tech = sc.get("techniques") or {}
        acc: list[dict] = []
        for klass in ("sufficient", "advisory", "failure"):
            walk_techniques(tech.get(klass, []), acc, klass)
        seen = set()
        for t in acc:
            key = (num, t["id"])
            if key in seen:
                continue
            seen.add(key)
            path = TECH_PATH.get(t["technology"], "general")
            chunks.append({
                "id": f"assoc:{num}:{t['id']}",
                "flavour": "technique", "standard": "wcag22",
                "sc_id": num, "level": sc["level"],
                "technique_id": t["id"], "technique_class": t["class"],
                "title": f"{t['id']} ({t['class']} for SC {num}): {t['title']}",
                "text": (f"Technique {t['id']} is listed as {t['class']} for "
                         f"SC {num} ({sc['handle']}). {t['title']}."),
                "source_url": f"{TECHNIQUES_BASE}/{path}/{t['id']}",
            })
            n_assoc += 1
    print(f"technique associations: {n_assoc}")

    # ---- 4. Understanding documents for in-scope SCs ------------------------
    n_und = 0
    for num in IN_SCOPE_SC:
        sc = sc_index[num]
        slug = sc["id"]
        html = fetch(f"{UNDERSTANDING_BASE}/{slug}.html", f"understanding-{slug}.html")
        if not html:
            continue
        for i, (head, text) in enumerate(split_sections(html)):
            chunks.append({
                "id": f"und:{num}:{i}",
                "flavour": "understanding", "standard": "wcag22",
                "sc_id": num, "level": sc["level"],
                "title": f"Understanding SC {num} — {head}",
                "text": text,
                "source_url": f"{UNDERSTANDING_BASE}/{slug}.html",
            })
            n_und += 1
    print(f"understanding chunks: {n_und}")

    # ---- 5. full-text techniques -------------------------------------------
    n_tech = 0
    for tid, tech in FULL_TEXT_TECHNIQUES:
        html = fetch(f"{TECHNIQUES_BASE}/{tech}/{tid}", f"technique-{tid}.html")
        if not html:
            continue
        for i, (head, text) in enumerate(split_sections(html)):
            chunks.append({
                "id": f"tech:{tid}:{i}",
                "flavour": "technique", "standard": "wcag22",
                "sc_id": None, "level": None,
                "technique_id": tid,
                "technique_class": "failure" if tech == "failures" else "sufficient",
                "title": f"Technique {tid} — {head}",
                "text": text,
                "source_url": f"{TECHNIQUES_BASE}/{tech}/{tid}",
            })
            n_tech += 1
    print(f"full-text technique chunks: {n_tech}")

    # ---- 6. ARIA-in-HTML and web.dev LCP -----------------------------------
    n_other = 0
    for url, standard, flavour, label in OTHER_SOURCES:
        name = re.sub(r"[^a-z0-9]+", "-", url.lower()).strip("-") + ".html"
        html = fetch(url, name)
        if not html:
            continue
        for i, (head, text) in enumerate(split_sections(html)):
            chunks.append({
                "id": f"{standard}:{i}:{abs(hash(head)) % 10000}",
                "flavour": flavour, "standard": standard,
                "sc_id": None, "level": None,
                "title": f"{label} — {head}",
                "text": text,
                "source_url": url,
            })
            n_other += 1
    print(f"other-source chunks: {n_other}")

    CHUNKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CHUNKS_PATH.open("w", encoding="utf-8") as fh:
        for c in chunks:
            fh.write(json.dumps(c, ensure_ascii=False) + "\n")

    by_flavour: dict[str, int] = {}
    for c in chunks:
        by_flavour[c["flavour"]] = by_flavour.get(c["flavour"], 0) + 1
    print(f"\nwrote {len(chunks)} chunks -> {CHUNKS_PATH}")
    print("by flavour:", by_flavour)


if __name__ == "__main__":
    main()
