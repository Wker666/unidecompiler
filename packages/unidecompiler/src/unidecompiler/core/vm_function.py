from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Callable, Generic, TypeVar

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.effects import BuildArray, ExceptionMatch, Push, RaiseTop, RaiseWithCause, ReraiseTop, ReturnTop, StoreLocal
from unidecompiler.core.function_assembly import FunctionBlockSpec, assemble_entry_function
from unidecompiler.core.function_assembly import assemble_function
from unidecompiler.core.function_assembly import assemble_function_without_blocks
from unidecompiler.core.low_level_cfg_structuring import apply_low_level_cfg_structuring
from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    ArrayLiteral,
    BasicBlock,
    BinaryOp,
    Branch,
    Break,
    Call,
    CapturedVar,
    CollectionProjection,
    Const,
    Continue,
    Expr,
    ExprStmt,
    ForEach,
    ForRange,
    FunctionIR,
    GetAttr,
    GetItem,
    Global,
    If,
    Jump,
    MapLiteral,
    MultiBranch,
    NewObject,
    ObjectLiteral,
    Phi,
    Raise,
    Reraise,
    Return,
    SetLiteral,
    SourceRef,
    Stmt,
    StoreAttr,
    StoreItem,
    Switch,
    ExceptHandler,
    Try,
    TableLiteral,
    UnaryOp,
    Unsupported,
    Var,
    While,
    Yield,
)
from unidecompiler.core.stack_machine import StackLiftResult
from unidecompiler.core.vm_bytecode import VMBytecodeStep, run_vm_steps
from unidecompiler.core.vm_region import (
    VMRegionCallbacks,
    VMLinearState,
    VMRegionProfile,
    VMStatefulCallbacks,
    lift_control_region,
    lift_stateful_low_level_cfg,
    lift_stateful_control_prefix,
)
from unidecompiler.core.vm_structures import contains_vm_unsupported, is_vm_return, vm_return


InputT = TypeVar("InputT")
LiftResultT = TypeVar("LiftResultT")
LiftHandler = Callable[[InputT], LiftResultT | None]
LiftFallback = Callable[[InputT], LiftResultT]


@dataclass(frozen=True)
class VMFunctionSpec:
    name: str
    params: tuple[str, ...]
    frontend: str
    instruction_count: int
    local_names: tuple[str, ...] = ()
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class VMBlockSpec:
    id: str
    statements: tuple[Stmt, ...] = ()
    terminator: object | None = None


@dataclass(frozen=True)
class VMLiftRule(Generic[InputT, LiftResultT]):
    name: str
    lift: LiftHandler[InputT, LiftResultT]
    accept: Callable[[LiftResultT], bool] | None = None


@dataclass(frozen=True)
class VMLiftTable(Generic[InputT, LiftResultT]):
    """Table of frontend-local lift attempts with core-owned dispatch order."""

    rules: tuple[VMLiftRule[InputT, LiftResultT], ...]
    fallback: LiftFallback[InputT]

    def lift(self, unit: InputT) -> LiftResultT:
        for rule in self.rules:
            result = rule.lift(unit)
            if result is None:
                continue
            if rule.accept is not None and not rule.accept(result):
                continue
            return result
        return self.fallback(unit)


def lift_linear_vm_function(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    *,
    initial_locals: dict[str, Expr] | None = None,
    initial_stack: tuple[Expr, ...] = (),
    structured_lift: str | None = None,
) -> FunctionIR:
    result = run_vm_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)
    if result.state.diagnostics:
        return unsupported_vm_function(spec, tuple(result.state.diagnostics), structured_lift=structured_lift)
    if result.stopped_at is not None and result.state.terminator is None:
        return unsupported_vm_function(spec, (result.stopped_at.opcode,), structured_lift=structured_lift)
    if result.state.terminator is None and _ends_with_raise(tuple(result.state.statements)):
        return entry_vm_function(
            spec,
            statements=tuple(result.state.statements),
            terminator=None,
            status="ok",
            structured_lift=structured_lift,
        )
    if result.state.terminator is None:
        return unsupported_vm_function(spec, ("missing-return",), structured_lift=structured_lift)
    return entry_vm_function(
        spec,
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
        status="ok",
        structured_lift=structured_lift,
    )


def lift_vm_step_function(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    **kwargs: Any,
) -> FunctionIR:
    """Lift VM steps and retain a frontend-neutral instruction projection."""
    function = _lift_vm_step_function(spec, steps, **kwargs)
    instructions = tuple(_generic_instruction(step) for step in steps)
    return replace(function, metadata={**function.metadata, "bytecode_instructions": instructions})


def _generic_instruction(step: VMBytecodeStep) -> dict[str, Any]:
    decoded = step.decoded
    operands = () if decoded is None else tuple(
        {"role": operand.role, "text": operand.text or str(operand.value)}
        for operand in decoded.operands
    )
    return {
        "offset": step.source.offset,
        "opcode": step.opcode,
        "operands": operands,
        "raw": step.raw or ("" if decoded is None else decoded.raw),
        "artifact_range": None if decoded is None else decoded.artifact_range,
        "source": step.source,
        "control": tuple(
            {"kind": hint.kind, "target": hint.target, "flow": hint.flow}
            for hint in step.hints
            if hint.kind in {"branch-target", "loop-backedge", "case-target", "default-target"}
            and hint.target is not None
        ),
    }


