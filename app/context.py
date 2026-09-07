"""Context engineering: what the model actually receives on each turn.

One pipeline, three stages, kept in one module because they are one story:

    recipes  ->  which facts fully determine THIS class of fix?
    budget   ->  what fits, and what gets dropped (with the reason recorded)
    prompt   ->  how it is presented, and where the cache breakpoints go

The premise, stated plainly because it is the most exposed claim in the design:
**the DOM fits.** claude-opus-5 has a 1M-token window and a government homepage is
a few hundred thousand tokens at worst. Capacity is not the constraint. What
cutting buys is cost per turn, time-to-first-token, and - the one that matters -
quality. Handed the whole DOM a model writes *generic* advice, because nothing in
the payload tells it which 40 nodes out of 4,000 are the answer. Selection is the
signal.

Two honest notes on measurement. Budget decisions happen *before* the prompt
exists, so they use a fast local estimate; after assembly exactly one
`messages.count_tokens` call gives the authoritative total, and the inspector
shows both numbers plus the drift, so a badly calibrated estimator is obvious
rather than quietly wrong. Anything that does not fit keeps its `drop_reason` and
is still shipped to the inspector - what got cut tells you more than what stayed.

And on presentation: **flavour becomes authority.** A normative criterion is
quoted verbatim and labelled as the requirement; a technique is labelled
informative; community guidance is labelled as neither. The retrieval difference
between these is small - the presentation difference is what stops the model
asserting a technique as though it were a rule. Cache breakpoints are placed by
stability, not by section: frozen content first, volatile last, so a ten-turn
conversation pays for the page context once.
"""
from __future__ import annotations

import json
from typing import Callable

from app import db
from app.config import BUDGET, NEVER_DROP
from app.contrast import assess, parse_color
from app.models import ContextBlock, ContextBundle


# ==========================================================================
# Recipes - issue-type context extraction
# ==========================================================================

# rule id -> recipe name
RULE_RECIPES: dict[str, str] = {
    # naming: the answer is *what should the name say*, which needs surrounding prose
    "image-alt": "naming", "input-image-alt": "naming", "area-alt": "naming",
    "link-name": "naming", "button-name": "naming", "label": "naming",
    "aria-input-field-name": "naming", "empty-heading": "naming",
    # contrast: five facts fully determine the verdict
    "color-contrast": "contrast", "color-contrast-enhanced": "contrast",
    # aria: a role-graph problem, where surrounding text is noise
    "aria-allowed-attr": "aria", "aria-required-parent": "aria",
    "aria-required-children": "aria", "aria-roles": "aria",
    "aria-valid-attr": "aria", "aria-valid-attr-value": "aria",
    "aria-required-attr": "aria", "aria-allowed-role": "aria",
    # performance: a chain problem
    "lcp": "lcp",
}

MAX_NEARBY_CHARS = 400


# ---------------------------------------------------------------- helpers

def _attrs(node: dict) -> dict:
    return json.loads(node.get("attrs_json") or "{}")


def _styles(node: dict) -> dict:
    return json.loads(node.get("styles_json") or "{}")


def ancestors(page_id: int, selector: str, limit: int = 12) -> list[dict]:
    """Walk up the indexed tree. Cheap - it is a primary-key lookup per level."""
    out: list[dict] = []
    node = db.get_node(page_id, selector)
    while node and node.get("parent_selector") and len(out) < limit:
        parent = db.get_node(page_id, node["parent_selector"])
        if not parent:
            break
        out.append(parent)
        node = parent
    return out


def painting_background(page_id: int, node: dict) -> tuple[str, dict | None]:
    """Find the ancestor that actually paints the background behind this element.

    This is the whole point of the contrast recipe. An element with
    `background-color: rgba(0,0,0,0)` is transparent: the colour a user sees comes
    from an ancestor. An assistant that assumes "white page background" computes a
    plausible, confident, wrong ratio - and then recommends the wrong colour.
    """
    own = _styles(node).get("background-color", "")
    parsed = parse_color(own)
    if parsed and parsed[3] > 0:
        return own, node
    for anc in ancestors(page_id, node["selector"]):
        c = _styles(anc).get("background-color", "")
        p = parse_color(c)
        if p and p[3] > 0:
            return c, anc
    return "rgb(255, 255, 255)", None  # canvas default


