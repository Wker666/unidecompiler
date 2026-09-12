from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from threading import Event
from typing import Any, Mapping

import z3

from unidecompiler.core.cfg import CFG, CFGEdge, build_cfg, validate_cfg_consistency
from unidecompiler.core.ir import (
    ArrayLiteral, Assign, AssignMany, BasicBlock, BinaryOp, Branch, Call,
    CollectionProjection, Const, Delete, DoWhile, Expr, ExprStmt, ForEach,
    ForRange, FunctionIR, GetItem, If, Jump, MapLiteral, ModuleIR,
    MultiBranch, ObjectLiteral, Phi, Raise, Reraise, Return, SetLiteral,
    StoreItem, TableLiteral, Terminator, UnaryOp, Unsupported, Var, While,
    exceptional_transfers,
)
from unidecompiler.core.operators import normalize_numeric_operator
from unidecompiler.input_sources import expand_input_path
from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine


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
        if self.sort is not SymbolicSort.BITVEC and self.bit_width is not None:
            raise ValueError("bit_width is only valid for bit-vector inputs")


@dataclass(frozen=True)
class SymbolicLimits:
    max_paths: int = 256
    max_steps: int = 100_000
    max_loop_unroll: int = 32
    max_call_depth: int = 32
    solver_timeout_ms: int = 5_000

    def validate(self) -> None:
        if any(value <= 0 for value in (
            self.max_paths, self.max_steps, self.max_loop_unroll, self.max_call_depth,
        )):
            raise ValueError("symbolic limits must be positive")
        if self.solver_timeout_ms <= 0:
            raise ValueError("solver_timeout_ms must be positive")


