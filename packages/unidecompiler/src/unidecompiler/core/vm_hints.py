from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from unidecompiler.core.ir import SourceRef


VMHintKind = Literal[
    "block-boundary",
    "branch-target",
    "case-target",
    "default-target",
    "fallthrough",
    "loop-backedge",
    "exception-region",
    "exception-handler",
    "branch-value",
    "materialized-condition",
    "call-shape",
    "aggregate-shape",
]

VMControlFlowKind = Literal["conditional", "unconditional", "multiway"]


@dataclass(frozen=True)
class VMHint:
    """VM-neutral fact supplied by a frontend without recovering structure."""

    kind: VMHintKind
    source: SourceRef
    target: int | None = None
    value: object | None = None
    label: str = ""
    detail: str | None = None
    flow: VMControlFlowKind | None = None
