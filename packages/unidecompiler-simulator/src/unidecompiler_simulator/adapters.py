from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from unidecompiler.core.ir import FunctionIR, ModuleIR
from unidecompiler.plugins import FrontendModule


class _NotHandled:
    def __repr__(self) -> str:
        return "NotHandled"


NotHandled = _NotHandled()


@dataclass(frozen=True)
class CallRequest:
    """Data-only call request passed to an optional frontend adapter."""

    callee: object
    args: tuple[object, ...]
    keywords: tuple[tuple[str, object], ...] = ()
    context: object | None = None


@dataclass(frozen=True)
class SimulationTargetCandidate:
    """A frontend-owned lookup query suitable for presenting to a host."""

    label: str
    query: object

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("simulation target label must not be empty")
        if callable(self.query):
            raise TypeError("simulation target query cannot be executable")


@dataclass(frozen=True)
class SimulationTarget:
    """A verified generic function target with an opaque frontend query."""

    label: str
    query: object
    function_index: int
    params: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.label:
            raise ValueError("simulation target label must not be empty")
        if self.function_index < 0:
            raise ValueError("simulation target function_index must be non-negative")
        if not all(isinstance(param, str) for param in self.params):
            raise TypeError("simulation target parameters must be strings")
        if callable(self.query):
            raise TypeError("simulation target query cannot be executable")


@dataclass(frozen=True)
class IntrinsicCall:
    """A data-only request for a simulator-owned pure runtime operation."""

    name: str
    bit_width: int | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("intrinsic name must not be empty")
        if self.bit_width is not None and self.bit_width <= 0:
            raise ValueError("intrinsic bit_width must be positive")


@dataclass(frozen=True)
class ResolvedFunction:
    """A frontend-selected generic function plus an opaque identity token.

    The simulator validates that ``function`` belongs to the lifted module and
    owns all execution. ``context`` is only passed back to adapter hooks; it is
    never called, interpreted, or used as control flow by the simulator.
    """

    function: FunctionIR
    context: object | None = None
    identifier: str = ""

    def __post_init__(self) -> None:
        if callable(self.context):
            raise TypeError("resolved function context cannot be executable")


@runtime_checkable
class SimulationAdapter(Protocol):
    """Optional frontend runtime facts, never a frontend execution engine.

    Implementations may define any subset of the operation methods. Missing
    methods are treated as ``NotHandled``. They must not expose execute/run/
    step/eval methods or return executable callbacks.
    """

    frontend_id: str

    def resolve_function(
        self,
        query: object,
        decoded_module: FrontendModule,
        lifted_module: ModuleIR,
    ) -> ResolvedFunction | _NotHandled: ...

    def list_simulation_targets(
        self,
        decoded_module: FrontendModule,
        lifted_module: ModuleIR,
    ) -> tuple[SimulationTargetCandidate, ...] | _NotHandled: ...


def adapter_for(plugin: object) -> SimulationAdapter | None:
    adapter = getattr(plugin, "simulation_adapter", None)
    if adapter is None:
        return None
    if isinstance(adapter, type):
        adapter = adapter()
    frontend_id = getattr(adapter, "frontend_id", None)
    if not isinstance(frontend_id, str) or not frontend_id:
        raise TypeError("simulation_adapter must declare a non-empty frontend_id")
    if not callable(getattr(adapter, "resolve_function", None)):
        raise TypeError("simulation_adapter must provide resolve_function")
    forbidden = {"execute_function", "run", "step", "eval", "interpret", "next_instruction"}
    exposed = forbidden.intersection(name for name in dir(adapter) if not name.startswith("__"))
    if exposed:
        names = ", ".join(sorted(exposed))
        raise TypeError(f"simulation_adapter exposes forbidden execution methods: {names}")
    return adapter  # type: ignore[return-value]


def call_adapter(adapter: object | None, name: str, *args: Any) -> object:
    if adapter is None:
        return NotHandled
    method = getattr(adapter, name, None)
    if method is None:
        return NotHandled
    result = method(*args)
    if callable(result) and not isinstance(result, ResolvedFunction):
        raise TypeError(f"adapter operation {name!r} returned an executable callback")
    return result