class SymbolicCancellation:
    """Cooperative cancellation control, kept outside symbolic IR state."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


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
    current: str
    constraints: list[z3.BoolRef] = field(default_factory=list)
    predecessor_source: str | None = None
    predecessor_edge: str | None = None
    blocks: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    steps: int = 0
    loop_counts: dict[str, int] = field(default_factory=dict)
    returns: tuple[Any, ...] | None = None
    raised: Any | None = None
    status: SymbolicStatus | None = None
    diagnostic: str | None = None

    def clone(self) -> _State:
        return replace(
            self, env=dict(self.env), constraints=list(self.constraints),
            blocks=list(self.blocks), edges=list(self.edges), loop_counts=dict(self.loop_counts),
        )


class SymbolicEngine:
    """Bounded generic-IR path exploration without frontend bytecode execution."""

    def __init__(self, registry: FrontendRegistry | None = None) -> None:
        self._registry = registry

    @classmethod
    def from_registry(cls, registry: FrontendRegistry) -> SymbolicEngine:
        return cls(registry)

    def explore_function(
        self,
        module: ModuleIR,
        function: FunctionIR,
        *,
        symbolic_inputs: tuple[SymbolicInput, ...] = (),
        concrete_args: Mapping[str, Any] | None = None,
        limits: SymbolicLimits | None = None,
        cancellation: SymbolicCancellation | None = None,
    ) -> SymbolicResult:
        try:
            return _SymbolicRunner(module, function, symbolic_inputs, concrete_args, limits, cancellation).run()
        except (TypeError, ValueError) as error:
            return SymbolicResult(SymbolicStatus.INVALID_REQUEST, diagnostic=str(error))

    def explore_artifact(
        self,
        data: bytes,
        display_path: str,
        query: object,
        *,
        frontend_id: str | None = None,
        symbolic_inputs: tuple[SymbolicInput, ...] = (),
        concrete_args: Mapping[str, Any] | None = None,
        limits: SymbolicLimits | None = None,
        cancellation: SymbolicCancellation | None = None,
    ) -> SymbolicResult:
        try:
            registry = self._registry or FrontendRegistry.discover()
            prepared = SimulationEngine.from_registry(registry).prepare_artifact_target(
                data, display_path, query, frontend_id=frontend_id
            )
        except Exception as error:
            return SymbolicResult(
                SymbolicStatus.INVALID_REQUEST,
                diagnostic=f"{type(error).__name__}: {error}",
            )
        return self.explore_function(
            prepared.module, prepared.function, symbolic_inputs=symbolic_inputs,
            concrete_args=concrete_args, limits=limits, cancellation=cancellation,
        )

    def explore_path(self, path: str, query: object, **kwargs: Any) -> SymbolicResult:
        artifacts = expand_input_path(path)
        if len(artifacts) != 1:
            return SymbolicResult(
                SymbolicStatus.INVALID_REQUEST,
                diagnostic="symbolic execution requires exactly one input artifact",
            )
        artifact = artifacts[0]
        return self.explore_artifact(artifact.data, artifact.display_path, query, **kwargs)


class _SymbolicRunner:
    def __init__(
        self,
        module: ModuleIR,
        function: FunctionIR,
        symbolic_inputs: tuple[SymbolicInput, ...],
        concrete_args: Mapping[str, Any] | None,
        limits: SymbolicLimits | None,
        cancellation: SymbolicCancellation | None,
    ) -> None:
        self.module = module
        self.function = function
        self.symbolic_inputs = tuple(symbolic_inputs)
        self.concrete_args = dict(concrete_args or {})
        self.limits = limits or SymbolicLimits()
        self.cancellation = cancellation
        self.pruned_paths = 0
        self.explored_paths = 0

    def run(self) -> SymbolicResult:
        env, cfg = self._validate_request()
        if cfg.entry is None:
            return SymbolicResult(
                SymbolicStatus.COMPLETED,
                (SymbolicPath(0, SymbolicStatus.COMPLETED),), 1,
            )
        pending = [_State(env=env, current=cfg.entry)]
        paths: list[SymbolicPath] = []
        while pending:
            if len(paths) >= self.limits.max_paths:
                paths.append(SymbolicPath(
                    len(paths), SymbolicStatus.PATH_LIMIT,
                    diagnostic="maximum symbolic path count exceeded",
                ))
                break
            state = pending.pop()
            self.explored_paths += 1
            try:
                outcomes = self._run_state(state, cfg)
            except _UnsupportedValue as error:
                state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, str(error)
                outcomes = [state]
            for outcome in outcomes:
                if outcome.status is not None:
                    paths.append(self._path(len(paths), outcome))
                else:
                    pending.append(outcome)
        return SymbolicResult(
            self._overall_status(paths), tuple(paths), self.explored_paths, self.pruned_paths,
        )

    def _validate_request(self) -> tuple[dict[str, Any], CFG]:
        self.limits.validate()
        if not self._contains_function(self.module, self.function):
            raise ValueError(f"function {self.function.name!r} does not belong to the current ModuleIR")
        names = {item.name for item in self.symbolic_inputs}
        for item in self.symbolic_inputs:
            item.validate()
        if len(names) != len(self.symbolic_inputs) or not names.issubset(self.function.params):
            raise ValueError("symbolic inputs must name unique function parameters")
        if set(self.concrete_args) & names or not set(self.concrete_args).issubset(self.function.params):
            raise ValueError("concrete arguments overlap or name unknown parameters")
        if set(self.function.params) != names | set(self.concrete_args):
            raise ValueError("every function parameter needs a symbolic or concrete argument")
        cfg = build_cfg(self.function)
        diagnostics = validate_cfg_consistency(cfg)
        if diagnostics:
            raise ValueError("; ".join(diagnostics))
        self._validate_phis(cfg)
        env = dict(self.concrete_args)
        for item in self.symbolic_inputs:
            env[item.name] = self._symbol(item)
        return env, cfg

    @staticmethod
    def _contains_function(module: ModuleIR, target: FunctionIR) -> bool:
        def walk(functions: tuple[FunctionIR, ...]):
            for item in functions:
                yield item
                yield from walk(item.nested_functions)
        return any(item is target for item in walk(tuple(module.functions)))

    def _validate_phis(self, cfg: CFG) -> None:
        for block in cfg.blocks.values():
            incoming_edges = cfg.incoming_edges(block.id)
            for statement in block.statements:
                if not (isinstance(statement, Assign) and isinstance(statement.value, Phi)):
                    continue
                if not isinstance(statement.target, Var):
                    raise ValueError(f"Phi assignment in {block.id!r} has a non-local target")
                phi = statement.value
                labels = tuple(label for label, _ in phi.incoming)
                if len(labels) != len(set(labels)):
                    raise ValueError(f"Phi in {block.id!r} has duplicate incoming labels")
                if phi.edge_ids:
                    if len(phi.edge_ids) != len(phi.incoming):
                        raise ValueError(f"Phi in {block.id!r} has a mismatched edge_id count")
                    if len(phi.edge_ids) != len(set(phi.edge_ids)):
                        raise ValueError(f"Phi in {block.id!r} has duplicate edge IDs")
                    known = {edge.edge_id: edge for edge in incoming_edges}
                    if set(phi.edge_ids) != set(known):
                        raise ValueError(f"Phi in {block.id!r} does not cover exact incoming CFG edges")
                    for label, edge_id in zip(labels, phi.edge_ids):
                        if known[edge_id].source != label:
                            raise ValueError(f"Phi in {block.id!r} label does not match edge {edge_id!r}")
                else:
                    if len(incoming_edges) != len(phi.incoming):
                        raise ValueError(f"Phi in {block.id!r} does not cover exact incoming CFG edges")
                    if len({edge.source for edge in incoming_edges}) != len(incoming_edges):
                        raise ValueError(f"Phi in {block.id!r} needs concrete edge IDs for parallel predecessors")
                    if set(labels) != {edge.source for edge in incoming_edges}:
                        raise ValueError(f"Phi in {block.id!r} labels do not match CFG predecessors")

    @staticmethod
    def _symbol(item: SymbolicInput) -> z3.ExprRef:
        if item.sort is SymbolicSort.BOOL:
            return z3.Bool(item.name)
        if item.sort is SymbolicSort.REAL:
            return z3.Real(item.name)
        if item.sort is SymbolicSort.BITVEC:
            return z3.BitVec(item.name, item.bit_width or 1)
        return z3.Int(item.name)

    def _run_state(self, state: _State, cfg: CFG) -> list[_State]:
        while state.current in cfg.blocks:
            if self._cancelled():
                state.status, state.diagnostic = SymbolicStatus.CANCELLED, "symbolic execution cancelled"
                return [state]
            if state.steps >= self.limits.max_steps:
                state.status, state.diagnostic = SymbolicStatus.STEP_LIMIT, "maximum symbolic steps exceeded"
                return [state]
            block = cfg.blocks[state.current]
            transfers = exceptional_transfers(block)
            if transfers:
                sources = ", ".join(
                    f"{transfer.target}@{getattr(transfer.source, 'offset', None)}" for transfer in transfers
                )
                state.status, state.diagnostic = (
                    SymbolicStatus.UNSUPPORTED,
                    f"exceptional transfers from block {block.id!r} require symbolic exception provenance: {sources}",
                )
                return [state]
            state.blocks.append(block.id)
            state.steps += 1
            phi_state = self._execute_phis(block, state)
            if phi_state is not None:
                return [phi_state]
            candidates = self._execute_statements(block.statements, state)
            ready: list[_State] = []
            for candidate in candidates:
                if candidate.status is not None:
                    ready.append(candidate)
                else:
                    ready.extend(self._execute_terminator(block.terminator, candidate, cfg))
            if len(ready) != 1 or ready[0].status is not None:
                return ready
            state = ready[0]
        state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, f"missing target block {state.current!r}"
        return [state]

    def _execute_phis(self, block: BasicBlock, state: _State) -> _State | None:
        assignments: list[tuple[str, Any]] = []
        for statement in block.statements:
            if isinstance(statement, Assign) and isinstance(statement.value, Phi):
                if not isinstance(statement.target, Var):
                    state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, "Phi target is not a local variable"
                    return state
                value = self._phi_value(statement.value, state)
                if value is _MISSING:
                    state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, "Phi lacks matching concrete predecessor edge"
                    return state
                assignments.append((statement.target.name, value))
        state.env.update(assignments)
        return None

    def _phi_value(self, phi: Phi, state: _State) -> Any:
        if state.predecessor_edge is None:
            return _MISSING
        if phi.edge_ids:
            for edge_id, (_, value) in zip(phi.edge_ids, phi.incoming):
                if edge_id == state.predecessor_edge:
                    return self._eval(value, state)
            return _MISSING
        if state.predecessor_source is None:
            return _MISSING
        for label, value in phi.incoming:
            if label == state.predecessor_source:
                return self._eval(value, state)
        return _MISSING

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
                    if not isinstance(statement.target, Var):
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic assignment target is unsupported"
                    else:
                        item.env[statement.target.name] = self._eval(statement.value, item)
                elif isinstance(statement, AssignMany):
                    if len(statement.targets) != len(statement.values):
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic multiple assignment has mismatched arity"
                    elif not all(isinstance(target, Var) for target in statement.targets):
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic multiple assignment target is unsupported"
                    else:
                        values = tuple(self._eval(value, item) for value in statement.values)
                        item.env.update({target.name: value for target, value in zip(statement.targets, values)})
                elif isinstance(statement, ExprStmt):
                    self._eval(statement.value, item)
                elif isinstance(statement, Delete):
                    if isinstance(statement.target, Var):
                        item.env.pop(statement.target.name, None)
                    else:
                        item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic delete target is unsupported"
                elif isinstance(statement, StoreItem):
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, "symbolic container mutation is unsupported"
                elif isinstance(statement, If):
                    next_states.extend(self._fork_statement(statement, item))
                    continue
                elif isinstance(statement, Return):
                    item.status = SymbolicStatus.COMPLETED
                    item.returns = tuple(self._eval(value, item) for value in statement.values)
                elif isinstance(statement, Raise):
                    item.status, item.raised = SymbolicStatus.RAISED, self._eval(statement.value, item)
                elif isinstance(statement, Unsupported):
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, statement.message
                elif isinstance(statement, (While, DoWhile, ForEach, ForRange, Reraise, Call)):
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported statement {type(statement).__name__}"
                else:
                    item.status, item.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported statement {type(statement).__name__}"
                next_states.append(item)
            states = next_states
        return states

    def _fork_statement(self, statement: If, state: _State) -> list[_State]:
        condition = self._condition(self._eval(statement.condition, state))
        result: list[_State] = []
        for predicate, body in ((condition, statement.then_body), (z3.Not(condition), statement.else_body)):
            child = self._constrain(state.clone(), predicate)
            if child is not None:
                result.extend(self._execute_statements(body, child))
        return result

    def _execute_terminator(self, terminator: Terminator | None, state: _State, cfg: CFG) -> list[_State]:
        if terminator is None:
            edges = self._ordinary_edges(cfg, state.current)
            if not edges:
                state.status, state.returns = SymbolicStatus.COMPLETED, ()
                return [state]
            return [self._move(state, edges[0])]
        if isinstance(terminator, Return):
            state.status = SymbolicStatus.COMPLETED
            state.returns = tuple(self._eval(value, state) for value in terminator.values)
            return [state]
        if isinstance(terminator, Jump):
            return [self._move(state, self._ordinary_edge(cfg, state.current, 0, terminator.target))]
        if isinstance(terminator, Branch):
            condition = self._condition(self._eval(terminator.condition, state))
            result: list[_State] = []
            for predicate, index, target in (
                (condition, 0, terminator.true_target), (z3.Not(condition), 1, terminator.false_target),
            ):
                child = self._constrain(state.clone(), predicate)
                if child is not None:
                    result.append(self._move(child, self._ordinary_edge(cfg, state.current, index, target)))
            return result
        if isinstance(terminator, MultiBranch):
            selector = self._eval(terminator.selector, state)
            evaluated_cases = tuple((self._eval(value, state), target) for value, target in terminator.cases)
            result: list[_State] = []
            for index, (value, target) in enumerate(evaluated_cases):
                child = self._constrain(state.clone(), self._equals(selector, value))
                if child is not None:
                    result.append(self._move(child, self._ordinary_edge(cfg, state.current, index, target)))
            default = self._constrain(
                state.clone(), z3.And(*(z3.Not(self._equals(selector, value)) for value, _ in evaluated_cases)),
            )
            if default is not None:
                result.append(self._move(
                    default,
                    self._ordinary_edge(cfg, state.current, len(evaluated_cases), terminator.default_target),
                ))
            return result
        state.status, state.diagnostic = SymbolicStatus.UNSUPPORTED, f"unsupported terminator {type(terminator).__name__}"
        return [state]

    @staticmethod
    def _ordinary_edges(cfg: CFG, source: str) -> tuple[CFGEdge, ...]:
        return tuple(edge for edge in cfg.outgoing_edges(source) if edge.kind != "exception")

    def _ordinary_edge(self, cfg: CFG, source: str, index: int, target: str) -> CFGEdge:
        edges = self._ordinary_edges(cfg, source)
        if index >= len(edges) or edges[index].target != target:
            raise _UnsupportedValue(f"CFG does not preserve expected concrete edge {source!r}->{target!r}")
        return edges[index]

    def _move(self, state: _State, edge: CFGEdge) -> _State:
        state.loop_counts[edge.edge_id] = state.loop_counts.get(edge.edge_id, 0) + 1
        if state.loop_counts[edge.edge_id] > self.limits.max_loop_unroll:
            state.status, state.diagnostic = SymbolicStatus.STEP_LIMIT, "maximum loop unroll exceeded"
            return state
        state.predecessor_source = edge.source
        state.predecessor_edge, state.current = edge.edge_id, edge.target
        state.edges.append(edge.edge_id)
        return state

    def _constrain(self, state: _State, predicate: z3.BoolRef) -> _State | None:
        state.constraints.append(predicate)
        solver = z3.Solver()
        solver.set(timeout=self.limits.solver_timeout_ms)
        solver.add(*state.constraints)
        result = solver.check()
        if result == z3.unsat:
            self.pruned_paths += 1
            return None
        if result == z3.unknown:
            state.status, state.diagnostic = SymbolicStatus.SOLVER_TIMEOUT, "solver returned unknown"
        return state

    def _eval(self, expr: Expr, state: _State) -> Any:
        if isinstance(expr, Const):
            return expr.value
        if isinstance(expr, Var):
            if expr.name not in state.env:
                raise _UnsupportedValue(f"undefined symbolic variable {expr.name!r}")
            return state.env[expr.name]
        if isinstance(expr, UnaryOp):
            value = self._eval(expr.value, state)
            try:
                if expr.op == "-": return -value
                if expr.op == "+": return +value
                if expr.op == "not": return z3.Not(self._condition(value))
                if expr.op == "~": return ~value
            except (TypeError, z3.Z3Exception) as error:
                raise _UnsupportedValue(f"unsupported unary operation {expr.op!r}: {error}") from error
            raise _UnsupportedValue(f"unsupported symbolic expression {expr.op}")
        if isinstance(expr, BinaryOp):
            return self._binary(expr, self._eval(expr.left, state), self._eval(expr.right, state))
        if isinstance(expr, ArrayLiteral):
            values = tuple(self._eval(item, state) for item in expr.items)
            return values if expr.kind == "tuple" else list(values)
        if isinstance(expr, (TableLiteral, MapLiteral, ObjectLiteral, SetLiteral, CollectionProjection, Call)):
            raise _UnsupportedValue(f"unsupported symbolic expression {type(expr).__name__}")
        if isinstance(expr, GetItem):
            obj, key = self._eval(expr.obj, state), self._eval(expr.key, state)
            try:
                return obj[key]
            except (KeyError, IndexError, TypeError) as error:
                raise _UnsupportedValue(f"unsupported symbolic getitem: {error}") from error
        raise _UnsupportedValue(f"unsupported symbolic expression {type(expr).__name__}")

    def _binary(self, expr: BinaryOp, left: Any, right: Any) -> Any:
        op = normalize_numeric_operator(expr.op)
        if expr.numeric_domain not in {"default", "signed", "unsigned", "float"}:
            raise _UnsupportedValue(f"unknown numeric domain {expr.numeric_domain!r}")
        if expr.overflow_policy not in {"wrap", "trap"}:
            raise _UnsupportedValue(f"unknown overflow policy {expr.overflow_policy!r}")
        if expr.overflow_policy == "trap":
            raise _UnsupportedValue("trap overflow policy requires an explicit symbolic overflow model")
        if expr.bit_width is not None and expr.bit_width <= 0:
            raise _UnsupportedValue("numeric bit width must be positive")
        try:
            if expr.numeric_domain in {"signed", "unsigned"}:
                return self._bitvec_binary(op, left, right, expr)
            if expr.numeric_domain == "float" or expr.bit_width is not None:
                raise _UnsupportedValue("float or width-qualified default arithmetic is unsupported")
            operations = {
                "+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right,
                "/": lambda: left / right, "%": lambda: left % right,
                "==": lambda: self._equals(left, right), "!=": lambda: z3.Not(self._equals(left, right)),
                "<": lambda: left < right, "<=": lambda: left <= right, ">": lambda: left > right, ">=": lambda: left >= right,
                "and": lambda: z3.And(self._condition(left), self._condition(right)),
                "or": lambda: z3.Or(self._condition(left), self._condition(right)),
            }
            if op == "//":
                raise _UnsupportedValue("floor division requires an explicit symbolic numeric model")
            return operations[op]()
        except KeyError as error:
            raise _UnsupportedValue(f"unsupported symbolic binary operation {op!r}") from error
        except (TypeError, z3.Z3Exception) as error:
            raise _UnsupportedValue(f"unsupported symbolic binary operation {op!r}: {error}") from error

    def _bitvec_binary(self, op: str, left: Any, right: Any, expr: BinaryOp) -> Any:
        if not (z3.is_bv(left) and z3.is_bv(right)):
            raise _UnsupportedValue("signed or unsigned arithmetic requires bit-vector operands")
        if left.size() != right.size() or left.size() != expr.bit_width:
            raise _UnsupportedValue("bit-vector operands must match the declared numeric width")
        unsigned = expr.numeric_domain == "unsigned"
        operations = {
            "+": lambda: left + right, "-": lambda: left - right, "*": lambda: left * right,
            "/": lambda: z3.UDiv(left, right) if unsigned else left / right,
            "%": lambda: z3.URem(left, right) if unsigned else z3.SRem(left, right),
            "==": lambda: left == right, "!=": lambda: left != right,
            "<": lambda: z3.ULT(left, right) if unsigned else left < right,
            "<=": lambda: z3.ULE(left, right) if unsigned else left <= right,
            ">": lambda: z3.UGT(left, right) if unsigned else left > right,
            ">=": lambda: z3.UGE(left, right) if unsigned else left >= right,
            "&": lambda: left & right, "|": lambda: left | right, "^": lambda: left ^ right,
            "<<": lambda: left << right, ">>": lambda: z3.LShR(left, right) if unsigned else left >> right,
            ">>>": lambda: z3.LShR(left, right),
        }
        if op == "//":
            raise _UnsupportedValue("floor division is not defined for bit-vector symbolic values")
        try:
            return operations[op]()
        except KeyError as error:
            raise _UnsupportedValue(f"unsupported bit-vector operation {op!r}") from error

    @staticmethod
    def _equals(left: Any, right: Any) -> z3.BoolRef:
        result = left == right
        return z3.BoolVal(result) if isinstance(result, bool) else result

    @staticmethod
    def _condition(value: Any) -> z3.BoolRef:
        if isinstance(value, bool):
            return z3.BoolVal(value)
        if isinstance(value, int):
            return z3.BoolVal(value != 0)
        if isinstance(value, float):
            return z3.BoolVal(value != 0.0)
        if z3.is_bool(value):
            return value
        if z3.is_bv(value) or z3.is_arith(value):
            return value != 0
        raise _UnsupportedValue("non-scalar branch condition")

    def _path(self, path_id: int, state: _State) -> SymbolicPath:
        model = None
        if state.status in {SymbolicStatus.COMPLETED, SymbolicStatus.RAISED}:
            solver = z3.Solver()
            solver.set(timeout=self.limits.solver_timeout_ms)
            solver.add(*state.constraints)
            if solver.check() == z3.sat:
                result = solver.model()
                model = {
                    declaration.name(): result.eval(declaration(), model_completion=True)
                    for declaration in result.decls()
                }
        return SymbolicPath(
            path_id, state.status or SymbolicStatus.UNSUPPORTED, tuple(state.constraints),
            state.returns or (), state.raised, model, tuple(state.blocks), tuple(state.edges),
            state.steps, state.diagnostic,
        )

    @staticmethod
    def _overall_status(paths: list[SymbolicPath]) -> SymbolicStatus:
        statuses = {path.status for path in paths}
        for status in (
            SymbolicStatus.PATH_LIMIT, SymbolicStatus.UNSUPPORTED, SymbolicStatus.SOLVER_TIMEOUT,
            SymbolicStatus.STEP_LIMIT, SymbolicStatus.CALL_DEPTH_LIMIT, SymbolicStatus.CANCELLED,
        ):
            if status in statuses:
                return status
        return SymbolicStatus.RAISED if statuses == {SymbolicStatus.RAISED} else SymbolicStatus.COMPLETED

    def _cancelled(self) -> bool:
        return bool(self.cancellation is not None and self.cancellation.cancelled)


class _UnsupportedValue(Exception):
    pass


_MISSING = object()