def _lift_vm_step_function(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    *,
    profile: VMRegionProfile[VMBytecodeStep] | None = None,
    callbacks: VMRegionCallbacks[VMBytecodeStep] | None = None,
    stateful_callbacks: VMStatefulCallbacks[VMBytecodeStep] | None = None,
    initial_locals: dict[str, Expr] | None = None,
    initial_stack: tuple[Expr, ...] = (),
    raw_window: Callable[[int], tuple[str, ...]] | None = None,
) -> FunctionIR:
    """Lift a frontend-submitted VM function through the generic core pipeline."""

    if not steps:
        return entry_vm_function(spec, terminator=vm_return(source=SourceRef(frontend=spec.frontend)), structured_lift="empty")
    if profile is not None and stateful_callbacks is not None and _has_exception_region_facts(steps):
        protected = _lift_exception_region_candidate(spec, steps, profile, stateful_callbacks, callbacks)
        if protected is not None:
            return protected
    if stateful_callbacks is not None and any(
        any(hint.kind == "materialized-condition" for hint in step.hints) for step in steps
    ):
        low_level = _lift_low_level_cfg_candidate(spec, steps, profile, stateful_callbacks) if profile is not None else None
        if low_level is not None and not _function_has_unsupported(low_level):
            return low_level
    if profile is not None and any(profile.is_control(step) for step in steps):
        if callbacks is not None:
            statements = lift_control_region(steps, 0, len(steps), profile, callbacks, initial_stack)
            if statements and any(not isinstance(statement, Unsupported) for statement in statements):
                has_unsupported = any(contains_vm_unsupported(statement) for statement in statements)
                has_return = any(is_vm_return(statement) for statement in statements)
                has_unbound_loop_control = _has_unbound_loop_control(statements)
                if (has_unsupported or has_unbound_loop_control) and stateful_callbacks is not None:
                    stateful = _lift_stateful_candidate(spec, steps, profile, stateful_callbacks, raw_window)
                    if stateful is not None and not _function_has_unsupported(stateful) and not _function_has_unbound_loop_control(stateful):
                        return stateful
                    if has_unbound_loop_control:
                        low_level = _lift_low_level_cfg_candidate(spec, steps, profile, stateful_callbacks)
                        if low_level is not None:
                            return low_level
                function = entry_vm_function(
                    spec,
                    statements=statements,
                    terminator=None if has_return else vm_return(source=SourceRef(frontend=spec.frontend)),
                    status="partial" if has_unsupported or has_unbound_loop_control else "ok",
                    unsupported_reason=(
                        "contains VM control-flow regions that were partially recovered"
                        if has_unsupported
                        else "contains loop control outside a recovered loop"
                        if has_unbound_loop_control
                        else None
                    ),
                    unsupported_opcodes=_unsupported_opcodes_from_statements(statements),
                    structured_lift="generic-vm-pipeline",
                )
                if (
                    stateful_callbacks is not None
                    and (
                        (
                            _has_backward_jump(steps, profile)
                            and not _function_has_recovered_loop_construct(function)
                            and not _function_has_collection_projection(function)
                        )
                        or (_raise_effect_count(steps) > _function_raise_statement_count(function))
                    )
                ):
                    low_level = _lift_low_level_cfg_candidate(spec, steps, profile, stateful_callbacks)
                    if (
                        low_level is not None
                        and not _function_has_unsupported(low_level)
                        and not _function_reads_unbound_locals(low_level)
                    ):
                        return low_level
                finalized = finalize_recovered_vm_function(spec, function)
                if finalized.metadata.get("decompile_status") == "unsupported" and stateful_callbacks is not None:
                    low_level = _lift_low_level_cfg_candidate(spec, steps, profile, stateful_callbacks)
                    if low_level is not None and not _function_reads_unbound_locals(low_level):
                        return low_level
                return finalized
        if stateful_callbacks is not None:
            stateful = _lift_stateful_candidate(spec, steps, profile, stateful_callbacks, raw_window)
            if (
                stateful is not None
                and stateful.metadata.get("decompile_status") == "ok"
                and not _function_has_unbound_loop_control(stateful)
                and not _function_reads_unbound_locals(stateful)
                and not _function_has_degenerate_branch(stateful)
            ):
                return stateful
            low_level = _lift_low_level_cfg_candidate(spec, steps, profile, stateful_callbacks)
            if low_level is not None:
                return finalize_recovered_vm_function(spec, low_level)
    result = run_vm_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)
    if result.state.diagnostics:
        return unsupported_vm_function(spec, tuple(result.state.diagnostics), structured_lift="generic-vm-pipeline")
    if result.stopped_at is not None and result.state.terminator is None:
        index = steps.index(result.stopped_at)
        return unsupported_vm_function(
            spec,
            (result.stopped_at.opcode,),
            raw=raw_window(index) if raw_window is not None else (result.stopped_at.raw,),
            structured_lift="generic-vm-pipeline",
        )
    if result.state.terminator is None and _ends_with_raise(tuple(result.state.statements)):
        return finalize_recovered_vm_function(spec, entry_vm_function(
            spec,
            statements=tuple(result.state.statements),
            terminator=None,
            status="ok",
            structured_lift="generic-vm-pipeline",
        ))
    if result.state.terminator is None:
        return unsupported_vm_function(spec, ("missing-return",), structured_lift="generic-vm-pipeline")
    statements = tuple(result.state.statements)
    has_unsupported = any(contains_vm_unsupported(statement) for statement in statements)
    return finalize_recovered_vm_function(spec, entry_vm_function(
        spec,
        statements=statements,
        terminator=result.state.terminator,
        status="partial" if has_unsupported else "ok",
        unsupported_reason="contains VM opcodes with unknown thin effects" if has_unsupported else None,
        unsupported_opcodes=_unsupported_opcodes_from_statements(statements),
        structured_lift="generic-vm-pipeline",
    ))


def entry_vm_function(
    spec: VMFunctionSpec,
    *,
    statements: tuple[Stmt, ...] = (),
    terminator=None,
    status: str = "ok",
    unsupported_reason: str | None = None,
    unsupported_opcodes: tuple[str, ...] = (),
    unsupported_raw: tuple[str, ...] = (),
    structured_lift: str | None = None,
) -> FunctionIR:
    return assemble_entry_function(
        name=spec.name,
        params=spec.params,
        frontend=spec.frontend,
        statements=statements,
        terminator=terminator,
        metadata={
            **(spec.metadata or {}),
            "decompile_status": status,
            "unsupported_reason": unsupported_reason,
            "unsupported_opcodes": unsupported_opcodes,
            "unsupported_raw": unsupported_raw,
            "instruction_count": spec.instruction_count,
            "local_names": spec.local_names,
            "stack_lifter": "generic",
            **({"structured_lift": structured_lift} if structured_lift else {}),
        },
        recovery_kind=structured_lift,
    )


def finalize_recovered_vm_function(spec: VMFunctionSpec, function: FunctionIR) -> FunctionIR:
    if function.metadata.get("decompile_status") == "unsupported":
        return function
    structural_reason = _unsafe_structural_recovery_reason(function)
    if function.recovery_kind == "generic-vm-low-level-cfg":
        if structural_reason is None:
            if _function_has_try_regions(function):
                return apply_low_level_cfg_structuring(
                    function,
                    is_safe=lambda structured: (
                        _unsafe_structural_recovery_reason(structured) is None
                        and not _function_unbound_local_reads(structured, spec)
                    ),
                )
            return apply_low_level_cfg_structuring(
                function,
                is_safe=lambda structured: (
                    _unsafe_structural_recovery_reason(structured) is None
                    and not _function_unbound_local_reads(structured, spec)
                    and (
                        _function_has_degenerate_branch(function)
                        or not _function_has_degenerate_branch(structured)
                    )
                ),
            )
        unsupported = unsupported_vm_function(
            spec,
            (),
            reason=structural_reason,
            structured_lift=function.recovery_kind,
        )
        return replace(unsupported, metadata={**unsupported.metadata, "unbound_locals": ()})
    unbound = _function_unbound_local_reads(function, spec)
    if not unbound and structural_reason is None:
        return function
    if structural_reason is not None:
        unsupported = unsupported_vm_function(
            spec,
            (),
            reason=structural_reason,
            structured_lift=function.recovery_kind,
        )
        return replace(unsupported, metadata={**unsupported.metadata, "unbound_locals": unbound})
    unsupported = unsupported_vm_function(
        spec,
        (),
        reason="recovered IR reads locals that were never bound",
        structured_lift=function.recovery_kind,
    )
    return replace(unsupported, metadata={**unsupported.metadata, "unbound_locals": unbound})


def _function_reads_unbound_locals(function: FunctionIR) -> bool:
    inferred_locals = _defined_names_in_function(function)
    spec = VMFunctionSpec(
        name=function.name,
        params=function.params,
        frontend=str(function.source.frontend if function.source else function.metadata.get("frontend", "unknown")),
        instruction_count=int(function.metadata.get("instruction_count", 0) or 0),
        local_names=tuple(sorted(set(function.metadata.get("local_names", ()) or ()) | inferred_locals)),
        metadata=function.metadata,
    )
    return bool(_function_unbound_local_reads(function, spec))


def _function_has_degenerate_branch(function: FunctionIR) -> bool:
    return any(
        isinstance(block.terminator, Branch)
        and block.terminator.true_target == block.terminator.false_target
        for block in function.blocks
    )


def _function_has_try_regions(function: FunctionIR) -> bool:
    def contains_try(statements: tuple[Stmt, ...]) -> bool:
        return any(isinstance(statement, Try) for statement in statements)

    return any(contains_try(block.statements) for block in function.blocks)


def _function_unbound_local_reads(function: FunctionIR, spec: VMFunctionSpec) -> tuple[str, ...]:
    if function.recovery_kind == "generic-vm-low-level-cfg":
        return _function_unbound_local_reads_cfg(function, spec)
    local_names = set(spec.params) | set(spec.local_names)
    bound = set(spec.params)
    unbound: set[str] = set()
    for block in function.blocks:
        _collect_unbound_from_statements(block.statements, local_names, bound, unbound)
        if block.terminator is not None:
            _collect_unbound_from_terminator(block.terminator, local_names, bound, unbound)
    return tuple(sorted(unbound))


