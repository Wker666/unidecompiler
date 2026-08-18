from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from unidecompiler.core.vm_bytecode import VMBytecodeStep, run_vm_steps
from unidecompiler.core.effects import (
    AssignValueOnBranch,
    BuildArray,
    BuildMap,
    BuildSet,
    InvokeMethod,
    LoadLocal,
    RaiseTop,
    ReturnValues,
    ReturnTop,
    ReturnVoid,
    StoreLocal,
    StoreMany,
    StoreManyFromPopOrder,
    Unpack,
    UnknownOpcode,
    apply_effects,
)
from unidecompiler.core.ir import (
    Assign,
    BinaryOp,
    Branch,
    Break,
    Call,
    CollectionProjection,
    Const,
    Continue,
    Expr,
    ForEach,
    GetItem,
    Global,
    If,
    IndirectRef,
    Jump,
    MultiBranch,
    Phi,
    Raise,
    Return,
    SourceRef,
    Stmt,
    Terminator,
    UnaryOp,
    Unsupported,
    Var,
    While,
)
from unidecompiler.core.stack_machine import StackMachineState


InstructionT = TypeVar("InstructionT")


@dataclass(frozen=True)
class VMRegionProfile(Generic[InstructionT]):
    """Frontend-supplied bytecode facts used by the generic region lifter.

    This seam keeps VM-specific opcode names out of the structuring algorithm.
    A frontend may classify CPython, JVM, Lua, or another VM instruction into
    these categories while the core pass decides how regions nest.
    """

    frontend: str
    is_noise: Callable[[InstructionT], bool]
    is_control: Callable[[InstructionT], bool]
    is_jump: Callable[[InstructionT], bool]
    is_forward_jump: Callable[[InstructionT], bool]
    is_backward_jump: Callable[[InstructionT], bool]
    is_iter_start: Callable[[InstructionT], bool]
    is_async_iter_start: Callable[[InstructionT], bool]
    is_conditional_jump: Callable[[InstructionT], bool]
    is_cleanup: Callable[[InstructionT], bool]
    is_null_jump: Callable[[InstructionT], bool]
    is_not_null_jump: Callable[[InstructionT], bool]
    is_truthy_jump: Callable[[InstructionT], bool]
    target_offset: Callable[[InstructionT], int | None]
    offset: Callable[[InstructionT], int | None]
    raw_window: Callable[[int], tuple[str, ...]]
    target_offsets: Callable[[InstructionT], tuple[int, ...]] = lambda _instruction: ()
    await_region_end: Callable[[int, int], int | None] = lambda _start, _end: None


@dataclass(frozen=True)
class VMRegionOpcodeClasses:
    """Frontend-local opcode classes used by the generic hint profile."""

    noise: frozenset[str] = frozenset()
    control: frozenset[str] = frozenset()
    jumps: frozenset[str] = frozenset()
    forward_jumps: frozenset[str] = frozenset()
    backward_jumps: frozenset[str] = frozenset()
    iter_starts: frozenset[str] = frozenset()
    async_iter_starts: frozenset[str] = frozenset()
    conditional_jumps: frozenset[str] = frozenset()
    cleanup: frozenset[str] = frozenset()
    null_jumps: frozenset[str] = frozenset()
    not_null_jumps: frozenset[str] = frozenset()
    truthy_jumps: frozenset[str] = frozenset()


def build_hint_region_profile(
    steps: tuple[VMBytecodeStep, ...],
    *,
    frontend: str,
    opcode_classes: VMRegionOpcodeClasses,
    raw_window: Callable[[int], tuple[str, ...]],
    await_region_end: Callable[[int, int], int | None] = lambda _start, _end: None,
) -> VMRegionProfile[VMBytecodeStep]:
    """Build a control profile from decoded VM steps and neutral hints."""

    return VMRegionProfile(
        frontend=frontend,
        is_noise=lambda step: step.opcode in opcode_classes.noise,
        is_control=lambda step: step.opcode in opcode_classes.control or _hint_target(step) is not None,
        is_jump=lambda step: step.opcode in opcode_classes.jumps,
        is_forward_jump=lambda step: step.opcode in opcode_classes.forward_jumps and _hint_target_is_forward(step),
        is_backward_jump=lambda step: step.opcode in opcode_classes.backward_jumps and _hint_target_is_backward(step),
        is_iter_start=lambda step: step.opcode in opcode_classes.iter_starts,
        is_async_iter_start=lambda step: step.opcode in opcode_classes.async_iter_starts,
        is_conditional_jump=lambda step: step.opcode in opcode_classes.conditional_jumps,
        is_cleanup=lambda step: step.opcode in opcode_classes.cleanup,
        is_null_jump=lambda step: step.opcode in opcode_classes.null_jumps,
        is_not_null_jump=lambda step: step.opcode in opcode_classes.not_null_jumps,
        is_truthy_jump=lambda step: step.opcode in opcode_classes.truthy_jumps,
        target_offset=_hint_target,
        target_offsets=_hint_targets,
        offset=lambda step: step.source.offset,
        raw_window=raw_window,
        await_region_end=await_region_end,
    )


@dataclass(frozen=True)
class VMRegionCallbacks(Generic[InstructionT]):
    """Frontend-specific low-level lifting callbacks.

    The core owns region walking and nesting. The frontend owns expression and
    VM stack details that are required to lift a linear slice.
    """

    lift_slice: Callable[[int, int, tuple[Expr, ...]], tuple[Stmt, ...]]
    lift_expr: Callable[[int, int, tuple[Expr, ...]], Expr | None]
    lift_iter_loop: Callable[[int, Expr | None], tuple[ForEach, int] | None]
    lift_async_iter_loop: Callable[[int, int, int], tuple[tuple[Stmt, ...], ForEach, int] | None]
    lift_comprehension: Callable[[int, int, int, Expr | None], tuple[tuple[Stmt, ...], tuple[Stmt, ...], int] | None]
    pattern_subject_stack: Callable[[Expr], tuple[Expr, ...]] = lambda condition: _condition_result_stack(condition)
    pattern_sibling_stack: Callable[[Expr], tuple[Expr, ...]] = lambda condition: _condition_subject_stack(condition)
    capture_pattern_condition: Callable[[int, int, tuple[Expr, ...]], tuple[tuple[Stmt, ...], Expr] | None] = (
        lambda _start, _end, _stack: None
    )


@dataclass(frozen=True)
class VMLinearState:
    """VM-neutral snapshot after evaluating a linear instruction slice."""

    locals: dict[str, Expr]
    stack: tuple[Expr, ...]
    statements: tuple[Stmt, ...] = ()
    edge_statements: tuple[Stmt, ...] = ()
    terminator: Terminator | None = None
    stopped_at: int | None = None


@dataclass(frozen=True)
class _IterBinding:
    target: Var
    statements: tuple[Stmt, ...]
    body_start: int
    body_stack: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class VMStatefulCallbacks(Generic[InstructionT]):
    """Frontend-local VM semantics needed by generic stateful prefix recovery."""

    initial_locals: Callable[[], dict[str, Expr]]
    lift_linear: Callable[[int, int, dict[str, Expr], tuple[Expr, ...]], VMLinearState | None]
    branch_condition: Callable[[InstructionT, tuple[Expr, ...]], Expr | None]
    branch_stack_width: Callable[[InstructionT], int] = lambda _instruction: 1


@dataclass(frozen=True)
class VMControlPrefixResult:
    statements: tuple[Stmt, ...]
    terminator: Terminator | None
    status: str
    stopped_at: int | None = None
    unsupported_instruction: int | None = None


@dataclass(frozen=True)
class VMControlCFGResult:
    blocks: tuple[tuple[str, tuple[Stmt, ...], Terminator | None], ...]
    diagnostics: tuple[str, ...] = ()


def lift_stateful_control_prefix(
    instructions: tuple[InstructionT, ...],
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
) -> VMControlPrefixResult | None:
    """Recover a linear prefix with conditional regions using VM-neutral facts."""

    if not any(profile.is_control(instruction) for instruction in instructions):
        return None
    return _lift_stateful_control_range(
        instructions,
        0,
        len(instructions),
        VMLinearState(locals=callbacks.initial_locals(), stack=()),
        profile,
        callbacks,
    )