def nearby_text(page_id: int, node: dict, budget: int = MAX_NEARBY_CHARS) -> list[str]:
    """Text near the element that could plausibly serve as its accessible name."""
    out: list[str] = []
    parent_sel = node.get("parent_selector")
    if parent_sel:
        for sib in db.get_children(page_id, parent_sel):
            if sib["selector"] == node["selector"]:
                continue
            t = (sib.get("own_text") or "").strip()
            if t:
                out.append(f"<{sib['tag']}> {t}")
            for child in db.get_children(page_id, sib["selector"]):
                ct = (child.get("own_text") or "").strip()
                if ct:
                    out.append(f"<{sib['tag']}><{child['tag']}> {ct}")
    trimmed, used = [], 0
    for line in out:
        if used + len(line) > budget:
            break
        trimmed.append(line)
        used += len(line)
    return trimmed


def preceding_heading(page_id: int, node: dict) -> str | None:
    """Nearest heading above this node in document order."""
    target_idx = node.get("idx")
    if target_idx is None:
        return None
    best = None
    for n in db.all_nodes(page_id):
        if n["idx"] >= target_idx:
            break
        if n["tag"] in {"h1", "h2", "h3", "h4", "h5", "h6"} and (n.get("own_text") or "").strip():
            best = n
    return f"<{best['tag']}> {best['own_text']}" if best else None


def fmt_node(node: dict, include_html: bool = True) -> str:
    a = _attrs(node)
    attr_str = " ".join(f'{k}="{v}"' for k, v in a.items()) if a else ""
    head = f"<{node['tag']}{' ' + attr_str if attr_str else ''}>"
    lines = [head]
    if node.get("own_text"):
        lines.append(f"  text: {node['own_text']}")
    if include_html and node.get("outer_html") and len(node["outer_html"]) < 500:
        lines.append(f"  html: {node['outer_html']}")
    return "\n".join(lines)


def _block(kind: str, label: str, content: str, source: str) -> ContextBlock:
    return ContextBlock(kind=kind, label=label, content=content, source=source)


# ---------------------------------------------------------------- recipes

def recipe_naming(page_id: int, issue: dict, node: dict | None) -> list[ContextBlock]:
    """image-alt / link-name / label.

    The developer's real question is not "does this need a name" - axe already said
    so. It is "what should the name SAY". That answer lives in the surrounding
    prose, never in the DOM shape, so this recipe spends its budget on text.
    """
    blocks: list[ContextBlock] = []
    if not node:
        return blocks

    a = _attrs(node)
    parts = [f"Failing element ({issue['rule']}):", fmt_node(node)]
    if node.get("landmark_path"):
        parts.append(f"\nLandmark ancestry: {node['landmark_path']}")

    # A filename is often the only hint of intent the developer left behind.
    src = a.get("src") or a.get("href")
    if src:
        parts.append(f"Resource filename: {src.rsplit('/', 1)[-1]}")

    # A wrapping <a> changes the answer completely: the alt must describe the link
    # destination, not the picture.
    for anc in ancestors(page_id, node["selector"], limit=4):
        if anc["tag"] in {"a", "button", "figure", "label"}:
            parts.append(f"\nEnclosing <{anc['tag']}>:\n{fmt_node(anc, include_html=False)}")
            if anc["tag"] == "figure":
                for child in db.get_children(page_id, anc["selector"]):
                    if child["tag"] == "figcaption":
                        parts.append(f"  figcaption: {child.get('own_text')}")
            if anc["tag"] == "a" and anc.get("attrs_json"):
                href = _attrs(anc).get("href")
                if href:
                    parts.append(f"  link destination: {href}")
    blocks.append(_block("page_evidence", "Failing element + naming context",
                         "\n".join(parts), node["selector"]))

    near = nearby_text(page_id, node)
    if near:
        blocks.append(_block("page_evidence", "Nearby text (candidate name sources)",
                             "\n".join(near), node["selector"]))

    heading = preceding_heading(page_id, node)
    if heading:
        blocks.append(_block("page_evidence", "Nearest preceding heading",
                             heading, node["selector"]))
    return blocks


