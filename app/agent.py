"""The assistant: its tool surface, and the loop that drives it.

**Turn asymmetry** is enforced here and it is the core latency decision:

    turn 0  - tools are NOT offered. The recipe already pre-fetched everything the
              known issue needs, so "help me fix this" costs exactly one round trip
              and starts streaming immediately.
    turn 1+ - tools are offered, because a follow-up is an unpredictable question
              and cannot be pre-fetched for.

The loop is hand-written rather than using the SDK tool runner: span emission and
budget enforcement happen between turns anyway, and owning ~40 lines of loop is
easier to explain than hooks into someone else's.

**Latency is written into each tool description**, so the model can decide whether
a question justifies the wait. A model that cannot see cost cannot budget.

**Every tool returns a discriminated union**, never an exception and never a bare
empty result:

    {"status": "ok" | "not_found" | "stale" | "ambiguous" | "timeout",
     "data": ..., "reason": ..., "suggestion": ...}

`not_found` carries the nearest matching selectors, so a miss is a lead rather than
an invitation to invent. `stale` fires when the page hash has moved since the scan
and the tool refuses to answer at all - answering confidently from a stale index is
worse than admitting the page changed.

Full re-scan is deliberately NOT a tool. It costs 30+ seconds of a developer's time
and belongs to the user, as a button, not to the model's own initiative.
"""
from __future__ import annotations

import difflib
import json
import logging
import time
from typing import Any, Iterator

from app import db, tracing, verifier
from app.config import (ASSISTANT_MODEL, BUDGET, MAX_OUTPUT_TOKENS, model_for,
                        require_api_key)
from app.context import (FIRST_TURN_REQUEST, SYSTEM_PROMPT, _attrs, _styles,
                         apply_budget, build_evidence, build_messages,
                         estimate_tokens, render_context_text, spec_block,
                         trim_conversation)
from app.contrast import assess
from app.models import ContextBlock, ContextBundle
from app.retrieval import (anchor_retrieve, get_chunk, normative_for_sc,
                           semantic_retrieve)
from app.scanner import ScannerError, validate_fix as run_validate


# ==========================================================================
# Tool surface
# ==========================================================================

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "get_node",
        "description": (
            "Read one element from the page index by CSS selector: attributes, text, "
            "computed styles, roles, and landmark ancestry. "
            "COST: instant (<10ms, local index read). Use freely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string",
                             "description": "CSS selector as shown in the evidence."}
            },
            "required": ["selector"],
        },
    },
    {
        "name": "find_related_issues",
        "description": (
            "Find other scan violations on this page, filtered by rule id or by the "
            "element they affect. Use this to answer 'will this fix break anything "
            "else' or 'is this the same problem elsewhere'. "
            "COST: instant (<10ms, local index read)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "rule": {"type": "string", "description": "axe rule id, e.g. image-alt"},
                "selector": {"type": "string", "description": "CSS selector"},
            },
        },
    },
    {
        "name": "get_spec",
        "description": (
            "Retrieve authoritative text you were not given up front: a WCAG success "
            "criterion by number (e.g. '1.4.3'), a technique by id (e.g. 'H37'), or a "
            "free-text query against the grounding corpus. "
            "COST: instant (<10ms, local index read). "
            "Prefer this over recalling criterion text from memory."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sc_id": {"type": "string", "description": "e.g. 1.4.3"},
                "query": {"type": "string", "description": "free-text lookup"},
            },
        },
    },
    {
        "name": "contrast_ratio",
        "description": (
            "Compute a WCAG contrast ratio and the applicable threshold. ALWAYS use "
            "this instead of calculating a ratio yourself - a wrong ratio looks exactly "
            "like a right one and the developer will not check it. "
            "COST: fast (<300ms, local computation, no browser)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "foreground": {"type": "string", "description": "e.g. '#8a8a8a' or 'rgb(138,138,138)'"},
                "background": {"type": "string", "description": "the colour actually painted behind it"},
                "font_size_px": {"type": "number"},
                "font_weight": {"type": "string", "description": "e.g. '400' or 'bold'"},
                "level": {"type": "string", "description": "'AA' (default) or 'AAA'"},
            },
            "required": ["foreground", "background"],
        },
    },
    {
        "name": "validate_fix",
        "description": (
            "Apply your proposed markup to the real page in a headless browser and "
            "re-run the accessibility scan against it. Returns whether the original "
            "violation actually cleared AND whether your change introduced any NEW "
            "violation. "
            "COST: expensive (2-5 seconds, launches a browser). "
            "Worth it before telling the developer a fix is correct - especially when "
            "you are changing roles, ARIA attributes, or anything structural."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string",
                             "description": "element to replace, as shown in the evidence"},
                "patched_html": {"type": "string",
                                 "description": "complete replacement outerHTML for that element"},
            },
            "required": ["selector", "patched_html"],
        },
    },
]


