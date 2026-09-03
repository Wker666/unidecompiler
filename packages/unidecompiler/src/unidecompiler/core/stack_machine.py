from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass, replace
from typing import Callable, Generic, TypeVar

from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    CapturedVar,
    Const,
    Delete,
    Expr,
    Global,
    IndirectRef,
    Placeholder,
    Return,
    Stmt,
    Terminator,
    Var,
)


InstructionT = TypeVar("InstructionT")


@dataclass
class StackMachineState:
    """VM-neutral operand stack and local-value state.

    Frontends translate their own instructions into calls on this object. The
    state object deliberately has no opcode names, bytecode-family switches, or
    language-specific semantics; it only knows stack/value effects.
    """

    locals: dict[str, Expr] = field(default_factory=dict)
    stack: list[Expr] = field(default_factory=list)
    statements: list[Stmt] = field(default_factory=list)
    terminator: Terminator | None = None
    diagnostics: list[str] = field(default_factory=list)

    def push(self, value: Expr) -> None:
        self.stack.append(value)

    def pop(self, diagnostic: str = "stack-underflow") -> Expr | None:
        if not self.stack:
            self.diagnostics.append(diagnostic)
            return None
        return self.stack.pop()

    def pop_many(self, count: int, diagnostic: str = "stack-underflow") -> tuple[Expr, ...] | None:
        if count < 0:
            self.diagnostics.append(f"invalid-pop-count:{count}")
            return None
        if len(self.stack) < count:
            self.diagnostics.append(diagnostic)
            return None
        if count == 0:
            return ()
        values = tuple(self.stack[-count:])
        del self.stack[-count:]
        return values

    def load_local(self, name: str, fallback: Expr) -> None:
        self.stack.append(self.locals.get(name, fallback))

    def store_local(self, name: str) -> bool:
        value = self.pop()
        if value is None:
            return False
        self.locals[name] = value
        return True

    def assign_local(self, name: str, target: Var) -> bool:
        value = self.pop()
        if value is None:
            return False
        self.locals[name] = value
        self.append_statement(Assign(source=target.source, target=target, value=value))
        return True

    def append_statement(self, statement: Stmt) -> None:
        written_locals, written_globals, written_captures = _directly_written_storage(
            statement
        )
        self.materialize_pending_stack(
            statement.source,
            written_names=written_locals,
            written_globals=written_globals,
            written_captures=written_captures,
        )
        self.statements.append(statement)

    def materialize_value(
        self,
        value: Expr,
        source=None,
        *,
        label: str = "value",
        preferred_name: str | None = None,
        force_variable: bool = False,
    ) -> Expr:
        """Evaluate a deferred VM value once and return its stable identity.

        Generic expressions describe computations, while an operand-stack slot
        denotes the result of a computation which has already happened.  This
        helper is the boundary between those two meanings.  References remain
        references; callers which create an address must freeze its address
        components instead of turning the reference itself into an ordinary
        value temporary.
        """

        if isinstance(value, (Const, IndirectRef, Placeholder)) or (
            isinstance(value, (Var, Global, CapturedVar)) and not force_variable
        ):
            return value
        target = Var(
            name=preferred_name or f"order_tmp_{len(self.statements)}_{label}_v",
            source=source,
        )
        self.statements.append(Assign(source=source, target=target, value=value))
        return target

    def materialize_stack_slot(
        self,
        index: int,
        source=None,
        *,
        label: str = "shared",
        preferred_name: str | None = None,
        force_variable: bool = False,
    ) -> Expr:
        """Give every alias of one deferred stack value a single temporary."""

        value = self.stack[index]
        stable = self.materialize_value(
            value,
            source,
            label=label,
            preferred_name=preferred_name,
            force_variable=force_variable,
        )
        if stable is value:
            return value
        self.stack[:] = [stable if candidate is value else candidate for candidate in self.stack]
        return stable

    def return_values(self, values: tuple[Expr, ...], source=None) -> None:
        self.terminator = Return(source=source, values=values)

    def return_top(self, source=None) -> bool:
        value = self.pop()
        if value is None:
            return False
        self.terminator = Return(source=source, values=(value,))
        return True

    def return_void(self, source=None) -> None:
        self.terminator = Return(source=source)

    def materialize_pending_stack(
        self,
        source=None,
        *,
        written_names: frozenset[str] = frozenset(),
        written_globals: frozenset[str] = frozenset(),
        written_captures: frozenset[str] = frozenset(),
    ) -> None:
        if not self.stack:
            return
        materialized: list[Expr] = []
        identity_snapshots: dict[int, Expr] = {}
        storage_snapshots: dict[tuple[type[Expr], str], Expr] = {}
        for index, value in enumerate(self.stack):
            if isinstance(value, Const):
                materialized.append(value)
                continue
            if isinstance(value, IndirectRef):
                materialized.append(
                    self._freeze_reference_dependencies(
                        value,
                        written_names,
                        written_globals,
                        written_captures,
                        source,
                    )
                )
                continue
            is_overwritten_storage = (
                isinstance(value, Var) and value.name in written_names
                or isinstance(value, Global) and value.name in written_globals
                or isinstance(value, CapturedVar) and value.name in written_captures
            )
            if isinstance(value, (Var, Global, CapturedVar)) and not is_overwritten_storage:
                materialized.append(value)
                continue
            storage_key = (
                (type(value), value.name)
                if isinstance(value, (Var, Global, CapturedVar))
                else None
            )
            if storage_key is not None and storage_key in storage_snapshots:
                materialized.append(storage_snapshots[storage_key])
                continue
            target = identity_snapshots.get(id(value))
            if target is None:
                target = self.materialize_value(
                    value,
                    source,
                    label=str(index),
                    force_variable=isinstance(value, (Var, Global, CapturedVar)),
                )
                identity_snapshots[id(value)] = target
                if storage_key is not None:
                    storage_snapshots[storage_key] = target
            materialized.append(target)
        self.stack[:] = materialized

    def _freeze_reference_dependencies(
        self,
        reference: IndirectRef,
        written_names: frozenset[str],
        written_globals: frozenset[str],
        written_captures: frozenset[str],
        source=None,
    ) -> IndirectRef:
        target = reference.target
        if isinstance(target, Var):
            # A variable reference denotes the variable's location, not the
            # value currently stored there.  Writes do not invalidate it.
            return reference
        frozen = _freeze_address_expr(
            self,
            target,
            written_names,
            written_globals,
            written_captures,
            source,
        )
        if frozen is target:
            return reference
        return replace(reference, target=frozen)


