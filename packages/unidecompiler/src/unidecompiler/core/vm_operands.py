from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from unidecompiler.core.ir import SourceRef


VMOperandRole = Literal[
    "constant",
    "local",
    "global",
    "register",
    "target",
    "attribute",
    "member",
    "immediate",
    "raw",
]


@dataclass(frozen=True)
class VMOperand:
    """VM-neutral operand cell decoded from a frontend instruction."""

    role: VMOperandRole
    value: Any
    text: str = ""


@dataclass(frozen=True)
class VMDecodedInstruction:
    """Thin decoded instruction submitted before core executes effects."""

    opcode: str
    source: SourceRef
    operands: tuple[VMOperand, ...] = ()
    raw: str = ""
