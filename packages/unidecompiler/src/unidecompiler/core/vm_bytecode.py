from __future__ import annotations

from dataclasses import dataclass, replace

from unidecompiler.core.effects import Effect, Invoke, InvokeKw, OfferImplicitCallArguments, apply_effects
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
    steps = normalize_vm_steps(steps)
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


def normalize_vm_steps(steps: tuple[VMBytecodeStep, ...]) -> tuple[VMBytecodeStep, ...]:
    """Resolve adjacent VM-neutral call-slot facts before lifting.

    This is intentionally driven only by generic effects.  A frontend can
    describe a stack value offered by its calling convention, while core owns
    the decision to attach it to the following generic invocation.
    """

    normalized: list[VMBytecodeStep] = []
    offer: OfferImplicitCallArguments | None = None
    for step in steps:
        effects = step.effects
        if effects is not None and offer is not None:
            effects = _apply_offered_call_arguments(effects, offer)
            if effects != step.effects:
                step = replace(step, effects=effects)
        normalized.append(step)
        offer = _offered_call_arguments(step.effects)
    return tuple(normalized)


def _apply_offered_call_arguments(
    effects: tuple[Effect, ...],
    offer: OfferImplicitCallArguments,
) -> tuple[Effect, ...]:
    if offer.count <= 0 or len(effects) != 1:
        return effects
    effect = effects[0]
    if (
        isinstance(effect, (Invoke, InvokeKw))
        and effect.implicit_arg_count == 0
        and (offer.max_explicit_arg_count is None or effect.arg_count <= offer.max_explicit_arg_count)
    ):
        return (replace(effect, implicit_arg_count=effect.implicit_arg_count + offer.count),)
    return effects


def _offered_call_arguments(effects: tuple[Effect, ...] | None) -> OfferImplicitCallArguments | None:
    if effects is None:
        return None
    offers = tuple(
        effect
        for effect in effects
        if isinstance(effect, OfferImplicitCallArguments) and effect.count > 0
    )
    return offers[0] if len(offers) == 1 else None