def _ok(data: Any, **extra: Any) -> dict:
    return {"status": "ok", "data": data, **extra}


def _err(status: str, reason: str, **extra: Any) -> dict:
    return {"status": status, "reason": reason, **extra}


def _nearest_selectors(page_id: int, selector: str, n: int = 3) -> list[str]:
    """Closest selectors in the index, so a miss is actionable rather than a dead end."""
    candidates = [row["selector"] for row in db.all_nodes(page_id)]
    return difflib.get_close_matches(selector, candidates, n=n, cutoff=0.3)


# ---------------------------------------------------------------- handlers

def t_get_node(page_id: int, selector: str, **_: Any) -> dict:
    node = db.get_node(page_id, selector)
    if not node:
        near = _nearest_selectors(page_id, selector)
        return _err("not_found",
                    f"No element matches '{selector}' in the page index.",
                    suggestion=(f"Nearest indexed selectors: {near}" if near
                                else "No similar selectors found. The element may "
                                     "have been removed since the scan."),
                    nearest=near)
    return _ok({
        "selector": node["selector"],
        "tag": node["tag"],
        "attributes": _attrs(node),
        "text": node["own_text"],
        "landmark_path": node["landmark_path"],
        "explicit_role": node["explicit_role"],
        "implicit_role": node["implicit_role"],
        "computed_styles": _styles(node),
        "outer_html": node["outer_html"],
        "parent_selector": node["parent_selector"],
    })


def t_find_related_issues(page_id: int, rule: str | None = None,
                          selector: str | None = None, **_: Any) -> dict:
    if not rule and not selector:
        return _err("ambiguous", "Provide either a rule id or a selector.")
    rows: list[dict] = []
    if rule:
        rows.extend(db.find_issues_by_rule(page_id, rule))
    if selector:
        with db.connect() as conn:
            found = conn.execute(
                "SELECT * FROM issues WHERE page_id = ? AND node_selector = ?",
                (page_id, selector)).fetchall()
        rows.extend(dict(r) for r in found)
    if not rows:
        return _ok([], note="No other violations match that filter on this page.")
    seen, out = set(), []
    for r in rows:
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        out.append({"issue_id": r["id"], "rule": r["rule"], "impact": r["impact"],
                    "selector": r["node_selector"], "help": r["help"]})
    return _ok(out)


def t_get_spec(page_id: int, sc_id: str | None = None,
               query: str | None = None, **_: Any) -> dict:
    if sc_id:
        chunk = normative_for_sc(sc_id.strip())
        if not chunk:
            return _err("not_found",
                        f"No success criterion '{sc_id}' in the corpus.",
                        suggestion="Check the number, or use `query` for free-text search.")
        return _ok({"id": chunk["sc_id"], "title": chunk["title"],
                    "flavour": chunk["flavour"], "text": chunk["text"],
                    "source_url": chunk["source_url"]})
    if query:
        hits = semantic_retrieve(query, top_k=4)
        return _ok([{"id": h["id"], "title": h["title"], "flavour": h["flavour"],
                     "text": h["text"][:900], "source_url": h["source_url"]}
                    for h in hits])
    return _err("ambiguous", "Provide either sc_id or query.")