def recipe_contrast(page_id: int, issue: dict, node: dict | None) -> list[ContextBlock]:
    """color-contrast.

    Five facts fully determine the verdict: foreground, the background actually
    painted behind it, font size, font weight, and the level. The recipe computes
    the ratio itself rather than shipping raw colours and hoping - see app/contrast.py.
    """
    blocks: list[ContextBlock] = []
    if not node:
        return blocks

    s = _styles(node)
    fg = s.get("color", "")
    bg, bg_node = painting_background(page_id, node)
    verdict = assess(fg, bg, s.get("font-size", "16px"), s.get("font-weight", "400"))

    parts = [f"Failing element ({issue['rule']}):", fmt_node(node, include_html=False)]
    if node.get("landmark_path"):
        parts.append(f"Landmark ancestry: {node['landmark_path']}")
    parts.append("")
    parts.append("Computed colour facts:")
    parts.append(f"  foreground (this element):  {fg}")
    if bg_node is not None and bg_node["selector"] != node["selector"]:
        parts.append(f"  this element's own background: {s.get('background-color')} (transparent)")
        parts.append(f"  background is painted by:    <{bg_node['tag']}"
                     f" class=\"{_attrs(bg_node).get('class', '')}\"> -> {bg}")
        parts.append(f"    ancestor selector: {bg_node['selector']}")
    else:
        parts.append(f"  background (this element):  {bg}")
    parts.append(f"  font-size: {s.get('font-size')}   font-weight: {s.get('font-weight')}")
    parts.append("")
    parts.append("Contrast computed by the system (do not recompute):")
    parts.append(f"  ratio    = {verdict['ratio']}:1")
    parts.append(f"  required = {verdict['required']}:1")
    parts.append(f"  {verdict['threshold_reason']}")
    parts.append(f"  result   = {'PASS' if verdict['passes'] else 'FAIL'}")

    blocks.append(_block("page_evidence", "Contrast facts (computed)",
                         "\n".join(parts), node["selector"]))

    bgi = s.get("background-image", "none")
    if bgi and bgi != "none":
        blocks.append(_block("page_evidence", "Background image present",
                             f"This element has background-image: {bgi}. A ratio "
                             f"against a flat colour may not reflect what a user sees.",
                             node["selector"]))
    return blocks


def recipe_aria(page_id: int, issue: dict, node: dict | None) -> list[ContextBlock]:
    """aria-allowed-attr and friends.

    The brief's 'cryptic' case. This is a role-graph problem: what matters is the
    element's explicit and implicit role, the offending attribute, and the roles of
    its parent and children. Surrounding prose is pure noise here, so unlike the
    naming recipe this one spends nothing on text.
    """
    blocks: list[ContextBlock] = []
    if not node:
        return blocks

    a = _attrs(node)
    aria_attrs = {k: v for k, v in a.items() if k.startswith("aria-") or k == "role"}

    parts = [f"Failing element ({issue['rule']}):", fmt_node(node, include_html=False), ""]
    parts.append("Role resolution:")
    parts.append(f"  explicit role (role=...):  {node.get('explicit_role') or '(none)'}")
    parts.append(f"  implicit role (from <{node['tag']}>): {node.get('implicit_role') or '(none)'}")
    parts.append(f"  effective role: {node.get('explicit_role') or node.get('implicit_role') or '(none)'}")
    parts.append("")
    parts.append("ARIA attributes present on this element:")
    for k, v in (aria_attrs or {"(none)": ""}).items():
        parts.append(f"  {k}=\"{v}\"")

    parent_sel = node.get("parent_selector")
    if parent_sel:
        parent = db.get_node(page_id, parent_sel)
        if parent:
            parts.append("")
            parts.append("Parent (matters for required-parent / required-children rules):")
            parts.append(f"  <{parent['tag']}> explicit={parent.get('explicit_role') or '-'} "
                         f"implicit={parent.get('implicit_role') or '-'}")

    children = db.get_children(page_id, node["selector"])[:8]
    if children:
        parts.append("")
        parts.append("Children:")
        for c in children:
            parts.append(f"  <{c['tag']}> explicit={c.get('explicit_role') or '-'} "
                         f"implicit={c.get('implicit_role') or '-'}")

    if issue.get("failure_summary"):
        parts.append("")
        parts.append(f"axe failure summary:\n{issue['failure_summary']}")

    blocks.append(_block("page_evidence", "Role graph around the failing element",
                         "\n".join(parts), node["selector"]))
    return blocks


