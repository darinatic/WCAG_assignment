"""Core data structures.

`ContextBundle` is the spine of the whole system (design spec §4.1). The prompt is
rendered *from* it, the debug panel *renders* it, MLflow *logs* it. One structure,
three consumers - so the inspector physically cannot show something different from
what the model received.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Any, Literal

BlockKind = Literal[
    "system", "spec_normative", "spec_advisory",
    "page_evidence", "conversation", "tool_result",
]


@dataclass
class ContextBlock:
    kind: BlockKind
    label: str            # human-readable, shown in the inspector
    content: str
    token_count: int = 0
    source: str = ""      # sc_id, node selector, technique id, turn index
    included: bool = True
    drop_reason: str | None = None
    cache_breakpoint: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ContextBundle:
    """Everything the model received on one turn, plus everything it did not."""
    blocks: list[ContextBlock] = field(default_factory=list)
    budget: dict[str, dict[str, int]] = field(default_factory=dict)
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    # Two different numbers, and conflating them makes the drift readout nonsense:
    # `input_tokens` is count_tokens on the assembled prompt - the authoritative size,
    # and the only fair thing to reconcile the budgeter's estimate against.
    # `billed_input_tokens` is what the API actually charged as fresh input, which
    # EXCLUDES everything served from cache, so it collapses once caching works.
    input_tokens: int | None = None
    billed_input_tokens: int | None = None
    output_tokens: int | None = None
    recipe: str | None = None
    # Which model actually answered. Routing is per-issue (see config.model_for), so
    # this has to be observable or the reader cannot tell which one they are judging.
    model: str | None = None
    notes: list[str] = field(default_factory=list)

    def add(self, block: ContextBlock) -> None:
        self.blocks.append(block)

    @property
    def included(self) -> list[ContextBlock]:
        return [b for b in self.blocks if b.included]

    @property
    def dropped(self) -> list[ContextBlock]:
        return [b for b in self.blocks if not b.included]

    def of_kind(self, kind: BlockKind) -> list[ContextBlock]:
        return [b for b in self.blocks if b.kind == kind and b.included]

    def total_tokens(self) -> int:
        return sum(b.token_count for b in self.included)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.to_dict() for b in self.blocks],
            "budget": self.budget,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "input_tokens": self.input_tokens,
            "billed_input_tokens": self.billed_input_tokens,
            "output_tokens": self.output_tokens,
            "recipe": self.recipe,
            "model": self.model,
            "notes": self.notes,
            "total_tokens": self.total_tokens(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class Citation:
    """One criterion or technique reference extracted from an answer."""
    raw: str                      # as written by the model, e.g. "SC 1.4.3"
    identifier: str               # normalised, e.g. "1.4.3"
    kind: Literal["success_criterion", "technique"]
    in_retrieved_set: bool = False
    verdict: Literal["supported", "partial", "unsupported", "fabricated",
                     "unchecked"] = "unchecked"
    explanation: str = ""
    quoted_text: str = ""         # what the spec actually says

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