def t_contrast_ratio(page_id: int, foreground: str, background: str,
                     font_size_px: float = 16.0, font_weight: str = "400",
                     level: str = "AA", **_: Any) -> dict:
    result = assess(foreground, background, font_size_px, font_weight, level)
    if result["ratio"] is None:
        return _err("ambiguous",
                    f"Could not parse one of the colours: fg={foreground!r} bg={background!r}",
                    suggestion="Use hex (#rrggbb) or rgb()/rgba() notation.")
    return _ok(result)


def t_validate_fix(page_id: int, selector: str, patched_html: str, **_: Any) -> dict:
    page = db.get_page(page_id)
    if not page:
        return _err("not_found", f"Page {page_id} is not in the index.")
    rule = None
    with db.connect() as conn:
        r = conn.execute(
            "SELECT rule FROM issues WHERE page_id = ? AND node_selector = ? LIMIT 1",
            (page_id, selector)).fetchone()
        if r:
            rule = r["rule"]
    try:
        result = run_validate(
            url=page["resolved_url"] or page["url"],
            selector=selector,
            patched_html=patched_html,
            rule=rule,
            expect_hash=page["content_hash"],
        )
    except ScannerError as exc:
        return _err("timeout", f"Validation could not run: {exc}",
                    suggestion="Describe the fix without machine verification, and "
                               "tell the developer it was not verified.")

    status = result.get("status")
    if status == "stale":
        return _err("stale",
                    "The page has changed since it was scanned, so this fix could not "
                    "be verified against the markup you were shown.",
                    suggestion="Tell the developer the page changed and offer to re-scan.",
                    expected_hash=result.get("expected_hash"),
                    actual_hash=result.get("actual_hash"))
    if status == "not_found":
        near = _nearest_selectors(page_id, selector)
        return _err("not_found", result.get("reason", "Selector did not match."),
                    suggestion=f"Nearest indexed selectors: {near}" if near else None,
                    nearest=near)

    return _ok({
        "outcome": status,                       # cleared | partial | regressed
        "target_rule": result.get("rule"),
        "violations_before": result.get("violations_before"),
        "violations_after": result.get("violations_after"),
        "still_failing": result.get("remaining", []),
        "newly_introduced": result.get("introduced", []),
    }, elapsed_ms=result.get("_elapsed_ms"))


HANDLERS = {
    "get_node": t_get_node,
    "find_related_issues": t_find_related_issues,
    "get_spec": t_get_spec,
    "contrast_ratio": t_contrast_ratio,
    "validate_fix": t_validate_fix,
}


def dispatch(name: str, page_id: int, arguments: dict) -> dict:
    handler = HANDLERS.get(name)
    if not handler:
        return _err("not_found", f"Unknown tool '{name}'.")
    try:
        return handler(page_id, **arguments)
    except TypeError as exc:
        return _err("ambiguous", f"Bad arguments for {name}: {exc}")
    except Exception as exc:  # noqa: BLE001
        return _err("timeout", f"{name} failed: {exc}")


# ==========================================================================
# Orchestration - context assembly, tool loop, verification
# ==========================================================================

log = logging.getLogger(__name__)
MAX_TOOL_ROUNDS = 4


def get_client() -> Any:
    import anthropic
    return anthropic.Anthropic(api_key=require_api_key())


# ---------------------------------------------------------------- context

