from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import operator
import math
import struct
from pathlib import Path
from threading import Event
from typing import Any

from unidecompiler.core.ir import (
    ArrayLiteral,
    Assign,
    AssignMany,
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
    IndirectCall,
    IndirectRef,
    Jump,
    MapLiteral,
    MultiBranch,
    MultiReturn,
    NewObject,
    ObjectLiteral,
    Placeholder,
    Phi,
    Raise,
    Reraise,
    Return,
    SetLiteral,
    Stmt,
    StoreAttr,
    StoreItem,
    Switch,
    TableLiteral,
    Try,
    UnaryOp,
    Unsupported,
    Var,
    While,
    Yield,
    ModuleIR,
)
from unidecompiler.input_sources import expand_input_path
from unidecompiler.plugin_registry import FrontendRegistry

from unidecompiler_simulator.adapters import (
    CallRequest,
    IntrinsicCall,
    NotHandled,
    ResolvedFunction,
    SimulationTarget,
    SimulationTargetCandidate,
    adapter_for,
    call_adapter,
)
from unidecompiler_simulator.environment import (
    ExternalCallRequest,
    ExternalCallStatus,
    ExternalEnvironment,
    ExternalFunction,
    call_environment,
)
from unidecompiler_simulator.values import (
    ObjectValue,
    SliceValue,
    TableValue,
    snapshot_value,
    validate_runtime_value,
)


class SimulationStatus(StrEnum):
    COMPLETED = "completed"
    RAISED = "raised"
    YIELDED = "yielded"
    UNSUPPORTED = "unsupported"
    STEP_LIMIT = "step_limit"
    CALL_DEPTH_LIMIT = "call_depth_limit"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class SimulationLimits:
    max_steps: int = 100_000
    max_call_depth: int = 128
    max_trace_events: int = 10_000

    def validate(self) -> None:
        if self.max_steps <= 0 or self.max_call_depth <= 0 or self.max_trace_events <= 0:
            raise ValueError("simulation limits must be positive")


class SimulationCancellation:
    """Cooperative cancellation control, separate from IR and runtime values."""

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()


@dataclass(frozen=True)
class SimulationEvent:
    function: str
    block: str | None
    kind: str
    function_index: int = -1
    detail: str = ""
    source: object | None = None
    locals: dict[str, Any] = field(default_factory=dict)
    args: tuple[Any, ...] = ()
    values: tuple[Any, ...] = ()
    exception: Any | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SimulationResult:
    status: SimulationStatus
    values: tuple[Any, ...] = ()
    exception: Any | None = None
    cause: Any | None = None
    locals: dict[str, Any] = field(default_factory=dict)
    steps: int = 0
    diagnostic: str | None = None
    events: tuple[SimulationEvent, ...] = ()
    trace_truncated: bool = False


@dataclass(frozen=True)
class SimulationTargetListing:
    frontend_id: str | None
    targets: tuple[SimulationTarget, ...] = ()
    diagnostic: str | None = None