def recipe_lcp(page_id: int, issue: dict, node: dict | None) -> list[ContextBlock]:
    """Largest Contentful Paint.

    LCP is a *chain* problem: the blocker is usually three levels up in <head>, not
    the element itself. So this recipe deliberately spends most of its budget on
    things that are not the failing element - render-blocking resources in document
    order, the element's own request, and which hints are absent.
    """
    blocks: list[ContextBlock] = []
    page = db.get_page(page_id) or {}
    perf = json.loads(page.get("perf_json") or "{}")
    lcp = perf.get("lcp") or {}

    parts = [f"LCP: {lcp.get('value_ms')}ms  (Core Web Vitals 'good' threshold: 2500ms)",
             f"LCP element: <{lcp.get('element_tag')}>  {lcp.get('element_selector')}",
             f"LCP resource: {lcp.get('url') or '(text node, no request)'}"]

    resources = perf.get("resources") or []
    by_url = {r["url"]: r for r in resources}
    if lcp.get("url") and lcp["url"] in by_url:
        r = by_url[lcp["url"]]
        parts.append(f"  transfer size: {r['transfer_size']:,} bytes")
        parts.append(f"  decoded size:  {r['decoded_size']:,} bytes")
        parts.append(f"  discovered at: {r['start']}ms, took {r['duration']}ms")

    if node:
        a = _attrs(node)
        parts.append("")
        parts.append("LCP element markup:")
        parts.append(f"  <{node['tag']} " + " ".join(f'{k}="{v}"' for k, v in a.items()) + ">")
        parts.append(f"  loading={a.get('loading') or '(not set)'}  "
                     f"fetchpriority={a.get('fetchpriority') or '(not set)'}")

    blocking = perf.get("render_blocking") or []
    parts.append("")
    parts.append("Render-blocking resources in <head>, in document order:")
    for b in blocking:
        flag = "BLOCKING" if b["blocking"] else "non-blocking"
        parts.append(f"  [{b['document_order']}] {b['kind']}: {b['href']}  "
                     f"media={b['media']}  -> {flag}")
        res = next((r for r in resources if r["url"].endswith(str(b["href"]).lstrip("./"))), None)
        if res:
            parts.append(f"        {res['transfer_size']:,} bytes, {res['duration']}ms")

    hints = perf.get("resource_hints") or []
    parts.append("")
    parts.append(f"Resource hints present: {hints if hints else '(none - no preload/preconnect)'}")
    nav = perf.get("navigation") or {}
    if nav:
        parts.append(f"Navigation: responseEnd={nav.get('response_end')}ms  "
                     f"DCL={nav.get('dom_content_loaded')}ms  load={nav.get('load_event')}ms")

    blocks.append(_block("page_evidence", "LCP chain (element + blockers)",
                         "\n".join(parts), lcp.get("element_selector", "")))
    return blocks