def _function_unbound_local_reads_cfg(function: FunctionIR, spec: VMFunctionSpec) -> tuple[str, ...]:
    local_names = set(spec.params) | set(spec.local_names)
    local_names -= _defined_names_in_function(function)
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return ()
    block_defs = {block.id: _defined_names_in_statements(block.statements) for block in function.blocks}
    entry_bound = {block.id: set(local_names) for block in function.blocks}
    if cfg.entry in entry_bound:
        entry_bound[cfg.entry] = set(spec.params)
    changed = True
    while changed:
        changed = False
        for block in function.blocks:
            if block.id == cfg.entry:
                continue
            predecessors = cfg.predecessors(block.id)
            if not predecessors:
                candidate = set()
            else:
                exits = [entry_bound[pred] | block_defs.get(pred, set()) for pred in predecessors]
                candidate = set.intersection(*exits) if exits else set()
            if candidate != entry_bound[block.id]:
                entry_bound[block.id] = candidate
                changed = True

    unbound: set[str] = set()
    for block in function.blocks:
        bound = set(entry_bound[block.id])
        predecessors = cfg.predecessors(block.id)
        _collect_unbound_from_statements_cfg(block.statements, local_names, bound, predecessors, entry_bound, block_defs, unbound)
        if block.terminator is not None:
            _collect_unbound_from_terminator(block.terminator, local_names, bound, unbound)
    return tuple(sorted(unbound))


def _defined_names_in_function(function: FunctionIR) -> set[str]:
    defined: set[str] = set()
    for block in function.blocks:
        defined.update(_defined_names_in_statements(block.statements))
    return defined


def _defined_names_in_statements(statements: tuple[Stmt, ...]) -> set[str]:
    defined: set[str] = set()
    for statement in statements:
        if isinstance(statement, Assign) and isinstance(statement.target, Var):
            defined.add(statement.target.name)
        elif isinstance(statement, AssignMany):
            defined.update(target.name for target in statement.targets)
        elif isinstance(statement, Try):
            defined.update(_defined_names_in_statements(statement.body))
            for handler in statement.handlers:
                if handler.binding is not None:
                    defined.add(handler.binding.name)
                defined.update(_defined_names_in_statements(handler.body))
        elif isinstance(statement, Switch):
            for _value, body in statement.cases:
                defined.update(_defined_names_in_statements(body))
            defined.update(_defined_names_in_statements(statement.default_body))
    return defined


def _collect_unbound_from_statements_cfg(
    statements: tuple[Stmt, ...],
    local_names: set[str],
    bound: set[str],
    predecessors: tuple[str, ...],
    entry_bound: dict[str, set[str]],
    block_defs: dict[str, set[str]],
    unbound: set[str],
) -> None:
    for statement in statements:
        if isinstance(statement, Assign) and isinstance(statement.value, Phi):
            for pred, value in statement.value.incoming:
                pred_bound = entry_bound.get(pred, set()) | block_defs.get(pred, set())
                _collect_unbound_from_expr(value, local_names, pred_bound, unbound)
            if isinstance(statement.target, Var):
                bound.add(statement.target.name)
            continue
        if isinstance(statement, AssignMany):
            for value in statement.values:
                if isinstance(value, Phi):
                    for pred, incoming in value.incoming:
                        pred_bound = entry_bound.get(pred, set()) | block_defs.get(pred, set())
                        _collect_unbound_from_expr(incoming, local_names, pred_bound, unbound)
                else:
                    _collect_unbound_from_expr(value, local_names, bound, unbound)
            for target in statement.targets:
                bound.add(target.name)
            continue
        before = set(bound)
        _collect_unbound_from_statements((statement,), local_names, bound, unbound)
        if predecessors and isinstance(statement, Assign):
            bound.update(before)


def _function_has_one_shot_loop_condition(function: FunctionIR) -> bool:
    return any(_statements_have_one_shot_loop_condition(block.statements) for block in function.blocks)


def _function_has_escaped_stack_temporary(function: FunctionIR) -> bool:
    if function.recovery_kind == "generic-vm-low-level-cfg":
        return False
    return any(_statements_have_escaped_stack_temporary(block.statements) for block in function.blocks)


def _unsafe_structural_recovery_reason(function: FunctionIR) -> str | None:
    if _function_has_missing_cfg_target(function):
        return "recovered low-level CFG has unresolved control-flow targets"
    if _function_has_empty_control_body(function):
        return "recovered IR contains an empty control-flow body"
    if _function_has_one_shot_loop_condition(function):
        return "recovered IR collapsed a repeated loop condition into a one-shot branch"
    if _function_has_escaped_stack_temporary(function):
        return "recovered IR exposed an internal VM stack temporary"
    return None


def _function_has_missing_cfg_target(function: FunctionIR) -> bool:
    for block in function.blocks:
        terminator = block.terminator
        if isinstance(terminator, Jump) and terminator.target.startswith("missing_"):
            return True
        if isinstance(terminator, Branch) and (
            terminator.true_target.startswith("missing_") or terminator.false_target.startswith("missing_")
        ):
            return True
        if isinstance(terminator, MultiBranch) and (
            terminator.default_target.startswith("missing_")
            or any(target.startswith("missing_") for _value, target in terminator.cases)
        ):
            return True
    return False


def _function_has_empty_control_body(function: FunctionIR) -> bool:
    return any(_statements_have_empty_control_body(block.statements) for block in function.blocks)