def _directly_written_storage(
    statement: Stmt,
) -> tuple[frozenset[str], frozenset[str], frozenset[str]]:
    """Return local identities overwritten before pending stack values resume."""

    if isinstance(statement, (Assign, Delete)):
        target = statement.target
        if isinstance(target, IndirectRef):
            target = target.target
        if isinstance(target, Var):
            return frozenset((target.name,)), frozenset(), frozenset()
        if isinstance(target, Global):
            return frozenset(), frozenset((target.name,)), frozenset()
        if isinstance(target, CapturedVar):
            return frozenset(), frozenset(), frozenset((target.name,))
    if isinstance(statement, AssignMany):
        return (
            frozenset(target.name for target in statement.targets),
            frozenset(),
            frozenset(),
        )
    return frozenset(), frozenset(), frozenset()


def _expr_reads_storage(
    expr: Expr,
    local_names: frozenset[str],
    global_names: frozenset[str],
    capture_names: frozenset[str],
) -> bool:
    if isinstance(expr, Var):
        return expr.name in local_names
    if isinstance(expr, Global):
        return expr.name in global_names
    if isinstance(expr, CapturedVar):
        return expr.name in capture_names
    if not is_dataclass(expr):
        return False
    return any(
        _value_reads_storage(
            getattr(expr, item.name),
            local_names,
            global_names,
            capture_names,
        )
        for item in fields(expr)
        if item.name not in {"source", "type"}
    )