def lift_stateful_low_level_cfg(
    instructions: tuple[InstructionT, ...],
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
) -> VMControlCFGResult | None:
    """Build a semantics-preserving low-level CFG when structuring is unsafe."""

    if not instructions:
        return VMControlCFGResult(blocks=())
    leaders = _cfg_leaders(instructions, profile)
    if not leaders:
        return None
    sorted_leaders = sorted(leaders)
    leader_names = {index: f"block_{profile.offset(instructions[index])}" for index in sorted_leaders}
    leader_positions = {leader: position for position, leader in enumerate(sorted_leaders)}
    diagnostics: list[str] = []
    incoming: dict[int, VMLinearState] = {sorted_leaders[0]: VMLinearState(locals=callbacks.initial_locals(), stack=())}
    # Keep the concrete CFG predecessors alongside each merged VM state.
    # ``Phi`` nodes are edge-labelled values; using the old anonymous
    # ``existing`` label for the first path loses information that later core
    # structurers need to materialize a merge safely.
    incoming_predecessors: dict[int, tuple[str, ...]] = {sorted_leaders[0]: ()}
    lifted_blocks: dict[int, VMLinearState] = {}
    terminators: dict[int, Terminator | None] = {}
    worklist = [sorted_leaders[0]]

    while worklist:
        start = worklist.pop(0)
        position = leader_positions[start]
        in_state = incoming[start]
        end = sorted_leaders[position + 1] if position + 1 < len(sorted_leaders) else len(instructions)
        control_index = end - 1 if end > start and profile.is_control(instructions[end - 1]) else None
        linear_end = control_index if control_index is not None else end
        early_terminal = _first_terminal_effect_index(instructions, start, linear_end)
        if early_terminal is not None:
            linear_end = early_terminal + 1
            control_index = None
        lifted = callbacks.lift_linear(start, linear_end, in_state.locals.copy(), in_state.stack)
        if lifted is None:
            retry_start = _skip_noise_and_cleanup(instructions, start, linear_end, profile)
            if retry_start != start:
                lifted = callbacks.lift_linear(retry_start, linear_end, in_state.locals.copy(), in_state.stack)
        if lifted is None:
            diagnostics.append(f"cannot lift low-level block at {profile.offset(instructions[start])}")
            lifted = VMLinearState(locals=in_state.locals.copy(), stack=in_state.stack)
        if in_state.edge_statements and not lifted.statements:
            lifted = VMLinearState(
                locals=lifted.locals,
                stack=lifted.stack,
                statements=(*in_state.edge_statements, *lifted.statements),
                edge_statements=(),
                terminator=lifted.terminator,
                stopped_at=lifted.stopped_at,
            )
        lifted_blocks[start] = lifted
        terminator: Terminator | None = lifted.terminator
        if terminator is None and control_index is not None:
            lifted = _apply_control_instruction_effects(lifted, instructions[control_index])
            lifted_blocks[start] = lifted
            terminator = lifted.terminator
        if terminator is None and control_index is not None:
            terminator = _low_level_terminator(
                instructions,
                control_index,
                lifted,
                profile,
                callbacks,
                leader_names,
            )
        block_raises = _statements_end_with_raise(lifted.statements)
        if terminator is None and not block_raises and position + 1 < len(sorted_leaders):
            terminator = Jump(
                source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[end - 1])),
                target=leader_names[sorted_leaders[position + 1]],
            )
        if terminator is None and not block_raises and position + 1 >= len(sorted_leaders) and lifted.stack:
            terminator = Return(
                source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[end - 1])),
                values=lifted.stack,
            )
        terminators[start] = terminator
        if block_raises:
            continue
        outgoing = lifted
        if control_index is not None and (
            profile.is_conditional_jump(instructions[control_index])
            or isinstance(terminator, MultiBranch)
        ):
            width = callbacks.branch_stack_width(instructions[control_index])
            if len(lifted.stack) >= width:
                outgoing = VMLinearState(
                    locals=lifted.locals,
                    stack=_stack_without_condition(lifted.stack, width),
                    statements=lifted.statements,
                    terminator=lifted.terminator,
                    stopped_at=lifted.stopped_at,
                )
        successors = _low_level_successors(terminator, leader_names, sorted_leaders, position)
        if successors:
            outgoing = _materialize_cross_block_stack(outgoing, profile, instructions[start])
            lifted_blocks[start] = outgoing
        for successor in successors:
            successor_outgoing = _low_level_successor_state(
                outgoing,
                terminator,
                successor,
                leader_names,
                profile,
                instructions[control_index] if control_index is not None else None,
                instructions[successor],
            )
            current = incoming.get(successor)
            predecessor = leader_names[start]
            merged = _merge_low_level_incoming(
                current,
                successor_outgoing,
                predecessor,
                profile,
                instructions[successor],
                current_predecessors=incoming_predecessors.get(successor, ()),
            )
            if merged is None:
                continue
            previous_predecessors = incoming_predecessors.get(successor, ())
            if predecessor not in previous_predecessors:
                incoming_predecessors[successor] = (*previous_predecessors, predecessor)
            if merged != current:
                incoming[successor] = merged
                if successor not in worklist:
                    worklist.append(successor)

    blocks: list[tuple[str, tuple[Stmt, ...], Terminator | None]] = []
    for start in sorted_leaders:
        lifted = lifted_blocks.get(start)
        if lifted is None:
            continue
        blocks.append((leader_names[start], tuple(lifted.statements), terminators.get(start)))
    return VMControlCFGResult(blocks=tuple(blocks), diagnostics=tuple(diagnostics))


def _lift_stateful_control_range(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
) -> VMControlPrefixResult | None:
    statements: list[Stmt] = []
    cursor = start
    stopped_at: int | None = None

    while cursor < end:
        control_index = _next_region_control(instructions, cursor, end, profile)
        if control_index is None:
            suffix = callbacks.lift_linear(cursor, end, state.locals, state.stack)
            if suffix is None:
                break
            statements.extend(suffix.statements)
            return VMControlPrefixResult(
                statements=tuple(statements),
                terminator=suffix.terminator,
                status="ok",
                stopped_at=end,
            )

        prefix = callbacks.lift_linear(cursor, control_index, state.locals, state.stack)
        if prefix is None:
            stopped_at = control_index
            break
        statements.extend(prefix.statements)
        state = prefix
        branch = instructions[control_index]
        if profile.is_cleanup(branch):
            cursor = control_index + 1
            continue
        if profile.is_jump(branch):
            target = profile.target_offset(branch)
            if target is None:
                stopped_at = control_index
                break
            target_index = _instruction_index_by_offset(instructions, target, profile)
            if target_index is None or target_index <= control_index:
                stopped_at = control_index
                break
            loop = _consume_stateful_tail_condition_loop(
                instructions,
                cursor,
                control_index,
                target_index,
                state,
                profile,
                callbacks,
                end,
            )
            if loop is not None:
                loop_statement, state, cursor = loop
                statements.append(loop_statement)
                continue
            cursor = target_index
            continue
        if not profile.is_conditional_jump(branch):
            stopped_at = control_index
            break

        lifted = _consume_stateful_conditional(instructions, cursor, control_index, state, profile, callbacks, end)
        if lifted is None:
            stopped_at = control_index
            break
        branch_statement, state, cursor = lifted
        statements.append(branch_statement)

    if not statements and not state.stack:
        return None
    source = _source_for(profile, instructions, stopped_at)
    return VMControlPrefixResult(
        statements=(*tuple(statements), *_materialize_stack_snapshot(state.stack, source)),
        terminator=Return(source=source),
        status="partial",
        stopped_at=stopped_at,
        unsupported_instruction=stopped_at,
    )


def _cfg_leaders(
    instructions: tuple[InstructionT, ...],
    profile: VMRegionProfile[InstructionT],
) -> set[int]:
    leaders = {0}
    for index, instruction in enumerate(instructions):
        for hint in tuple(getattr(instruction, "hints", ()) or ()):
            if hint.kind != "exception-region" or not isinstance(hint.value, dict):
                continue
            for offset in (hint.value.get("start"), hint.value.get("end"), hint.value.get("target")):
                if not isinstance(offset, int):
                    continue
                target_index = _instruction_index_by_offset(instructions, offset, profile)
                if target_index is not None:
                    leaders.add(target_index)
        if _instruction_has_terminal_effect(instruction):
            leaders.add(index)
            if index + 1 < len(instructions):
                leaders.add(index + 1)
        if not profile.is_control(instruction):
            continue
        for target in _profile_target_offsets(instruction, profile):
            target_index = _instruction_index_by_offset(instructions, target, profile)
            if target_index is not None:
                leaders.add(target_index)
        if index + 1 < len(instructions):
            leaders.add(index + 1)
    return leaders


def _first_terminal_effect_index(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
) -> int | None:
    for index in range(start, end):
        if _instruction_has_terminal_effect(instructions[index]):
            return index
    return None


def _instruction_has_terminal_effect(instruction: InstructionT) -> bool:
    return any(
        isinstance(effect, (ReturnTop, ReturnValues, ReturnVoid, RaiseTop))
        for effect in tuple(getattr(instruction, "effects", ()) or ())
    )


def _apply_control_instruction_effects(
    state: VMLinearState,
    instruction: InstructionT,
) -> VMLinearState:
    effects = tuple(
        effect
        for effect in tuple(getattr(instruction, "effects", ()) or ())
        if not isinstance(effect, AssignValueOnBranch)
    )
    if not effects:
        return state
    stack_state = StackMachineState(
        locals=dict(state.locals),
        stack=list(state.stack),
        statements=list(state.statements),
        terminator=state.terminator,
    )
    if not apply_effects(stack_state, effects):
        return state
    return VMLinearState(
        locals=stack_state.locals,
        stack=tuple(stack_state.stack),
        statements=tuple(stack_state.statements),
        terminator=stack_state.terminator,
        stopped_at=state.stopped_at,
    )


def _low_level_terminator(
    instructions: tuple[InstructionT, ...],
    index: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
    leader_names: dict[int, str],
) -> Terminator | None:
    instruction = instructions[index]
    target = profile.target_offset(instruction)
    target_index = _instruction_index_by_offset(instructions, target, profile) if target is not None else None
    target_name = leader_names.get(target_index) if target_index is not None else None
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instruction))
    fallthrough_name = leader_names.get(index + 1) if index + 1 < len(instructions) else None
    multi_targets = tuple(dict.fromkeys(_profile_target_offsets(instruction, profile)))
    if len(multi_targets) > 1 and not profile.is_conditional_jump(instruction):
        width = callbacks.branch_stack_width(instruction)
        if len(state.stack) < width:
            return None
        selector = state.stack[-width]
        default_offset = _hint_default_target(instruction) or target
        hint_cases = _hint_case_targets(instruction)
        if hint_cases:
            cases = tuple(
                (Const(value=value, source=source), leader_names[target_index])
                for value, target_offset in hint_cases
                for target_index in [_instruction_index_by_offset(instructions, target_offset, profile)]
                if target_index is not None and target_index in leader_names
            )
        else:
            case_offsets = multi_targets
            if default_offset is not None and case_offsets and case_offsets[0] == default_offset:
                case_offsets = case_offsets[1:]
            cases = tuple(
                (Const(value=case_index, source=source), leader_names[target_index])
                for case_index, target_offset in enumerate(case_offsets)
                for target_index in [_instruction_index_by_offset(instructions, target_offset, profile)]
                if target_index is not None and target_index in leader_names
            )
        default_index = _instruction_index_by_offset(instructions, default_offset, profile) if default_offset is not None else None
        default_name = leader_names.get(default_index) if default_index is not None else None
        return MultiBranch(
            source=source,
            selector=selector,
            cases=cases,
            default_target=default_name or f"missing_{default_offset}",
        )
    if len(multi_targets) > 1:
        return None
    if _hint_target_polarity(instruction) == "iter-false":
        iterator = state.stack[-1] if state.stack else Global(name="iterator", source=source)
        condition = Call(
            source=source,
            callee=Global(name="iter_has_next", source=source),
            args=(iterator,),
        )
        return Branch(
            source=source,
            condition=condition,
            true_target=fallthrough_name or f"missing_fallthrough_{profile.offset(instruction)}",
            false_target=target_name or f"missing_{target}",
        )
    if not profile.is_conditional_jump(instruction):
        if profile.is_jump(instruction):
            return Jump(source=source, target=target_name or f"missing_{target}")
        return None
    width = callbacks.branch_stack_width(instruction)
    if len(state.stack) < width:
        return None
    condition = callbacks.branch_condition(instruction, state.stack[-width:])
    if condition is None:
        return None
    if _hint_target_polarity(instruction) == "target-if-true":
        return Branch(
            source=source,
            condition=condition,
            true_target=target_name or f"missing_{target}",
            false_target=fallthrough_name or f"missing_fallthrough_{profile.offset(instruction)}",
        )
    return Branch(
        source=source,
        condition=condition,
        true_target=fallthrough_name or f"missing_fallthrough_{profile.offset(instruction)}",
        false_target=target_name or f"missing_{target}",
    )


