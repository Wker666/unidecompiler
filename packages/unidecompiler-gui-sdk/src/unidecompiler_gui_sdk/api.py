"""Versioned data contracts shared by GUI plugins and the GUI host."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


API_VERSION = "1"


@dataclass(frozen=True)
class SourceLocation:
    frontend: str
    offset: int | None
    line: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class FunctionSnapshot:
    id: str
    name: str
    status: str
    params: tuple[str, ...] = ()
    source: SourceLocation | None = None


@dataclass(frozen=True)
class AstNodeSnapshot:
    id: str
    kind: str
    source: SourceLocation | None = None
    children: tuple["AstNodeSnapshot", ...] = ()


@dataclass(frozen=True)
class ReferenceSnapshot:
    kind: str
    name: str
    function_id: str | None
    source: SourceLocation | None = None
    target_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentSnapshot:
    id: str
    display_name: str
    status: str
    frontend_id: str | None
    revision: int
    functions: tuple[FunctionSnapshot, ...] = ()
    pseudocode: str = ""


@dataclass(frozen=True)
class SelectionSnapshot:
    document_id: str | None = None
    function_id: str | None = None
    source: SourceLocation | None = None


@dataclass(frozen=True)
class SimulationEventSnapshot:
    kind: str
    function: str
    block: str | None = None
    detail: str = ""
    source: SourceLocation | None = None
    args: tuple[Any, ...] = ()
    values: tuple[Any, ...] = ()
    exception: str | None = None
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class SimulationResultSnapshot:
    status: str
    values: tuple[Any, ...] = ()
    exception: str | None = None
    cause: str | None = None
    locals: tuple[tuple[str, Any], ...] = ()
    steps: int = 0
    diagnostic: str | None = None
    events: tuple[SimulationEventSnapshot, ...] = ()
    trace_truncated: bool = False


@dataclass(frozen=True)
class SimulationTargetSnapshot:
    """Presentation-safe simulator target with a frontend-owned opaque query."""

    label: str
    query: object
    params: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimulationTargetJobSnapshot:
    id: str
    document_id: str
    revision: int
    status: str
    targets: tuple[SimulationTargetSnapshot, ...] = ()
    diagnostic: str | None = None
    stale: bool = False


@dataclass(frozen=True)
class SimulationJobSnapshot:
    id: str
    document_id: str
    revision: int
    status: str
    result: SimulationResultSnapshot | None = None
    diagnostic: str | None = None
    stale: bool = False


@dataclass(frozen=True)
class PanelState:
    kind: str
    columns: tuple[str, ...] = ()
    rows: tuple[tuple[str, ...], ...] = ()
    text: str = ""

    @classmethod
    def text_view(cls, text: str) -> "PanelState":
        return cls("text", text=text)

    @classmethod
    def table(cls, columns: tuple[str, ...], rows: tuple[tuple[object, ...], ...]) -> "PanelState":
        return cls("table", columns=columns, rows=tuple(tuple(str(value) for value in row) for row in rows))


CommandCallback = Callable[["PluginContext"], None]


@dataclass(frozen=True)
class Command:
    id: str
    title: str
    callback: CommandCallback
    shortcut: str | None = None


@dataclass(frozen=True)
class Panel:
    id: str
    title: str
    state: PanelState = field(default_factory=lambda: PanelState.text_view(""))


class CommandRegistrar(Protocol):
    def register(self, command: Command) -> None: ...


class PanelRegistrar(Protocol):
    def register(self, panel: Panel) -> None: ...


class PluginSettings(Protocol):
    """JSON-compatible settings isolated to the current plugin ID."""

    def get(self, key: str, default: Any = None) -> Any: ...
    def set(self, key: str, value: Any) -> None: ...
    def delete(self, key: str) -> None: ...


class PluginContext(Protocol):
    @property
    def documents(self) -> tuple[DocumentSnapshot, ...]: ...

    @property
    def active_document(self) -> DocumentSnapshot | None: ...

    @property
    def selection(self) -> SelectionSnapshot: ...

    def get_document(self, document_id: str) -> DocumentSnapshot | None: ...
    def get_function(self, document_id: str, function_id: str) -> FunctionSnapshot | None: ...
    def get_ast(self, document_id: str, function_id: str | None = None) -> AstNodeSnapshot | None: ...
    def get_references(self, document_id: str, function_id: str | None = None) -> tuple[ReferenceSnapshot, ...]: ...
    def focus_source(self, document_id: str, source: SourceLocation) -> bool: ...
    def focus_function(self, document_id: str, function_id: str) -> bool: ...
    def request_simulation_targets(self, document_id: str) -> SimulationTargetJobSnapshot: ...
    def get_target_job(self, job_id: str) -> SimulationTargetJobSnapshot | None: ...
    def submit_simulation(self, document_id: str, query: object, args: tuple[object, ...] = (), runtime_path: str | None = None) -> SimulationJobSnapshot: ...
    def get_job(self, job_id: str) -> SimulationJobSnapshot | None: ...
    def cancel_job(self, job_id: str) -> bool: ...
    def set_panel_state(self, panel_id: str, state: PanelState) -> None: ...
    def subscribe(self, event: str, callback: Callable[[object], None]) -> Callable[[], None]: ...

    @property
    def commands(self) -> CommandRegistrar: ...

    @property
    def panels(self) -> PanelRegistrar: ...

    @property
    def settings(self) -> PluginSettings: ...
