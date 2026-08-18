from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar

from unidecompiler.core.ir import Assign, Const, Expr, IndirectRef, Return, Stmt, Terminator, Var


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
        self.statements.append(Assign(source=target.source, target=target, value=value))
        return True

    def append_statement(self, statement: Stmt) -> None:
        self.materialize_pending_stack(statement.source)
        self.statements.append(statement)

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

    def materialize_pending_stack(self, source=None) -> None:
        if not self.stack:
            return
        materialized: list[Expr] = []
        for index, value in enumerate(self.stack):
            if isinstance(value, (Const, IndirectRef, Var)):
                materialized.append(value)
                continue
            target = Var(name=f"order_tmp_{len(self.statements)}_{index}_v", source=source)
            self.statements.append(Assign(source=source, target=target, value=value))
            materialized.append(target)
        self.stack[:] = materialized


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