def _low_level_successors(
    terminator: Terminator | None,
    leader_names: dict[int, str],
    sorted_leaders: list[int],
    position: int,
) -> tuple[int, ...]:
    name_to_leader = {name: leader for leader, name in leader_names.items()}
    if isinstance(terminator, Jump):
        target = name_to_leader.get(terminator.target)
        return () if target is None else (target,)
    if isinstance(terminator, Branch):
        targets = []
        for name in (terminator.true_target, terminator.false_target):
            target = name_to_leader.get(name)
            if target is not None and target not in targets:
                targets.append(target)
        return tuple(targets)
    if isinstance(terminator, MultiBranch):
        targets = []
        for name in (*[target for _value, target in terminator.cases], terminator.default_target):
            target = name_to_leader.get(name)
            if target is not None and target not in targets:
                targets.append(target)
        return tuple(targets)
    if isinstance(terminator, Return):
        return ()
    if position + 1 < len(sorted_leaders):
        return (sorted_leaders[position + 1],)
    return ()


def _low_level_successor_state(
    state: VMLinearState,
    terminator: Terminator | None,
    successor: int,
    leader_names: dict[int, str],
    profile: VMRegionProfile[InstructionT],
    control_instruction: InstructionT | None,
    successor_instruction: InstructionT,
) -> VMLinearState:
    if not isinstance(terminator, Branch):
        return state
    target_name = leader_names.get(successor)
    edge_state = _apply_branch_edge_effects(
        state,
        tuple(getattr(control_instruction, "effects", ()) or ()) if control_instruction is not None else (),
        target_name == terminator.true_target,
    )
    if target_name != terminator.true_target:
        return edge_state
    if not isinstance(terminator.condition, Call):
        return edge_state
    if not isinstance(terminator.condition.callee, Global) or terminator.condition.callee.name != "iter_has_next":
        return edge_state
    if len(terminator.condition.args) != 1 or not state.stack:
        return edge_state
    iterator = terminator.condition.args[0]
    if state.stack[-1] != iterator:
        return edge_state
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(successor_instruction))
    next_value = Call(
        source=source,
        callee=Global(name="iter_next", source=source),
        args=(iterator,),
    )
    return VMLinearState(
        locals=edge_state.locals,
        stack=(*edge_state.stack, next_value),
        statements=edge_state.statements,
        terminator=edge_state.terminator,
        stopped_at=edge_state.stopped_at,
    )


def _apply_branch_edge_effects(
    state: VMLinearState,
    effects: tuple[object, ...],
    is_true_edge: bool,
) -> VMLinearState:
    locals_ = dict(state.locals)
    changed = False
    for effect in effects:
        if not isinstance(effect, AssignValueOnBranch):
            continue
        if (effect.branch == "true") != is_true_edge:
            continue
        target = effect.target or Var(name=effect.name, source=effect.source)
        locals_[effect.name] = effect.value
        if isinstance(target, Var):
            locals_[target.name] = effect.value
        changed = True
    if not changed:
        return state
    return VMLinearState(
        locals=locals_,
        stack=state.stack,
        statements=state.statements,
        edge_statements=(
            *state.edge_statements,
            *(
                Assign(source=effect.source, target=target, value=effect.value)
                for effect in effects
                if isinstance(effect, AssignValueOnBranch) and ((effect.branch == "true") == is_true_edge)
                for target in [effect.target or Var(name=effect.name, source=effect.source)]
            ),
        ),
        terminator=state.terminator,
        stopped_at=state.stopped_at,
    )


def _statements_end_with_raise(statements: tuple[Stmt, ...]) -> bool:
    return bool(statements) and isinstance(statements[-1], Raise)


def _materialize_cross_block_stack(
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    instruction: InstructionT,
) -> VMLinearState:
    if not state.stack:
        return state
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instruction))
    statements = list(state.statements)
    stack: list[Expr] = []
    changed = False
    for index, value in enumerate(state.stack):
        if isinstance(value, (Const, IndirectRef, Var)):
            stack.append(value)
            continue
        target = Var(name=f"order_tmp_{profile.offset(instruction)}_{index}_v", source=source)
        statements.append(Assign(source=source, target=target, value=value))
        stack.append(target)
        changed = True
    if not changed:
        return state
    return VMLinearState(
        locals=state.locals,
        stack=tuple(stack),
        statements=tuple(statements),
        terminator=state.terminator,
        stopped_at=state.stopped_at,
    )


def _merge_low_level_incoming(
    current: VMLinearState | None,
    incoming: VMLinearState,
    predecessor: str,
    profile: VMRegionProfile[InstructionT],
    instruction: InstructionT,
    *,
    current_predecessors: tuple[str, ...] = (),
) -> VMLinearState | None:
    if current is None:
        return incoming
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instruction))
    locals_ = dict(current.locals)
    changed = False
    for name, value in incoming.locals.items():
        if name not in locals_:
            locals_[name] = value
            changed = True
            continue
        merged = _merge_phi_expr(
            locals_[name],
            value,
            predecessor,
            source,
            suffix=name,
            current_predecessors=current_predecessors,
        )
        if merged != locals_[name]:
            locals_[name] = merged
            changed = True
    if len(current.stack) != len(incoming.stack):
        return None
    stack_values: list[Expr] = []
    for index, (left, right) in enumerate(zip(current.stack, incoming.stack, strict=True)):
        merged = _merge_phi_expr(
            left,
            right,
            predecessor,
            source,
            suffix=f"stack{index}",
            current_predecessors=current_predecessors,
        )
        stack_values.append(merged)
        changed = changed or merged is not left
    if not changed:
        return current
    return VMLinearState(
        locals=locals_,
        stack=tuple(stack_values),
        statements=current.statements or incoming.statements,
        edge_statements=current.edge_statements or incoming.edge_statements,
    )


def _merge_phi_expr(
    current: Expr,
    incoming: Expr,
    predecessor: str,
    source: SourceRef,
    *,
    suffix: str = "value",
    current_predecessors: tuple[str, ...] = (),
) -> Expr:
    if isinstance(current, Phi):
        pairs = list(current.incoming)
        for index, (pred, value) in enumerate(pairs):
            if pred != predecessor:
                continue
            if not isinstance(incoming, Phi) and not _same_logical_expr(value, incoming):
                current_offset = _expr_source_offset(value)
                incoming_offset = _expr_source_offset(incoming)
                if current_offset is not None and incoming_offset is not None and incoming_offset < current_offset:
                    return current
                pairs[index] = (pred, incoming)
                return Phi(source=current.source or source, incoming=tuple(sorted(pairs, key=lambda item: item[0])))
            return current
        pairs.append((predecessor, incoming))
        return Phi(source=current.source or source, incoming=tuple(sorted(pairs, key=lambda item: item[0])))
    if _same_logical_expr(current, incoming):
        return current
    labelled_current = tuple(
        (current_predecessor, current)
        for current_predecessor in dict.fromkeys(current_predecessors)
    )
    return Phi(
        source=source,
        incoming=(
            *(labelled_current or (("existing", current),)),
            (predecessor, incoming),
        ),
    )


def _same_logical_expr(left: Expr, right: Expr) -> bool:
    return _same_logical_expr_seen(left, right, set())


def _same_logical_expr_seen(left: Expr, right: Expr, seen: set[tuple[int, int]]) -> bool:
    if left is right:
        return True
    pair = (id(left), id(right))
    if pair in seen:
        return True
    seen.add(pair)
    if isinstance(left, Var) and isinstance(right, Var):
        return left.name == right.name
    if isinstance(left, Global) and isinstance(right, Global):
        return left.name == right.name
    if isinstance(left, Const) and isinstance(right, Const):
        return left.value == right.value
    if isinstance(left, Phi) and isinstance(right, Phi):
        if len(left.incoming) != len(right.incoming):
            return False
        left_incoming = dict(left.incoming)
        right_incoming = dict(right.incoming)
        if left_incoming.keys() != right_incoming.keys():
            return False
        return all(
            _same_logical_expr_seen(left_incoming[pred], right_incoming[pred], seen)
            for pred in left_incoming
        )
    if isinstance(left, BinaryOp) and isinstance(right, BinaryOp):
        return (
            left.op == right.op
            and left.semantics == right.semantics
            and _same_logical_expr_seen(left.left, right.left, seen)
            and _same_logical_expr_seen(left.right, right.right, seen)
        )
    if isinstance(left, UnaryOp) and isinstance(right, UnaryOp):
        return left.op == right.op and _same_logical_expr_seen(left.value, right.value, seen)
    if isinstance(left, Call) and isinstance(right, Call):
        return (
            _same_logical_expr_seen(left.callee, right.callee, seen)
            and len(left.args) == len(right.args)
            and all(
                _same_logical_expr_seen(left_arg, right_arg, seen)
                for left_arg, right_arg in zip(left.args, right.args, strict=True)
            )
        )
    if isinstance(left, GetItem) and isinstance(right, GetItem):
        return _same_logical_expr_seen(left.obj, right.obj, seen) and _same_logical_expr_seen(left.key, right.key, seen)
    if isinstance(left, CollectionProjection) and isinstance(right, CollectionProjection):
        return (
            left.kind == right.kind
            and _same_logical_expr_seen(left.target, right.target, seen)
            and _same_logical_expr_seen(left.iterable, right.iterable, seen)
            and _same_logical_expr_seen(left.value, right.value, seen)
        )
    return False


def _expr_source_offset(expr: Expr) -> int | None:
    source = getattr(expr, "source", None)
    return getattr(source, "offset", None) if source is not None else None


