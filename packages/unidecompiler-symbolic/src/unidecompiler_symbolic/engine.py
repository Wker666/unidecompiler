from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Mapping

import z3

from unidecompiler.core.ir import (
    ArrayLiteral,
    Assign,
    AssignMany,
    BasicBlock,
    BinaryOp,
    Branch,
    Break,
    Call,
    CollectionProjection,
    Const,
    Continue,
    Delete,
    DoWhile,
    Expr,
    ExprStmt,
    ForEach,
    ForRange,
    FunctionIR,
    GetItem,
    If,
    Jump,
    MapLiteral,
    MultiBranch,
    ModuleIR,
    ObjectLiteral,
    Phi,
    Raise,
    Return,
    Reraise,
    SetLiteral,
    StoreItem,
    TableLiteral,
    Terminator,
    UnaryOp,
    Unsupported,
    Var,
    While,
    exceptional_transfers,
)
from unidecompiler.core.cfg import build_cfg, validate_cfg_consistency


class SymbolicStatus(StrEnum):
    COMPLETED = "completed"
    RAISED = "raised"
    UNSUPPORTED = "unsupported"
    STEP_LIMIT = "step_limit"
    PATH_LIMIT = "path_limit"
    CALL_DEPTH_LIMIT = "call_depth_limit"
    SOLVER_TIMEOUT = "solver_timeout"
    CANCELLED = "cancelled"
    INVALID_REQUEST = "invalid_request"


class SymbolicSort(StrEnum):
    BOOL = "bool"
    INT = "int"
    REAL = "real"
    BITVEC = "bitvec"


@dataclass(frozen=True)
class SymbolicInput:
    name: str
    sort: SymbolicSort = SymbolicSort.INT
    bit_width: int | None = None

    def validate(self) -> None:
        if not self.name:
            raise ValueError("symbolic input name must not be empty")
        if self.sort is SymbolicSort.BITVEC and (self.bit_width is None or self.bit_width <= 0):
            raise ValueError("bit-vector inputs require a positive bit_width")
        if self.sort is not SymbolicSort.BITVEC:
            if self.bit_width is not None:
                raise ValueError("bit_width is only valid for bit-vector inputs")


@dataclass(frozen=True)
class SymbolicLimits:
    max_paths: int = 256
    max_steps: int = 100_000
    max_loop_unroll: int = 32
    max_call_depth: int = 32
    solver_timeout_ms: int = 5_000

    def validate(self) -> None:
        if any(value <= 0 for value in (self.max_paths, self.max_steps, self.max_loop_unroll, self.max_call_depth)):
            raise ValueError("symbolic limits must be positive")
        if self.solver_timeout_ms <= 0:
            raise ValueError("solver_timeout_ms must be positive")


@dataclass(frozen=True)
class SymbolicPath:
    path_id: int
    status: SymbolicStatus
    constraints: tuple[z3.BoolRef, ...] = ()
    returns: tuple[Any, ...] = ()
    raised: Any | None = None
    model: Mapping[str, Any] | None = None
    blocks: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()
    steps: int = 0
    diagnostic: str | None = None


@dataclass(frozen=True)
class SymbolicResult:
    status: SymbolicStatus
    paths: tuple[SymbolicPath, ...] = ()
    explored_paths: int = 0
    pruned_paths: int = 0
    diagnostic: str | None = None


@dataclass
class _State:
    env: dict[str, Any]
    constraints: list[z3.BoolRef] = field(default_factory=list)
    current: str = ""
    predecessor: str | None = None
    predecessor_edge: str | None = None
    blocks: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    steps: int = 0
    loop_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    returns: tuple[Any, ...] | None = None
    raised: Any | None = None
    status: SymbolicStatus | None = None
    diagnostic: str | None = None

    def clone(self) -> _State:
        return replace(
            self,
            env=dict(self.env),
            constraints=list(self.constraints),
            blocks=list(self.blocks),
            edges=list(self.edges),
            loop_counts=dict(self.loop_counts),
        )