def recipe_fallback(page_id: int, issue: dict, node: dict | None) -> list[ContextBlock]:
    """Honest degradation for the ~40 axe rules without a dedicated recipe.

    Labelled as generic in the UI so the developer knows the assistant is working
    with less context than usual, rather than silently getting a vaguer answer.
    """
    blocks: list[ContextBlock] = []
    parts = [f"Failing element ({issue['rule']}):"]
    if node:
        parts.append(fmt_node(node))
        if node.get("landmark_path"):
            parts.append(f"Landmark ancestry: {node['landmark_path']}")
        for anc in ancestors(page_id, node["selector"], limit=2):
            parts.append(f"Ancestor <{anc['tag']}> class="
                         f"\"{_attrs(anc).get('class', '')}\"")
        near = nearby_text(page_id, node, budget=200)
        if near:
            parts.append("Nearby text: " + " | ".join(near))
    else:
        parts.append(issue.get("node_html") or "(element not found in index)")
    if issue.get("failure_summary"):
        parts.append(f"\naxe failure summary:\n{issue['failure_summary']}")
    parts.append("\n[Generic context: no dedicated recipe for this rule.]")
    blocks.append(_block("page_evidence", "Generic context (no recipe for this rule)",
                         "\n".join(parts), node["selector"] if node else ""))
    return blocks


RECIPE_FNS: dict[str, Callable] = {
    "naming": recipe_naming,
    "contrast": recipe_contrast,
    "aria": recipe_aria,
    "lcp": recipe_lcp,
    "fallback": recipe_fallback,
}


def select_recipe(rule: str) -> str:
    return RULE_RECIPES.get(rule, "fallback")


def build_evidence(page_id: int, issue: dict) -> tuple[str, list[ContextBlock]]:
    """Entry point: (recipe_name, evidence blocks) for one issue."""
    name = select_recipe(issue["rule"])
    selector = issue.get("node_selector") or issue.get("target")
    node = db.get_node(page_id, selector) if selector else None
    blocks = RECIPE_FNS[name](page_id, issue, node)
    return name, blocks


# ==========================================================================
# Budget - what fits, and what was dropped and why
# ==========================================================================

# Claude's tokenizer averages ~3.6 characters per token on English prose; code and
# markup run denser, so we use a slightly conservative divisor and reconcile after.
CHARS_PER_TOKEN = 3.6


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, int(len(text) / CHARS_PER_TOKEN))


def apply_budget(blocks: list[ContextBlock]) -> ContextBundle:
    """Admit blocks in priority order, per-kind, until each kind's budget is spent.

    Priority is encoded by the order of BUDGET's keys: system and the normative
    criterion are admitted first and are never evicted; advisory spec chunks are
    the first thing to go, because they carry the least authority.
    """
    bundle = ContextBundle()
    for b in blocks:
        if not b.token_count:
            b.token_count = estimate_tokens(b.content)

    spent: dict[str, int] = {k: 0 for k in BUDGET}

    # Admission order: kinds in BUDGET order, and within a kind, original order.
    order = list(BUDGET.keys())
    ranked = sorted(
        blocks,
        key=lambda b: (order.index(b.kind) if b.kind in order else len(order),),
    )

    for b in ranked:
        kind_budget = BUDGET.get(b.kind)
        if kind_budget is None:
            b.included = True
            bundle.add(b)
            continue

        if b.kind in NEVER_DROP:
            b.included = True
            spent[b.kind] += b.token_count
            bundle.add(b)
            continue

        if spent[b.kind] + b.token_count <= kind_budget:
            b.included = True
            spent[b.kind] += b.token_count
        else:
            b.included = False
            b.drop_reason = (
                f"{b.kind} budget exhausted "
                f"({spent[b.kind]}/{kind_budget} tokens used; this block needs "
                f"{b.token_count})"
            )
        bundle.add(b)

    bundle.budget = {
        kind: {"allocated": limit, "used": spent.get(kind, 0),
               "remaining": max(0, limit - spent.get(kind, 0))}
        for kind, limit in BUDGET.items()
    }
    return bundle