def build_bundle(page_id: int, issue: dict, history: list[dict],
                 user_message: str) -> tuple[ContextBundle, set[str]]:
    """Assemble everything the model will see this turn.

    Returns the bundle plus the set of citable identifiers that were actually
    supplied - the verifier's membership check is exactly this set.
    """
    blocks: list[ContextBlock] = []
    retrieved_ids: set[str] = set()

    # The system prompt is part of what the model receives, so it belongs in the
    # bundle - NEVER_DROP already names "system". Without it the inspector showed
    # `system 0 / 1200`, advertising a budget line it never measured, and the
    # estimator drift silently carried the whole system prompt as error.
    # render_context_text() selects blocks by explicit kind, so this one is not
    # rendered into the user message; build_messages() sends it as the system param.
    blocks.append(ContextBlock(
        kind="system", label="system prompt", content=SYSTEM_PROMPT,
        token_count=estimate_tokens(SYSTEM_PROMPT), cache_breakpoint=True,
        source="app/context.py"))

    sc_tags = json.loads(issue.get("sc_tags_json") or "[]")

    # --- Path A: deterministic anchor ---------------------------------------
    with tracing.span("retrieve_anchor", "retriever", sc_tags=json.dumps(sc_tags)) as sp:
        anchor = anchor_retrieve(sc_tags)
        for chunk in anchor["normative"]:
            blocks.append(spec_block(chunk))
            retrieved_ids.add(chunk["sc_id"])
        for chunk in anchor["advisory"][:6]:
            blocks.append(spec_block(chunk))
            if chunk.get("technique_id"):
                retrieved_ids.add(chunk["technique_id"])
            if chunk.get("sc_id"):
                retrieved_ids.add(chunk["sc_id"])
        tracing.set_io(sp, inputs={"sc_tags": sc_tags},
                       outputs={"sc_ids": anchor["sc_ids"],
                                "normative": len(anchor["normative"]),
                                "advisory": len(anchor["advisory"][:6])})

    # --- Path B: semantic hop -----------------------------------------------
    query = user_message if history else f"{issue['rule']} {issue.get('help', '')}"
    with tracing.span("retrieve_semantic", "retriever", query=query[:200]) as sp:
        exclude = {f"sc:{i}" for i in anchor["sc_ids"]}
        hits = semantic_retrieve(query, top_k=4, exclude_ids=exclude)
        for h in hits:
            blocks.append(spec_block(h))
            if h.get("technique_id"):
                retrieved_ids.add(h["technique_id"])
            if h.get("sc_id"):
                retrieved_ids.add(h["sc_id"])
        tracing.set_io(sp, inputs={"query": query},
                       outputs={"hits": [{"id": h["id"], "flavour": h["flavour"],
                                          "score": h["score"], "title": h["title"]}
                                         for h in hits]})

    # --- page evidence via the issue-type recipe ----------------------------
    with tracing.span("build_evidence", "parser", rule=issue["rule"]) as sp:
        recipe_name, evidence = build_evidence(page_id, issue)
        blocks.extend(evidence)
        tracing.set_io(sp, outputs={"recipe": recipe_name,
                                    "blocks": [b.label for b in evidence]})

    # --- conversation --------------------------------------------------------
    kept, dropped = trim_conversation(history, BUDGET["conversation"])
    for i, msg in enumerate(kept):
        content = msg["content"] if isinstance(msg["content"], str) else json.dumps(msg["content"])
        blocks.append(ContextBlock(
            kind="conversation", label=f"turn {i} ({msg['role']})",
            content=content, source=str(i)))

    bundle = apply_budget(blocks)
    bundle.recipe = recipe_name

    # Mark which blocks sit inside the cached prefix. Spec and evidence are frozen
    # for the life of this issue and are re-read from cache on every follow-up;
    # conversation and tool results are volatile and deliberately uncached.
    for b in bundle.blocks:
        b.cache_breakpoint = b.kind in {
            "system", "spec_normative", "spec_advisory", "page_evidence"}
    if dropped:
        bundle.notes.append(
            f"{dropped} earliest conversation turn(s) dropped to stay within the "
            f"{BUDGET['conversation']}-token conversation budget."
        )
    if not anchor["normative"]:
        bundle.notes.append(
            "No WCAG success criterion applies (performance finding): the "
            "deterministic anchor returned nothing, so grounding leans on "
            "lower-authority community sources."
        )
    return bundle, retrieved_ids


# ---------------------------------------------------------------- turn

