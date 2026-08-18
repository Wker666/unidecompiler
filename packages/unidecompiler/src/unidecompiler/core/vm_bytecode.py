from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.effects import Effect, apply_effects
from unidecompiler.core.ir import Expr, SourceRef
from unidecompiler.core.stack_machine import StackLiftResult, StackMachineState
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction


@dataclass(frozen=True)
class VMBytecodeStep:
    """A VM-neutral bytecode row submitted by a frontend.

    Frontends decode VM-specific opcodes and operands into this thin shape.
    Core passes interpret the effect stream and decide how to recover stack
    state, statements, regions, and final IR.
    """

    opcode: str
    source: SourceRef
    effects: tuple[Effect, ...] | None
    raw: str = ""
    decoded: VMDecodedInstruction | None = None
    hints: tuple[VMHint, ...] = ()


def run_vm_steps(
    steps: tuple[VMBytecodeStep, ...],
    *,
    initial_locals: dict[str, Expr] | None = None,
    initial_stack: tuple[Expr, ...] = (),
) -> StackLiftResult[VMBytecodeStep]:
    state = StackMachineState(
        locals=dict(initial_locals or {}),
        stack=list(initial_stack),
    )
    for step in steps:
        if step.effects is None:
            return StackLiftResult(state=state, stopped_at=step)
        if not apply_effects(state, step.effects):
            return StackLiftResult(state=state, stopped_at=step)
        if state.diagnostics or state.terminator is not None:
            return StackLiftResult(state=state, stopped_at=step)
    return StackLiftResult(state=state)