def trim_conversation(history: list[dict], limit_tokens: int) -> tuple[list[dict], int]:
    """Drop oldest turns until the conversation fits.

    Deliberately a drop-oldest guard rather than summarisation (design spec §10).
    Summarisation is better, but it is another model call in the latency path and
    another thing that can silently lose a constraint the developer stated three
    turns ago. Dropping is cruder and observable; the inspector says exactly how
    many turns went and why.
    """
    kept: list[dict] = []
    used = 0
    dropped = 0
    for msg in reversed(history):
        text = msg.get("content") if isinstance(msg.get("content"), str) else str(msg.get("content"))
        cost = estimate_tokens(text)
        if used + cost > limit_tokens and kept:
            dropped = len(history) - len(kept)
            break
        kept.append(msg)
        used += cost
    kept.reverse()
    return kept, dropped


# ==========================================================================
# Prompt - flavour becomes authority; cache breakpoints
# ==========================================================================

SYSTEM_PROMPT = """\
You are an accessibility and web-performance engineer helping a developer fix a \
specific issue that an automated scan found on their own page. They are a \
competent developer but not an accessibility specialist. They are about to ship \
whatever you tell them.

## What you are given
Every turn you receive a context block containing:
- The exact normative text of the relevant WCAG success criterion, when one applies.
- Informative supporting material (Understanding documents, Techniques).
- Evidence extracted from the developer's actual page: the failing element, and the
  specific surrounding facts that determine the fix for this class of issue.

## Grounding rules - these are not style preferences
1. The NORMATIVE block is the requirement. Quote or paraphrase it faithfully. Never
   state that a criterion requires something it does not say.
2. Blocks labelled INFORMATIVE (Understanding, Techniques) describe accepted ways to
   satisfy a criterion. They are not themselves requirements. Never write "WCAG
   requires you to use aria-label" - WCAG requires an accessible name; aria-label is
   one technique among several.
3. Blocks labelled COMMUNITY are not W3C material. Attribute them as general
   practice, never as a standard.
4. Cite ONLY identifiers that appear in the context you were given. Write success
   criteria as [SC 1.4.3] and techniques as [H37]. If you believe a criterion is
   relevant but it is not in your context, say so plainly instead of citing it from
   memory - your recollection of criterion numbers is exactly the thing that is
   unreliable here.
5. Numbers that the system computed for you - contrast ratios, thresholds, byte
   sizes, timings - are authoritative. Do not recompute them and do not contradict
   them. If a computed value looks wrong to you, say so rather than substituting
   your own arithmetic.

## What a good answer looks like
Write for a competent developer who is not an accessibility specialist. Plain words
over jargon: "screen readers announce this as just 'link'" beats "the accessible name
is not programmatically determinable". Use the specialist term only when they will
meet it again in a ticket or a lint rule, and gloss it once when you do.

Structure your first response as:

**What's wrong** - one or two sentences. What is actually broken on this page, in
plain language, no rule IDs.

**Who it affects** - one or two sentences, concrete. "A screen reader user hears
'link' with no indication of where it goes", not "users with disabilities may be
impacted".

**The fix** - corrected markup for THIS element, in a code block, using the real
content from this page. Not a generic template. If the right text depends on editorial
intent you cannot see, give your best inference from the surrounding content and say
what you inferred it from. One code block. Do not also show the CSS, the JS and three
alternatives unless the developer asks.

**Watch out for** - ONLY if there is a real trap that will bite them. This section is
usually absent. Never pad it.

## Length - treat this as a hard constraint
Keep the whole first answer under about 300 words, not counting the code block. Keep
the code block to the element you are fixing plus the minimum surrounding markup
needed to place it.

Do not restate the context block back to the developer. Do not enumerate criteria
other than the one being violated. Do not list alternative approaches unless the fix
genuinely depends on a choice only they can make - and then give two, not five. Do not
append a closing summary; the sections above already are the summary.

A follow-up answer is narrower, not thinner: answer the question that was asked rather
than restating the whole issue, but keep citing this page's real values, elements and
text. A follow-up that drops to generic advice has lost the only thing that made it
worth asking.

Length is not a style preference here. The developer clicked a button and is watching
a spinner; every extra paragraph is time spent waiting for text they did not ask for,
and a long answer costs them twice - once reading it, once working out which part
matters.

## Tool results
When a tool returns a status other than "ok" - not_found, stale, ambiguous, timeout -
you MUST tell the developer what happened. A stale page index or a selector that no
longer matches is important information, not an inconvenience to work around. Never
answer from the original context as though the tool had succeeded.

## Scope
You help with accessibility and web performance on the page in front of you. If
asked something adjacent - which framework to adopt, rewriting the whole page,
general career or design questions - decline in one sentence and offer the nearest
thing you can actually help with. Do not refuse rudely and do not lecture.
"""