def lift_control_region(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMRegionCallbacks[InstructionT],
    initial_stack: tuple[Expr, ...] = (),
    break_boundary: int | None = None,
) -> tuple[Stmt, ...]:
    """Lift a bytecode region into structured generic IR statements."""

    if break_boundary is None:
        break_boundary = end
    statements: list[Stmt] = []
    cursor = start
    while cursor < end:
        if statements and isinstance(statements[-1], Terminator):
            break
        cursor = _skip_noise(instructions, cursor, end, profile)
        if cursor >= end:
            break
        if profile.is_cleanup(instructions[cursor]):
            cursor += 1
            continue
        await_end = profile.await_region_end(cursor, end)
        if await_end is not None:
            await_state = _lift_step_prefix_state(instructions, cursor, await_end, initial_stack)
            if await_state is not None:
                statements.extend(statement for statement in await_state.statements if not isinstance(statement, Unsupported))
                initial_stack = await_state.stack
            else:
                statements.extend(callbacks.lift_slice(cursor, await_end, initial_stack))
            if _contains_terminator_statement(statements):
                break
            cursor = await_end
            continue
        next_control = _next_region_control(instructions, cursor, end, profile)
        if next_control is None:
            statements.extend(callbacks.lift_slice(cursor, end, initial_stack))
            break
        instruction = instructions[next_control]
        source = SourceRef(frontend=profile.frontend, offset=profile.offset(instruction))

        if profile.is_jump(instruction):
            statements.extend(_supported_slice(callbacks.lift_slice(cursor, next_control, initial_stack)))
            if _contains_terminator_statement(statements):
                break
            if profile.is_forward_jump(instruction) and _jump_target_outside_region(
                instructions,
                instruction,
                break_boundary,
                profile,
            ):
                statements.append(Break(source=source))
            elif profile.is_backward_jump(instruction) and _jump_target_at_or_before_boundary(
                instructions,
                instruction,
                break_boundary,
                profile,
            ):
                statements.append(Continue(source=source))
            break

        if profile.is_iter_start(instruction):
            iterable = callbacks.lift_expr(cursor, next_control, initial_stack)
            comprehension = callbacks.lift_comprehension(cursor, next_control, end, iterable)
            if comprehension is not None:
                prefix, comp_statements, exit_index = comprehension
                statements.extend(prefix)
                statements.extend(comp_statements)
                cursor = exit_index
                if any(isinstance(statement, Return) for statement in comp_statements):
                    break
                continue
            generic_projection = _lift_generic_collection_projection(
                instructions,
                cursor,
                next_control,
                end,
                iterable,
                profile,
                callbacks,
                initial_stack,
            )
            if generic_projection is not None:
                prefix, projection_statements, exit_index = generic_projection
                statements.extend(prefix)
                statements.extend(projection_statements)
                cursor = exit_index
                continue
            loop_result = callbacks.lift_iter_loop(next_control, iterable)
            if loop_result is not None:
                prefix = callbacks.lift_slice(cursor, next_control, initial_stack)
                loop, exit_index = loop_result
                statements.extend(statement for statement in prefix if not isinstance(statement, Unsupported))
                statements.append(loop)
                cursor = exit_index + 1
                continue
            generic_loop = _lift_generic_iter_loop(
                instructions,
                next_control,
                end,
                iterable,
                profile,
                callbacks,
                break_boundary,
            )
            if generic_loop is not None:
                prefix = callbacks.lift_slice(cursor, next_control, initial_stack)
                loop, exit_index = generic_loop
                statements.extend(statement for statement in prefix if not isinstance(statement, Unsupported))
                statements.append(loop)
                if _contains_terminator_statement(loop.body):
                    break
                cursor = exit_index + 1
                continue
            linear_slice = _supported_slice(callbacks.lift_slice(cursor, next_control + 1, initial_stack))
            if linear_slice:
                statements.extend(linear_slice)
                if _contains_terminator_statement(statements):
                    break
                cursor = next_control + 1
                continue

        if profile.is_async_iter_start(instruction):
            async_loop = callbacks.lift_async_iter_loop(cursor, next_control, end)
            if async_loop is not None:
                prefix, loop, exit_index = async_loop
                statements.extend(prefix)
                statements.append(loop)
                cursor = exit_index
                continue
            generic_async_loop = _lift_generic_async_iter_loop(
                instructions,
                cursor,
                next_control,
                end,
                profile,
                callbacks,
                break_boundary,
                initial_stack,
            )
            if generic_async_loop is not None:
                prefix, loop, exit_index = generic_async_loop
                statements.extend(prefix)
                statements.append(loop)
                cursor = exit_index
                continue

        if not profile.is_conditional_jump(instruction):
            linear_slice = _supported_slice(callbacks.lift_slice(cursor, next_control, initial_stack))
            if linear_slice:
                statements.extend(linear_slice)
                if _contains_terminator_statement(statements):
                    break
                cursor = next_control
                continue
            if _contains_terminator_statement(statements):
                break
            statements.append(
                Unsupported(
                    source=source,
                    message="unsupported region",
                    detail=f"stopped at {_opcode_name(instruction)}",
                    raw=profile.raw_window(next_control),
                )
            )
            break

        prefix_state = _lift_step_prefix_state(
            instructions,
            cursor,
            next_control,
            initial_stack,
        )
        linear_prefix = (
            prefix_state.statements
            if prefix_state is not None
            else callbacks.lift_slice(cursor, next_control, initial_stack)
        )
        condition = prefix_state.stack[-1] if prefix_state is not None and prefix_state.stack else None
        body_initial_stack = prefix_state.stack[:-1] if prefix_state is not None and prefix_state.stack else initial_stack
        if condition is None:
            condition = callbacks.lift_expr(cursor, next_control, initial_stack)
            body_initial_stack = initial_stack
        if condition is None:
            pattern_capture = callbacks.capture_pattern_condition(cursor, next_control, initial_stack)
            if pattern_capture is not None:
                capture_statements, condition = pattern_capture
                linear_prefix = capture_statements
                body_initial_stack = callbacks.pattern_subject_stack(condition) or initial_stack
            else:
                statements.extend(linear_prefix)
                if _contains_terminator_statement(linear_prefix):
                    break
                statements.append(
                    Unsupported(
                        source=source,
                        message="unsupported region",
                        detail=f"condition stack unavailable at {_opcode_name(instruction)}",
                        raw=profile.raw_window(next_control),
                    )
                )
                break
        statements.extend(statement for statement in linear_prefix if not isinstance(statement, Unsupported))
        target_offset = profile.target_offset(instruction)
        if target_offset is None:
            statements.append(
                Unsupported(
                    source=source,
                    message="unsupported region",
                    detail=f"non-offset branch target at {_opcode_name(instruction)}",
                    raw=profile.raw_window(next_control),
                )
            )
            break
        target_index = _instruction_index_by_offset(instructions, target_offset, profile)
        if target_index is not None and target_index > end:
            local_then_body = lift_control_region(
                instructions,
                next_control + 1,
                end,
                profile,
                callbacks,
                callbacks.pattern_subject_stack(condition) or body_initial_stack,
                break_boundary,
            )
            statements.append(If(source=source, condition=condition, then_body=local_then_body))
            cursor = end
            break
        if target_index is None:
            statements.append(
                Unsupported(
                    source=source,
                    message="unsupported region",
                    detail=f"branch target unavailable at {_opcode_name(instruction)}",
                    raw=profile.raw_window(next_control),
                )
            )
            break

        if profile.is_not_null_jump(instruction):
            condition = BinaryOp(source=source, op="==", left=condition, right=Const(value=None, source=source))
            false_target_index = target_index
        elif profile.is_null_jump(instruction):
            condition = BinaryOp(source=source, op="!=", left=condition, right=Const(value=None, source=source))
            false_target_index = target_index
        elif profile.is_truthy_jump(instruction):
            false_target_index = next_control + 1
        else:
            false_target_index = target_index

        then_start = next_control + 1 if not profile.is_truthy_jump(instruction) else target_index
        then_end = (
            false_target_index
            if not profile.is_truthy_jump(instruction)
            else _next_backward_or_region_end(instructions, target_index, end, profile)
        )
        if profile.is_truthy_jump(instruction):
            body_backedge = _loop_backedge_index(instructions, target_index, end, next_control, profile)
            if body_backedge < end:
                then_end = body_backedge
            false_jump_target = _single_unconditional_forward_jump_target(instructions, false_target_index, end, profile)
            if false_jump_target is not None:
                false_target_index = false_jump_target
                join_index = false_target_index

        while_backedge_index = _trailing_backedge_to_region(
            instructions,
            then_start,
            then_end,
            cursor,
            profile,
        )
        if while_backedge_index is not None:
            body = lift_control_region(
                instructions,
                then_start,
                while_backedge_index,
                profile,
                callbacks,
                callbacks.pattern_subject_stack(condition) or body_initial_stack,
                break_boundary=false_target_index,
            )
            statements.append(While(source=source, condition=condition, body=body))
            cursor = false_target_index
            initial_stack = body_initial_stack
            continue

        pretest_loop = _pretest_loop_backedge(
            instructions,
            then_start,
            false_target_index,
            next_control,
            profile,
        )
        if pretest_loop is not None:
            body = lift_control_region(
                instructions,
                then_start,
                pretest_loop,
                profile,
                callbacks,
                callbacks.pattern_subject_stack(condition) or body_initial_stack,
                break_boundary=false_target_index,
            )
            statements.append(While(source=source, condition=condition, body=body))
            cursor = false_target_index
            initial_stack = body_initial_stack
            continue

        else_body: tuple[Stmt, ...] = ()
        join_index = false_target_index

        jump_forward_index = _trailing_forward_jump(instructions, then_start, then_end, profile)
        if jump_forward_index is not None:
            join_offset = profile.target_offset(instructions[jump_forward_index])
            if join_offset is not None:
                found_join = _instruction_index_by_offset(instructions, join_offset, profile)
                if found_join is not None and found_join <= end:
                    then_end = jump_forward_index
                    merged_value = _lift_branch_stack_value(
                        instructions,
                        then_start,
                        then_end,
                        false_target_index,
                        found_join,
                        profile,
                        initial_stack,
                    )
                    if merged_value is not None:
                        temp = Var(name=f"branch_value_{profile.offset(instruction)}", source=source)
                        statements.append(
                            If(
                                source=source,
                                condition=condition,
                                then_body=(Assign(source=source, target=temp, value=merged_value[0]),),
                                else_body=(Assign(source=source, target=temp, value=merged_value[1]),),
                            )
                        )
                        cursor = found_join
                        initial_stack = (*body_initial_stack, temp)
                        continue
                    else_body = lift_control_region(instructions, false_target_index, found_join, profile, callbacks, (), break_boundary)
                    join_index = found_join

        then_initial_stack = callbacks.pattern_subject_stack(condition) or body_initial_stack
        then_body = lift_control_region(
            instructions,
            then_start,
            then_end,
            profile,
            callbacks,
            then_initial_stack,
            break_boundary,
        )
        if not then_body and _region_is_backward_jump(instructions, then_start, then_end, profile):
            then_body = (Continue(source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[_skip_noise(instructions, then_start, then_end, profile)]))),)
        if not then_body and _region_is_forward_break(instructions, then_start, then_end, break_boundary, profile):
            then_body = (Break(source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[_skip_noise(instructions, then_start, then_end, profile)]))),)
        statements.append(If(source=source, condition=condition, then_body=then_body, else_body=else_body))
        if _contains_terminator_statement(then_body) and not else_body:
            cursor = join_index
            if cursor >= end:
                break
        sibling_stack = callbacks.pattern_sibling_stack(condition)
        cursor = join_index
        if sibling_stack:
            initial_stack = sibling_stack
        if profile.is_truthy_jump(instruction):
            cursor = _next_after_backward(instructions, then_end, end, profile)
    return tuple(statements)


