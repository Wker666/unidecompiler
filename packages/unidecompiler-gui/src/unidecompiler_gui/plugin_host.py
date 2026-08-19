"""GUI-side host for trusted plugins; no plugin API leaks into core."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
import json
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from unidecompiler import DecompileResult
from unidecompiler.input_sources import load_input_entry
from unidecompiler_gui_sdk import (
    AstNodeSnapshot, Command, DocumentSnapshot, FunctionSnapshot, Panel,
    PanelState, ReferenceSnapshot, SelectionSnapshot, SimulationJobSnapshot,
    SimulationEventSnapshot, SimulationResultSnapshot, SimulationTargetJobSnapshot,
    SimulationTargetSnapshot, SourceLocation,
)
from unidecompiler_simulator import SimulationCancellation, SimulationEngine
from unidecompiler_simulation_host_python import PythonFileEnvironment

from PySide6.QtCore import QObject, QSettings, QThread, Qt, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QDockWidget, QPlainTextEdit, QTableWidget, QTableWidgetItem


def _source(value: object | None) -> SourceLocation | None:
    if value is None:
        return None
    frontend = getattr(value, "frontend", None)
    if not isinstance(frontend, str):
        return None
    line = getattr(value, "line", None)
    return SourceLocation(frontend, getattr(value, "offset", None), line if isinstance(line, int) else None, getattr(value, "detail", None))


def _ast_snapshot(value: object, prefix: str = "root") -> AstNodeSnapshot:
    children: list[AstNodeSnapshot] = []
    if is_dataclass(value):
        for field in fields(value):
            item = getattr(value, field.name)
            if is_dataclass(item):
                children.append(_ast_snapshot(item, f"{prefix}.{field.name}"))
            elif isinstance(item, tuple):
                children.extend(
                    _ast_snapshot(child, f"{prefix}.{field.name}.{index}")
                    for index, child in enumerate(item)
                    if is_dataclass(child)
                )
    return AstNodeSnapshot(prefix, type(value).__name__, _source(getattr(value, "source", None)), tuple(children))


class _SimulationWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, job_id: str, engine, entry, query: object, args: tuple[object, ...], runtime_path: str | None, cancellation: SimulationCancellation) -> None:
        super().__init__()
        self.job_id = job_id
        self.engine = engine
        self.entry = entry
        self.query = query
        self.args = args
        self.runtime_path = runtime_path
        self.cancellation = cancellation

    @Slot()
    def run(self) -> None:
        try:
            artifact = load_input_entry(self.entry)
            environment = PythonFileEnvironment.load(Path(self.runtime_path)) if self.runtime_path else None
            result = SimulationEngine.from_registry(self.engine.registry).simulate_artifact(
                artifact.data, artifact.display_path, self.query, args=self.args,
                environment=environment, cancellation=self.cancellation,
            )
            self.completed.emit(self.job_id, result)
        except Exception as error:
            self.failed.emit(self.job_id, f"{type(error).__name__}: {error}")


class _SimulationTargetWorker(QObject):
    completed = Signal(str, object)
    failed = Signal(str, str)

    def __init__(self, job_id: str, engine, entry) -> None:
        super().__init__()
        self.job_id = job_id
        self.engine = engine
        self.entry = entry

    @Slot()
    def run(self) -> None:
        try:
            artifact = load_input_entry(self.entry)
            listing = SimulationEngine.from_registry(self.engine.registry).list_artifact_targets(
                artifact.data, artifact.display_path,
            )
            self.completed.emit(self.job_id, listing)
        except Exception as error:
            self.failed.emit(self.job_id, f"{type(error).__name__}: {error}")


class _Registrar:
    def __init__(self, callback: Callable[[object], None]) -> None:
        self._callback = callback

    def register(self, value: object) -> None:
        self._callback(value)


class _PluginSettings:
    def __init__(self, settings, plugin_id: str) -> None:
        self._settings = settings
        self._prefix = f"plugins/{plugin_id}/"

    def get(self, key: str, default: object = None) -> object:
        self._key(key)
        raw = self._settings.value(self._prefix + key, None)
        if not isinstance(raw, str):
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set(self, key: str, value: object) -> None:
        self._key(key)
        try:
            encoded = json.dumps(value)
        except (TypeError, ValueError) as error:
            raise ValueError("plugin settings values must be JSON-compatible") from error
        self._settings.setValue(self._prefix + key, encoded)

    def delete(self, key: str) -> None:
        self._key(key)
        self._settings.remove(self._prefix + key)

    @staticmethod
    def _key(key: str) -> None:
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise ValueError("plugin settings key must be a relative non-empty path")


class _PluginContext:
    """Plugin-scoped SDK view; it never exposes the Workbench itself."""

    def __init__(self, host: "PluginHost", plugin_id: str) -> None:
        self._host = host
        self.plugin_id = plugin_id
        self.settings = _PluginSettings(host._settings, plugin_id)
        self.commands = _Registrar(lambda command: host.register_command(command, plugin_id))
        self.panels = _Registrar(lambda panel: host.register_panel(panel, plugin_id))

    @property
    def documents(self): return self._host.documents
    @property
    def active_document(self): return self._host.active_document
    @property
    def selection(self): return self._host.selection
    def get_document(self, document_id): return self._host.get_document(document_id)
    def get_function(self, document_id, function_id): return self._host.get_function(document_id, function_id)
    def get_ast(self, document_id, function_id=None): return self._host.get_ast(document_id, function_id)
    def get_references(self, document_id, function_id=None): return self._host.get_references(document_id, function_id)
    def focus_source(self, document_id, source): return self._host.focus_source(document_id, source)
    def focus_function(self, document_id, function_id): return self._host.focus_function(document_id, function_id)
    def submit_simulation(self, document_id, query, args=(), runtime_path=None):
        return self._host.submit_simulation(document_id, query, args, runtime_path)
    def request_simulation_targets(self, document_id):
        return self._host.request_simulation_targets(document_id)
    def get_target_job(self, job_id): return self._host.get_target_job(job_id)
    def get_job(self, job_id): return self._host.get_job(job_id)
    def cancel_job(self, job_id): return self._host.cancel_job(job_id)
    def set_panel_state(self, panel_id, state):
        return self._host.set_panel_state(f"{self.plugin_id}.{panel_id}", state)
    def subscribe(self, event, callback): return self._host.subscribe(event, callback)


class PluginHost(QObject):
    """Adapter over Workbench public presentation state for SDK plugins."""

    def __init__(self, workbench: Any, menu) -> None:
        super().__init__(workbench)
        self._workbench = workbench
        self._settings = getattr(workbench, "_settings", QSettings("unidecompiler", "unidecompiler-gui"))
        self._menu = menu
        self._commands: dict[str, QAction] = {}
        self._panels: dict[str, tuple[QDockWidget, object]] = {}
        self._subscriptions: dict[str, list[Callable[[], None]]] = {}
        self._events: dict[str, list[Callable[[object], None]]] = {}
        self._jobs: dict[str, SimulationJobSnapshot] = {}
        self._target_jobs: dict[str, SimulationTargetJobSnapshot] = {}
        self._job_threads: dict[str, QThread] = {}
        self._job_workers: dict[str, QObject] = {}
        self._job_cancellations: dict[str, SimulationCancellation] = {}
        self.commands = _Registrar(self.register_command)
        self.panels = _Registrar(self.register_panel)

    def for_plugin(self, plugin_id: str) -> _PluginContext:
        return _PluginContext(self, plugin_id)

    @property
    def documents(self) -> tuple[DocumentSnapshot, ...]:
        return tuple(self._document(result) for result in self._workbench.results)

    @property
    def active_document(self) -> DocumentSnapshot | None:
        result = self._workbench._selected_result()
        return None if result is None else self._document(result)

    @property
    def selection(self) -> SelectionSnapshot:
        result = self._workbench._selected_result()
        if result is None:
            return SelectionSnapshot()
        item = self._workbench.input_tree.currentItem()
        data = None if item is None else item.data(0, Qt.ItemDataRole.UserRole)
        function_id = data[1] if isinstance(data, tuple) else None
        return SelectionSnapshot(result.display_path, function_id)

    def _document(self, result: DecompileResult) -> DocumentSnapshot:
        return DocumentSnapshot(
            result.display_path, Path(result.display_path.partition("!")[0]).name,
            result.status, result.frontend_id, self._revision(result.display_path),
            tuple(
                FunctionSnapshot(
                    item.id, item.name, item.status,
                    self._function_params(result, item.id), _source(item.source),
                )
                for item in result.functions
            ),
            "" if result.pseudocode is None else result.pseudocode.text,
        )

    def _revision(self, document_id: str) -> int:
        return self._workbench._document_revisions.get(document_id, 0)

    def _result(self, document_id: str) -> DecompileResult | None:
        return next((result for result in self._workbench.results if result.display_path == document_id), None)

    def get_document(self, document_id: str) -> DocumentSnapshot | None:
        result = self._result(document_id)
        return None if result is None else self._document(result)

    def get_function(self, document_id: str, function_id: str) -> FunctionSnapshot | None:
        result = self._result(document_id)
        if result is None:
            return None
        item = next((item for item in result.functions if item.id == function_id), None)
        return None if item is None else FunctionSnapshot(
            item.id, item.name, item.status, self._function_params(result, item.id),
            _source(item.source),
        )

    def get_ast(self, document_id: str, function_id: str | None = None) -> AstNodeSnapshot | None:
        result = self._result(document_id)
        if result is None or result.ast is None:
            return None
        if function_id is None:
            return _ast_snapshot(result.ast, "module")
        function = self._function_ast(result, function_id)
        return None if function is None else _ast_snapshot(function, f"function:{function_id}")

    @staticmethod
    def _walk_ast_functions(value: object):
        for function in getattr(value, "functions", ()):
            yield function
            yield from PluginHost._walk_ast_functions(function)
        for function in getattr(value, "nested_functions", ()):
            yield function
            yield from PluginHost._walk_ast_functions(function)

    def _function_ast(self, result: DecompileResult, function_id: str) -> object | None:
        target = next((item for item in result.functions if item.id == function_id), None)
        if target is None or result.ast is None:
            return None
        functions = tuple(self._walk_ast_functions(result.ast))
        source_matches = [
            function for function in functions
            if _source(getattr(function, "source", None)) == _source(target.source)
        ]
        if len(source_matches) == 1:
            return source_matches[0]
        name_matches = [function for function in functions if getattr(function, "name", None) == target.name]
        return name_matches[0] if len(name_matches) == 1 else None

    def _function_params(self, result: DecompileResult, function_id: str) -> tuple[str, ...]:
        function = self._function_ast(result, function_id)
        params = getattr(function, "params", ())
        return tuple(param for param in params if isinstance(param, str))

    def get_references(self, document_id: str, function_id: str | None = None) -> tuple[ReferenceSnapshot, ...]:
        result = self._result(document_id)
        if result is None:
            return ()
        return tuple(
            ReferenceSnapshot(item.kind, item.name, item.function_id, _source(item.source), tuple(item.target_ids))
            for item in result.symbols.references
            if function_id is None or item.function_id == function_id
        )

    def focus_source(self, document_id: str, source: SourceLocation) -> bool:
        result = self._result(document_id)
        if result is None:
            return False
        if result.pseudocode is None:
            return False
        candidates = [
            item for item in result.pseudocode.source_map
            if item.function_id is not None
            and item.source.frontend == source.frontend
            and isinstance(item.source.offset, int)
            and isinstance(source.offset, int)
        ]
        if not candidates:
            return False
        mapping = min(candidates, key=lambda item: abs(item.source.offset - source.offset))
        function_id = mapping.function_id
        if function_id is None:
            return False
        self._workbench._navigate_to_function(function_id, result)
        self._workbench.detail_tabs.setCurrentWidget(self._workbench.pseudocode)
        self._workbench._focus_source(result, source, function_id)
        return True

    def focus_function(self, document_id: str, function_id: str) -> bool:
        result = self._result(document_id)
        if result is None or self.get_function(document_id, function_id) is None:
            return False
        self._workbench._navigate_to_function(function_id, result)
        self._workbench.detail_tabs.setCurrentWidget(self._workbench.pseudocode)
        return True

    def register_command(self, command: Command, plugin_id: str | None = None) -> None:
        command_id = self._qualified_id(plugin_id, command.id)
        if command_id in self._commands:
            raise ValueError(f"duplicate GUI plugin command {command.id!r}")
        action = QAction(command.title, self._workbench)
        if command.shortcut:
            action.setShortcut(QKeySequence(command.shortcut))
        context = self.for_plugin(plugin_id) if plugin_id else self
        action.triggered.connect(lambda: self._invoke_command(command, context))
        self._menu.addAction(action)
        self._commands[command_id] = action

    def _invoke_command(self, command: Command, context: object) -> None:
        try:
            command.callback(context)  # type: ignore[arg-type]
        except Exception as error:
            self._workbench.statusBar().showMessage(
                f"Plugin command {command.id!r} failed: {type(error).__name__}: {error}"
            )

    @staticmethod
    def _qualified_id(plugin_id: str | None, local_id: str) -> str:
        return local_id if plugin_id is None else f"{plugin_id}.{local_id}"

    def register_panel(self, panel: Panel, plugin_id: str | None = None) -> None:
        panel_id = self._qualified_id(plugin_id, panel.id)
        if panel_id in self._panels:
            raise ValueError(f"duplicate GUI plugin panel {panel.id!r}")
        dock = QDockWidget(panel.title, self._workbench)
        widget = self._render(panel.state)
        dock.setWidget(widget)
        self._workbench.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        self._panels[panel_id] = (dock, widget)

    def set_panel_state(self, panel_id: str, state: PanelState) -> None:
        panel = self._panels.get(panel_id)
        if panel is None:
            raise KeyError(panel_id)
        dock, previous = panel
        previous.deleteLater()
        widget = self._render(state)
        dock.setWidget(widget)
        self._panels[panel_id] = (dock, widget)

    def _render(self, state: PanelState):
        if state.kind == "table":
            table = QTableWidget(len(state.rows), len(state.columns))
            table.setHorizontalHeaderLabels(list(state.columns))
            for row, values in enumerate(state.rows):
                for column, value in enumerate(values):
                    table.setItem(row, column, QTableWidgetItem(value))
            return table
        text = QPlainTextEdit()
        text.setReadOnly(True)
        text.setPlainText(state.text)
        return text

    def document_updated(self, document_id: str) -> None:
        self.emit("document_updated", self.get_document(document_id))

    def document_selected(self) -> None:
        self.emit("document_selected", self.active_document)

    def subscribe(self, event: str, callback: Callable[[object], None]) -> Callable[[], None]:
        callbacks = self._events.setdefault(event, [])
        callbacks.append(callback)
        return lambda: callbacks.remove(callback) if callback in callbacks else None

    def emit(self, event: str, payload: object) -> None:
        for callback in tuple(self._events.get(event, ())):
            try:
                callback(payload)
            except Exception as error:
                self._workbench.statusBar().showMessage(f"Plugin event failed: {type(error).__name__}: {error}")

    def submit_simulation(self, document_id: str, query: object, args: tuple[object, ...] = (), runtime_path: str | None = None) -> SimulationJobSnapshot:
        result = self._result(document_id)
        entry = self._workbench._entry_for_display_path(document_id)
        if result is None or entry is None:
            return SimulationJobSnapshot("", document_id, 0, "invalid_request", diagnostic="document is unavailable")
        job_id = str(uuid4())
        cancellation = SimulationCancellation()
        snapshot = SimulationJobSnapshot(job_id, document_id, self._revision(document_id), "running")
        self._jobs[job_id] = snapshot
        worker = _SimulationWorker(job_id, self._workbench.engine, entry, query, args, runtime_path, cancellation)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._simulation_completed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(self._simulation_failed)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._job_threads[job_id] = thread
        self._job_workers[job_id] = worker
        self._job_cancellations[job_id] = cancellation
        self.emit("simulation_started", snapshot)
        return snapshot

    def request_simulation_targets(self, document_id: str) -> SimulationTargetJobSnapshot:
        result = self._result(document_id)
        entry = self._workbench._entry_for_display_path(document_id)
        if result is None or entry is None:
            return SimulationTargetJobSnapshot("", document_id, 0, "invalid_request", diagnostic="document is unavailable")
        job_id = str(uuid4())
        snapshot = SimulationTargetJobSnapshot(job_id, document_id, self._revision(document_id), "running")
        self._target_jobs[job_id] = snapshot
        worker = _SimulationTargetWorker(job_id, self._workbench.engine, entry)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._target_discovery_completed)
        worker.completed.connect(thread.quit)
        worker.failed.connect(self._target_discovery_failed)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.start()
        self._job_threads[job_id] = thread
        self._job_workers[job_id] = worker
        self.emit("simulation_targets_started", snapshot)
        return snapshot

    def _target_discovery_completed(self, job_id: str, listing: object) -> None:
        previous = self._target_jobs[job_id]
        targets = tuple(
            SimulationTargetSnapshot(item.label, item.query, tuple(item.params))
            for item in getattr(listing, "targets", ())
        )
        snapshot = SimulationTargetJobSnapshot(
            job_id, previous.document_id, previous.revision, "completed", targets,
            getattr(listing, "diagnostic", None),
            self._revision(previous.document_id) != previous.revision,
        )
        self._target_jobs[job_id] = snapshot
        self._job_threads.pop(job_id, None)
        self._job_workers.pop(job_id, None)
        self.emit("simulation_targets_completed", snapshot)

    def _target_discovery_failed(self, job_id: str, message: str) -> None:
        previous = self._target_jobs[job_id]
        snapshot = SimulationTargetJobSnapshot(
            job_id, previous.document_id, previous.revision, "failed",
            diagnostic=message, stale=self._revision(previous.document_id) != previous.revision,
        )
        self._target_jobs[job_id] = snapshot
        self._job_threads.pop(job_id, None)
        self._job_workers.pop(job_id, None)
        self.emit("simulation_targets_failed", snapshot)

    def _simulation_completed(self, job_id: str, result: object) -> None:
        previous = self._jobs[job_id]
        stale = self._revision(previous.document_id) != previous.revision
        result_snapshot = _simulation_result_snapshot(result)
        snapshot = SimulationJobSnapshot(
            job_id, previous.document_id, previous.revision, result_snapshot.status,
            result_snapshot, stale=stale,
        )
        self._jobs[job_id] = snapshot
        self._job_threads.pop(job_id, None)
        self._job_workers.pop(job_id, None)
        self._job_cancellations.pop(job_id, None)
        self.emit("simulation_completed", snapshot)

    def _simulation_failed(self, job_id: str, message: str) -> None:
        previous = self._jobs[job_id]
        snapshot = SimulationJobSnapshot(job_id, previous.document_id, previous.revision, "failed", diagnostic=message)
        self._jobs[job_id] = snapshot
        self._job_threads.pop(job_id, None)
        self._job_workers.pop(job_id, None)
        self._job_cancellations.pop(job_id, None)
        self.emit("simulation_failed", snapshot)

    def get_job(self, job_id: str) -> SimulationJobSnapshot | None:
        return self._jobs.get(job_id)

    def get_target_job(self, job_id: str) -> SimulationTargetJobSnapshot | None:
        return self._target_jobs.get(job_id)

    def cancel_job(self, job_id: str) -> bool:
        cancellation = self._job_cancellations.get(job_id)
        if cancellation is None:
            return False
        cancellation.cancel()
        return True


def _simulation_result_snapshot(result: object) -> SimulationResultSnapshot:
    events = tuple(
        SimulationEventSnapshot(
            kind=str(getattr(event, "kind", "")),
            function=str(getattr(event, "function", "")),
            block=getattr(event, "block", None),
            detail=str(getattr(event, "detail", "")),
            source=_source(getattr(event, "source", None)),
            args=tuple(getattr(event, "args", ())),
            values=tuple(getattr(event, "values", ())),
            exception=_display_value(getattr(event, "exception", None)),
            stdout=str(getattr(event, "stdout", "")),
            stderr=str(getattr(event, "stderr", "")),
        )
        for event in getattr(result, "events", ())
    )
    return SimulationResultSnapshot(
        status=str(getattr(getattr(result, "status", None), "value", "completed")),
        values=tuple(getattr(result, "values", ())),
        exception=_display_value(getattr(result, "exception", None)),
        cause=_display_value(getattr(result, "cause", None)),
        locals=tuple(sorted(dict(getattr(result, "locals", {})).items())),
        steps=int(getattr(result, "steps", 0)),
        diagnostic=getattr(result, "diagnostic", None),
        events=events,
        trace_truncated=bool(getattr(result, "trace_truncated", False)),
    )


def _display_value(value: object | None) -> str | None:
    return None if value is None else repr(value)