def run_turn(page_id: int, issue: dict, history: list[dict],
             user_message: str) -> Iterator[dict[str, Any]]:
    """Execute one turn, yielding events for the SSE stream.

    Events: {"type": "token"|"tool"|"error"|"done", ...}
    """
    client = get_client()
    allow_tools = bool(history)  # turn 0 makes no tool calls, by design
    started = time.time()

    bundle, retrieved_ids = build_bundle(page_id, issue, history, user_message)
    system, messages = build_messages(bundle, history, user_message)

    # Grounded issues get the fast model; ungrounded ones buy capability instead.
    model = model_for(bundle.recipe)
    bundle.model = model

    # One authoritative token count for the assembled prompt, reconciled against
    # the fast per-block estimates the budgeter used.
    try:
        counted = client.messages.count_tokens(
            model=model, system=system, messages=messages,
            **({"tools": TOOL_DEFINITIONS} if allow_tools else {}))
        bundle.input_tokens = counted.input_tokens
    except Exception as exc:  # noqa: BLE001
        log.warning("count_tokens failed: %s", exc)

    yield {"type": "bundle", "bundle": bundle.to_dict()}

    tool_events: list[dict] = []
    answer_parts: list[str] = []

    with tracing.span("assistant_turn", "chain",
                      rule=issue["rule"], recipe=bundle.recipe or "",
                      tools_enabled=allow_tools) as turn_span:
        tracing.set_io(turn_span, inputs={
            "user_message": user_message,
            "context_tokens": bundle.total_tokens(),
            "context_preview": render_context_text(bundle)[:2000],
        })

        for round_no in range(MAX_TOOL_ROUNDS):
            params: dict[str, Any] = {
                "model": model,
                "max_tokens": MAX_OUTPUT_TOKENS,
                "system": system,
                "messages": messages,
            }
            if allow_tools:
                params["tools"] = TOOL_DEFINITIONS

            try:
                with client.messages.stream(**params) as stream:
                    for text in stream.text_stream:
                        answer_parts.append(text)
                        yield {"type": "token", "text": text}
                    final = stream.get_final_message()
            except Exception as exc:  # noqa: BLE001
                log.exception("model call failed")
                yield {"type": "error", "message": str(exc)}
                return

            usage = getattr(final, "usage", None)
            if usage:
                bundle.cache_read_tokens = getattr(usage, "cache_read_input_tokens", None)
                bundle.cache_write_tokens = getattr(usage, "cache_creation_input_tokens", None)
                bundle.output_tokens = getattr(usage, "output_tokens", None)
                # NOT bundle.input_tokens: that holds the count_tokens measurement of
                # the whole assembled prompt, which is what the estimate is reconciled
                # against. usage.input_tokens is only the uncached remainder.
                bundle.billed_input_tokens = getattr(usage, "input_tokens", None)

            if final.stop_reason != "tool_use":
                break

            # --- execute the tools the model asked for ------------------------
            messages.append({"role": "assistant", "content": final.content})
            results = []
            for block in final.content:
                if getattr(block, "type", "") != "tool_use":
                    continue
                t0 = time.time()
                with tracing.span(f"tool:{block.name}", "tool",
                                  arguments=json.dumps(block.input)[:800]) as tsp:
                    result = dispatch(block.name, page_id, dict(block.input))
                    elapsed = int((time.time() - t0) * 1000)
                    tracing.set_io(tsp, inputs=dict(block.input),
                                   outputs={"status": result.get("status")})
                event = {"type": "tool", "name": block.name,
                         "status": result.get("status"), "elapsed_ms": elapsed,
                         "arguments": dict(block.input),
                         "result_preview": json.dumps(result)[:600]}
                tool_events.append(event)
                yield event

                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result)[:6000],
                    "is_error": result.get("status") not in {"ok"},
                })

                bundle.add(ContextBlock(
                    kind="tool_result",
                    label=f"{block.name} -> {result.get('status')}",
                    content=json.dumps(result, indent=1)[:2000],
                    token_count=estimate_tokens(json.dumps(result)),
                    source=block.name))

            messages.append({"role": "user", "content": results})

        answer = "".join(answer_parts)
        tracing.set_io(turn_span, outputs={"answer": answer[:4000],
                                           "tool_calls": len(tool_events)})

    # --- verification --------------------------------------------------------
    citations = verifier.verify(answer, retrieved_ids, client=client)
    summary = verifier.summarise(citations)

    yield {
        "type": "done",
        "answer": answer,
        "citations": [c.to_dict() for c in citations],
        "citation_summary": summary,
        "bundle": bundle.to_dict(),
        "tools": tool_events,
        "elapsed_ms": int((time.time() - started) * 1000),
    }