def _single_unconditional_forward_jump_target(
    instructions: tuple[InstructionT, ...],
    index: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    cursor = _skip_noise(instructions, index, end, profile)
    if cursor >= end:
        return None
    instruction = instructions[cursor]
    if profile.is_conditional_jump(instruction) or not profile.is_jump(instruction):
        return None
    if not profile.is_forward_jump(instruction):
        return None
    target_offset = profile.target_offset(instruction)
    if target_offset is None:
        return None
    return _instruction_index_by_offset(instructions, target_offset, profile)


def _supported_slice(statements: tuple[Stmt, ...]) -> tuple[Stmt, ...]:
    return tuple(statement for statement in statements if not isinstance(statement, Unsupported))


def _lift_branch_stack_value(
    instructions: tuple[InstructionT, ...],
    then_start: int,
    then_end: int,
    else_start: int,
    else_end: int,
    profile: VMRegionProfile[InstructionT],
    initial_stack: tuple[Expr, ...],
) -> tuple[Expr, Expr] | None:
    if not all(isinstance(instruction, VMBytecodeStep) for instruction in instructions[then_start:then_end]):
        return None
    if not all(isinstance(instruction, VMBytecodeStep) for instruction in instructions[else_start:else_end]):
        return None
    then_result = run_vm_steps(instructions[then_start:then_end], initial_stack=initial_stack)
    else_result = run_vm_steps(instructions[else_start:else_end], initial_stack=initial_stack)
    if (
        then_result.state.diagnostics
        or else_result.state.diagnostics
        or then_result.stopped_at is not None
        or else_result.stopped_at is not None
        or then_result.state.statements
        or else_result.state.statements
    ):
        return None
    if len(then_result.state.stack) != len(initial_stack) + 1:
        return None
    if len(else_result.state.stack) != len(initial_stack) + 1:
        return None
    return then_result.state.stack[-1], else_result.state.stack[-1]


def _contains_terminator_statement(statements: tuple[object, ...] | list[object]) -> bool:
    for statement in statements:
        if isinstance(statement, Return):
            return True
        for body_name in ("then_body", "else_body", "body"):
            body = getattr(statement, body_name, ())
            if body and _contains_terminator_statement(body):
                return True
    return False


def _skip_noise(
    instructions: tuple[InstructionT, ...],
    cursor: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int:
    while cursor < end and _is_region_noise(instructions[cursor], profile):
        cursor += 1
    return cursor


def _skip_noise_and_cleanup(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int:
    cursor = start
    while cursor < end and (_is_region_noise(instructions[cursor], profile) or profile.is_cleanup(instructions[cursor])):
        cursor += 1
    return cursor


def _is_region_noise(instruction: InstructionT, profile: VMRegionProfile[InstructionT]) -> bool:
    if profile.is_noise(instruction):
        return True
    if profile.is_control(instruction):
        return False
    effects = getattr(instruction, "effects", None)
    return effects == ()


def _hint_target(step: VMBytecodeStep) -> int | None:
    for hint in step.hints:
        if hint.kind == "default-target" and hint.target is not None:
            return hint.target
    for hint in step.hints:
        if hint.kind in {"branch-target", "loop-backedge"} and hint.target is not None:
            return hint.target
    return None


def _hint_targets(step: VMBytecodeStep) -> tuple[int, ...]:
    return tuple(
        dict.fromkeys(
            hint.target
            for hint in step.hints
            if hint.kind in {"branch-target", "loop-backedge", "case-target", "default-target"} and hint.target is not None
        )
    )


def _hint_target_polarity(step) -> str:
    for hint in getattr(step, "hints", ()):
        if hint.kind in {"branch-target", "loop-backedge"} and hint.detail == "iter-next-target-if-false":
            return "iter-false"
        if hint.kind in {"branch-target", "loop-backedge"} and hint.detail == "target-if-true":
            return "target-if-true"
        if hint.kind in {"branch-target", "loop-backedge"} and hint.detail == "target-if-false":
            return "target-if-false"
    return "true"


def _hint_default_target(step) -> int | None:
    for hint in getattr(step, "hints", ()):
        if hint.kind == "default-target" and hint.target is not None:
            return hint.target
    return None


def _hint_case_targets(step) -> tuple[tuple[object, int], ...]:
    return tuple(
        (hint.value, hint.target)
        for hint in getattr(step, "hints", ())
        if hint.kind == "case-target" and hint.target is not None
    )


def _profile_target_offsets(instruction: InstructionT, profile: VMRegionProfile[InstructionT]) -> tuple[int, ...]:
    offsets = profile.target_offsets(instruction)
    if offsets:
        return offsets
    target = profile.target_offset(instruction)
    return () if target is None else (target,)


def _has_hint_kind(step: VMBytecodeStep, kind: str) -> bool:
    return any(hint.kind == kind for hint in step.hints)


def _hint_target_is_forward(step: VMBytecodeStep) -> bool:
    target = _hint_target(step)
    return target is None or step.source.offset is None or target > step.source.offset


def _hint_target_is_backward(step: VMBytecodeStep) -> bool:
    target = _hint_target(step)
    return target is not None and step.source.offset is not None and target <= step.source.offset


def _split_condition_stack(stack: tuple[Expr, ...], width: int) -> tuple[tuple[Expr, ...], tuple[Expr, ...]]:
    if width <= 0:
        return stack, ()
    return stack[:-width], stack[-width:]


def _stack_without_condition(stack: tuple[Expr, ...], width: int) -> tuple[Expr, ...]:
    if width <= 0:
        return stack
    return stack[:-width]


def _lift_step_prefix_state(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    initial_stack: tuple[Expr, ...],
) -> VMLinearState | None:
    if not all(isinstance(instruction, VMBytecodeStep) for instruction in instructions[start:end]):
        return None
    result = run_vm_steps(
        instructions[start:end],
        initial_stack=initial_stack,
    )
    if result.state.diagnostics or result.stopped_at is not None:
        return None
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
    )


def _condition_result_stack(condition: Expr) -> tuple[Expr, ...]:
    shape_test = _shape_test_expr(condition)
    if shape_test is not None:
        return (shape_test,)
    return ()


def _condition_subject_stack(condition: Expr) -> tuple[Expr, ...]:
    shape_test = _shape_test_expr(condition)
    if shape_test is not None and shape_test.args:
        return (shape_test.args[0],)
    return ()


def _shape_test_expr(condition: Expr) -> Call | None:
    if _is_shape_test_expr(condition):
        return condition
    if isinstance(condition, BinaryOp):
        return _shape_test_expr(condition.left) or _shape_test_expr(condition.right)
    if isinstance(condition, Call):
        for arg in condition.args:
            shape_test = _shape_test_expr(arg)
            if shape_test is not None:
                return shape_test
    return None


def _is_shape_test_expr(expr: Expr) -> bool:
    return (
        isinstance(expr, Call)
        and isinstance(expr.callee, Global)
        and expr.callee.name == "shape_test"
    )


def _lift_generic_iter_loop(
    instructions: tuple[InstructionT, ...],
    iter_start_index: int,
    end: int,
    iterable: Expr | None,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMRegionCallbacks[InstructionT],
    break_boundary: int,
) -> tuple[ForEach, int] | None:
    if iterable is None:
        return None
    loop_next_index = _skip_noise(instructions, iter_start_index + 1, end, profile)
    if loop_next_index >= end or profile.target_offset(instructions[loop_next_index]) is None:
        return None
    exit_offset = profile.target_offset(instructions[loop_next_index])
    if exit_offset is None:
        return None
    exit_index = _instruction_index_by_offset(instructions, exit_offset, profile)
    if exit_index is None or exit_index <= loop_next_index:
        return None

    binding = _generic_iter_item_binding(instructions, loop_next_index + 1, exit_index, profile)
    if binding is None:
        return None
    target = binding.target
    binding_statements = binding.statements
    body_start = binding.body_start
    body_end = _loop_backedge_index(instructions, body_start, exit_index, loop_next_index, profile)
    body = (
        *binding_statements,
        *lift_control_region(
            instructions,
            body_start,
            body_end,
            profile,
            callbacks,
            binding.body_stack,
            break_boundary=exit_index if break_boundary == end else break_boundary,
        ),
    )
    return (
        ForEach(
            source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[loop_next_index])),
            target=target,
            iterable=iterable,
            body=body,
        ),
        exit_index,
    )


def _lift_generic_collection_projection(
    instructions: tuple[InstructionT, ...],
    prefix_start: int,
    iter_start_index: int,
    end: int,
    iterable: Expr | None,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMRegionCallbacks[InstructionT],
    initial_stack: tuple[Expr, ...],
) -> tuple[tuple[Stmt, ...], tuple[Stmt, ...], int] | None:
    if iterable is None:
        return None
    accumulator = _collection_accumulator_between(instructions, iter_start_index + 1, end, profile)
    if accumulator is None:
        return None
    accumulator_index, collection_kind = accumulator
    projection_target = _single_iter_projection_value(
        instructions,
        accumulator_index + 1,
        end,
        collection_kind,
        profile,
        callbacks,
    )
    nested_iter = _next_iter_start(instructions, accumulator_index + 1, end, profile)
    if nested_iter is None:
        return None
    loop_index = _skip_noise(instructions, nested_iter + 1, end, profile)
    if loop_index >= end or profile.target_offset(instructions[loop_index]) is None:
        return None
    exit_offset = profile.target_offset(instructions[loop_index])
    if exit_offset is None:
        return None
    exit_index = _instruction_index_by_offset(instructions, exit_offset, profile)
    if exit_index is None or exit_index <= loop_index:
        return None
    after = _skip_noise(instructions, exit_index + 1, end, profile)
    if after >= end:
        return None

    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[accumulator_index]))
    if projection_target is None:
        return None
    target, value = projection_target
    projection = CollectionProjection(
        source=source,
        kind=collection_kind,
        target=target,
        iterable=iterable,
        value=value,
    )
    prefix = tuple(
        statement
        for statement in callbacks.lift_slice(prefix_start, iter_start_index, initial_stack)
        if not isinstance(statement, Unsupported)
    )
    preserved_names = _loaded_local_names(instructions, iter_start_index + 1, accumulator_index)
    store_effect = _single_effect(instructions[after], StoreLocal)
    if store_effect is None:
        return_index = _next_return_top(instructions, after, end)
        if return_index is not None:
            return prefix, (Return(source=source, values=(projection,)),), return_index + 1
        temp = Var(name=f"{collection_kind}_projection_{profile.offset(instructions[accumulator_index])}", source=source)
        return prefix, (Assign(source=source, target=temp, value=projection),), after
    target = store_effect.target or Var(name=store_effect.name, source=source)
    cursor = after + 1
    while cursor < end:
        cleanup_store = _single_effect(instructions[cursor], StoreLocal)
        if cleanup_store is None or cleanup_store.name not in preserved_names:
            break
        cursor += 1
    return prefix, (Assign(source=source, target=target, value=projection),), cursor