class _SimulationStop(Exception):
    def __init__(self, status: SimulationStatus, message: str, value: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.value = value


class _Raised(Exception):
    def __init__(self, value: Any, cause: Any | None = None) -> None:
        self.value = value
        self.cause = cause


class _ReturnSignal(Exception):
    def __init__(self, values: tuple[Any, ...]) -> None:
        self.values = values


class _BreakSignal(Exception):
    pass


class _ContinueSignal(Exception):
    pass


class _YieldSignal(Exception):
    def __init__(self, value: Any) -> None:
        self.value = value


@dataclass(frozen=True)
class _IndirectTarget:
    selector: Any
    signature: str
    context: object | None


@dataclass(frozen=True)
class _MultiReturnValue:
    values: tuple[Any, ...]


@dataclass(frozen=True)
class _Reference:
    target: Expr


@dataclass
class _Frame:
    function: FunctionIR
    locals: dict[str, Any]
    context: object | None
    predecessor: str | None = None
    active_exception: _Raised | None = None


class SimulationEngine:
    """Bounded generic IR execution, independent of frontend implementations."""

    def __init__(self, registry: FrontendRegistry | None = None) -> None:
        self.registry = registry or FrontendRegistry.discover()

    @classmethod
    def discover(cls) -> "SimulationEngine":
        return cls(FrontendRegistry.discover())

    @classmethod
    def from_registry(cls, registry: FrontendRegistry) -> "SimulationEngine":
        return cls(registry)

    def simulate_function(
        self,
        module: ModuleIR,
        function: FunctionIR,
        args: tuple[Any, ...] = (),
        *,
        adapter: object | None = None,
        environment: ExternalEnvironment | None = None,
        target_context: object | None = None,
        limits: SimulationLimits | None = None,
        cancellation: SimulationCancellation | None = None,
    ) -> SimulationResult:
        limits = limits or SimulationLimits()
        try:
            limits.validate()
            for value in args:
                validate_runtime_value(value)
            if callable(target_context):
                raise TypeError("target context cannot be executable")
            if not self._contains_function(module, function):
                raise ValueError("target function does not belong to the supplied ModuleIR")
            runner = _Runner(module, adapter, environment, limits, cancellation)
            return runner.run(function, args, target_context)
        except (TypeError, ValueError) as error:
            return SimulationResult(
                status=SimulationStatus.INVALID_REQUEST,
                diagnostic=str(error),
            )

    def simulate_artifact(
        self,
        data: bytes,
        display_path: str,
        query: object,
        *,
        frontend_id: str | None = None,
        args: tuple[Any, ...] = (),
        environment: ExternalEnvironment | None = None,
        limits: SimulationLimits | None = None,
        cancellation: SimulationCancellation | None = None,
    ) -> SimulationResult:
        try:
            frontend = self.registry.select(data, display_path, explicit_id=frontend_id)
            decoded = frontend.decode(data, display_path)
            module = frontend.lift(decoded)
            adapter = adapter_for(frontend)
            if adapter is None:
                return SimulationResult(
                    status=SimulationStatus.INVALID_REQUEST,
                    diagnostic=f"frontend {frontend.id!r} does not support simulation function lookup",
                )
            if getattr(adapter, "frontend_id", None) != frontend.id:
                raise TypeError("simulation adapter frontend_id does not match frontend")
            resolved = call_adapter(adapter, "resolve_function", query, decoded, module)
            if resolved is NotHandled:
                return SimulationResult(
                    status=SimulationStatus.INVALID_REQUEST,
                    diagnostic=f"frontend {frontend.id!r} did not resolve the function query",
                )
            if not isinstance(resolved, ResolvedFunction):
                raise TypeError("resolve_function must return ResolvedFunction or NotHandled")
            return self.simulate_function(
                module,
                resolved.function,
                args,
                adapter=adapter,
                environment=environment,
                target_context=resolved.context,
                limits=limits,
                cancellation=cancellation,
            )
        except Exception as error:
            return SimulationResult(
                status=SimulationStatus.INVALID_REQUEST,
                diagnostic=f"{type(error).__name__}: {error}",
            )

    def simulate_path(
        self,
        path: Path | str,
        query: object,
        **kwargs: Any,
    ) -> SimulationResult:
        artifacts = expand_input_path(Path(path))
        if len(artifacts) != 1:
            return SimulationResult(
                status=SimulationStatus.INVALID_REQUEST,
                diagnostic="simulation requires exactly one input artifact",
            )
        artifact = artifacts[0]
        return self.simulate_artifact(artifact.data, artifact.display_path, query, **kwargs)

    def list_artifact_targets(
        self,
        data: bytes,
        display_path: str,
        *,
        frontend_id: str | None = None,
    ) -> SimulationTargetListing:
        """List frontend-owned function queries after automatic input selection."""
        try:
            frontend = self.registry.select(data, display_path, explicit_id=frontend_id)
            decoded = frontend.decode(data, display_path)
            module = frontend.lift(decoded)
            adapter = adapter_for(frontend)
            if adapter is None:
                return SimulationTargetListing(
                    frontend.id,
                    diagnostic=f"frontend {frontend.id!r} does not support simulation",
                )
            candidates = call_adapter(adapter, "list_simulation_targets", decoded, module)
            if candidates is NotHandled:
                return SimulationTargetListing(
                    frontend.id,
                    diagnostic=f"frontend {frontend.id!r} does not enumerate simulation targets",
                )
            if not isinstance(candidates, tuple) or not all(
                isinstance(candidate, SimulationTargetCandidate) for candidate in candidates
            ):
                raise TypeError("list_simulation_targets must return target candidates or NotHandled")
            functions = tuple(self._walk_functions(tuple(module.functions)))
            indexes = {id(function): index for index, function in enumerate(functions)}
            targets: list[SimulationTarget] = []
            for candidate in candidates:
                resolved = call_adapter(adapter, "resolve_function", candidate.query, decoded, module)
                if not isinstance(resolved, ResolvedFunction):
                    continue
                index = indexes.get(id(resolved.function))
                if index is None:
                    raise TypeError("simulation target does not belong to the lifted module")
                targets.append(
                    SimulationTarget(candidate.label, candidate.query, index, resolved.function.params)
                )
            return SimulationTargetListing(frontend.id, tuple(targets))
        except Exception as error:
            return SimulationTargetListing(None, diagnostic=f"{type(error).__name__}: {error}")

    @staticmethod
    def _contains_function(module: ModuleIR, target: FunctionIR) -> bool:
        return any(candidate is target for candidate in SimulationEngine._walk_functions(tuple(module.functions)))

    @staticmethod
    def _walk_functions(functions: tuple[FunctionIR, ...]):
        for function in functions:
            yield function
            yield from SimulationEngine._walk_functions(function.nested_functions)


class _Runner:
    def __init__(
        self,
        module: Any,
        adapter: object | None,
        environment: ExternalEnvironment | None,
        limits: SimulationLimits,
        cancellation: SimulationCancellation | None,
    ) -> None:
        self.module = module
        self.adapter = adapter
        self.environment = environment
        self.limits = limits
        self.cancellation = cancellation
        self.steps = 0
        self.events: list[SimulationEvent] = []
        self.frames: list[_Frame] = []
        self.last_locals: dict[str, Any] = {}
        self._iterator_positions: dict[int, int] = {}
        self.trace_truncated = False
        self._function_indexes = {
            id(function): index
            for index, function in enumerate(SimulationEngine._walk_functions(tuple(module.functions)))
        }

    def run(self, function: FunctionIR, args: tuple[Any, ...], context: object | None) -> SimulationResult:
        try:
            values = self._call(function, args, (), context)
            locals_ = snapshot_value(self.last_locals)
            return SimulationResult(
                status=SimulationStatus.COMPLETED,
                values=tuple(snapshot_value(value) for value in values),
                locals=locals_,
                steps=self.steps,
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )
        except _SimulationStop as stop:
            locals_ = snapshot_value(self.last_locals)
            return SimulationResult(
                status=stop.status,
                exception=snapshot_value(stop.value),
                locals=locals_,
                steps=self.steps,
                diagnostic=stop.message,
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )
        except _BreakSignal:
            return SimulationResult(
                status=SimulationStatus.UNSUPPORTED,
                locals=snapshot_value(self.last_locals),
                steps=self.steps,
                diagnostic="break used outside a loop",
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )
        except _ContinueSignal:
            return SimulationResult(
                status=SimulationStatus.UNSUPPORTED,
                locals=snapshot_value(self.last_locals),
                steps=self.steps,
                diagnostic="continue used outside a loop",
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )
        except _Raised as raised:
            locals_ = snapshot_value(self.last_locals)
            return SimulationResult(
                status=SimulationStatus.RAISED,
                exception=snapshot_value(raised.value),
                cause=snapshot_value(raised.cause),
                locals=locals_,
                steps=self.steps,
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )
        except _YieldSignal as yielded:
            locals_ = snapshot_value(self.last_locals)
            return SimulationResult(
                status=SimulationStatus.YIELDED,
                values=(snapshot_value(yielded.value),),
                locals=locals_,
                steps=self.steps,
                events=tuple(self.events),
                trace_truncated=self.trace_truncated,
            )

    def _call(
        self,
        function: FunctionIR,
        args: tuple[Any, ...],
        keywords: tuple[tuple[str, Any], ...],
        context: object | None,
    ) -> tuple[Any, ...]:
        if not any(candidate is function for candidate in self._all_functions()):
            raise _SimulationStop(
                SimulationStatus.INVALID_REQUEST,
                f"call target {function.name!r} does not belong to the current ModuleIR",
            )
        if len(self.frames) >= self.limits.max_call_depth:
            raise _SimulationStop(SimulationStatus.CALL_DEPTH_LIMIT, "maximum call depth exceeded")
        frame = _Frame(function, self._bind_args(function, args, keywords), context)
        self.frames.append(frame)
        try:
            if not function.blocks:
                return ()
            blocks = {block.id: block for block in function.blocks}
            block_index = {block.id: index for index, block in enumerate(function.blocks)}
            current = function.blocks[0].id
            while current in blocks:
                block = blocks[current]
                self._event("enter-block", current)
                try:
                    self._execute_statements(block.statements, current)
                except _ReturnSignal as returned:
                    return returned.values
                terminator = block.terminator
                if terminator is None:
                    index = block_index[current] + 1
                    if index >= len(function.blocks):
                        return ()
                    frame.predecessor = current
                    current = function.blocks[index].id
                elif isinstance(terminator, Return):
                    return self._eval_values(terminator.values)
                elif isinstance(terminator, Jump):
                    frame.predecessor = current
                    current = terminator.target
                elif isinstance(terminator, Branch):
                    frame.predecessor = current
                    current = terminator.true_target if self._truthy(self._eval_expr(terminator.condition)) else terminator.false_target
                elif isinstance(terminator, MultiBranch):
                    selector = self._eval_expr(terminator.selector)
                    current = terminator.default_target
                    for value, target in terminator.cases:
                        if self._truthy(
                            self._binary(
                                "==",
                                selector,
                                self._eval_expr(value),
                                frame.context,
                                "dynamic",
                            )
                        ):
                            current = target
                            break
                    frame.predecessor = block.id
                else:
                    self._unsupported(f"unsupported terminator {type(terminator).__name__}")
            self._unsupported(f"missing target block in {function.name}")
        finally:
            self.last_locals = snapshot_value(frame.locals)
            self.frames.pop()

    def _execute_statements(self, statements: tuple[Stmt, ...], block_id: str) -> None:
        for statement in statements:
            self._tick(statement.source)
            self._event("statement", block_id, type(statement).__name__, statement.source)
            self._execute_statement(statement, block_id)

    def _execute_statement(self, statement: Stmt, block_id: str) -> None:
        frame = self.frames[-1]
        if isinstance(statement, Assign):
            value = self._eval_expr(statement.value)
            self._assign(statement.target, value)
        elif isinstance(statement, AssignMany):
            values = self._eval_values(statement.values)
            if len(values) != len(statement.targets):
                self._unsupported(
                    f"assignment arity mismatch: {len(statement.targets)} targets, {len(values)} values"
                )
            for target, value in zip(statement.targets, values, strict=True):
                self._assign(target, value)
        elif isinstance(statement, StoreAttr):
            obj = self._eval_expr(statement.obj)
            value = self._eval_expr(statement.value)
            self._store_attr(obj, statement.attr, value)
        elif isinstance(statement, StoreItem):
            obj = self._eval_expr(statement.obj)
            key = self._eval_expr(statement.key)
            value = self._eval_expr(statement.value)
            self._store_item(obj, key, value)
        elif isinstance(statement, ExprStmt):
            self._eval_expr(statement.value)
        elif isinstance(statement, If):
            body = statement.then_body if self._truthy(self._eval_expr(statement.condition)) else statement.else_body
            self._execute_statements(body, block_id)
        elif isinstance(statement, Switch):
            selector = self._eval_expr(statement.selector)
            body = statement.default_body
            for value, candidate in statement.cases:
                if self._truthy(
                    self._binary(
                        "==",
                        selector,
                        self._eval_expr(value),
                        frame.context,
                        "dynamic",
                    )
                ):
                    body = candidate
                    break
            self._execute_statements(body, block_id)
        elif isinstance(statement, While):
            while self._truthy(self._eval_expr(statement.condition)):
                self._tick(statement.source)
                try:
                    self._execute_statements(statement.body, block_id)
                except _BreakSignal:
                    break
                except _ContinueSignal:
                    continue
        elif isinstance(statement, ForEach):
            for value in self._iter(self._eval_expr(statement.iterable)):
                frame.locals[statement.target.name] = value
                try:
                    self._execute_statements(statement.body, block_id)
                except _BreakSignal:
                    break
                except _ContinueSignal:
                    continue
        elif isinstance(statement, ForRange):
            start = self._eval_expr(statement.start)
            stop = self._eval_expr(statement.stop)
            step = self._eval_expr(statement.step)
            try:
                values = range(start, stop, step)
            except (TypeError, ValueError) as error:
                self._unsupported(f"invalid range: {error}")
            for value in values:
                frame.locals[statement.target.name] = value
                try:
                    self._execute_statements(statement.body, block_id)
                except _BreakSignal:
                    break
                except _ContinueSignal:
                    continue
        elif isinstance(statement, Break):
            raise _BreakSignal
        elif isinstance(statement, Continue):
            raise _ContinueSignal
        elif isinstance(statement, Raise):
            value = self._eval_expr(statement.value)
            cause = None if statement.cause is None else self._eval_expr(statement.cause)
            raise _Raised(value, cause)
        elif isinstance(statement, Return):
            raise _ReturnSignal(self._eval_values(statement.values))
        elif isinstance(statement, Reraise):
            if frame.active_exception is None:
                self._unsupported("reraise used outside an active exception handler")
            raise frame.active_exception
        elif isinstance(statement, Try):
            try:
                self._execute_statements(statement.body, block_id)
            except _Raised as raised:
                previous = frame.active_exception
                frame.active_exception = raised
                try:
                    handled = False
                    for handler in statement.handlers:
                        expected = self._eval_expr(handler.exception_type)
                        if self._matches_exception(raised.value, expected, frame.context):
                            if handler.binding is not None:
                                frame.locals[handler.binding.name] = raised.value
                            self._execute_statements(handler.body, block_id)
                            handled = True
                            break
                    if not handled:
                        raise
                finally:
                    frame.active_exception = previous
        elif isinstance(statement, Unsupported):
            detail = statement.detail or statement.message
            self._unsupported(detail)
        elif isinstance(statement, Yield):
            raise _YieldSignal(self._eval_expr(statement.value))
        else:
            self._unsupported(f"unsupported statement {type(statement).__name__}")

    def _eval_expr(self, expr: Expr) -> Any:
        self._tick(expr.source)
        frame = self.frames[-1]
        if type(expr) is Expr:
            self._unsupported("unresolved expression")
        if isinstance(expr, Var):
            if expr.name not in frame.locals:
                self._unsupported(f"unknown local {expr.name!r}")
            return frame.locals[expr.name]
        if isinstance(expr, Const):
            validate_runtime_value(expr.value)
            return expr.value
        if isinstance(expr, Global):
            result = self._adapter_value("resolve_global", expr.name, frame.context)
            if result is NotHandled:
                if self.environment is None:
                    self._unsupported(
                        f"global resolution for {expr.name!r} requires a frontend simulation adapter"
                    )
                return ExternalFunction(expr.name)
            return result
        if isinstance(expr, CapturedVar):
            result = self._adapter_value("resolve_captured", expr.name, frame.context)
            if result is NotHandled:
                self._unsupported(
                    f"captured variable resolution for {expr.name!r} requires a frontend simulation adapter"
                )
            return result
        if isinstance(expr, MultiReturn):
            value = self._eval_expr(expr.value)
            if isinstance(value, _MultiReturnValue):
                return value
            if isinstance(value, tuple):
                return _MultiReturnValue(value)
            return _MultiReturnValue(self._expand_value(value))
        if isinstance(expr, Placeholder):
            self._unsupported(f"unresolved placeholder {expr.label!r}:{expr.token}")
        if isinstance(expr, UnaryOp):
            value = self._eval_expr(expr.value)
            op = expr.op.strip()
            if op in {"-", "neg"}:
                try:
                    return -value
                except (TypeError, ValueError):
                    pass
            if op in {"+", "pos"}:
                try:
                    return +value
                except (TypeError, ValueError):
                    pass
            if op in {"not", "!"}:
                return not self._truthy(value)
            if op in {"~", "bitnot"}:
                try:
                    return ~value
                except (TypeError, ValueError):
                    pass
            result = self._adapter_value("unary_op", op, value, frame.context)
            if result is NotHandled:
                self._unsupported(f"unhandled unary operation {op!r}")
            return result
        if isinstance(expr, BinaryOp):
            if expr.op in {"and", "&&"}:
                left = self._eval_expr(expr.left)
                if not self._truthy(left):
                    return left
                return self._eval_expr(expr.right)
            if expr.op in {"or", "||"}:
                left = self._eval_expr(expr.left)
                if self._truthy(left):
                    return left
                return self._eval_expr(expr.right)
            left = self._eval_expr(expr.left)
            right = self._eval_expr(expr.right)
            return self._binary(
                expr.op,
                left,
                right,
                frame.context,
                expr.semantics,
                expr.numeric_domain,
                expr.bit_width,
            )
        if isinstance(expr, Call):
            callee = self._eval_expr(expr.callee)
            args = tuple(self._eval_expr(arg) for arg in expr.args)
            keywords = tuple(
                (self._eval_expr(field.key), self._eval_expr(field.value))
                for field in expr.keywords
            )
            if not all(isinstance(key, str) for key, _ in keywords):
                self._unsupported("call keyword names must be strings")
            return self._invoke(callee, args, tuple(keywords), frame.context, expr.returns, expr.source)
        if isinstance(expr, IndirectCall):
            selector = self._eval_expr(expr.selector)
            return _IndirectTarget(selector, expr.signature, frame.context)
        if isinstance(expr, ArrayLiteral):
            return [self._eval_expr(item) for item in expr.items]
        if isinstance(expr, SetLiteral):
            return {self._eval_expr(item) for item in expr.items}
        if isinstance(expr, TableLiteral):
            return TableValue(
                [self._eval_expr(item) for item in expr.array_items],
                {
                    self._eval_expr(field.key): self._eval_expr(field.value)
                    for field in expr.fields
                },
            )
        if isinstance(expr, ObjectLiteral):
            return ObjectValue(
                "object",
                {
                    str(self._eval_expr(field.key)): self._eval_expr(field.value)
                    for field in expr.fields
                },
            )
        if isinstance(expr, MapLiteral):
            return {self._eval_expr(field.key): self._eval_expr(field.value) for field in expr.fields}
        if isinstance(expr, CollectionProjection):
            values = []
            for item in self._iter(self._eval_expr(expr.iterable)):
                frame.locals[expr.target.name] = item
                values.append(self._eval_expr(expr.value))
            return set(values) if expr.kind == "set" else values
        if isinstance(expr, NewObject):
            args = tuple(self._eval_expr(arg) for arg in expr.args)
            result = self._adapter_value("create_object", expr.type_name, args, frame.context)
            if result is NotHandled:
                return ObjectValue(expr.type_name)
            return result
        if isinstance(expr, GetAttr):
            return self._get_attr(self._eval_expr(expr.obj), expr.attr)
        if isinstance(expr, GetItem):
            return self._get_item(self._eval_expr(expr.obj), self._eval_expr(expr.key))
        if isinstance(expr, IndirectRef):
            return _Reference(expr.target)
        if isinstance(expr, Phi):
            if not expr.incoming:
                self._unsupported("empty phi expression")
            predecessor = frame.predecessor
            incoming = dict(expr.incoming)
            if predecessor in incoming:
                return self._eval_expr(incoming[predecessor])
            if len(incoming) == 1:
                return self._eval_expr(next(iter(incoming.values())))
            self._unsupported(f"phi has no incoming value for predecessor {predecessor!r}")
        self._unsupported(f"unsupported expression {type(expr).__name__}")

    def _invoke(
        self,
        callee: Any,
        args: tuple[Any, ...],
        keywords: tuple[tuple[str, Any], ...],
        context: object | None,
        returns: int | str,
        source: object | None = None,
    ) -> Any:
        if isinstance(callee, IntrinsicCall):
            if keywords:
                self._unsupported(f"intrinsic {callee.name!r} does not accept keyword arguments")
            return self._call_result(
                (self._invoke_intrinsic(callee.name, args, callee.bit_width),),
                returns,
            )
        if isinstance(callee, ExternalFunction):
            return self._invoke_external(callee.name, args, keywords, returns, source)
        if isinstance(callee, _IndirectTarget):
            result = call_adapter(
                self.adapter,
                "resolve_indirect_call",
                callee.selector,
                callee.signature,
                args,
                keywords,
                callee.context,
            )
            if not isinstance(result, ResolvedFunction):
                self._unsupported("unhandled indirect call")
            values = self._call(result.function, args, keywords, result.context)
            return self._call_result(values, returns)
        if isinstance(callee, ResolvedFunction):
            values = self._call(callee.function, args, keywords, callee.context)
            return self._call_result(values, returns)
        result = call_adapter(
            self.adapter,
            "resolve_call",
            CallRequest(callee=callee, args=args, keywords=keywords, context=context),
        )
        if isinstance(result, ResolvedFunction):
            values = self._call(result.function, args, keywords, result.context)
            return self._call_result(values, returns)
        if isinstance(callee, str) and self.environment is not None:
            return self._invoke_external(callee, args, keywords, returns, source)
        self._unsupported("unhandled call")

    def _invoke_external(
        self,
        name: str,
        args: tuple[Any, ...],
        keywords: tuple[tuple[str, Any], ...],
        returns: int | str,
        source: object | None,
    ) -> Any:
        frame = self.frames[-1]
        request = ExternalCallRequest(
            name=name,
            args=args,
            keywords=keywords,
            caller=frame.function.name,
            source=source,
        )
        try:
            result = call_environment(self.environment, request)
        except Exception as error:
            raise _SimulationStop(
                SimulationStatus.INVALID_REQUEST,
                f"external environment call failed: {type(error).__name__}: {error}",
            ) from error
        if result is NotHandled or result.status is ExternalCallStatus.NOT_HANDLED:
            self._unsupported(f"external environment did not handle function {name!r}")
        self._event(
            "external-call",
            frame.predecessor,
            detail=result.diagnostic or name,
            source=source,
            args=args,
            values=result.values,
            exception=result.exception,
            stdout=result.stdout,
            stderr=result.stderr,
        )
        if result.status is ExternalCallStatus.RAISED:
            raise _Raised(result.exception)
        return self._call_result(tuple(result.values), returns)

    def _call_result(self, values: tuple[Any, ...], returns: int | str) -> Any:
        if returns == 0:
            return None
        if returns == 1:
            return values[0] if values else None
        return _MultiReturnValue(values)

    def _invoke_intrinsic(self, name: str, args: tuple[Any, ...], bit_width: int | None = None) -> Any:
        try:
            if name == "neg":
                return -args[0]
            if name == "bitnot":
                return ~args[0]
            if name in {"is_zero", "is_null"}:
                return args[0] == (0 if name == "is_zero" else None)
            if name == "select":
                if len(args) != 3:
                    self._unsupported("select expects three arguments")
                return args[0] if self._truthy(args[2]) else args[1]
            if name == "range_continues":
                if len(args) != 3:
                    self._unsupported("range_continues expects three arguments")
                current, limit, step = args
                if step == 0:
                    self._unsupported("range_continues does not accept a zero step")
                return current <= limit if step > 0 else current >= limit
            if name == "range":
                if not 1 <= len(args) <= 3:
                    self._unsupported("range expects one to three arguments")
                return list(range(*args))
            if name == "iter_has_next":
                value = self._iterator_value(args, "iter_has_next")
                return self._iterator_positions.get(id(value), 0) < len(value)
            if name == "iter_next":
                value = self._iterator_value(args, "iter_next")
                position = self._iterator_positions.get(id(value), 0)
                if position >= len(value):
                    self._unsupported("iter_next called after iterator exhaustion")
                self._iterator_positions[id(value)] = position + 1
                return value[position]
            if name == "len":
                return len(args[0])
            if name == "bool":
                return self._truthy(args[0])
            if name == "int":
                return int(args[0])
            if name == "float":
                return float(args[0])
            if name == "str":
                return str(args[0])
            if name == "abs":
                return abs(args[0])
            if name == "sqrt":
                return math.sqrt(args[0])
            if name == "min":
                return min(args)
            if name == "max":
                return max(args)
            if name == "sum":
                return sum(args[0])
            if name == "slice":
                if not 1 <= len(args) <= 3:
                    self._unsupported("slice expects one to three arguments")
                values = list(args) + [None, None, None]
                return SliceValue(values[0], values[1], values[2])
            if name == "new_array":
                if len(args) != 1:
                    self._unsupported("new_array expects one argument")
                return [None] * int(args[0])
            if name in {"rotl", "rotr", "clz", "ctz", "popcnt"}:
                if bit_width is None:
                    self._unsupported(f"intrinsic {name!r} requires a bit width")
                if len(args) not in {1, 2} and name in {"rotl", "rotr"}:
                    self._unsupported(f"intrinsic {name!r} expects one or two arguments")
                if len(args) != 1 and name in {"clz", "ctz", "popcnt"}:
                    self._unsupported(f"intrinsic {name!r} expects one argument")
                mask = (1 << bit_width) - 1
                value = int(args[0]) & mask
                if name == "rotl" or name == "rotr":
                    amount = int(args[1]) & (bit_width - 1)
                    if name == "rotl":
                        return ((value << amount) | (value >> ((bit_width - amount) & (bit_width - 1)))) & mask
                    return ((value >> amount) | (value << ((bit_width - amount) & (bit_width - 1)))) & mask
                if name == "popcnt":
                    return value.bit_count()
                if name == "clz":
                    return bit_width if value == 0 else bit_width - value.bit_length()
                return bit_width if value == 0 else (value & -value).bit_length() - 1
        except (IndexError, TypeError, ValueError, OverflowError) as error:
            self._unsupported(f"invalid intrinsic {name!r}: {error}")
        self._unsupported(f"unsupported intrinsic {name!r}")

    def _binary(
        self,
        op: str,
        left: Any,
        right: Any,
        context: object | None,
        semantics: str,
        numeric_domain: str = "default",
        bit_width: int | None = None,
    ) -> Any:
        if semantics == "dynamic":
            result = self._adapter_value("binary_op", op, left, right, context)
            if result is not NotHandled:
                return result
        if numeric_domain not in {"default", "signed", "unsigned", "float"}:
            self._unsupported(f"unknown numeric domain {numeric_domain!r}")
        if numeric_domain in {"signed", "unsigned"}:
            if bit_width is None or bit_width <= 0:
                self._unsupported(
                    f"{numeric_domain} numeric operation requires a positive bit width"
                )
            if not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in (left, right)
            ):
                self._unsupported(
                    f"{numeric_domain} numeric operation requires integer operands"
                )
            mask = (1 << bit_width) - 1

            def normalize(value: int) -> int:
                value &= mask
                if numeric_domain == "signed" and value & (1 << (bit_width - 1)):
                    value -= 1 << bit_width
                return value

            left = normalize(left)
            right = normalize(right)
            if numeric_domain == "unsigned":
                left &= mask
                right &= mask
            if op in {"/", "%"}:
                if right == 0:
                    self._unsupported(f"division by zero in numeric binary operation {op!r}")
                if numeric_domain == "unsigned":
                    quotient = left // right
                else:
                    quotient = (abs(left) // abs(right)) * (
                        -1 if (left < 0) != (right < 0) else 1
                    )
                if op == "/":
                    return quotient & mask if numeric_domain == "unsigned" else normalize(quotient)
                remainder = left - quotient * right
                return remainder & mask if numeric_domain == "unsigned" else normalize(remainder)
            if op == ">>" and numeric_domain == "unsigned":
                return (left & mask) >> (right & (bit_width - 1))
            if op == ">>>" and numeric_domain == "unsigned":
                return (left & mask) >> (right & (bit_width - 1))
            numeric_operations = {
                "+": operator.add,
                "-": operator.sub,
                "*": operator.mul,
                "&": operator.and_,
                "|": operator.or_,
                "^": operator.xor,
                "<<": operator.lshift,
                "==": operator.eq,
                "!=": operator.ne,
                "<": operator.lt,
                "<=": operator.le,
                ">": operator.gt,
                ">=": operator.ge,
            }
            implementation = numeric_operations.get(op)
            if implementation is not None:
                try:
                    result = implementation(left, right)
                    if op in {"+", "-", "*", "&", "|", "^", "<<"}:
                        return normalize(result)
                    return result
                except (TypeError, ValueError):
                    self._unsupported(f"invalid numeric binary operation {op!r}")
        if numeric_domain == "float":
            if bit_width not in {32, 64, None}:
                self._unsupported(f"unsupported floating-point width {bit_width}")
            if bit_width == 32:
                left = _float32(left)
                right = _float32(right)
            if op == "%":
                try:
                    result = math.fmod(left, right)
                    return _float32(result) if bit_width == 32 else result
                except (TypeError, ValueError, ZeroDivisionError):
                    self._unsupported("invalid floating-point remainder")
            float_operations = {
                "+": operator.add,
                "-": operator.sub,
                "*": operator.mul,
                "/": operator.truediv,
                "==": operator.eq,
                "!=": operator.ne,
                "<": operator.lt,
                "<=": operator.le,
                ">": operator.gt,
                ">=": operator.ge,
            }
            implementation = float_operations.get(op)
            if implementation is not None:
                try:
                    result = implementation(left, right)
                    return _float32(result) if bit_width == 32 else result
                except (TypeError, ValueError, ZeroDivisionError):
                    self._unsupported(f"invalid floating-point operation {op!r}")
        if (
            semantics == "static"
            and op in {"/", "%"}
            and isinstance(left, int)
            and not isinstance(left, bool)
            and isinstance(right, int)
            and not isinstance(right, bool)
        ):
            if right == 0:
                self._unsupported(f"division by zero in static binary operation {op!r}")
            quotient = (abs(left) // abs(right)) * (-1 if (left < 0) != (right < 0) else 1)
            if op == "/":
                return quotient
            return left - quotient * right
        if numeric_domain == "float" and op == "%":
            try:
                return math.fmod(left, right)
            except (TypeError, ValueError, ZeroDivisionError):
                if semantics == "static":
                    self._unsupported(
                        f"invalid static binary operation {op!r} for "
                        f"{type(left).__name__} and {type(right).__name__}"
                    )
        operations = {
            "+": operator.add, "-": operator.sub, "*": operator.mul, "/": operator.truediv,
            "//": operator.floordiv, "%": operator.mod, "**": operator.pow,
            "==": operator.eq, "!=": operator.ne, "<": operator.lt, "<=": operator.le,
            ">": operator.gt, ">=": operator.ge, "&": operator.and_, "|": operator.or_,
            "^": operator.xor, "<<": operator.lshift, ">>": operator.rshift,
            "is": operator.is_, "is not": operator.is_not,
            "in": lambda a, b: a in b, "not in": lambda a, b: a not in b,
        }
        implementation = operations.get(op)
        if implementation is not None:
            try:
                return implementation(left, right)
            except (TypeError, ValueError, ZeroDivisionError):
                if semantics == "static":
                    self._unsupported(
                        f"invalid static binary operation {op!r} for "
                        f"{type(left).__name__} and {type(right).__name__}"
                    )
        if semantics == "static":
            self._unsupported(f"unsupported static binary operation {op!r}")
        result = self._adapter_value("binary_op", op, left, right, context)
        if result is NotHandled:
            self._unsupported(f"unhandled binary operation {op!r}")
        return result

    def _truthy(self, value: Any) -> bool:
        result = call_adapter(self.adapter, "truthy", value, self.frames[-1].context)
        if result is not NotHandled:
            if not isinstance(result, bool):
                raise TypeError("adapter truthy operation must return bool or NotHandled")
            return result
        return bool(value)

    def _get_attr(self, obj: Any, attr: str) -> Any:
        if isinstance(obj, ObjectValue):
            if attr in obj.fields:
                return obj.fields[attr]
        elif isinstance(obj, dict) and attr in obj:
            return obj[attr]
        result = self._adapter_value("get_attr", obj, attr, self.frames[-1].context)
        if result is NotHandled:
            self._unsupported(f"unhandled attribute read {attr!r}")
        return result

    def _store_attr(self, obj: Any, attr: str, value: Any) -> None:
        if isinstance(obj, ObjectValue):
            obj.fields[attr] = value
            return
        if isinstance(obj, dict):
            obj[attr] = value
            return
        result = call_adapter(self.adapter, "set_attr", obj, attr, value, self.frames[-1].context)
        if result is NotHandled:
            self._unsupported(f"unhandled attribute write {attr!r}")

    def _get_item(self, obj: Any, key: Any) -> Any:
        if isinstance(key, SliceValue):
            if isinstance(obj, (str, bytes, list, tuple)):
                return obj[slice(key.start, key.stop, key.step)]
            self._unsupported("slice indexing requires a sequence")
        result = self._adapter_value("get_item", obj, key, self.frames[-1].context)
        if result is not NotHandled:
            return result
        if isinstance(obj, TableValue):
            if isinstance(key, int) and 1 <= key <= len(obj.array_items):
                return obj.array_items[key - 1]
            if key in obj.fields:
                return obj.fields[key]
        try:
            return obj[key]
        except (KeyError, IndexError, TypeError):
            self._unsupported(f"unhandled item read {key!r}")

    def _store_item(self, obj: Any, key: Any, value: Any) -> None:
        result = call_adapter(self.adapter, "set_item", obj, key, value, self.frames[-1].context)
        if result is not NotHandled:
            return
        if isinstance(obj, TableValue):
            if isinstance(key, int) and key >= 1:
                while len(obj.array_items) < key:
                    obj.array_items.append(None)
                obj.array_items[key - 1] = value
            else:
                obj.fields[key] = value
            return
        try:
            obj[key] = value
            return
        except (KeyError, IndexError, TypeError):
            self._unsupported(f"unhandled item write {key!r}")

    def _iter(self, value: Any):
        result = self._adapter_value("iterate", value, self.frames[-1].context)
        if result is not NotHandled:
            if not isinstance(result, (str, bytes, list, tuple, set, dict)):
                self._unsupported("adapter iterator result is not an in-memory iterable")
            return result
        if isinstance(value, TableValue):
            return value.array_items if value.array_items else value.fields
        if isinstance(value, (str, bytes, list, tuple, set, dict)):
            return value
        raise _SimulationStop(SimulationStatus.UNSUPPORTED, "value is not iterable")

    def _iterator_value(self, args: tuple[Any, ...], name: str) -> list[Any] | tuple[Any, ...] | str | bytes:
        if len(args) != 1 or not isinstance(args[0], (list, tuple, str, bytes)):
            self._unsupported(f"{name} expects an in-memory sequence")
        return args[0]

    def _assign(self, target: Expr, value: Any) -> None:
        if isinstance(target, Var):
            self.frames[-1].locals[target.name] = value
            return
        if isinstance(target, CapturedVar):
            result = call_adapter(
                self.adapter,
                "set_captured",
                target.name,
                value,
                self.frames[-1].context,
            )
            if result is NotHandled:
                self._unsupported(
                    f"captured variable assignment for {target.name!r} requires a frontend simulation adapter"
                )
            return
        if isinstance(target, IndirectRef):
            self._assign(target.target, value)
            return
        if isinstance(target, GetAttr):
            self._store_attr(self._eval_expr(target.obj), target.attr, value)
            return
        if isinstance(target, GetItem):
            self._store_item(self._eval_expr(target.obj), self._eval_expr(target.key), value)
            return
        self._unsupported(f"unsupported assignment target {type(target).__name__}")

    def _all_functions(self) -> tuple[FunctionIR, ...]:
        functions: list[FunctionIR] = []

        def walk(items: tuple[FunctionIR, ...]) -> None:
            for function in items:
                functions.append(function)
                walk(function.nested_functions)

        walk(self.module.functions)
        return tuple(functions)

    @staticmethod
    def _bind_args(
        function: FunctionIR,
        args: tuple[Any, ...],
        keywords: tuple[tuple[str, Any], ...],
    ) -> dict[str, Any]:
        if len(args) > len(function.params):
            raise _SimulationStop(
                SimulationStatus.INVALID_REQUEST,
                f"function {function.name!r} received too many positional arguments",
            )
        bound: dict[str, Any] = dict(zip(function.params, args, strict=False))
        for name, value in keywords:
            if name not in function.params:
                raise _SimulationStop(
                    SimulationStatus.INVALID_REQUEST,
                    f"function {function.name!r} has no parameter {name!r}",
                )
            if name in bound:
                raise _SimulationStop(
                    SimulationStatus.INVALID_REQUEST,
                    f"function {function.name!r} received duplicate argument {name!r}",
                )
            bound[name] = value
        missing = [name for name in function.params if name not in bound]
        if missing:
            raise _SimulationStop(
                SimulationStatus.INVALID_REQUEST,
                f"function {function.name!r} is missing arguments: {', '.join(missing)}",
            )
        return bound

    def _adapter_value(self, name: str, *args: Any) -> object:
        result = call_adapter(self.adapter, name, *args)
        if result is not NotHandled and not isinstance(result, (ResolvedFunction, IntrinsicCall, _IndirectTarget)):
            validate_runtime_value(result)
        return result

    def _eval_values(self, expressions: tuple[Expr, ...]) -> tuple[Any, ...]:
        values: list[Any] = []
        for expression in expressions:
            values.extend(self._expand_value(self._eval_expr(expression)))
        return tuple(values)

    @staticmethod
    def _expand_value(value: Any) -> tuple[Any, ...]:
        if isinstance(value, _MultiReturnValue):
            return value.values
        return (value,)

    def _matches_exception(self, value: Any, expected: Any, context: object | None) -> bool:
        result = self._adapter_value("matches_exception", value, expected, context)
        if result is not NotHandled:
            if not isinstance(result, bool):
                raise TypeError("adapter exception matcher must return bool or NotHandled")
            return result
        if value == expected:
            return True
        if isinstance(expected, str):
            return expected in {type(value).__name__, type(value).__qualname__}
        return False

    def _tick(self, source: object | None) -> None:
        if self.cancellation is not None and self.cancellation.cancelled:
            raise _SimulationStop(SimulationStatus.CANCELLED, "simulation cancelled")
        self.steps += 1
        if self.steps > self.limits.max_steps:
            raise _SimulationStop(SimulationStatus.STEP_LIMIT, "maximum simulation steps exceeded")

    def _event(
        self,
        kind: str,
        block: str | None,
        detail: str = "",
        source: object | None = None,
        *,
        args: tuple[Any, ...] = (),
        values: tuple[Any, ...] = (),
        exception: Any | None = None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        frame = self.frames[-1]
        if self.trace_truncated:
            return
        if len(self.events) >= self.limits.max_trace_events - 1:
            self.trace_truncated = True
            self.events.append(
                SimulationEvent(
                    function=frame.function.name,
                    function_index=self._function_indexes.get(id(frame.function), -1),
                    block=block,
                    kind="trace-truncated",
                    detail=f"trace limited to {self.limits.max_trace_events} events",
                    source=source,
                    locals=snapshot_value(frame.locals),
                )
            )
            return
        self.events.append(
            SimulationEvent(
                function=frame.function.name,
                function_index=self._function_indexes.get(id(frame.function), -1),
                block=block,
                kind=kind,
                detail=detail,
                source=source,
                locals=snapshot_value(frame.locals),
                args=tuple(snapshot_value(value) for value in args),
                values=tuple(snapshot_value(value) for value in values),
                exception=snapshot_value(exception),
                stdout=stdout,
                stderr=stderr,
            )
        )

    def _unsupported(self, message: str) -> None:
        raise _SimulationStop(SimulationStatus.UNSUPPORTED, message)


def _float32(value: Any) -> float:
    return struct.unpack("!f", struct.pack("!f", float(value)))[0]
