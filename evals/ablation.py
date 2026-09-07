"""Context ablation (design spec §7.5).

The central claim of the context-engineering layer is that cutting context makes
answers *better*, not merely cheaper — that selection is signal, not just savings.
That is an assertion until it is measured, so this module provides three arms the
eval suite can run:

    full      the recipe as designed
    minimal   the failing element only, no ancestors, no nearby text, no computed facts
    firehose  a raw DOM dump truncated to the SAME token count as `full`

`firehose` is the arm that makes the experiment worth running. It holds token count
constant and varies only *what was chosen*, so any difference between it and `full`
is attributable to selection rather than to volume. Comparing `full` against a
whole-DOM arm with 20x the tokens would prove nothing except that budgets exist.

Activated by monkeypatching the recipe entry point, so the rest of the pipeline —
retrieval, budgeting, prompting, verification — is byte-identical across arms.
"""
from __future__ import annotations

import json

from app import context
from app.context import estimate_tokens
from app.models import ContextBlock

ARMS = ("full", "minimal", "firehose")
_ACTIVE = "full"


def active() -> str:
    return _ACTIVE


def _minimal(page_id: int, issue: dict) -> tuple[str, list[ContextBlock]]:
    """The failing element and nothing else."""
    selector = issue.get("node_selector") or issue.get("target")
    node = db.get_node(page_id, selector) if selector else None
    body = (recipes.fmt_node(node) if node
            else (issue.get("node_html") or "(element not available)"))
    return "minimal", [ContextBlock(
        kind="page_evidence", label="Failing element only (ablation: minimal)",
        content=f"Failing element ({issue['rule']}):\n{body}",
        source=selector or "")]


def _firehose(page_id: int, issue: dict) -> tuple[str, list[ContextBlock]]:
    """Raw DOM, truncated to the same budget the real recipe would have used."""
    _, real_blocks = context.build_evidence(page_id, issue)
    target_tokens = sum(estimate_tokens(b.content) for b in real_blocks) or 500
    budget_chars = int(target_tokens * 3.6)

    parts: list[str] = []
    used = 0
    for n in db.all_nodes(page_id):
        attrs = json.loads(n["attrs_json"] or "{}")
        attr_str = " ".join(f'{k}="{v}"' for k, v in attrs.items())
        line = f"<{n['tag']}{' ' + attr_str if attr_str else ''}>{n['own_text'] or ''}"
        if used + len(line) > budget_chars:
            break
        parts.append(line)
        used += len(line)

    selector = issue.get("node_selector") or issue.get("target")
    header = (f"Failing element ({issue['rule']}): {selector}\n"
              f"Full DOM, document order, truncated at the same token budget:\n")
    return "firehose", [ContextBlock(
        kind="page_evidence", label="Raw DOM dump (ablation: firehose)",
        content=header + "\n".join(parts), source=selector or "")]


_ORIGINAL = context.build_evidence


def activate(arm: str) -> None:
    """Swap the recipe entry point for the chosen arm."""
    global _ACTIVE
    if arm not in ARMS:
        raise ValueError(f"unknown arm {arm!r}; expected one of {ARMS}")
    _ACTIVE = arm

    if arm == "full":
        context.build_evidence = _ORIGINAL
    elif arm == "minimal":
        context.build_evidence = _minimal
    else:
        context.build_evidence = _firehose

    # agent.py imported the symbol directly, so rebind it there too.
    import app.agent as _agent
    _agent.build_evidence = context.build_evidence