def _single_iter_projection_value(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    collection_kind: str,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMRegionCallbacks[InstructionT],
) -> tuple[Var, Expr] | None:
    nested_iter = _next_iter_start(instructions, start, end, profile)
    if nested_iter is None:
        return None
    loop_index = _skip_noise(instructions, nested_iter + 1, end, profile)
    if loop_index >= end or profile.target_offset(instructions[loop_index]) is None:
        return None
    exit_offset = profile.target_offset(instructions[loop_index])
    if exit_offset is None:
        return None
    exit_index = _instruction_index_by_offset(instructions, exit_offset, profile)
    if exit_index is None or exit_index <= loop_index:
        return None
    append_index = _single_append_index(instructions, loop_index + 1, exit_index, collection_kind)
    if append_index is None:
        return None
    target = _single_loop_target(instructions[loop_index + 1])
    if target is None:
        return None
    value = callbacks.lift_expr(loop_index + 2, append_index, (Var(name=target.name, source=target.source),))
    if value is None:
        return None
    return target, value


def _single_append_index(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    collection_kind: str,
) -> int | None:
    matches: list[int] = []
    for index in range(start, end):
        effects = tuple(getattr(instructions[index], "effects", ()) or ())
        if collection_kind == "list" and any(
            isinstance(effect, InvokeMethod) and effect.attr == "append" for effect in effects
        ):
            matches.append(index)
        elif collection_kind == "set" and any(isinstance(effect, InvokeMethod) and effect.attr == "add" for effect in effects):
            matches.append(index)
    if len(matches) != 1:
        return None
    return matches[0]


def _single_loop_target(instruction: InstructionT) -> Var | None:
    effects = tuple(getattr(instruction, "effects", ()) or ())
    store_many = tuple(
        effect
        for effect in effects
        if isinstance(effect, (StoreMany, StoreManyFromPopOrder)) and len(effect.names) == 1
    )
    if store_many:
        return Var(name=store_many[0].names[0], source=store_many[0].source)
    store_local = tuple(effect for effect in effects if isinstance(effect, StoreLocal))
    if len(store_local) == 1:
        effect = store_local[0]
        return effect.target or Var(name=effect.name, source=effect.source)
    return None


def _collection_accumulator_between(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> tuple[int, str] | None:
    cursor = start
    while cursor < end:
        instruction = instructions[cursor]
        if profile.is_iter_start(instruction):
            return None
        effects = tuple(getattr(instruction, "effects", ()) or ())
        if any(isinstance(effect, BuildMap) for effect in effects):
            return cursor, "map"
        if any(isinstance(effect, BuildArray) and effect.kind == "list" for effect in effects):
            return cursor, "list"
        if any(isinstance(effect, BuildSet) for effect in effects):
            return cursor, "set"
        cursor += 1
    return None


def _next_iter_start(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    for index in range(start, end):
        if profile.is_iter_start(instructions[index]):
            return index
    return None


def _loaded_local_names(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
) -> frozenset[str]:
    names: set[str] = set()
    for instruction in instructions[start:end]:
        for effect in tuple(getattr(instruction, "effects", ()) or ()):
            if isinstance(effect, LoadLocal):
                names.add(effect.name)
    return frozenset(names)


def _single_effect(instruction: InstructionT, effect_type):
    effects = tuple(getattr(instruction, "effects", ()) or ())
    if len(effects) != 1 or not isinstance(effects[0], effect_type):
        return None
    return effects[0]


def _next_return_top(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
) -> int | None:
    for index in range(start, end):
        if _single_effect(instructions[index], ReturnTop) is not None:
            return index
    return None


def _lift_generic_async_iter_loop(
    instructions: tuple[InstructionT, ...],
    prefix_start: int,
    async_iter_index: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMRegionCallbacks[InstructionT],
    break_boundary: int,
    initial_stack: tuple[Expr, ...],
) -> tuple[tuple[Stmt, ...], ForEach, int] | None:
    iterable = callbacks.lift_expr(prefix_start, async_iter_index, initial_stack)
    if iterable is None:
        return None
    cursor = _skip_noise(instructions, async_iter_index + 1, end, profile)
    if cursor >= end:
        return None
    next_offset = profile.offset(instructions[cursor])
    await_end = profile.await_region_end(cursor, end)
    if await_end is None:
        return None
    binding = _generic_iter_item_binding(instructions, await_end, end, profile)
    if binding is None:
        return None
    target = binding.target
    binding_statements = binding.statements
    body_start = binding.body_start
    body_end = _loop_backedge_index(instructions, body_start, end, cursor, profile)
    if body_end >= end:
        return None
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[async_iter_index]))
    body = (
        *binding_statements,
        *lift_control_region(
            instructions,
            body_start,
            body_end,
            profile,
            callbacks,
            binding.body_stack,
            break_boundary=body_end if break_boundary == end else break_boundary,
        ),
    )
    prefix = tuple(
        statement
        for statement in callbacks.lift_slice(prefix_start, async_iter_index, initial_stack)
        if not isinstance(statement, Unsupported)
    )
    return (
        prefix,
        ForEach(
            source=source,
            target=target,
            iterable=Call(source=source, callee=Global(name="async_iter", source=source), args=(iterable,)),
            body=body,
        ),
        body_end + 1 if next_offset is not None else body_end,
    )


def _generic_iter_item_binding(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> tuple[Var, tuple[Stmt, ...], int] | None:
    cursor = _skip_noise(instructions, start, end, profile)
    if cursor >= end:
        return None
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[cursor]))
    first_effects = tuple(getattr(instructions[cursor], "effects", ()) or ())
    if len(first_effects) == 1 and isinstance(first_effects[0], StoreLocal):
        effect = first_effects[0]
        return _IterBinding(effect.target or Var(name=effect.name, source=source), (), cursor + 1)

    unpack_count: int | None = None
    if len(first_effects) == 1 and isinstance(first_effects[0], Unpack):
        effect = first_effects[0]
        unpack_count = effect.count or (effect.before + effect.after)
        cursor = _skip_noise(instructions, cursor + 1, end, profile)
        if cursor >= end:
            return None
        source = SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[cursor]))
        first_effects = tuple(getattr(instructions[cursor], "effects", ()) or ())

    if first_effects and isinstance(first_effects[0], (StoreMany, StoreManyFromPopOrder)) and unpack_count is None:
        effect = first_effects[0]
        if not effect.names:
            return None
        target = Var(name=effect.names[0], source=source)
        body_stack = tuple(
            Var(name=load.name, source=source)
            for load in first_effects[1:]
            if isinstance(load, LoadLocal)
        )
        return _IterBinding(target, (), cursor + 1, body_stack)

    if len(first_effects) == 1 and isinstance(first_effects[0], (StoreMany, StoreManyFromPopOrder)):
        names = first_effects[0].names
        if not names:
            return None
        item = Var(name=f"iter_item_{profile.offset(instructions[start])}", source=source)
        count = unpack_count or len(names)
        assignments = tuple(
            Assign(
                source=source,
                target=Var(name=name, source=source),
                value=GetItem(source=source, obj=item, key=Const(value=index, source=source)),
            )
            for index, name in enumerate(names[:count])
        )
        return _IterBinding(item, assignments, cursor + 1)
    if unpack_count:
        names: list[str] = []
        name_cursor = cursor
        while name_cursor < end and len(names) < unpack_count:
            effects = tuple(getattr(instructions[name_cursor], "effects", ()) or ())
            if len(effects) != 1 or not isinstance(effects[0], StoreLocal):
                break
            names.append(effects[0].name)
            name_cursor = _skip_noise(instructions, name_cursor + 1, end, profile)
        if len(names) == unpack_count:
            item = Var(name=f"iter_item_{profile.offset(instructions[start])}", source=source)
            assignments = tuple(
                Assign(
                    source=source,
                    target=Var(name=name, source=source),
                    value=GetItem(source=source, obj=item, key=Const(value=index, source=source)),
                )
                for index, name in enumerate(names)
            )
            return _IterBinding(item, assignments, name_cursor)
    return None


def _loop_backedge_index(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    loop_next_index: int,
    profile: VMRegionProfile[InstructionT],
) -> int:
    loop_offset = profile.offset(instructions[loop_next_index])
    for index in range(end - 1, start - 1, -1):
        if not profile.is_backward_jump(instructions[index]):
            continue
        target = profile.target_offset(instructions[index])
        if target is None or loop_offset is None or target <= loop_offset:
            return index
    return end


