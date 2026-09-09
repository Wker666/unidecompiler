from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, TypedDict

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
    "exception-handler-pop",
    "exception-edge-state",
    "branch-value",
    "materialized-condition",
    "call-shape",
    "aggregate-shape",
]

VMControlFlowKind = Literal["conditional", "unconditional", "multiway"]
VMExceptionHandlerFrameKind = Literal["active", "protected", "any"]
VMExceptionStackSlot = Literal["resume-position", "exception"]


class VMExceptionEdgeStateValue(TypedDict, total=False):
    """Neutral state fact for an implicit exceptional CFG edge.

    ``handler`` scopes the fact to the active or protected handler target
    offset for one CFG clone. Omitting it retains the legacy, unscoped fact.
    """

    stack_depth: int
    push_exception: bool
    stack_suffix: tuple[VMExceptionStackSlot, ...]
    handler: int


class VMExceptionHandlerPopValue(TypedDict, total=False):
    """Optional precise selector for an exception-handler-pop hint."""

    handler: int
    frame_kind: VMExceptionHandlerFrameKind
    if_present: bool


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