class SymbolicEngine:
    """Explore Generic IR paths without executing frontend bytecode."""

    def explore_function(
        self,
        module: ModuleIR,
        function: FunctionIR,
        *,
        symbolic_inputs: tuple[SymbolicInput, ...] = (),
        concrete_args: Mapping[str, Any] | None = None,
        limits: SymbolicLimits | None = None,
        cancellation: Any | None = None,
    ) -> SymbolicResult:
        limits = limits or SymbolicLimits()
        try:
            limits.validate()
            self._validate_membership(module, function)
            symbolic_inputs = tuple(symbolic_inputs)
            for item in symbolic_inputs:
                item.validate()
            names = {item.name for item in symbolic_inputs}
            if len(names) != len(symbolic_inputs) or not names.issubset(function.params):
                raise ValueError("symbolic inputs must name unique function parameters")
            supplied = dict(concrete_args or {})
            if set(supplied) & names or not set(supplied).issubset(function.params):
                raise ValueError("concrete arguments overlap or name unknown parameters")
            if set(function.params) != names | set(supplied):
                raise ValueError("every function parameter needs a symbolic or concrete argument")
            env = dict(supplied)
            for item in symbolic_inputs:
                env[item.name] = self._symbol(item)
        except (TypeError, ValueError) as exc:
            return SymbolicResult(SymbolicStatus.INVALID_REQUEST, diagnostic=str(exc))

        self._limits = limits
        self._cancellation = cancellation
        self._module = module
        try:
            cfg = build_cfg(function)
            diagnostics = validate_cfg_consistency(cfg)
        except (TypeError, ValueError) as exc:
            return SymbolicResult(SymbolicStatus.INVALID_REQUEST, diagnostic=str(exc))
        if diagnostics:
            return SymbolicResult(SymbolicStatus.INVALID_REQUEST, diagnostic="; ".join(diagnostics))
        if len(set(cfg.declared_block_ids)) != len(cfg.declared_block_ids):
            return SymbolicResult(SymbolicStatus.INVALID_REQUEST, diagnostic="duplicate block IDs in function CFG")
        blocks = dict(cfg.blocks)
        if not blocks:
            path = SymbolicPath(0, SymbolicStatus.COMPLETED, returns=())
            return SymbolicResult(SymbolicStatus.COMPLETED, (path,), 1, 0)
        initial = _State(env=env, current=function.blocks[0].id)
        pending = [initial]
        paths: list[SymbolicPath] = []
        pruned = 0
        while pending:
            if len(paths) >= limits.max_paths:
                paths.append(SymbolicPath(len(paths), SymbolicStatus.PATH_LIMIT, diagnostic="maximum path count exceeded"))
                break
            state = pending.pop()
            try:
                outcomes = self._run_state(function, blocks, state, cfg)
            except _UnsupportedValue as exc:
                state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, str(exc)
                outcomes = [state]
            for outcome in outcomes:
                if outcome.status is SymbolicStatus.UNSUPPORTED and outcome.diagnostic is None:
                    outcome.diagnostic = "unsupported symbolic state"
                if outcome.status is SymbolicStatus.UNSUPPORTED or outcome.status is SymbolicStatus.SOLVER_TIMEOUT:
                    path = self._path(len(paths), outcome)
                    paths.append(path)
                    continue
                if outcome.status in (SymbolicStatus.COMPLETED, SymbolicStatus.RAISED, SymbolicStatus.STEP_LIMIT, SymbolicStatus.CANCELLED):
                    paths.append(self._path(len(paths), outcome))
                elif outcome.current:
                    pending.append(outcome)
        overall = SymbolicStatus.COMPLETED
        if any(path.status is SymbolicStatus.PATH_LIMIT for path in paths):
            overall = SymbolicStatus.PATH_LIMIT
        elif any(path.status is SymbolicStatus.UNSUPPORTED for path in paths):
            overall = SymbolicStatus.UNSUPPORTED
        elif any(path.status is SymbolicStatus.SOLVER_TIMEOUT for path in paths):
            overall = SymbolicStatus.SOLVER_TIMEOUT
        elif paths and all(path.status is SymbolicStatus.RAISED for path in paths):
            overall = SymbolicStatus.RAISED
        elif any(path.status is SymbolicStatus.STEP_LIMIT for path in paths):
            overall = SymbolicStatus.STEP_LIMIT
        elif any(path.status is SymbolicStatus.CANCELLED for path in paths):
            overall = SymbolicStatus.CANCELLED
        return SymbolicResult(overall, tuple(paths), len(paths), pruned)

    def _validate_membership(self, module: ModuleIR, function: FunctionIR) -> None:
        def walk(items: tuple[FunctionIR, ...]):
            for item in items:
                yield item
                yield from walk(item.nested_functions)
        if not any(item is function for item in walk(module.functions)):
            raise ValueError(f"function {function.name!r} does not belong to the current ModuleIR")

    def _symbol(self, item: SymbolicInput) -> z3.ExprRef:
        if item.sort is SymbolicSort.BOOL:
            return z3.Bool(item.name)
        if item.sort is SymbolicSort.REAL:
            return z3.Real(item.name)
        if item.sort is SymbolicSort.BITVEC:
            return z3.BitVec(item.name, item.bit_width or 1)
        return z3.Int(item.name)

    def _solver_check(self, constraints: list[z3.BoolRef]) -> z3.CheckSatResult:
        solver = z3.Solver()
        solver.set(timeout=self._limits.solver_timeout_ms)
        solver.add(*constraints)
        return solver.check()

    def _run_state(self, function: FunctionIR, blocks: dict[str, BasicBlock], state: _State, cfg: Any) -> list[_State]:
        while state.current in blocks:
            if self._cancelled():
                state.status, state.diagnostic = SymbolicStatus.CANCELLED, "symbolic execution cancelled"
                return [state]
            if state.steps >= self._limits.max_steps:
                state.status, state.diagnostic = SymbolicStatus.STEP_LIMIT, "maximum symbolic steps exceeded"
                return [state]
            block = blocks[state.current]
            if exceptional_transfers(block):
                state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, "exceptional CFG transfers require explicit symbolic exception provenance"
                return [state]
            state.blocks.append(block.id)
            state.steps += 1
            phi_outcome = self._execute_phis(block, state)
            if phi_outcome is not None:
                return [phi_outcome]
            branches = self._execute_statements(block.statements, state)
            ready: list[_State] = []
            for candidate in branches:
                if candidate.status is not None:
                    ready.append(candidate)
                else:
                    ready.extend(self._execute_terminator(block.terminator, candidate, blocks, cfg))
            if len(ready) != 1 or ready[0].status is not None:
                return ready
            state = ready[0]
        state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, "missing target block"
        return [state]

    def _execute_phis(self, block: BasicBlock, state: _State) -> _State | None:
        # Phi expressions are evaluated against the exact predecessor edge.
        for statement in block.statements:
            if isinstance(statement, Assign) and isinstance(statement.value, Phi):
                value = self._phi_value(statement.value, state)
                if value is None:
                    state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, "Phi lacks matching predecessor edge"
                    return state
                state.env[statement.target.name] = value
        return None

    def _phi_value(self, phi: Phi, state: _State) -> Any | None:
        if state.predecessor_edge and phi.edge_ids:
            for edge_id, (_, value) in zip(phi.edge_ids, phi.incoming):
                if edge_id == state.predecessor_edge:
                    return self._eval(value, state)
        if state.predecessor:
            matches = [value for label, value in phi.incoming if label == state.predecessor]
            if len(matches) == 1:
                return self._eval(matches[0], state)
        return None

    def _execute_statements(self, statements: tuple[Any, ...], state: _State) -> list[_State]:
        states = [state]
        for statement in statements:
            next_states: list[_State] = []
            for item in states:
                if item.status is not None:
                    next_states.append(item)
                    continue
                if isinstance(statement, Assign):
                    if isinstance(statement.value, Phi):
                        next_states.append(item)
                        continue
                    item.env[getattr(statement.target, "name", "")] = self._eval(statement.value, item)
                elif isinstance(statement, AssignMany):
                    values = tuple(self._eval(value, item) for value in statement.values)
                    for target, value in zip(statement.targets, values):
                        item.env[target.name] = value
                elif isinstance(statement, ExprStmt):
                    self._eval(statement.value, item)
                elif isinstance(statement, Delete):
                    if isinstance(statement.target, Var):
                        item.env.pop(statement.target.name, None)
                    else:
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic delete target is unsupported"
                elif isinstance(statement, (StoreItem,)):
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic container mutation is unsupported"
                elif isinstance(statement, If):
                    next_states.extend(self._branch_statement(statement, item))
                    continue
                elif isinstance(statement, While):
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "structured loops must be represented as CFG blocks"
                elif isinstance(statement, (DoWhile, ForEach, ForRange, Break, Continue, Raise, Reraise, Unsupported, Call)):
                    if isinstance(statement, Raise):
                        item.status, item.raised = SymbolicStatus.RAISED, self._eval(statement.value, item)
                    elif isinstance(statement, Unsupported):
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, statement.message
                    else:
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported statement {type(statement).__name__}"
                else:
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported statement {type(statement).__name__}"
                next_states.append(item)
            states = next_states
        return states

    def _branch_statement(self, statement: If, state: _State) -> list[_State]:
        condition = self._condition(self._eval(statement.condition, state))
        return self._fork(state, condition, statement.then_body, statement.else_body)

    def _fork(self, state: _State, condition: z3.BoolRef, true_body: tuple[Any, ...], false_body: tuple[Any, ...]) -> list[_State]:
        result: list[_State] = []
        for value, body in ((condition, true_body), (z3.Not(condition), false_body)):
            child = state.clone()
            child.constraints.append(value)
            check = self._solver_check(child.constraints)
            if check == z3.unsat:
                continue
            if check == z3.unknown:
                child.status, child.diagnostic = SymbolicStatus.SOLVER_TIMEOUT, "solver returned unknown"
                result.append(child)
                continue
            result.extend(self._execute_statements(body, child))
        return result

    def _execute_terminator(self, terminator: Terminator | None, state: _State, blocks: dict[str, BasicBlock], cfg: Any) -> list[_State]:
        if terminator is None:
            ids = list(blocks)
            index = ids.index(state.current)
            if index + 1 >= len(ids):
                state.status, state.returns = SymbolicStatus.COMPLETED, ()
                return [state]
            target = ids[index + 1]
            edge = self._edge_id(cfg, state.current, target, "fallthrough")
            return [self._move(state, target, edge)]
        if isinstance(terminator, Return):
            state.status, state.returns = SymbolicStatus.COMPLETED, tuple(self._eval(value, state) for value in terminator.values)
            return [state]
        if isinstance(terminator, Jump):
            target = terminator.target
            return [self._move(state, target, self._edge_id(cfg, state.current, target, "jump"))]
        if isinstance(terminator, Branch):
            condition = self._condition(self._eval(terminator.condition, state))
            result: list[_State] = []
            for value, target, kind, ordinal in ((condition, terminator.true_target, "true", 0), (z3.Not(condition), terminator.false_target, "false", 1)):
                child = state.clone(); child.constraints.append(value)
                check = self._solver_check(child.constraints)
                if check == z3.unsat:
                    continue
                if check == z3.unknown:
                    child.status, child.diagnostic = SymbolicStatus.SOLVER_TIMEOUT, "solver returned unknown"; result.append(child); continue
                result.append(self._move(child, target, self._edge_id(cfg, state.current, target, kind, ordinal)))
            return result
        if isinstance(terminator, MultiBranch):
            selector = self._eval(terminator.selector, state)
            result: list[_State] = []
            for index, (value, target) in enumerate(terminator.cases):
                child = state.clone(); child.constraints.append(selector == self._eval(value, state))
                check = self._solver_check(child.constraints)
                if check == z3.sat:
                    result.append(self._move(child, target, self._edge_id(cfg, state.current, target, "case", index)))
                elif check == z3.unknown:
                    child.status, child.diagnostic = SymbolicStatus.SOLVER_TIMEOUT, "solver returned unknown"
                    result.append(child)
            child = state.clone(); child.constraints.append(z3.And(*[selector != self._eval(value, state) for value, _ in terminator.cases]))
            check = self._solver_check(child.constraints)
            if check == z3.sat:
                target = terminator.default_target
                result.append(self._move(child, target, self._edge_id(cfg, state.current, target, "default", len(terminator.cases))))
            elif check == z3.unknown:
                child.status, child.diagnostic = SymbolicStatus.SOLVER_TIMEOUT, "solver returned unknown"
                result.append(child)
            return result
        state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported terminator {type(terminator).__name__}"
        return [state]

    def _edge_id(self, cfg: Any, source: str, target: str, kind: str, ordinal: int = 0) -> str:
        edges = tuple(edge for edge in cfg.outgoing_edges(source) if edge.target == target and edge.kind == kind)
        if not edges:
            raise _UnsupportedValue(f"missing CFG edge {source!r}->{target!r} ({kind})")
        return edges[min(ordinal, len(edges) - 1)].edge_id

    def _move(self, state: _State, target: str, edge: str) -> _State:
        key = (state.current, target)
        state.loop_counts[key] = state.loop_counts.get(key, 0) + 1
        if state.loop_counts[key] > self._limits.max_loop_unroll:
            state.status, state.diagnostic = SymbolicStatus.STEP_LIMIT, "maximum loop unroll exceeded"
            return state
        state.predecessor, state.predecessor_edge, state.current = state.current, edge, target
        state.edges.append(edge)
        return state

    def _eval(self, expr: Expr, state: _State) -> Any:
        if isinstance(expr, Const): return expr.value
        if isinstance(expr, Var):
            if expr.name not in state.env:
                raise _UnsupportedValue(f"undefined symbolic variable {expr.name!r}")
            return state.env[expr.name]
        if isinstance(expr, UnaryOp):
            value = self._eval(expr.value, state)
            return {"-": lambda: -value, "+": lambda: +value, "not": lambda: z3.Not(self._condition(value)), "~": lambda: ~value}.get(expr.op, lambda: self._unsupported_value(expr.op))()
        if isinstance(expr, BinaryOp):
            left, right = self._eval(expr.left, state), self._eval(expr.right, state)
            ops = {"+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right, "/": lambda: left / right, "//": lambda: left / right, "%": lambda: left % right, "==": lambda: left == right, "!=": lambda: left != right, "<": lambda: left < right, "<=": lambda: left <= right, ">": lambda: left > right, ">=": lambda: left >= right, "and": lambda: z3.And(self._condition(left), self._condition(right)), "or": lambda: z3.Or(self._condition(left), self._condition(right)), "&": lambda: left & right, "|": lambda: left | right, "^": lambda: left ^ right, "<<": lambda: left << right, "shl": lambda: left << right, ">>": lambda: left >> right, "shr": lambda: left >> right}
            try: return ops[expr.op]()
            except KeyError: return self._unsupported_value(expr.op)
        if isinstance(expr, ArrayLiteral):
            values = tuple(self._eval(item, state) for item in expr.items)
            return values if expr.kind == "tuple" else list(values)
        if isinstance(expr, (TableLiteral, MapLiteral, ObjectLiteral, SetLiteral)):
            return self._unsupported_value(type(expr).__name__)
        if isinstance(expr, GetItem):
            obj, key = self._eval(expr.obj, state), self._eval(expr.key, state)
            try: return obj[key]
            except Exception: return self._unsupported_value("getitem")
        if isinstance(expr, CollectionProjection): return self._unsupported_value("collection projection")
        if isinstance(expr, Call): return self._unsupported_value("call")
        return self._unsupported_value(type(expr).__name__)

    def _unsupported_value(self, detail: str) -> Any:
        raise _UnsupportedValue(f"unsupported symbolic expression {detail}")

    def _condition(self, value: Any) -> z3.BoolRef:
        if isinstance(value, bool): return z3.BoolVal(value)
        if z3.is_bool(value): return value
        if z3.is_bv(value) or z3.is_arith(value): return value != 0
        raise _UnsupportedValue("non-scalar branch condition")

    def _cancelled(self) -> bool:
        return bool(self._cancellation is not None and getattr(self._cancellation, "cancelled", False))

    def _path(self, path_id: int, state: _State) -> SymbolicPath:
        model = None
        if state.status in (SymbolicStatus.COMPLETED, SymbolicStatus.RAISED):
            solver = z3.Solver(); solver.add(*state.constraints)
            if solver.check() == z3.sat:
                value = solver.model(); model = {str(decl.name()): value.eval(decl(), model_completion=True) for decl in value.decls()}
        return SymbolicPath(path_id, state.status or SymbolicStatus.UNSUPPORTED, tuple(state.constraints), state.returns or (), state.raised, model, tuple(state.blocks), tuple(state.edges), state.steps, state.diagnostic)


class _UnsupportedValue(Exception):
    pass