def _consume_stateful_conditional(
    instructions: tuple[InstructionT, ...],
    condition_start_index: int,
    branch_index: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
    range_end: int | None = None,
) -> tuple[If | While, VMLinearState, int] | None:
    branch = instructions[branch_index]
    target_offset = profile.target_offset(branch)
    if target_offset is None:
        return None
    target_index = _instruction_index_by_offset(instructions, target_offset, profile)
    if target_index is None or target_index <= branch_index:
        return None
    if range_end is not None and target_index > range_end:
        return None

    source = SourceRef(frontend=profile.frontend, offset=profile.offset(branch))
    condition_width = callbacks.branch_stack_width(branch)
    if len(state.stack) < condition_width:
        return None
    base_stack, condition_stack = _split_condition_stack(state.stack, condition_width)
    condition = callbacks.branch_condition(branch, condition_stack)
    if condition is None:
        return None
    # This structurer treats the physical fallthrough as the ``then`` path.
    # Normalize target-if-true hints so every VM feeds that common shape.
    if _hint_target_polarity(branch) == "target-if-true":
        condition = UnaryOp(source=source, op="not ", value=condition)

    short_circuit_loop = _consume_stateful_short_circuit_loop(
        instructions,
        condition_start_index,
        branch_index,
        target_index,
        state,
        profile,
        callbacks,
        range_end,
    )
    if short_circuit_loop is not None:
        return short_circuit_loop

    fallthrough_start = branch_index + 1
    jump_index = _first_forward_jump(instructions, fallthrough_start, target_index, profile)
    backedge_index = _trailing_backedge_to_prefix(
        instructions,
        fallthrough_start,
        target_index,
        condition_start_index,
        branch_index,
        profile,
    )
    if backedge_index is not None:
        body_state = _lift_stateful_body(
            instructions,
            fallthrough_start,
            backedge_index,
            VMLinearState(locals=state.locals.copy(), stack=base_stack),
            profile,
            callbacks,
        )
        if body_state is None:
            return None
        next_locals = _merge_changed_locals(state.locals, body_state.locals)
        return (
            While(source=source, condition=condition, body=_state_body(body_state)),
            VMLinearState(locals=next_locals, stack=base_stack),
            target_index,
        )
    if jump_index is not None:
        join_offset = profile.target_offset(instructions[jump_index])
        join_index = _instruction_index_by_offset(instructions, join_offset, profile) if join_offset is not None else None
        if join_index is not None and join_index > target_index:
            then_state = _lift_stateful_branch_range(
                instructions,
                fallthrough_start,
                jump_index,
                VMLinearState(locals=state.locals.copy(), stack=base_stack),
                profile,
                callbacks,
            )
            else_state = _lift_stateful_branch_range(
                instructions,
                target_index,
                join_index,
                VMLinearState(locals=state.locals.copy(), stack=base_stack),
                profile,
                callbacks,
            )
            if then_state is None or else_state is None:
                return None
            then_body = list(_state_body(then_state))
            else_body = list(_state_body(else_state))
            if then_state.terminator is not None or else_state.terminator is not None:
                return (
                    If(source=source, condition=condition, then_body=tuple(then_body), else_body=tuple(else_body)),
                    VMLinearState(locals=_merge_expression_locals(then_state.locals, else_state.locals), stack=base_stack),
                    join_index,
                )
            if len(then_state.stack) != len(else_state.stack):
                return None
            next_locals = _merge_expression_locals(then_state.locals, else_state.locals)
            if len(then_state.stack) > len(base_stack):
                next_stack = list(base_stack)
                for index, (then_value, else_value) in enumerate(
                    zip(then_state.stack[len(base_stack) :], else_state.stack[len(base_stack) :], strict=False)
                ):
                    temp = Var(name=f"branch_value_{profile.offset(branch)}_{index}", source=source)
                    then_body.append(Assign(source=source, target=temp, value=then_value))
                    else_body.append(Assign(source=source, target=temp, value=else_value))
                    next_stack.append(temp)
                    next_locals[temp.name] = temp
                next_state = VMLinearState(locals=next_locals, stack=tuple(next_stack))
            else:
                next_state = VMLinearState(locals=next_locals, stack=base_stack)
            return (
                If(source=source, condition=condition, then_body=tuple(then_body), else_body=tuple(else_body)),
                next_state,
                join_index,
            )

    continue_index = _trailing_backedge_to_prefix(
        instructions,
        fallthrough_start,
        target_index,
        condition_start_index,
        branch_index,
        profile,
    )
    if continue_index is not None:
        then_state = _lift_stateful_branch_range(
            instructions,
            fallthrough_start,
            continue_index,
            VMLinearState(locals=state.locals.copy(), stack=base_stack),
            profile,
            callbacks,
        )
        if then_state is not None:
            then_body = (*_state_body(then_state), Continue(source=SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[continue_index]))))
            return (
                If(source=source, condition=condition, then_body=then_body),
                VMLinearState(locals=_merge_changed_locals(state.locals, then_state.locals), stack=base_stack),
                target_index,
            )

    then_state = _lift_stateful_branch_range(
        instructions,
        fallthrough_start,
        target_index,
        VMLinearState(locals=state.locals.copy(), stack=base_stack),
        profile,
        callbacks,
    )
    if then_state is None:
        return None
    next_locals = _merge_changed_locals(state.locals, then_state.locals)
    return (
        If(source=source, condition=condition, then_body=_state_body(then_state)),
        VMLinearState(locals=next_locals, stack=base_stack),
        target_index,
    )


def _consume_stateful_tail_condition_loop(
    instructions: tuple[InstructionT, ...],
    body_start: int,
    jump_index: int,
    condition_start: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
    range_end: int,
) -> tuple[While, VMLinearState, int] | None:
    branch_index = _next_region_control(instructions, condition_start, range_end, profile)
    if branch_index is None or branch_index == jump_index:
        return None
    short_circuit = _consume_stateful_tail_short_circuit_loop(
        instructions,
        body_start,
        branch_index,
        condition_start,
        state,
        profile,
        callbacks,
        range_end,
    )
    if short_circuit is not None:
        return short_circuit
    branch = instructions[branch_index]
    if not profile.is_conditional_jump(branch):
        return None
    target_offset = profile.target_offset(branch)
    if target_offset is None:
        return None
    target_index = _instruction_index_by_offset(instructions, target_offset, profile)
    if target_index is None or not (body_start <= target_index < condition_start):
        return None
    condition_prefix = callbacks.lift_linear(condition_start, branch_index, state.locals.copy(), state.stack)
    if condition_prefix is None:
        return None
    width = callbacks.branch_stack_width(branch)
    if len(condition_prefix.stack) < width:
        return None
    base_stack, condition_stack = _split_condition_stack(condition_prefix.stack, width)
    condition = callbacks.branch_condition(branch, condition_stack)
    if condition is None:
        return None
    condition = _negate_condition(condition, source=SourceRef(frontend=profile.frontend, offset=profile.offset(branch)))
    body_state = _lift_stateful_branch_range(
        instructions,
        target_index,
        condition_start,
        VMLinearState(locals=state.locals.copy(), stack=state.stack),
        profile,
        callbacks,
    )
    if body_state is None:
        return None
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(branch))
    return (
        While(source=source, condition=condition, body=_state_body(body_state)),
        VMLinearState(locals=_merge_changed_locals(state.locals, body_state.locals), stack=base_stack),
        branch_index + 1,
    )


def _consume_stateful_tail_short_circuit_loop(
    instructions: tuple[InstructionT, ...],
    range_body_start: int,
    branch_index: int,
    condition_start: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
    range_end: int,
) -> tuple[While, VMLinearState, int] | None:
    first_branch = instructions[branch_index]
    if not profile.is_conditional_jump(first_branch):
        return None
    first_target_offset = profile.target_offset(first_branch)
    if first_target_offset is None:
        return None
    first_target = _instruction_index_by_offset(instructions, first_target_offset, profile)
    if first_target is None:
        return None

    first_prefix = callbacks.lift_linear(condition_start, branch_index, state.locals.copy(), state.stack)
    if first_prefix is None:
        return None
    first_width = callbacks.branch_stack_width(first_branch)
    if len(first_prefix.stack) < first_width:
        return None
    base_stack, first_condition_stack = _split_condition_stack(first_prefix.stack, first_width)
    first_condition = callbacks.branch_condition(first_branch, first_condition_stack)
    if first_condition is None:
        return None

    second_branch_index = _next_region_control(instructions, branch_index + 1, range_end, profile)
    if second_branch_index is None or not profile.is_conditional_jump(instructions[second_branch_index]):
        return None
    second_branch = instructions[second_branch_index]
    second_target_offset = profile.target_offset(second_branch)
    if second_target_offset is None:
        return None
    second_target = _instruction_index_by_offset(instructions, second_target_offset, profile)
    if second_target is None:
        return None

    second_prefix = callbacks.lift_linear(branch_index + 1, second_branch_index, state.locals.copy(), base_stack)
    if second_prefix is None:
        return None
    second_width = callbacks.branch_stack_width(second_branch)
    if len(second_prefix.stack) < second_width:
        return None
    second_base_stack, second_condition_stack = _split_condition_stack(second_prefix.stack, second_width)
    if len(second_base_stack) != len(base_stack):
        return None
    second_condition = callbacks.branch_condition(second_branch, second_condition_stack)
    if second_condition is None:
        return None

    after_second_branch = second_branch_index + 1
    body_targets = [target for target in (first_target, second_target) if range_body_start <= target < condition_start]
    if not body_targets:
        return None
    actual_body_start = min(body_targets)
    if actual_body_start >= condition_start:
        return None
    body_state = _lift_stateful_branch_range(
        instructions,
        actual_body_start,
        condition_start,
        VMLinearState(locals=state.locals.copy(), stack=state.stack),
        profile,
        callbacks,
    )
    if body_state is None:
        return None

    exit_candidates = [target for target in (first_target, second_target, after_second_branch) if target >= after_second_branch]
    if not exit_candidates:
        return None
    loop_exit = min(exit_candidates)
    source = SourceRef(frontend=profile.frontend, offset=profile.offset(first_branch))
    condition = _short_circuit_continue_condition(
        first_condition=first_condition,
        first_branch_target=first_target,
        second_condition=second_condition,
        second_branch_target=second_target,
        body_start=actual_body_start,
        exit_index=loop_exit,
        source=source,
    )
    if condition is None:
        return None
    return (
        While(source=source, condition=condition, body=_state_body(body_state)),
        VMLinearState(locals=_merge_changed_locals(state.locals, body_state.locals), stack=base_stack),
        loop_exit,
    )


def _consume_stateful_short_circuit_loop(
    instructions: tuple[InstructionT, ...],
    condition_start: int,
    branch_index: int,
    target_index: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
    range_end: int | None,
) -> tuple[While, VMLinearState, int] | None:
    next_branch_index = _next_region_control(instructions, branch_index + 1, target_index, profile)
    if next_branch_index is None or not profile.is_conditional_jump(instructions[next_branch_index]):
        return None
    next_target_offset = profile.target_offset(instructions[next_branch_index])
    if next_target_offset is None:
        return None
    next_target_index = _instruction_index_by_offset(instructions, next_target_offset, profile)
    if next_target_index is None:
        return None
    if range_end is not None and next_target_index > range_end:
        return None

    first_branch = instructions[branch_index]
    second_branch = instructions[next_branch_index]
    first_width = callbacks.branch_stack_width(first_branch)
    if len(state.stack) < first_width:
        return None
    base_stack, first_condition_stack = _split_condition_stack(state.stack, first_width)
    first_jump_condition = callbacks.branch_condition(first_branch, first_condition_stack)
    if first_jump_condition is None:
        return None

    mid_state = callbacks.lift_linear(branch_index + 1, next_branch_index, state.locals.copy(), base_stack)
    if mid_state is None:
        return None
    second_width = callbacks.branch_stack_width(second_branch)
    if len(mid_state.stack) < second_width:
        return None
    second_base_stack, second_condition_stack = _split_condition_stack(mid_state.stack, second_width)
    if len(second_base_stack) != len(base_stack):
        return None
    second_jump_condition = callbacks.branch_condition(second_branch, second_condition_stack)
    if second_jump_condition is None:
        return None

    body_start = _short_circuit_body_start(
        first_branch_target=target_index,
        second_branch_target=next_target_index,
        after_second_branch=next_branch_index + 1,
    )
    if body_start is None:
        return None

    loop_exit = _short_circuit_exit_index(
        first_branch_target=target_index,
        second_branch_target=next_target_index,
        after_second_branch=next_branch_index + 1,
        body_start=body_start,
    )
    if loop_exit is None:
        return None
    backedge_index = _trailing_backedge_to_prefix(
        instructions,
        body_start,
        loop_exit,
        condition_start,
        next_branch_index,
        profile,
    )
    if backedge_index is None or body_start >= backedge_index:
        return None
    body_state = _lift_stateful_branch_range(
        instructions,
        body_start,
        backedge_index,
        VMLinearState(locals=state.locals.copy(), stack=base_stack),
        profile,
        callbacks,
    )
    if body_state is None:
        return None

    source = SourceRef(frontend=profile.frontend, offset=profile.offset(first_branch))
    loop_condition = _short_circuit_continue_condition(
        first_condition=first_jump_condition,
        first_branch_target=target_index,
        second_condition=second_jump_condition,
        second_branch_target=next_target_index,
        body_start=body_start,
        exit_index=loop_exit,
        source=source,
    )
    if loop_condition is None:
        return None
    return (
        While(source=source, condition=loop_condition, body=_state_body(body_state)),
        VMLinearState(locals=_merge_changed_locals(state.locals, body_state.locals), stack=base_stack),
        loop_exit,
    )