FLAVOUR_HEADERS = {
    "normative": ("NORMATIVE - THIS IS THE REQUIREMENT",
                  "Quoted verbatim from the W3C Recommendation."),
    "understanding": ("INFORMATIVE - W3C Understanding document",
                      "Explains intent. Not itself a requirement."),
    "technique": ("INFORMATIVE - W3C Technique",
                  "One accepted way to satisfy or fail the criterion. Not a requirement."),
    "community": ("COMMUNITY - not a W3C document",
                  "Widely-used practice. Lowest authority in this context."),
}


def spec_block(chunk: dict) -> ContextBlock:
    """Turn a retrieved chunk into a presentation-differentiated context block."""
    header, caveat = FLAVOUR_HEADERS.get(
        chunk["flavour"], ("REFERENCE", ""))
    kind = "spec_normative" if chunk["flavour"] == "normative" else "spec_advisory"
    body = (
        f"[{header}]\n"
        f"{caveat}\n"
        f"Source: {chunk.get('source_url', '')}\n"
        f"--- {chunk['title']} ---\n"
        f"{chunk['text']}"
    )
    return ContextBlock(
        kind=kind,
        label=f"{chunk['flavour']}: {chunk['title'][:70]}",
        content=body,
        source=chunk.get("sc_id") or chunk.get("technique_id") or chunk["id"],
    )


def render_context_text(bundle: ContextBundle) -> str:
    """The frozen, cacheable part of the prompt: spec + page evidence."""
    sections: list[str] = []

    normative = bundle.of_kind("spec_normative")
    if normative:
        sections.append("# THE REQUIREMENT\n\n" + "\n\n".join(b.content for b in normative))
    else:
        sections.append(
            "# THE REQUIREMENT\n\n"
            "No WCAG success criterion applies to this issue - it is a performance\n"
            "finding, not a conformance failure. There is no normative source here, so\n"
            "the supporting material below carries less authority than usual. Say so if\n"
            "the developer's question turns on whether something is required."
        )

    evidence = bundle.of_kind("page_evidence")
    if evidence:
        body = "\n\n".join(f"## {b.label}\n{b.content}" for b in evidence)
        sections.append("# EVIDENCE FROM THIS PAGE\n\n" + body)

    advisory = bundle.of_kind("spec_advisory")
    if advisory:
        sections.append("# SUPPORTING MATERIAL (informative)\n\n"
                        + "\n\n".join(b.content for b in advisory))

    return "\n\n".join(sections)


def build_messages(bundle: ContextBundle, history: list[dict],
                   user_message: str) -> tuple[list[dict], list[dict]]:
    """Return (system, messages) for the Anthropic API.

    Cache layout (design spec §7.3), max 4 breakpoints, render order tools -> system
    -> messages:

        [system]                      <- breakpoint 1, frozen for the session
        [context: spec + evidence]    <- breakpoint 2, frozen per issue
        [conversation]                   volatile, uncached
    """
    system = [{
        "type": "text",
        "text": SYSTEM_PROMPT,
        "cache_control": {"type": "ephemeral"},
    }]

    context_text = render_context_text(bundle)

    messages: list[dict] = [{
        "role": "user",
        "content": [
            {"type": "text", "text": context_text,
             "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": history[0]["content"] if history else user_message},
        ],
    }]

    # Everything after the first turn is volatile and deliberately uncached.
    for msg in history[1:]:
        messages.append({"role": msg["role"], "content": msg["content"]})

    if history:
        messages.append({"role": "user", "content": user_message})

    return system, messages


FIRST_TURN_REQUEST = (
    "Explain this issue and give me the fix for my page."
)