def _value_reads_storage(
    value: object,
    local_names: frozenset[str],
    global_names: frozenset[str],
    capture_names: frozenset[str],
) -> bool:
    if isinstance(value, Expr):
        return _expr_reads_storage(
            value,
            local_names,
            global_names,
            capture_names,
        )
    if isinstance(value, (tuple, list)):
        return any(
            _value_reads_storage(
                item,
                local_names,
                global_names,
                capture_names,
            )
            for item in value
        )
    if is_dataclass(value):
        return any(
            _value_reads_storage(
                getattr(value, item.name),
                local_names,
                global_names,
                capture_names,
            )
            for item in fields(value)
        )
    return False


def _freeze_address_expr(
    state: StackMachineState,
    target: Expr,
    written_names: frozenset[str],
    written_globals: frozenset[str],
    written_captures: frozenset[str],
    source=None,
) -> Expr:
    """Freeze only address components invalidated by an upcoming local write."""

    from unidecompiler.core.ir import GetAttr, GetItem

    if isinstance(target, GetItem):
        obj = target.obj
        key = target.key
        if _expr_reads_storage(
            obj, written_names, written_globals, written_captures
        ):
            obj = state.materialize_value(
                obj, source, label="address_obj", force_variable=True
            )
        if _expr_reads_storage(
            key, written_names, written_globals, written_captures
        ):
            key = state.materialize_value(
                key, source, label="address_key", force_variable=True
            )
        return replace(target, obj=obj, key=key)
    if isinstance(target, GetAttr):
        obj = target.obj
        if _expr_reads_storage(
            obj, written_names, written_globals, written_captures
        ):
            obj = state.materialize_value(
                obj, source, label="address_obj", force_variable=True
            )
        return replace(target, obj=obj)
    return target


@dataclass(frozen=True)
class StackLiftResult(Generic[InstructionT]):
    state: StackMachineState
    stopped_at: InstructionT | None = None

    @property
    def ok(self) -> bool:
        return not self.state.diagnostics and self.state.terminator is not None


InstructionHandler = Callable[[InstructionT, StackMachineState], bool]
EffectEmitter = Callable[[InstructionT], tuple[object, ...] | None]


class StackMachineLifter(Generic[InstructionT]):
    """Runs frontend-provided instruction effects over a generic stack state."""

    def __init__(self, initial_locals: dict[str, Expr] | None = None) -> None:
        self.initial_locals = dict(initial_locals or {})

    def lift(
        self,
        instructions: tuple[InstructionT, ...],
        handler: InstructionHandler[InstructionT],
    ) -> StackLiftResult[InstructionT]:
        state = StackMachineState(locals=self.initial_locals.copy())
        for instruction in instructions:
            handled = handler(instruction, state)
            if not handled:
                return StackLiftResult(state=state, stopped_at=instruction)
            if state.diagnostics or state.terminator is not None:
                return StackLiftResult(state=state, stopped_at=instruction)
        return StackLiftResult(state=state)

    def lift_effects(
        self,
        instructions: tuple[InstructionT, ...],
        emitter: EffectEmitter[InstructionT],
    ) -> StackLiftResult[InstructionT]:
        from unidecompiler.core.effects import apply_effects

        state = StackMachineState(locals=self.initial_locals.copy())
        for instruction in instructions:
            effects = emitter(instruction)
            if effects is None:
                return StackLiftResult(state=state, stopped_at=instruction)
            if not apply_effects(state, effects):
                return StackLiftResult(state=state, stopped_at=instruction)
            if state.diagnostics or state.terminator is not None:
                return StackLiftResult(state=state, stopped_at=instruction)
        return StackLiftResult(state=state)