def _short_circuit_body_start(
    *,
    first_branch_target: int,
    second_branch_target: int,
    after_second_branch: int,
) -> int | None:
    if first_branch_target == after_second_branch:
        return after_second_branch
    if first_branch_target == second_branch_target and after_second_branch < first_branch_target:
        return after_second_branch
    if second_branch_target == first_branch_target:
        return first_branch_target
    return None


def _short_circuit_exit_index(
    *,
    first_branch_target: int,
    second_branch_target: int,
    after_second_branch: int,
    body_start: int,
) -> int | None:
    candidates = [
        target
        for target in (first_branch_target, second_branch_target)
        if target != body_start and target > after_second_branch
    ]
    if candidates:
        return min(candidates)
    if first_branch_target == second_branch_target and first_branch_target > after_second_branch:
        return first_branch_target
    if first_branch_target == second_branch_target and body_start == after_second_branch:
        return first_branch_target
    return None


def _short_circuit_continue_condition(
    *,
    first_condition: Expr,
    first_branch_target: int,
    second_condition: Expr,
    second_branch_target: int,
    body_start: int,
    exit_index: int,
    source: SourceRef,
) -> Expr | None:
    if first_branch_target == body_start and second_branch_target == body_start:
        return BinaryOp(
            source=source,
            op="||",
            left=_negate_condition(first_condition, source=source),
            right=_negate_condition(second_condition, source=source),
            semantics="static",
        )
    if first_branch_target == body_start and second_branch_target == exit_index:
        return BinaryOp(
            source=source,
            op="||",
            left=_negate_condition(first_condition, source=source),
            right=second_condition,
            semantics="static",
        )
    if first_branch_target == exit_index and second_branch_target == body_start:
        return BinaryOp(
            source=source,
            op="&&",
            left=first_condition,
            right=_negate_condition(second_condition, source=source),
            semantics="static",
        )
    if first_branch_target == exit_index and second_branch_target == exit_index:
        return BinaryOp(
            source=source,
            op="&&",
            left=first_condition,
            right=second_condition,
            semantics="static",
        )
    return None


def _state_body(state: VMLinearState) -> tuple[Stmt, ...]:
    if state.terminator is None:
        return state.statements
    return (*state.statements, state.terminator)


def _negate_condition(condition: Expr, *, source: SourceRef) -> Expr:
    if isinstance(condition, BinaryOp):
        inverted = {
            "==": "!=",
            "!=": "==",
            "<": ">=",
            "<=": ">",
            ">": "<=",
            ">=": "<",
        }.get(condition.op)
        if inverted is not None:
            return BinaryOp(
                source=source,
                op=inverted,
                left=condition.left,
                right=condition.right,
                semantics=condition.semantics,
            )
    from unidecompiler.core.ir import UnaryOp

    return UnaryOp(source=source, op="not ", value=condition)


def _lift_stateful_body(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
) -> VMLinearState | None:
    if _next_region_control(instructions, start, end, profile) is None:
        return callbacks.lift_linear(start, end, state.locals, state.stack)
    nested = _lift_stateful_control_range(instructions, start, end, state, profile, callbacks)
    if nested is None:
        return None
    if nested.status == "ok":
        return VMLinearState(locals=state.locals, stack=state.stack, statements=nested.statements, terminator=nested.terminator)
    if nested.status == "partial" and nested.stopped_at == end - 1 and profile.is_backward_jump(instructions[end - 1]):
        source = SourceRef(frontend=profile.frontend, offset=profile.offset(instructions[end - 1]))
        return VMLinearState(locals=state.locals, stack=state.stack, statements=(*nested.statements, Continue(source=source)))
    return None


def _lift_stateful_branch_range(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    state: VMLinearState,
    profile: VMRegionProfile[InstructionT],
    callbacks: VMStatefulCallbacks[InstructionT],
) -> VMLinearState | None:
    if _next_region_control(instructions, start, end, profile) is not None:
        return _lift_stateful_body(instructions, start, end, state, profile, callbacks)
    return callbacks.lift_linear(start, end, state.locals, state.stack)


def _first_forward_jump(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    for index in range(start, end):
        if profile.is_forward_jump(instructions[index]):
            return index
    return None


def _merge_expression_locals(left: dict[str, Expr], right: dict[str, Expr]) -> dict[str, Expr]:
    merged = dict(right)
    merged.update(left)
    return merged


def _merge_changed_locals(base: dict[str, Expr], branch: dict[str, Expr]) -> dict[str, Expr]:
    merged = dict(base)
    for name, value in branch.items():
        if base.get(name) != value:
            merged[name] = value
    return merged


def _source_for(
    profile: VMRegionProfile[InstructionT],
    instructions: tuple[InstructionT, ...],
    index: int | None,
) -> SourceRef:
    return SourceRef(
        frontend=profile.frontend,
        offset=profile.offset(instructions[index]) if index is not None and 0 <= index < len(instructions) else None,
    )


def _materialize_stack_snapshot(stack: tuple[Expr, ...], source: SourceRef) -> tuple[Assign, ...]:
    return tuple(
        Assign(source=source, target=Var(name=f"stack{index}", source=source), value=value)
        for index, value in enumerate(stack)
    )


def _next_region_control(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    for index in range(start, end):
        if profile.is_control(instructions[index]):
            return index
    return None


def _next_backward_or_region_end(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int:
    index = start
    while index < end:
        await_end = profile.await_region_end(index, end)
        if await_end is not None and await_end > index:
            index = await_end
            continue
        if profile.is_backward_jump(instructions[index]):
            return index
        index += 1
    return end


def _next_after_backward(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int:
    for index in range(start, end):
        if profile.is_backward_jump(instructions[index]):
            return index + 1
    return start


def _trailing_forward_jump(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    if start >= end:
        return None
    candidate = end - 1
    if profile.is_forward_jump(instructions[candidate]):
        return candidate
    return None


def _trailing_backedge_to_region(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    region_start: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    if start >= end:
        return None
    cursor = end - 1
    while cursor >= start and profile.is_noise(instructions[cursor]):
        cursor -= 1
    if cursor < start or not profile.is_backward_jump(instructions[cursor]):
        return None
    target = profile.target_offset(instructions[cursor])
    region_offset = profile.offset(instructions[region_start])
    if target is None or region_offset is None:
        return None
    return cursor if target == region_offset else None


def _pretest_loop_backedge(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    condition_index: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    if start >= end:
        return None
    cursor = end - 1
    while cursor >= start and _is_region_noise(instructions[cursor], profile):
        cursor -= 1
    if cursor < start or not profile.is_backward_jump(instructions[cursor]):
        return None
    target = profile.target_offset(instructions[cursor])
    if target is None:
        return None
    target_index = _instruction_index_by_offset(instructions, target, profile)
    if target_index is None:
        return None
    return cursor if target_index == condition_index else None


def _trailing_backedge_to_prefix(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    prefix_start: int,
    prefix_end: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    if start >= end:
        return None
    cursor = end - 1
    while cursor >= start and profile.is_noise(instructions[cursor]):
        cursor -= 1
    if cursor < start or not profile.is_backward_jump(instructions[cursor]):
        return None
    target = profile.target_offset(instructions[cursor])
    if target is None:
        return None
    target_index = _instruction_index_by_offset(instructions, target, profile)
    if target_index is None:
        return None
    return cursor if prefix_start <= target_index <= prefix_end else None


def _region_is_backward_jump(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    profile: VMRegionProfile[InstructionT],
) -> bool:
    cursor = _skip_noise(instructions, start, end, profile)
    return cursor < end and profile.is_backward_jump(instructions[cursor])


def _region_is_forward_break(
    instructions: tuple[InstructionT, ...],
    start: int,
    end: int,
    region_end: int,
    profile: VMRegionProfile[InstructionT],
) -> bool:
    cursor = _skip_noise(instructions, start, end, profile)
    while cursor < end and profile.is_noise(instructions[cursor]):
        cursor += 1
    return (
        cursor < end
        and profile.is_forward_jump(instructions[cursor])
        and _jump_target_outside_region(instructions, instructions[cursor], region_end, profile)
    )


def _jump_target_outside_region(
    instructions: tuple[InstructionT, ...],
    instruction: InstructionT,
    region_end: int,
    profile: VMRegionProfile[InstructionT],
) -> bool:
    target = profile.target_offset(instruction)
    if target is None:
        return False
    if region_end <= 0:
        return True
    if region_end >= len(instructions):
        last_offset = profile.offset(instructions[-1])
        return last_offset is not None and target > last_offset
    boundary_offset = profile.offset(instructions[region_end])
    return boundary_offset is not None and target >= boundary_offset


def _jump_target_at_or_before_boundary(
    instructions: tuple[InstructionT, ...],
    instruction: InstructionT,
    boundary: int,
    profile: VMRegionProfile[InstructionT],
) -> bool:
    target = profile.target_offset(instruction)
    if target is None or boundary < 0:
        return False
    if boundary >= len(instructions):
        return True
    boundary_offset = profile.offset(instructions[boundary])
    return boundary_offset is not None and target <= boundary_offset


def _instruction_index_by_offset(
    instructions: tuple[InstructionT, ...],
    offset: int,
    profile: VMRegionProfile[InstructionT],
) -> int | None:
    for index, instruction in enumerate(instructions):
        if profile.offset(instruction) == offset:
            return index
    return None


def _opcode_name(instruction: object) -> str:
    return str(getattr(instruction, "opname", type(instruction).__name__))