def _statements_have_empty_control_body(statements: tuple[Stmt, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, (If, While, ForEach, ForRange)):
            if not tuple(getattr(statement, "body", ()) or ()) and isinstance(statement, (While, ForEach, ForRange)):
                return True
            if isinstance(statement, If) and not statement.then_body and not statement.else_body:
                return True
        if isinstance(statement, Try) and (
            _statements_have_empty_control_body(statement.body)
            or any(_statements_have_empty_control_body(handler.body) for handler in statement.handlers)
        ):
            return True
        nested_bodies = (
            tuple(getattr(statement, "then_body", ()) or ()),
            tuple(getattr(statement, "else_body", ()) or ()),
            tuple(getattr(statement, "body", ()) or ()),
        )
        if any(body and _statements_have_empty_control_body(body) for body in nested_bodies):
            return True
    return False


def _statements_have_one_shot_loop_condition(
    statements: tuple[Stmt, ...],
    *,
    inside_loop: bool = False,
) -> bool:
    for index, statement in enumerate(statements):
        if not inside_loop and isinstance(statement, If) and isinstance(statement.condition, Var):
            previous = statements[index - 1] if index > 0 else None
            if (
                isinstance(previous, Assign)
                and isinstance(previous.target, Var)
                and previous.target.name == statement.condition.name
                and _has_continue_in_statements(statement.then_body)
            ):
                return True
        conditional_bodies = (
            tuple(getattr(statement, "then_body", ()) or ()),
            tuple(getattr(statement, "else_body", ()) or ()),
        )
        if any(
            body and _statements_have_one_shot_loop_condition(body, inside_loop=inside_loop)
            for body in conditional_bodies
        ):
            return True
        body = tuple(getattr(statement, "body", ()) or ())
        if body and _statements_have_one_shot_loop_condition(
            body,
            # A ``While`` body repeats the assignment/condition pair on each
            # iteration.  A projected ``for``/iterator body can instead hide
            # a separate inner loop behind its own ``Continue``; keep the
            # conservative guard active there until that inner loop is
            # independently proven and recovered.
            inside_loop=inside_loop or isinstance(statement, While),
        ):
            return True
        if isinstance(statement, Try) and (
            _statements_have_one_shot_loop_condition(statement.body, inside_loop=inside_loop)
            or any(_statements_have_one_shot_loop_condition(handler.body, inside_loop=inside_loop) for handler in statement.handlers)
        ):
            return True
    return False


def _statements_have_escaped_stack_temporary(statements: tuple[Stmt, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, Assign) and _is_internal_stack_temp(statement.target):
            return True
        nested_bodies = (
            tuple(getattr(statement, "then_body", ()) or ()),
            tuple(getattr(statement, "else_body", ()) or ()),
            tuple(getattr(statement, "body", ()) or ()),
        )
        if any(body and _statements_have_escaped_stack_temporary(body) for body in nested_bodies):
            return True
        if isinstance(statement, Try) and (
            _statements_have_escaped_stack_temporary(statement.body)
            or any(_statements_have_escaped_stack_temporary(handler.body) for handler in statement.handlers)
        ):
            return True
    return False


def _is_internal_stack_temp(expr: Expr) -> bool:
    return isinstance(expr, Var) and expr.name.startswith("stack") and expr.name[5:].isdigit()


def _has_continue_in_statements(statements: tuple[Stmt, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, Continue):
            return True
        nested_bodies = (
            tuple(getattr(statement, "then_body", ()) or ()),
            tuple(getattr(statement, "else_body", ()) or ()),
            tuple(getattr(statement, "body", ()) or ()),
        )
        if any(body and _has_continue_in_statements(body) for body in nested_bodies):
            return True
        if isinstance(statement, Try) and (
            _has_continue_in_statements(statement.body)
            or any(_has_continue_in_statements(handler.body) for handler in statement.handlers)
        ):
            return True
    return False


def _collect_unbound_from_statements(
    statements: tuple[Stmt, ...],
    local_names: set[str],
    bound: set[str],
    unbound: set[str],
) -> None:
    for statement in statements:
        if isinstance(statement, Assign):
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            if isinstance(statement.target, Var):
                bound.add(statement.target.name)
            continue
        if isinstance(statement, AssignMany):
            for value in statement.values:
                _collect_unbound_from_expr(value, local_names, bound, unbound)
            for target in statement.targets:
                bound.add(target.name)
            continue
        if isinstance(statement, StoreItem):
            _collect_unbound_from_expr(statement.obj, local_names, bound, unbound)
            _collect_unbound_from_expr(statement.key, local_names, bound, unbound)
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            continue
        if isinstance(statement, StoreAttr):
            _collect_unbound_from_expr(statement.obj, local_names, bound, unbound)
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            continue
        if isinstance(statement, ExprStmt):
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            continue
        if isinstance(statement, Return):
            for value in statement.values:
                _collect_unbound_from_expr(value, local_names, bound, unbound)
            continue
        if isinstance(statement, Raise):
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            if statement.cause is not None:
                _collect_unbound_from_expr(statement.cause, local_names, bound, unbound)
            continue
        if isinstance(statement, Yield):
            _collect_unbound_from_expr(statement.value, local_names, bound, unbound)
            continue
        if isinstance(statement, If):
            _collect_unbound_from_expr(statement.condition, local_names, bound, unbound)
            then_bound = set(bound)
            else_bound = set(bound)
            _collect_unbound_from_statements(statement.then_body, local_names, then_bound, unbound)
            _collect_unbound_from_statements(statement.else_body, local_names, else_bound, unbound)
            bound.update(then_bound & else_bound)
            continue
        if isinstance(statement, Switch):
            _collect_unbound_from_expr(statement.selector, local_names, bound, unbound)
            exit_bounds: list[set[str]] = []
            for value, body in statement.cases:
                _collect_unbound_from_expr(value, local_names, bound, unbound)
                case_bound = set(bound)
                _collect_unbound_from_statements(body, local_names, case_bound, unbound)
                exit_bounds.append(case_bound)
            default_bound = set(bound)
            _collect_unbound_from_statements(statement.default_body, local_names, default_bound, unbound)
            exit_bounds.append(default_bound)
            bound.update(set.intersection(*exit_bounds))
            continue
        if isinstance(statement, While):
            _collect_unbound_from_expr(statement.condition, local_names, bound, unbound)
            body_bound = set(bound)
            _collect_unbound_from_statements(statement.body, local_names, body_bound, unbound)
            continue
        if isinstance(statement, ForEach):
            _collect_unbound_from_expr(statement.iterable, local_names, bound, unbound)
            body_bound = set(bound)
            body_bound.add(statement.target.name)
            _collect_unbound_from_statements(statement.body, local_names, body_bound, unbound)
            continue
        if isinstance(statement, ForRange):
            _collect_unbound_from_expr(statement.start, local_names, bound, unbound)
            _collect_unbound_from_expr(statement.stop, local_names, bound, unbound)
            _collect_unbound_from_expr(statement.step, local_names, bound, unbound)
            body_bound = set(bound)
            body_bound.add(statement.target.name)
            _collect_unbound_from_statements(statement.body, local_names, body_bound, unbound)
            continue
        if isinstance(statement, Try):
            body_bound = set(bound)
            _collect_unbound_from_statements(statement.body, local_names, body_bound, unbound)
            exit_bounds = [body_bound]
            for handler in statement.handlers:
                _collect_unbound_from_expr(handler.exception_type, local_names, bound, unbound)
                handler_bound = set(bound)
                if handler.binding is not None:
                    handler_bound.add(handler.binding.name)
                _collect_unbound_from_statements(handler.body, local_names, handler_bound, unbound)
                exit_bounds.append(handler_bound)
            bound.update(set.intersection(*exit_bounds))


def _collect_unbound_from_terminator(
    terminator: object,
    local_names: set[str],
    bound: set[str],
    unbound: set[str],
) -> None:
    if isinstance(terminator, Return):
        for value in terminator.values:
            _collect_unbound_from_expr(value, local_names, bound, unbound)
    elif isinstance(terminator, Branch):
        _collect_unbound_from_expr(terminator.condition, local_names, bound, unbound)
    elif isinstance(terminator, MultiBranch):
        _collect_unbound_from_expr(terminator.selector, local_names, bound, unbound)
        for value, _target in terminator.cases:
            _collect_unbound_from_expr(value, local_names, bound, unbound)


def _collect_unbound_from_expr(
    expr: Expr,
    local_names: set[str],
    bound: set[str],
    unbound: set[str],
) -> None:
    if isinstance(expr, Var):
        if expr.name in local_names and expr.name not in bound:
            unbound.add(expr.name)
        return
    if isinstance(expr, (Const, Global, CapturedVar)):
        return
    if isinstance(expr, UnaryOp):
        _collect_unbound_from_expr(expr.value, local_names, bound, unbound)
        return
    if isinstance(expr, BinaryOp):
        _collect_unbound_from_expr(expr.left, local_names, bound, unbound)
        _collect_unbound_from_expr(expr.right, local_names, bound, unbound)
        return
    if isinstance(expr, Call):
        _collect_unbound_from_expr(expr.callee, local_names, bound, unbound)
        for arg in expr.args:
            _collect_unbound_from_expr(arg, local_names, bound, unbound)
        for field in expr.keywords:
            _collect_unbound_from_expr(field.key, local_names, bound, unbound)
            _collect_unbound_from_expr(field.value, local_names, bound, unbound)
        return
    if isinstance(expr, CollectionProjection):
        projection_bound = set(bound)
        _collect_unbound_from_expr(expr.iterable, local_names, bound, unbound)
        projection_bound.add(expr.target.name)
        _collect_unbound_from_expr(expr.value, local_names, projection_bound, unbound)
        return
    if isinstance(expr, (GetAttr,)):
        _collect_unbound_from_expr(expr.obj, local_names, bound, unbound)
        return
    if isinstance(expr, GetItem):
        _collect_unbound_from_expr(expr.obj, local_names, bound, unbound)
        _collect_unbound_from_expr(expr.key, local_names, bound, unbound)
        return
    if isinstance(expr, NewObject):
        for arg in expr.args:
            _collect_unbound_from_expr(arg, local_names, bound, unbound)
        return
    if isinstance(expr, (ArrayLiteral, TableLiteral, SetLiteral, ObjectLiteral, MapLiteral)):
        for item in getattr(expr, "array_items", ()) or ():
            _collect_unbound_from_expr(item, local_names, bound, unbound)
        for item in getattr(expr, "items", ()) or ():
            _collect_unbound_from_expr(item, local_names, bound, unbound)
        for field in getattr(expr, "fields", ()) or ():
            _collect_unbound_from_expr(field.key, local_names, bound, unbound)
            _collect_unbound_from_expr(field.value, local_names, bound, unbound)
        return
    if isinstance(expr, Phi):
        for _pred, value in expr.incoming:
            _collect_unbound_from_expr(value, local_names, bound, unbound)


def block_vm_function(
    spec: VMFunctionSpec,
    *,
    blocks: tuple[VMBlockSpec, ...],
    status: str = "ok",
    unsupported_reason: str | None = None,
    unsupported_opcodes: tuple[str, ...] = (),
    unsupported_raw: tuple[str, ...] = (),
    structured_lift: str | None = None,
) -> FunctionIR:
    return assemble_function(
        name=spec.name,
        params=spec.params,
        frontend=spec.frontend,
        blocks=tuple(
            FunctionBlockSpec(id=block.id, statements=block.statements, terminator=block.terminator)
            for block in blocks
        ),
        metadata={
            **(spec.metadata or {}),
            "decompile_status": status,
            "unsupported_reason": unsupported_reason,
            "unsupported_opcodes": unsupported_opcodes,
            "unsupported_raw": unsupported_raw,
            "instruction_count": spec.instruction_count,
            "local_names": spec.local_names,
            "stack_lifter": "generic",
            **({"structured_lift": structured_lift} if structured_lift else {}),
        },
        recovery_kind=structured_lift,
    )


def partial_vm_function(
    spec: VMFunctionSpec,
    *,
    statements: tuple[Stmt, ...],
    source: SourceRef,
    unsupported: Unsupported | None = None,
    unsupported_reason: str,
    unsupported_opcodes: tuple[str, ...] = (),
    unsupported_raw: tuple[str, ...] = (),
    structured_lift: str | None = None,
) -> FunctionIR:
    body = (*statements, unsupported) if unsupported is not None else statements
    return entry_vm_function(
        spec,
        statements=body,
        terminator=vm_return(source=source),
        status="partial",
        unsupported_reason=unsupported_reason,
        unsupported_opcodes=unsupported_opcodes,
        unsupported_raw=unsupported_raw,
        structured_lift=structured_lift,
    )


def unsupported_vm_function(
    spec: VMFunctionSpec,
    opcodes: tuple[str, ...],
    *,
    reason: str = "contains VM opcodes that need CFG/stack structuring",
    raw: tuple[str, ...] = (),
    structured_lift: str | None = None,
) -> FunctionIR:
    return assemble_function_without_blocks(
        name=spec.name,
        params=spec.params,
        frontend=spec.frontend,
        metadata={
            **(spec.metadata or {}),
            "decompile_status": "unsupported",
            "unsupported_reason": reason,
            "unsupported_opcodes": opcodes,
            "unsupported_raw": raw,
            "instruction_count": spec.instruction_count,
            **({"structured_lift": structured_lift} if structured_lift else {}),
        },
        recovery_kind=structured_lift,
    )


def recover_vm_function(
    spec: VMFunctionSpec,
    lift: Callable[[], FunctionIR],
    *,
    raw: tuple[str, ...] = (),
) -> FunctionIR:
    """Contain one VM-function recovery failure as analyzable generic IR.

    A malformed method or an unanticipated core recovery defect must not
    suppress sibling functions in the same bytecode artifact.  Frontends only
    provide provenance text; the core owns the conservative unsupported result.
    """
    try:
        return lift()
    except Exception as error:
        return unsupported_vm_function(
            spec,
            (),
            reason=f"internal VM recovery error: {type(error).__name__}: {error}",
            raw=raw,
            structured_lift="generic-vm-pipeline",
        )


def empty_vm_function(
    spec: VMFunctionSpec,
    *,
    status: str = "ok",
    structured_lift: str | None = None,
) -> FunctionIR:
    return assemble_function_without_blocks(
        name=spec.name,
        params=spec.params,
        frontend=spec.frontend,
        metadata={
            **(spec.metadata or {}),
            "decompile_status": status,
            "instruction_count": spec.instruction_count,
            "local_names": spec.local_names,
            "stack_lifter": "generic",
            **({"structured_lift": structured_lift} if structured_lift else {}),
        },
        recovery_kind=structured_lift,
    )


def lift_steps(
    steps: tuple[VMBytecodeStep, ...],
    *,
    initial_locals: dict[str, Expr] | None = None,
    initial_stack: tuple[Expr, ...] = (),
) -> StackLiftResult[VMBytecodeStep]:
    return run_vm_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)


def _lift_stateful_candidate(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    profile: VMRegionProfile[VMBytecodeStep],
    stateful_callbacks: VMStatefulCallbacks[VMBytecodeStep],
    raw_window: Callable[[int], tuple[str, ...]] | None,
) -> FunctionIR | None:
    result = lift_stateful_control_prefix(steps, profile, stateful_callbacks)
    if result is None:
        return None
    unsupported_opcodes: tuple[str, ...] = ()
    unsupported_raw: tuple[str, ...] = ()
    if result.unsupported_instruction is not None:
        unsupported_step = steps[result.unsupported_instruction]
        unsupported_opcodes = (unsupported_step.opcode,)
        unsupported_raw = raw_window(result.unsupported_instruction) if raw_window is not None else (unsupported_step.raw,)
    function = entry_vm_function(
        spec,
        statements=result.statements,
        terminator=result.terminator,
        status=result.status,
        unsupported_reason="contains VM control-flow beyond a recovered prefix" if result.status == "partial" else None,
        unsupported_opcodes=unsupported_opcodes,
        unsupported_raw=unsupported_raw,
        structured_lift="generic-vm-pipeline",
    )
    return finalize_recovered_vm_function(spec, function)


def _lift_low_level_cfg_candidate(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    profile: VMRegionProfile[VMBytecodeStep],
    stateful_callbacks: VMStatefulCallbacks[VMBytecodeStep],
) -> FunctionIR | None:
    result = lift_stateful_low_level_cfg(steps, profile, stateful_callbacks)
    if result is None or not result.blocks:
        return None
    function = _assemble_low_level_cfg(spec, result)
    if not _function_has_exit_terminator(function):
        return unsupported_vm_function(
            spec,
            (),
            reason="low-level CFG recovery did not reach a function exit",
            structured_lift="generic-vm-low-level-cfg",
        )
    finalized = finalize_recovered_vm_function(spec, function)
    return finalized


def _assemble_low_level_cfg(spec: VMFunctionSpec, result) -> FunctionIR:
    return assemble_function(
        name=spec.name,
        params=spec.params,
        frontend=spec.frontend,
        blocks=tuple(
            FunctionBlockSpec(id=block_id, statements=statements, terminator=terminator)
            for block_id, statements, terminator in result.blocks
        ),
        metadata={
            **(spec.metadata or {}),
            "decompile_status": "partial",
            "structured_lift": "generic-vm-low-level-cfg",
            "local_names": spec.local_names,
            "instruction_count": spec.instruction_count,
            "unsupported_reason": None,
            "unsupported_opcodes": (),
            "unsupported_raw": (),
            "diagnostics": result.diagnostics,
        },
        recovery_kind="generic-vm-low-level-cfg",
    )


def _has_exception_region_facts(steps: tuple[VMBytecodeStep, ...]) -> bool:
    return any(
        hint.kind == "exception-region" and isinstance(hint.value, dict)
        for step in steps
        for hint in step.hints
    )


def _lift_exception_region_candidate(
    spec: VMFunctionSpec,
    steps: tuple[VMBytecodeStep, ...],
    profile: VMRegionProfile[VMBytecodeStep],
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
    region_callbacks: VMRegionCallbacks[VMBytecodeStep] | None,
) -> FunctionIR | None:
    """Recover simple typed handlers from neutral protected-region facts.

    The frontend supplies offsets and stack effects only. This core pass proves
    that a handler tests one exception type then terminates (return or raise)
    before replacing the protected CFG block with a structured generic region.
    """

    cfg = lift_stateful_low_level_cfg(steps, profile, callbacks)
    if cfg is None or not cfg.blocks:
        return None
    low_level = _assemble_low_level_cfg(spec, cfg)
    regions = _top_level_exception_regions(steps)
    if not regions:
        return None
    blocks_by_id = {block.id: block for block in low_level.blocks}
    replacements: dict[str, BasicBlock] = {}
    for region in regions:
        start = region["start"]
        end = region["end"]
        target = region["target"]
        block_id = f"block_{start}"
        protected_block = blocks_by_id.get(block_id)
        if protected_block is None:
            # A cleanup-only protected range can be reachable only from the
            # handler path. It is not part of the normal CFG and must not
            # prevent recovery of an independently proven outer range.
            continue
        handlers = _lift_typed_exception_handlers(
            steps,
            target,
            protected_block,
            profile,
            callbacks,
            direct_type=region.get("exception_type"),
            protected_depth=region.get("depth"),
            regions=regions,
            region_callbacks=region_callbacks,
        )
        if handlers is None:
            return None
        body: tuple[Stmt | object, ...] = (*protected_block.statements,)
        if protected_block.terminator is not None:
            body = (*body, protected_block.terminator)
        replacements[block_id] = BasicBlock(
            id=protected_block.id,
            statements=(Try(source=steps[_step_index_at_offset(steps, start, profile) or 0].source, body=body, handlers=handlers),),
            terminator=None,
        )
    if not replacements:
        return None
    function = replace(low_level, blocks=tuple(replacements.get(block.id, block) for block in low_level.blocks))
    return finalize_recovered_vm_function(spec, function)


def _top_level_exception_regions(steps: tuple[VMBytecodeStep, ...]) -> tuple[dict[str, object], ...]:
    decoded: list[dict[str, object]] = []
    for step in steps:
        for hint in step.hints:
            if hint.kind != "exception-region" or not isinstance(hint.value, dict):
                continue
            value = hint.value
            start, end, target = (value.get(key) for key in ("start", "end", "target"))
            if not all(isinstance(item, int) for item in (start, end, target)):
                continue
            decoded.append({
                "start": start,
                "end": end,
                "target": target,
                "depth": value.get("depth"),
                "lasti": value.get("lasti"),
                "exception_type": value.get("exception_type"),
            })
    handler_offsets = {entry["target"] for entry in decoded}
    regions = {
        (entry["start"], entry["end"], entry["target"]): entry
        for entry in decoded
        if entry["start"] not in handler_offsets
    }
    return tuple(regions[key] for key in sorted(regions))


def _lift_typed_exception_handlers(
    steps: tuple[VMBytecodeStep, ...],
    target_offset: int,
    protected_block: BasicBlock,
    profile: VMRegionProfile[VMBytecodeStep],
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
    direct_type: object | None = None,
    protected_depth: object | None = None,
    regions: tuple[dict[str, object], ...] = (),
    region_callbacks: VMRegionCallbacks[VMBytecodeStep] | None = None,
) -> tuple[ExceptHandler, ...] | None:
    entry = _step_index_at_offset(steps, target_offset, profile)
    if entry is None:
        return None
    if isinstance(direct_type, str) and direct_type:
        return _lift_direct_exception_handler(steps, entry, Global(name=direct_type, source=steps[entry].source), callbacks)
    handlers: list[ExceptHandler] = []
    while entry is not None:
        recovered = _lift_one_typed_exception_handler(
            steps,
            entry,
            profile,
            callbacks,
            protected_depth=protected_depth,
            regions=regions,
            region_callbacks=region_callbacks,
        )
        if recovered is None:
            return None
        handler, mismatch = recovered
        handlers.append(handler)
        next_match = _next_reachable_exception_match(steps, mismatch, profile)
        if next_match is None:
            break
        entry = mismatch
    return tuple(handlers)


def _next_reachable_exception_match(
    steps: tuple[VMBytecodeStep, ...],
    mismatch: int,
    profile: VMRegionProfile[VMBytecodeStep],
) -> int | None:
    """Find another typed handler only across proven fallthrough instructions.

    A false match can target cleanup code that reraises the active exception.
    Bytecode for a later, unrelated protected region may be physically nearby,
    but it is not a second handler.  Stop at every terminal effect or jump so
    handler chaining follows CFG reachability rather than instruction layout.
    """

    for index in range(mismatch, min(len(steps), mismatch + 8)):
        effects = tuple(steps[index].effects or ())
        if any(isinstance(effect, ExceptionMatch) for effect in effects):
            return index
        if any(isinstance(effect, (RaiseTop, RaiseWithCause, ReraiseTop, ReturnTop)) for effect in effects):
            return None
        if profile.is_jump(steps[index]) or profile.is_conditional_jump(steps[index]):
            return None
    return None


def _lift_direct_exception_handler(
    steps: tuple[VMBytecodeStep, ...],
    entry: int,
    exception_type: Expr,
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
) -> tuple[ExceptHandler, ...] | None:
    initial_locals = dict(callbacks.initial_locals())
    binding: Var | None = None
    body_start = entry
    if entry < len(steps):
        effects = tuple(steps[entry].effects or ())
        if len(effects) == 1 and isinstance(effects[0], StoreLocal):
            binding = effects[0].target if isinstance(effects[0].target, Var) else Var(name=effects[0].name, source=effects[0].source)
            initial_locals[effects[0].name] = binding
            body_start += 1
    initial_stack = () if binding is not None else (Global(name="current_exception", source=steps[entry].source),)
    lifted = callbacks.lift_linear(body_start, len(steps), initial_locals, initial_stack)
    if lifted is None or lifted.stack:
        return None
    statements = _handler_statements_through_terminal(tuple(lifted.statements))
    body: tuple[Stmt | object, ...] = statements
    if lifted.terminator is not None:
        body = (*body, lifted.terminator)
    if not body or (lifted.terminator is None and not _ends_with_raise(statements)):
        return None
    return (ExceptHandler(exception_type=exception_type, binding=binding, body=body),)


def _lift_one_typed_exception_handler(
    steps: tuple[VMBytecodeStep, ...],
    entry: int,
    profile: VMRegionProfile[VMBytecodeStep],
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
    *,
    protected_depth: object | None = None,
    regions: tuple[dict[str, object], ...] = (),
    region_callbacks: VMRegionCallbacks[VMBytecodeStep] | None = None,
) -> tuple[ExceptHandler, int] | None:
    match_index = next(
        (
            index
            for index in range(entry, min(len(steps), entry + 12))
            if any(isinstance(effect, ExceptionMatch) for effect in tuple(steps[index].effects or ()))
        ),
        None,
    )
    if match_index is None:
        return None
    expected = _handler_exception_type(steps[entry : match_index + 1])
    branch_index = match_index + 1
    if expected is None or branch_index >= len(steps) or not profile.is_conditional_jump(steps[branch_index]):
        return None
    mismatch_offset = profile.target_offset(steps[branch_index])
    mismatch = _step_index_at_offset(steps, mismatch_offset, profile) if mismatch_offset is not None else None
    if mismatch is None or mismatch <= branch_index:
        return None
    body_start = branch_index + 1
    initial_locals = dict(callbacks.initial_locals())
    initial_stack: tuple[Expr, ...] = (Global(name="current_exception", source=steps[entry].source),)
    binding: Var | None = None
    if body_start < mismatch:
        effects = tuple(steps[body_start].effects or ())
        if len(effects) == 1 and isinstance(effects[0], StoreLocal):
            binding = effects[0].target if isinstance(effects[0].target, Var) else Var(name=effects[0].name, source=effects[0].source)
            initial_locals[effects[0].name] = binding
            initial_stack = ()
            body_start += 1
    jump_index = next(
        (index for index in range(body_start, mismatch) if profile.is_jump(steps[index])),
        None,
    )
    body_end = jump_index if jump_index is not None else mismatch
    nested_body = _lift_nested_protected_handler_body(
        steps,
        body_start,
        body_end,
        initial_locals,
        initial_stack,
        profile,
        callbacks,
        protected_depth,
        regions,
        region_callbacks,
    )
    if nested_body is not None:
        return ExceptHandler(exception_type=expected, binding=binding, body=nested_body), mismatch
    short_circuit = _lift_exception_handler_short_circuit_value(
        steps,
        body_start,
        body_end,
        initial_locals,
        initial_stack,
        profile,
        callbacks,
    )
    lifted = short_circuit or callbacks.lift_linear(body_start, body_end, initial_locals, initial_stack)
    if lifted is None or lifted.stack:
        return None
    statements = _handler_statements_through_terminal(tuple(lifted.statements))
    body: tuple[Stmt | object, ...] = statements
    if lifted.terminator is not None:
        body = (*body, lifted.terminator)
    if jump_index is not None:
        jump_target = profile.target_offset(steps[jump_index])
        if jump_target is None:
            return None
        body = (*body, Jump(source=steps[jump_index].source, target=f"block_{jump_target}"))
    if not body or (lifted.terminator is None and jump_index is None and not _ends_with_raise(statements)):
        return None
    return ExceptHandler(exception_type=expected, binding=binding, body=body), mismatch


def _lift_nested_protected_handler_body(
    steps: tuple[VMBytecodeStep, ...],
    start: int,
    end: int,
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
    profile: VMRegionProfile[VMBytecodeStep],
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
    protected_depth: object | None,
    regions: tuple[dict[str, object], ...],
    region_callbacks: VMRegionCallbacks[VMBytecodeStep] | None,
) -> tuple[Stmt | object, ...] | None:
    """Compose one deeper exception-table region wholly inside a handler."""

    if region_callbacks is None or not isinstance(protected_depth, int):
        return None
    candidates: list[tuple[int, int, int, int]] = []
    for region in regions:
        offsets = (region.get("start"), region.get("end"), region.get("target"), region.get("depth"))
        if not all(isinstance(offset, int) for offset in offsets):
            continue
        region_start = _step_index_at_offset(steps, offsets[0], profile)
        region_end = _step_index_at_offset(steps, offsets[1], profile)
        target = _step_index_at_offset(steps, offsets[2], profile)
        if region_start is None or region_end is None or target is None:
            continue
        if (
            region.get("lasti") is False
            and start <= region_start < region_end <= end
            and region_end <= target < end
            and offsets[3] > protected_depth
        ):
            candidates.append((region_start, region_end, target, offsets[3]))
    if len(candidates) != 1:
        return None
    nested_start, nested_end, nested_target, nested_depth = candidates[0]
    prefix = lift_control_region(steps, start, nested_start, profile, region_callbacks, initial_stack)
    if any(isinstance(statement, (Return, Raise, Reraise)) for statement in prefix):
        return None
    # The protected slice can leave its result on the VM stack.  Include the
    # straight-line cleanup up to the handler entry so the normal path reaches
    # its terminal with the same stack state as the bytecode.
    normal = callbacks.lift_linear(nested_start, nested_target, initial_locals, ())
    nested = _lift_one_typed_exception_handler(
        steps,
        nested_target,
        profile,
        callbacks,
        protected_depth=nested_depth,
    )
    if normal is None or normal.stack or nested is None:
        return None
    nested_handler, _mismatch = nested
    normal_body: tuple[Stmt | object, ...] = tuple(normal.statements)
    if normal.terminator is not None:
        normal_body = (*normal_body, normal.terminator)
    if not _body_is_terminal(normal_body) or not _body_is_terminal(nested_handler.body):
        return None
    return (*prefix, Try(source=steps[nested_start].source, body=normal_body, handlers=(nested_handler,)))


def _body_is_terminal(body: tuple[Stmt | object, ...]) -> bool:
    return bool(body) and isinstance(body[-1], (Return, Raise, Reraise))


def _lift_exception_handler_short_circuit_value(
    steps: tuple[VMBytecodeStep, ...],
    start: int,
    end: int,
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
    profile: VMRegionProfile[VMBytecodeStep],
    callbacks: VMStatefulCallbacks[VMBytecodeStep],
) -> VMLinearState | None:
    """Lift a generic falsey-value fallback within an exception handler.

    The shape preserves a stack value on the true edge, discards it on the
    false edge, stores a fallback, then joins at the same local store. It is
    represented by the neutral branch-value hint, rather than an opcode name.
    """

    copy_index = next(
        (
            index
            for index in range(start, end)
            if any(hint.kind == "branch-value" for hint in steps[index].hints)
        ),
        None,
    )
    if copy_index is None or copy_index + 3 >= end:
        return None
    branch_index = copy_index + 2
    if not profile.is_conditional_jump(steps[branch_index]):
        return None
    store_index = _step_index_at_offset(steps, profile.target_offset(steps[branch_index]), profile)
    if store_index is None or store_index <= branch_index or store_index >= end:
        return None
    store_effects = tuple(steps[store_index].effects or ())
    fallback_effects = tuple(steps[store_index - 1].effects or ())
    if len(store_effects) != 1 or not isinstance(store_effects[0], StoreLocal):
        return None
    if len(fallback_effects) != 1 or not isinstance(fallback_effects[0], Push):
        return None
    prefix = callbacks.lift_linear(start, copy_index, initial_locals, initial_stack)
    if prefix is None or len(prefix.stack) != 1 or prefix.terminator is not None:
        return None
    target = store_effects[0].target or Var(name=store_effects[0].name, source=store_effects[0].source)
    if not isinstance(target, Var):
        return None
    locals_after = dict(prefix.locals)
    locals_after[store_effects[0].name] = target
    suffix = callbacks.lift_linear(store_index + 1, end, locals_after, ())
    if suffix is None or suffix.stack or suffix.terminator is not None:
        return None
    source = steps[copy_index].source
    return VMLinearState(
        locals=suffix.locals,
        stack=(),
        statements=(
            *prefix.statements,
            Assign(
                source=source,
                target=target,
                value=BinaryOp(
                    source=source,
                    op="or",
                    left=prefix.stack[-1],
                    right=fallback_effects[0].value,
                    semantics="dynamic",
                ),
            ),
            *suffix.statements,
        ),
    )


def _handler_statements_through_terminal(statements: tuple[Stmt, ...]) -> tuple[Stmt, ...]:
    for index, statement in enumerate(statements):
        if isinstance(statement, (Raise, Reraise)):
            return statements[: index + 1]
    return statements


def _handler_exception_type(steps: tuple[VMBytecodeStep, ...]) -> Expr | None:
    pending_tuple_size: int | None = None
    tuple_items: list[Expr] = []
    for step in reversed(steps):
        for effect in reversed(tuple(step.effects or ())):
            if isinstance(effect, BuildArray) and effect.kind == "tuple" and effect.count > 0:
                pending_tuple_size = effect.count
                continue
            if isinstance(effect, Push):
                if pending_tuple_size is not None:
                    tuple_items.append(effect.value)
                    if len(tuple_items) == pending_tuple_size:
                        return ArrayLiteral(source=effect.source, items=tuple(reversed(tuple_items)))
                    continue
                return effect.value
    return None


def _step_index_at_offset(
    steps: tuple[VMBytecodeStep, ...],
    offset: int | None,
    profile: VMRegionProfile[VMBytecodeStep],
) -> int | None:
    if offset is None:
        return None
    return next((index for index, step in enumerate(steps) if profile.offset(step) == offset), None)


def _function_has_unsupported(function: FunctionIR) -> bool:
    return (
        any(contains_vm_unsupported(statement) for block in function.blocks for statement in block.statements)
        or function.metadata.get("decompile_status") == "unsupported"
    )


def _function_has_unbound_loop_control(function: FunctionIR) -> bool:
    return any(_has_unbound_loop_control(tuple(block.statements)) for block in function.blocks)


def _has_backward_jump(
    steps: tuple[VMBytecodeStep, ...],
    profile: VMRegionProfile[VMBytecodeStep],
) -> bool:
    return any(profile.is_backward_jump(step) for step in steps)


def _raise_effect_count(steps: tuple[VMBytecodeStep, ...]) -> int:
    return sum(1 for step in steps for effect in step.effects or () if isinstance(effect, RaiseTop))


def _function_raise_statement_count(function: FunctionIR) -> int:
    return sum(_statements_raise_count(tuple(block.statements)) for block in function.blocks)


def _statements_raise_count(statements: tuple[object, ...]) -> int:
    count = 0
    for statement in statements:
        if isinstance(statement, Raise):
            count += 1
        then_body = tuple(getattr(statement, "then_body", ()) or ())
        else_body = tuple(getattr(statement, "else_body", ()) or ())
        body = tuple(getattr(statement, "body", ()) or ())
        if then_body:
            count += _statements_raise_count(then_body)
        if else_body:
            count += _statements_raise_count(else_body)
        if body:
            count += _statements_raise_count(body)
    return count


def _function_has_recovered_loop_construct(function: FunctionIR) -> bool:
    return any(_statements_have_loop_construct(tuple(block.statements)) for block in function.blocks)


def _function_has_collection_projection(function: FunctionIR) -> bool:
    return any(_statements_have_collection_projection(tuple(block.statements)) for block in function.blocks)


def _statements_have_collection_projection(statements: tuple[object, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, Assign) and _expr_has_collection_projection(statement.value):
            return True
        if isinstance(statement, Return) and any(_expr_has_collection_projection(value) for value in statement.values):
            return True
        then_body = tuple(getattr(statement, "then_body", ()) or ())
        else_body = tuple(getattr(statement, "else_body", ()) or ())
        body = tuple(getattr(statement, "body", ()) or ())
        if then_body and _statements_have_collection_projection(then_body):
            return True
        if else_body and _statements_have_collection_projection(else_body):
            return True
        if body and _statements_have_collection_projection(body):
            return True
    return False


def _expr_has_collection_projection(expr: Expr) -> bool:
    if isinstance(expr, CollectionProjection):
        return True
    if isinstance(expr, UnaryOp):
        return _expr_has_collection_projection(expr.value)
    if isinstance(expr, BinaryOp):
        return _expr_has_collection_projection(expr.left) or _expr_has_collection_projection(expr.right)
    if isinstance(expr, Call):
        return _expr_has_collection_projection(expr.callee) or any(_expr_has_collection_projection(arg) for arg in expr.args)
    if isinstance(expr, GetAttr):
        return _expr_has_collection_projection(expr.obj)
    if isinstance(expr, GetItem):
        return _expr_has_collection_projection(expr.obj) or _expr_has_collection_projection(expr.key)
    if isinstance(expr, Phi):
        return any(_expr_has_collection_projection(value) for _pred, value in expr.incoming)
    if isinstance(expr, (ArrayLiteral, TableLiteral, SetLiteral, ObjectLiteral, MapLiteral)):
        for item in getattr(expr, "array_items", ()) or ():
            if _expr_has_collection_projection(item):
                return True
        for item in getattr(expr, "items", ()) or ():
            if _expr_has_collection_projection(item):
                return True
        for field in getattr(expr, "fields", ()) or ():
            if _expr_has_collection_projection(field.key) or _expr_has_collection_projection(field.value):
                return True
    return False


def _statements_have_loop_construct(statements: tuple[object, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, (While, ForEach, ForRange)):
            return True
        then_body = tuple(getattr(statement, "then_body", ()) or ())
        else_body = tuple(getattr(statement, "else_body", ()) or ())
        body = tuple(getattr(statement, "body", ()) or ())
        if then_body and _statements_have_loop_construct(then_body):
            return True
        if else_body and _statements_have_loop_construct(else_body):
            return True
        if body and _statements_have_loop_construct(body):
            return True
    return False


def _function_has_exit_terminator(function: FunctionIR) -> bool:
    return any(isinstance(block.terminator, (Return, Raise)) for block in function.blocks)


def _has_unbound_loop_control(statements: tuple[object, ...], *, in_loop: bool = False) -> bool:
    for statement in statements:
        if isinstance(statement, (Break, Continue)):
            if not in_loop:
                return True
            continue
        if isinstance(statement, Try):
            if _has_unbound_loop_control(tuple(statement.body), in_loop=in_loop):
                return True
            if any(
                _has_unbound_loop_control(tuple(handler.body), in_loop=in_loop)
                for handler in statement.handlers
            ):
                return True
            continue
        if isinstance(statement, (While, ForEach, ForRange)):
            if _has_unbound_loop_control(tuple(statement.body), in_loop=True):
                return True
            continue
        then_body = tuple(getattr(statement, "then_body", ()) or ())
        else_body = tuple(getattr(statement, "else_body", ()) or ())
        if then_body and _has_unbound_loop_control(then_body, in_loop=in_loop):
            return True
        if else_body and _has_unbound_loop_control(else_body, in_loop=in_loop):
            return True
    return False


def _unsupported_opcodes_from_statements(statements: tuple[object, ...]) -> tuple[str, ...]:
    opcodes: set[str] = set()
    for statement in statements:
        if isinstance(statement, Unsupported):
            _collect_raw_opcodes(statement.raw, opcodes)
        for body_name in ("then_body", "else_body", "body"):
            body = getattr(statement, body_name, ())
            if body:
                opcodes.update(_unsupported_opcodes_from_statements(tuple(body)))
    return tuple(sorted(opcodes))


def _ends_with_raise(statements: tuple[Stmt, ...]) -> bool:
    return bool(statements) and isinstance(statements[-1], (Raise, Reraise))


def _collect_raw_opcodes(raw_lines: tuple[str, ...], opcodes: set[str]) -> None:
    for raw in raw_lines:
        parts = raw.split()
        if len(parts) >= 2:
            opcodes.add(parts[1])
