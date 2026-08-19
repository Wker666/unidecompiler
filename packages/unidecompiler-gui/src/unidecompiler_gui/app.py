"""Synchronous V1 PySide6 workbench. No frontend-private imports occur here."""
from __future__ import annotations

import sys
import re
import json
from threading import Event
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any

from unidecompiler import DecompileResult, DecompilerEngine, FrontendRegistrationError
from unidecompiler.input_sources import InputEntry, iter_input_entries, load_input_entry
from unidecompiler_gui.themes import Theme, builtin_themes, external_theme
from unidecompiler_simulator import (
    SimulationCancellation,
    SimulationEngine,
    SimulationLimits,
    SimulationResult,
    SimulationTarget,
    SimulationTargetListing,
)
from unidecompiler_simulation_host_python import PythonFileEnvironment

try:
    from PySide6.QtCore import QObject, QRect, QSize, QRegularExpression, QSettings, QThread, Qt, Signal, Slot
    from PySide6.QtGui import QAction, QActionGroup, QBrush, QColor, QFontDatabase, QKeySequence, QPainter, QPainterPath, QPen, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument, QTransform
    from PySide6.QtWidgets import (
        QApplication, QDialog, QFileDialog, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
        QComboBox, QFrame, QGraphicsScene, QGraphicsView, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QPushButton, QSplitter, QSpinBox, QStatusBar, QStyle, QTableWidget, QTableWidgetItem, QTabBar, QToolBar, QToolButton, QTreeWidget,
        QMenu, QTreeWidgetItem, QStackedWidget, QTabWidget, QVBoxLayout, QWidget,
    )
except ImportError as error:  # Keeps package metadata inspectable without Qt installed.
    raise RuntimeError("unidecompiler-gui requires PySide6; install the GUI package dependencies") from error


class NavigationTree(QTreeWidget):
    """Navigation columns with a stable 80/20 path-to-status split."""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.setColumnWidth(1, round(self.viewport().width() * 0.20))
        self.setColumnWidth(2, round(self.viewport().width() * 0.20))


class LineNumberArea(QWidget):
    def __init__(self, editor: "PseudocodeEditor") -> None:
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self.editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:
        self.editor.paint_line_numbers(event)


class FindPanel(QFrame):
    changed = Signal()
    next_requested = Signal()
    previous_requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("findPanel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)
        self.query = QLineEdit()
        self.query.setMinimumWidth(210)
        self.query.setPlaceholderText("Find")
        self.query.textChanged.connect(self.changed)
        self.query.returnPressed.connect(self.next_requested)
        layout.addWidget(self.query)
        self.count = QLabel()
        self.count.setMinimumWidth(48)
        layout.addWidget(self.count)
        self.case_sensitive = self._toggle("Aa", "Match case")
        self.whole_word = self._toggle("W", "Match whole word")
        self.regular_expression = self._toggle(".*", "Use regular expression")
        for button in (self.case_sensitive, self.whole_word, self.regular_expression):
            button.toggled.connect(self.changed)
            layout.addWidget(button)
        for text, tooltip, signal in (("Up", "Previous match", self.previous_requested), ("Down", "Next match", self.next_requested), ("x", "Close", self.close_requested)):
            button = self._button(text, tooltip)
            button.clicked.connect(signal)
            layout.addWidget(button)

    def _toggle(self, text: str, tooltip: str) -> QToolButton:
        button = self._button(text, tooltip)
        button.setCheckable(True)
        return button

    def _button(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        return button

    def set_match_count(self, current: int, total: int) -> None:
        self.count.setText("" if not self.query.text() else f"{current}/{total}")


class GoToLinePanel(QFrame):
    requested = Signal()
    close_requested = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("findPanel")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)
        self.query = QLineEdit()
        self.query.setMinimumWidth(210)
        self.query.setPlaceholderText("Go to Line (line:column)")
        self.query.returnPressed.connect(self.requested)
        layout.addWidget(self.query)
        close = QToolButton()
        close.setText("x")
        close.setToolTip("Close")
        close.clicked.connect(self.close_requested)
        layout.addWidget(close)


@dataclass(frozen=True)
class GlobalSearchHit:
    result: DecompileResult
    kind: str
    function_id: str | None
    offset: int | None
    source: object | None
    text_start: int | None
    text_end: int | None
    preview: str


class FrontendManagerDialog(QDialog):
    changed = Signal()

    def __init__(self, engine: DecompilerEngine, parent: QWidget) -> None:
        super().__init__(parent)
        self._engine = engine
        self.setWindowTitle("Frontends")
        self.resize(760, 420)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Frontend", "ID", "Source", "Supported inputs"])
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        self.register_button = QPushButton("Register folder")
        self.register_button.clicked.connect(self._register_folder)
        self.unload_button = QPushButton("Unload selected")
        self.unload_button.clicked.connect(self._unload_selected)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.register_button)
        buttons.addWidget(self.unload_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)
        self.refresh()

    def refresh(self) -> None:
        plugins = self._engine.registry.list()
        self.table.setRowCount(len(plugins))
        for row, plugin in enumerate(plugins):
            values = (
                plugin.display_name,
                plugin.id,
                self._engine.registry.source_for(plugin.id),
                ", ".join(plugin.supported_inputs),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 1:
                    item.setData(Qt.ItemDataRole.UserRole, plugin.id)
                self.table.setItem(row, column, item)

    def set_mutation_enabled(self, enabled: bool) -> None:
        self.register_button.setEnabled(enabled)
        self.unload_button.setEnabled(enabled)

    def _register_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Register frontend folder")
        if not directory:
            return
        try:
            self._engine.register_frontend_directory(directory)
        except (FrontendRegistrationError, ValueError, OSError) as error:
            QMessageBox.critical(self, "Frontend registration failed", str(error))
            return
        self.refresh()
        self.changed.emit()

    def _unload_selected(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        item = self.table.item(row, 1)
        frontend_id = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(frontend_id, str):
            return
        try:
            self._engine.unregister_frontend(frontend_id)
        except ValueError as error:
            QMessageBox.critical(self, "Frontend unload failed", str(error))
            return
        self.refresh()
        self.changed.emit()


class ControlFlowView(QGraphicsView):
    """A scrollable graphical projection of public CFG summary facts."""

    activated = Signal(object, str)

    def __init__(self) -> None:
        self._scene = QGraphicsScene()
        super().__init__(self._scene)
        self.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self._pending_center: tuple[float, float] | None = None

    def set_graph(self, graph: object | None, colors: dict[str, str]) -> None:
        self._scene.clear()
        if graph is None:
            self._scene.addText("No control-flow data")
            return
        positions, edge_lanes = _control_flow_layout(graph)
        width, height = _CFG_NODE_WIDTH, _CFG_NODE_HEIGHT
        for edge in graph.edges:
            origin = positions.get(edge.source)
            target = positions.get(edge.target)
            if origin is None or target is None:
                continue
            x1, y1 = origin
            x2, y2 = target
            lane = edge_lanes[edge]
            path = QPainterPath()
            path.moveTo(x1 + width, y1 + height / 2)
            if lane == 0:
                control_y = y1 + height / 2
                path.lineTo(x2, y2 + height / 2)
            else:
                control_y = lane * _CFG_LANE_HEIGHT if lane < 0 else height + lane * _CFG_LANE_HEIGHT
                path.cubicTo(x1 + width + _CFG_LANE_WIDTH, control_y, x2 - _CFG_LANE_WIDTH, control_y, x2, y2 + height / 2)
            edge_pen = QPen(QColor(_edge_color(edge.kind, colors)))
            edge_pen.setWidth(2)
            self._scene.addPath(path, edge_pen)
            if edge.kind != "fallthrough":
                label = self._scene.addText(edge.kind)
                label.setDefaultTextColor(QColor(_edge_color(edge.kind, colors)))
                label.setPos((x1 + x2 + width) / 2, control_y - (18 if lane < 0 else 0))
        for block in graph.blocks:
            x, y = positions[block.id]
            rect = self._scene.addRect(x, y, width, height, QPen(QColor(colors["status_info"])), QBrush(QColor(colors["line_number_background"])))
            rect.setData(0, (block.source, graph.function_id))
            text = self._scene.addText(f"{block.id}\n{block.statement_count} statements  {block.terminator or 'fallthrough'}")
            text.setDefaultTextColor(QColor(colors["syntax_function"]))
            text.setPos(x + 8, y + 6)
            text.setData(0, (block.source, graph.function_id))
        self._scene.setSceneRect(self._scene.itemsBoundingRect().adjusted(-24, -24, 24, 24))
        self.resetTransform()
        entry = positions.get(graph.entry)
        if entry is not None:
            self._pending_center = (entry[0] + width / 2, entry[1] + height / 2)

    def mousePressEvent(self, event) -> None:
        item = self._scene.itemAt(self.mapToScene(event.position().toPoint()), QTransform())
        while item is not None:
            data = item.data(0)
            if isinstance(data, tuple) and len(data) == 2 and data[0] is not None:
                self.activated.emit(data[0], data[1])
                break
            item = item.parentItem()
        super().mousePressEvent(event)

    def wheelEvent(self, event) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._pending_center is not None:
            self.centerOn(*self._pending_center)
            self._pending_center = None


_CFG_NODE_WIDTH = 210
_CFG_NODE_HEIGHT = 62
_CFG_NODE_GAP = 72
_CFG_LANE_HEIGHT = 52
_CFG_LANE_WIDTH = 90


def _control_flow_layout(graph: object) -> tuple[dict[str, tuple[int, int]], dict[object, int]]:
    """Lay bytecode blocks on one axis and reserve separate edge channels."""
    positions = {
        block.id: (index * (_CFG_NODE_WIDTH + _CFG_NODE_GAP), 0)
        for index, block in enumerate(graph.blocks)
    }
    edge_lanes: dict[object, int] = {edge: 0 for edge in graph.edges if edge.kind == "fallthrough"}
    forward = [edge for edge in graph.edges if edge.kind != "fallthrough" and positions[edge.target][0] > positions[edge.source][0]]
    backward = [edge for edge in graph.edges if edge.kind != "fallthrough" and positions[edge.target][0] < positions[edge.source][0]]
    edge_lanes.update(_allocate_edge_lanes(forward, positions, direction=-1))
    edge_lanes.update(_allocate_edge_lanes(backward, positions, direction=1))
    return positions, edge_lanes


def _allocate_edge_lanes(edges: list[object], positions: dict[str, tuple[int, int]], *, direction: int) -> dict[object, int]:
    """Reuse a channel when its edge interval does not overlap another one."""
    lanes: list[list[tuple[int, int]]] = []
    assigned: dict[object, int] = {}
    for edge in sorted(edges, key=lambda item: (min(positions[item.source][0], positions[item.target][0]), -max(positions[item.source][0], positions[item.target][0]))):
        start, end = sorted((positions[edge.source][0], positions[edge.target][0]))
        for index, intervals in enumerate(lanes):
            if all(end <= other_start or start >= other_end for other_start, other_end in intervals):
                intervals.append((start, end))
                assigned[edge] = direction * (index + 1)
                break
        else:
            lanes.append([(start, end)])
            assigned[edge] = direction * len(lanes)
    return assigned


def _edge_color(kind: str, colors: dict[str, str]) -> str:
    if kind == "branch":
        return colors["status_info"]
    if kind == "jump":
        return colors["status_warning"]
    return colors["syntax_comment"]


class DecompileWorker(QObject):
    progress = Signal(int, int, str)
    completed = Signal(object, bool)
    failed = Signal(str)

    def __init__(self, engine: DecompilerEngine, entries: tuple[InputEntry, ...], cancelled: Event) -> None:
        super().__init__()
        self._engine = engine
        self._entries = entries
        self._cancelled = cancelled

    @Slot()
    def run(self) -> None:
        results: list[tuple[DecompileResult, bytes | None]] = []
        try:
            for index, entry in enumerate(self._entries, start=1):
                if self._cancelled.is_set():
                    self.completed.emit(tuple(results), True)
                    return
                self.progress.emit(index, len(self._entries), entry.display_path)
                artifact = load_input_entry(entry)
                if self._cancelled.is_set():
                    self.completed.emit(tuple(results), True)
                    return
                result = self._engine.decompile_artifact(artifact)
                results.append((result, artifact.data if result.status == "resource" else None))
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")
            return
        self.completed.emit(tuple(results), False)


class SimulationWorker(QObject):
    """GUI host worker; frontend selection and execution remain in simulator."""

    completed = Signal(str, object)
    failed = Signal(str)

    def __init__(
        self,
        engine: DecompilerEngine,
        entry: InputEntry,
        *,
        target: SimulationTarget | None = None,
        args: tuple[object, ...] = (),
        runtime_path: Path | None = None,
        limits: SimulationLimits | None = None,
        cancellation: SimulationCancellation | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._entry = entry
        self._target = target
        self._args = args
        self._runtime_path = runtime_path
        self._limits = limits
        self._cancellation = cancellation

    @Slot()
    def run(self) -> None:
        try:
            artifact = load_input_entry(self._entry)
            simulator = SimulationEngine.from_registry(self._engine.registry)
            if self._target is None:
                self.completed.emit(
                    "targets",
                    simulator.list_artifact_targets(artifact.data, artifact.display_path),
                )
                return
            environment = (
                PythonFileEnvironment.load(self._runtime_path)
                if self._runtime_path is not None
                else None
            )
            self.completed.emit(
                "run",
                simulator.simulate_artifact(
                    artifact.data,
                    artifact.display_path,
                    self._target.query,
                    args=self._args,
                    environment=environment,
                    limits=self._limits,
                    cancellation=self._cancellation,
                ),
            )
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")


class Workbench(QMainWindow):
    def __init__(self, engine: DecompilerEngine | None = None) -> None:
        super().__init__()
        self.engine = engine or DecompilerEngine.discover()
        self.results: tuple[DecompileResult, ...] = ()
        self._resource_data: dict[str, bytes] = {}
        self._displayed_result_path: str | None = None
        self._displayed_function_id: str | None = None
        self._displayed_resource_path: str | None = None
        self._open_document_paths: list[str] = []
        self._closed_document_paths: set[str] = set()
        self._document_session_started = False
        self._open_completed_documents = False
        self._entries: tuple[InputEntry, ...] = ()
        self._opened_path: Path | None = None
        self._worker_thread: QThread | None = None
        self._worker: DecompileWorker | None = None
        self._cancelled: Event | None = None
        self._simulation_thread: QThread | None = None
        self._simulation_worker: SimulationWorker | None = None
        self._simulation_cancellation: SimulationCancellation | None = None
        self._simulation_targets: dict[str, SimulationTargetListing] = {}
        self._simulation_result: SimulationResult | None = None
        self._simulation_job_path = ""
        self._simulation_result_path = ""
        self._simulation_target_path = ""
        self._history: list[tuple[str, str | None]] = []
        self._history_index = -1
        self._restoring_history = False
        self._pinned_documents: set[str] = set()
        self.frontend_manager: FrontendManagerDialog | None = None
        self._settings = QSettings("unidecompiler", "unidecompiler-gui")
        self._themes = builtin_themes()
        self._theme = next(theme for theme in self._themes if theme.id == "dark")
        self.setWindowTitle("unidecompiler-gui")
        self.resize(1280, 820)
        self._build_ui()

    def _build_ui(self) -> None:
        open_file = self._action("Open file", QStyle.StandardPixmap.SP_FileIcon, self.open_file)
        open_directory = self._action("Open directory", QStyle.StandardPixmap.SP_DirIcon, self.open_directory)
        open_archive = self._action("Open archive", QStyle.StandardPixmap.SP_DriveHDIcon, self.open_archive)
        self.decompile_all_action = QAction("Decompile all pending", self, shortcut="Ctrl+Shift+Enter", triggered=self.decompile_all_pending)
        self.decompile_all_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        export = QAction("Export pseudocode", self, triggered=self.export_pseudocode)
        menu = self.menuBar().addMenu("File")
        menu.addAction(open_file)
        menu.addAction(open_directory)
        menu.addAction(open_archive)
        menu.addAction(self.decompile_all_action)
        menu.addAction(export)
        self.recent_menu = menu.addMenu("Recent")
        self._rebuild_recent_menu()

        edit_menu = self.menuBar().addMenu("Edit")
        find = QAction("Find", self, shortcut=QKeySequence.StandardKey.Find, triggered=self.focus_search)
        find_all = QAction("Find in all decompiled files", self, shortcut="Ctrl+Shift+F", triggered=self.focus_global_search)
        goto_line = QAction("Go to line", self, shortcut="Ctrl+G", triggered=self.go_to_line)
        zoom_in = QAction("Zoom in", self, shortcut="Ctrl+=", triggered=lambda: self.pseudocode.zoomIn(1))
        zoom_out = QAction("Zoom out", self, shortcut="Ctrl+-", triggered=lambda: self.pseudocode.zoomOut(1))
        edit_menu.addActions((find, find_all, goto_line, zoom_in, zoom_out))
        find_all.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView))
        find_all.setToolTip("Find in all decompiled files")
        navigate_menu = self.menuBar().addMenu("Navigate")
        self.back_action = QAction("Back", self, shortcut="Alt+Left", triggered=self.go_back)
        self.forward_action = QAction("Forward", self, shortcut="Alt+Right", triggered=self.go_forward)
        self.back_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowBack))
        self.forward_action.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_ArrowForward))
        navigate_menu.addActions((self.back_action, self.forward_action))
        self._update_history_actions()
        view_menu = self.menuBar().addMenu("View")
        theme_menu = view_menu.addMenu("Theme")
        theme_actions = QActionGroup(self)
        for theme in self._themes:
            action = QAction(theme.display_name, self, checkable=True, checked=theme == self._theme)
            action.triggered.connect(lambda _checked, selected=theme: self._apply_theme(selected))
            theme_actions.addAction(action)
            theme_menu.addAction(action)
        load_theme = QAction("Load stylesheet", self, triggered=self._load_theme)
        theme_menu.addAction(load_theme)
        windows_menu = view_menu.addMenu("Windows")
        self.diagnostics_action = QAction("Diagnostics", self, checkable=True)
        self.diagnostics_action.toggled.connect(self._set_diagnostics_visible)
        windows_menu.addAction(self.diagnostics_action)
        frontends_action = QAction("Frontends", self, triggered=self.open_frontend_manager)
        view_menu.addAction(frontends_action)
        toolbar = QToolBar("Workspace", self)
        toolbar.setObjectName("workspaceToolbar")
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        toolbar.addActions((open_file, open_directory, open_archive))
        toolbar.addSeparator()
        toolbar.addAction(self.decompile_all_action)
        toolbar.addSeparator()
        toolbar.addAction(find_all)
        toolbar.addAction(self.back_action)
        toolbar.addAction(self.forward_action)
        self.addToolBar(toolbar)

        self.input_tree = NavigationTree()
        self.input_tree.setHeaderLabels(["Input / module / function", "Status", "Frontend"])
        self.input_tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        input_header = self.input_tree.header()
        input_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        input_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        input_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.input_tree.itemSelectionChanged.connect(self._tree_selected)
        self.input_tree.itemClicked.connect(self._reopen_closed_tree_item)
        self.input_tree.itemActivated.connect(self._reopen_closed_tree_item)
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter inputs")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._filter_inputs)
        navigation = QWidget()
        navigation_layout = QVBoxLayout(navigation)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(2)
        navigation_layout.addWidget(self.filter_box)
        navigation_layout.addWidget(self.input_tree)
        self.pseudocode = PseudocodeEditor()
        self._syntax_highlighter = GenericPseudocodeHighlighter(self.pseudocode.document())
        self.pseudocode.cursorPositionChanged.connect(self._pseudocode_selected)
        self.pseudocode.cursorPositionChanged.connect(self._update_cursor_status)
        self.pseudocode.function_activated.connect(self._activate_function)
        self.resource_text = QPlainTextEdit()
        self.resource_text.setReadOnly(True)
        self.resource_binary = QPlainTextEdit()
        self.resource_binary.setReadOnly(True)
        fixed_font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        self.resource_text.setFont(fixed_font)
        self.resource_binary.setFont(fixed_font)
        self.resource_tabs = QTabWidget()
        self.resource_tabs.addTab(self.resource_text, "Text")
        self.resource_tabs.addTab(self.resource_binary, "Binary")
        self.ast_tree = QTreeWidget()
        self.ast_tree.setHeaderLabels(["AST"])
        self.ast_tree.itemSelectionChanged.connect(self._ast_selected)
        self.bytecode = QTableWidget(0, 4)
        self.bytecode.setHorizontalHeaderLabels(["Offset", "Opcode", "Operands", "Raw"])
        self.bytecode.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.bytecode.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        bytecode_header = self.bytecode.horizontalHeader()
        bytecode_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        bytecode_header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        bytecode_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        bytecode_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.bytecode.setColumnWidth(0, 64)
        self.bytecode.setColumnWidth(1, 160)
        self.bytecode.setColumnWidth(2, 250)
        self.bytecode.itemSelectionChanged.connect(self._bytecode_selected)
        self.references = QTableWidget(0, 4)
        self.references.setHorizontalHeaderLabels(["Kind", "Name", "Function", "Offset"])
        self.references.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.references.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.references.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.references.itemSelectionChanged.connect(self._reference_selected)
        self.references.cellDoubleClicked.connect(self._reference_activated)
        self.call_hierarchy = QTreeWidget()
        self.call_hierarchy.setHeaderLabels(["Call hierarchy", "Resolution"])
        self.call_hierarchy.itemDoubleClicked.connect(self._call_hierarchy_activated)
        self.browse = QTableWidget(0, 5)
        self.browse.setHorizontalHeaderLabels(["Kind", "Name", "Value", "Function", "Offset"])
        self.browse.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.browse.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.browse.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.browse.itemSelectionChanged.connect(self._browse_selected)
        self.browse_filter = QComboBox()
        self.browse_filter.addItem("All facts", None)
        for kind in ("string", "constant", "type", "member", "global"):
            self.browse_filter.addItem(kind.title(), kind)
        self.browse_filter.currentIndexChanged.connect(self._refresh_browse)
        browse_panel = QWidget()
        browse_layout = QVBoxLayout(browse_panel)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.addWidget(self.browse_filter)
        browse_layout.addWidget(self.browse)
        self.control_flow = QTableWidget(0, 4)
        self.control_flow.setHorizontalHeaderLabels(["Block", "Statements", "Terminator", "Edges"])
        self.control_flow.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.control_flow.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.control_flow_view = ControlFlowView()
        self.control_flow_view.activated.connect(self._control_flow_activated)
        control_flow_panel = QSplitter(Qt.Orientation.Vertical)
        control_flow_panel.addWidget(self.control_flow_view)
        control_flow_panel.addWidget(self.control_flow)
        control_flow_panel.setSizes([420, 180])
        self.simulation_frontend = QLabel("No simulation target selected")
        self.simulation_target = QComboBox()
        self.simulation_target.currentIndexChanged.connect(self._simulation_target_changed)
        self.simulation_args = QLineEdit("[]")
        self.simulation_args.setPlaceholderText("JSON argument array")
        self.simulation_runtime = QLineEdit(
            self._settings.value("simulation_runtime", "", type=str)
        )
        self.simulation_runtime.setPlaceholderText("Optional trusted runtime.py")
        self.simulation_runtime.editingFinished.connect(self._remember_simulation_runtime)
        self.simulation_runtime_browse = QToolButton()
        self.simulation_runtime_browse.setText("...")
        self.simulation_runtime_browse.setToolTip("Choose runtime.py")
        self.simulation_runtime_browse.clicked.connect(self._choose_simulation_runtime)
        self.simulation_steps = QSpinBox()
        self.simulation_steps.setRange(1, 10_000_000)
        self.simulation_steps.setValue(100_000)
        self.simulation_depth = QSpinBox()
        self.simulation_depth.setRange(1, 10_000)
        self.simulation_depth.setValue(128)
        self.simulation_trace_limit = QSpinBox()
        self.simulation_trace_limit.setRange(1, 1_000_000)
        self.simulation_trace_limit.setValue(10_000)
        self.simulation_run = QPushButton("Run")
        self.simulation_run.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay))
        self.simulation_run.clicked.connect(self._run_simulation)
        self.simulation_cancel = QToolButton()
        self.simulation_cancel.setText("x")
        self.simulation_cancel.setToolTip("Cancel simulation")
        self.simulation_cancel.clicked.connect(self._cancel_simulation)
        self.simulation_cancel.hide()
        self.simulation_clear = QToolButton()
        self.simulation_clear.setText("Clear")
        self.simulation_clear.setToolTip("Clear simulation trace")
        self.simulation_clear.clicked.connect(self._clear_simulation)
        simulation_controls = QHBoxLayout()
        simulation_controls.setContentsMargins(0, 0, 0, 0)
        simulation_controls.addWidget(QLabel("Target"))
        simulation_controls.addWidget(self.simulation_target, 2)
        simulation_controls.addWidget(QLabel("Args"))
        simulation_controls.addWidget(self.simulation_args, 2)
        simulation_controls.addWidget(QLabel("Steps"))
        simulation_controls.addWidget(self.simulation_steps)
        simulation_controls.addWidget(QLabel("Depth"))
        simulation_controls.addWidget(self.simulation_depth)
        simulation_controls.addWidget(QLabel("Trace"))
        simulation_controls.addWidget(self.simulation_trace_limit)
        simulation_controls.addWidget(self.simulation_run)
        simulation_controls.addWidget(self.simulation_cancel)
        simulation_controls.addWidget(self.simulation_clear)
        runtime_controls = QHBoxLayout()
        runtime_controls.setContentsMargins(0, 0, 0, 0)
        runtime_controls.addWidget(QLabel("Runtime"))
        runtime_controls.addWidget(self.simulation_runtime, 1)
        runtime_controls.addWidget(self.simulation_runtime_browse)
        self.simulation_filter = QComboBox()
        self.simulation_filter.addItem("All events", True)
        self.simulation_filter.addItem("Key events", False)
        self.simulation_filter.currentIndexChanged.connect(self._refresh_simulation_trace)
        self.simulation_status = QLabel()
        self.simulation_trace = QTableWidget(0, 7)
        self.simulation_trace.setHorizontalHeaderLabels(
            ["#", "Function", "Block", "Event", "Detail", "Offset", "Output"]
        )
        self.simulation_trace.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.simulation_trace.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.simulation_trace.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.simulation_trace.itemSelectionChanged.connect(self._simulation_event_selected)
        self.simulation_trace.cellDoubleClicked.connect(self._simulation_event_activated)
        self.simulation_details = QPlainTextEdit()
        self.simulation_details.setReadOnly(True)
        self.simulation_details.setFont(fixed_font)
        simulation_splitter = QSplitter(Qt.Orientation.Vertical)
        simulation_splitter.addWidget(self.simulation_trace)
        simulation_splitter.addWidget(self.simulation_details)
        simulation_splitter.setSizes([400, 180])
        simulation_panel = QWidget()
        simulation_layout = QVBoxLayout(simulation_panel)
        simulation_layout.setContentsMargins(0, 0, 0, 0)
        simulation_layout.addWidget(self.simulation_frontend)
        simulation_layout.addLayout(simulation_controls)
        simulation_layout.addLayout(runtime_controls)
        simulation_layout.addWidget(self.simulation_status)
        simulation_layout.addWidget(self.simulation_filter)
        simulation_layout.addWidget(simulation_splitter, 1)
        self.simulation_panel = simulation_panel
        self.global_search_query = QLineEdit()
        self.global_search_query.setPlaceholderText("Search all decompiled files")
        self.global_search_query.setClearButtonEnabled(True)
        self.global_search_query.textChanged.connect(self._invalidate_global_search)
        self.global_search_query.returnPressed.connect(self._run_global_search)
        self.global_search_scope = QComboBox()
        self.global_search_scope.addItem("Pseudocode", "pseudocode")
        self.global_search_scope.addItem("Pseudocode and bytecode", "all")
        self.global_search_scope.addItem("Bytecode", "bytecode")
        self.global_search_scope.currentIndexChanged.connect(self._invalidate_global_search)
        self.global_search_case = QToolButton()
        self.global_search_case.setObjectName("globalSearchToggle")
        self.global_search_case.setText("Aa")
        self.global_search_case.setCheckable(True)
        self.global_search_case.setToolTip("Match case")
        self.global_search_case.toggled.connect(self._invalidate_global_search)
        self.global_search_regex = QToolButton()
        self.global_search_regex.setObjectName("globalSearchToggle")
        self.global_search_regex.setText(".*")
        self.global_search_regex.setCheckable(True)
        self.global_search_regex.setToolTip("Use regular expression")
        self.global_search_regex.toggled.connect(self._invalidate_global_search)
        self.global_search_button = QToolButton()
        self.global_search_button.setText("Search")
        self.global_search_button.setToolTip("Search all decompiled files")
        self.global_search_button.clicked.connect(self._run_global_search)
        self.global_search_results = QTableWidget(0, 5)
        self.global_search_results.setHorizontalHeaderLabels(["Artifact", "Kind", "Function", "Offset", "Preview"])
        self.global_search_results.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.global_search_results.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        global_search_header = self.global_search_results.horizontalHeader()
        for column, width in enumerate((220, 96, 190, 64)):
            global_search_header.setSectionResizeMode(column, QHeaderView.ResizeMode.Fixed)
            self.global_search_results.setColumnWidth(column, width)
        global_search_header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.global_search_results.cellDoubleClicked.connect(self._global_search_activated)
        self.global_search_dialog = QDialog(self)
        self.global_search_dialog.setWindowTitle("Global Search")
        self.global_search_dialog.setModal(False)
        self.global_search_dialog.resize(960, 520)
        global_search_layout = QVBoxLayout(self.global_search_dialog)
        global_search_layout.setContentsMargins(0, 0, 0, 0)
        global_search_tools = QHBoxLayout()
        global_search_tools.setContentsMargins(0, 0, 0, 0)
        global_search_tools.addWidget(self.global_search_query, 1)
        global_search_tools.addWidget(self.global_search_scope)
        global_search_tools.addWidget(self.global_search_case)
        global_search_tools.addWidget(self.global_search_regex)
        global_search_tools.addWidget(self.global_search_button)
        global_search_layout.addLayout(global_search_tools)
        global_search_layout.addWidget(self.global_search_results)
        self.diagnostics = QTableWidget(0, 5)
        self.diagnostics.setHorizontalHeaderLabels(["Severity", "Function", "Offset", "Reason", "Raw context"])
        self.diagnostics.setMinimumHeight(84)
        self.diagnostics.setMaximumHeight(148)
        self.diagnostics.verticalHeader().setDefaultSectionSize(20)
        self.diagnostics.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        analysis_panel = QSplitter(Qt.Orientation.Vertical)
        analysis_panel.addWidget(self.ast_tree)
        analysis_panel.addWidget(self.bytecode)
        analysis_panel.setSizes([340, 260])
        self.analysis_panel = analysis_panel
        references_panel = QSplitter(Qt.Orientation.Vertical)
        references_panel.addWidget(self.references)
        references_panel.addWidget(self.call_hierarchy)
        references_panel.setSizes([340, 260])
        self.detail_tabs = QTabWidget()
        self.detail_tabs.addTab(self.pseudocode, "Pseudocode")
        self.detail_tabs.addTab(analysis_panel, "AST / Bytecode")
        self.detail_tabs.addTab(references_panel, "References / Calls")
        self.detail_tabs.addTab(browse_panel, "Browser")
        self.detail_tabs.addTab(control_flow_panel, "CFG")
        self.detail_tabs.addTab(self.simulation_panel, "Simulation")
        self.detail_tabs.currentChanged.connect(self._simulation_tab_changed)
        self.detail_tabs.setCurrentWidget(self.pseudocode)
        self.welcome_page = QWidget()
        self.welcome_page.setObjectName("welcomePage")
        welcome_layout = QVBoxLayout(self.welcome_page)
        welcome_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome_layout.setSpacing(10)
        self.empty_state = QLabel("No file opened")
        self.empty_state.setObjectName("welcomeTitle")
        self.empty_state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome_layout.addWidget(self.empty_state)
        welcome_subtitle = QLabel("Open a bytecode file, directory, or archive to begin")
        welcome_subtitle.setObjectName("welcomeSubtitle")
        welcome_subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome_layout.addWidget(welcome_subtitle)
        welcome_actions = QHBoxLayout()
        welcome_actions.setAlignment(Qt.AlignmentFlag.AlignCenter)
        for text, icon, callback in (
            ("Open file", QStyle.StandardPixmap.SP_FileIcon, self.open_file),
            ("Open directory", QStyle.StandardPixmap.SP_DirIcon, self.open_directory),
            ("Open archive", QStyle.StandardPixmap.SP_DriveHDIcon, self.open_archive),
        ):
            button = QPushButton(text)
            button.setObjectName("welcomeAction")
            button.setIcon(self.style().standardIcon(icon))
            button.clicked.connect(callback)
            welcome_actions.addWidget(button)
        welcome_layout.addLayout(welcome_actions)
        self.welcome_recent_label = QLabel("Recent")
        self.welcome_recent_label.setObjectName("welcomeRecentLabel")
        self.welcome_recent_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        welcome_layout.addWidget(self.welcome_recent_label)
        self.welcome_recent = QWidget()
        self.welcome_recent.setObjectName("welcomeRecent")
        self.welcome_recent_layout = QVBoxLayout(self.welcome_recent)
        self.welcome_recent_layout.setContentsMargins(0, 0, 0, 0)
        self.welcome_recent_layout.setSpacing(2)
        welcome_layout.addWidget(self.welcome_recent)
        self._rebuild_welcome_recent()
        self.detail_stack = QStackedWidget()
        self.detail_stack.addWidget(self.welcome_page)
        self.detail_stack.addWidget(self.detail_tabs)
        self.detail_stack.addWidget(self.resource_tabs)
        self.detail_stack.setCurrentWidget(self.welcome_page)
        self.document_tabs = QTabBar()
        self.document_tabs.setTabsClosable(True)
        self.document_tabs.setMovable(True)
        self.document_tabs.setUsesScrollButtons(True)
        self.document_tabs.currentChanged.connect(self._document_selected)
        self.document_tabs.tabCloseRequested.connect(self._close_document)
        self.document_tabs.tabBarDoubleClicked.connect(self._toggle_document_pin)
        self.document_tabs.tabMoved.connect(self._document_tab_moved)
        self.document_tabs.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.document_tabs.customContextMenuRequested.connect(self._document_context_menu)
        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        workspace_layout.addWidget(self.document_tabs, 0)
        workspace_layout.addWidget(self.detail_stack, 1)
        outer = QSplitter(Qt.Orientation.Horizontal)
        outer.addWidget(navigation)
        outer.addWidget(workspace)
        outer.setSizes([260, 940])
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(outer, 1)
        layout.addWidget(self.diagnostics, 0)
        self.diagnostics.hide()
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.frontend_status = QLabel()
        self.frontend_status.setObjectName("frontendStatus")
        self._update_frontend_status()
        self.statusBar().addPermanentWidget(self.frontend_status)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.hide()
        self.cancel_button = QToolButton()
        self.cancel_button.setText("x")
        self.cancel_button.setToolTip("Cancel decompilation")
        self.cancel_button.clicked.connect(self._cancel_decompilation)
        self.cancel_button.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)
        self.find_panel = FindPanel(self.pseudocode)
        self.find_panel.changed.connect(self._find_from_start)
        self.find_panel.next_requested.connect(self.find_next)
        self.find_panel.previous_requested.connect(self.find_previous)
        self.find_panel.close_requested.connect(self._hide_find_panel)
        self.pseudocode.resized.connect(self._position_find_panel)
        self.find_panel.hide()
        self.goto_panel = GoToLinePanel(self.pseudocode)
        self.goto_panel.requested.connect(self._go_to_requested_line)
        self.goto_panel.close_requested.connect(self._hide_go_to_line_panel)
        self.goto_panel.hide()
        self._apply_theme(self._theme)

    def _update_frontend_status(self) -> None:
        """Refresh the compact frontend indicator without touching open results."""
        if not self.results:
            self.frontend_status.clear()
            self.frontend_status.setToolTip("")
            self.frontend_status.hide()
            return
        self.frontend_status.show()
        plugins = self.engine.registry.list()
        names = [getattr(plugin, "display_name", plugin.id) for plugin in plugins]
        self.frontend_status.setText("Frontends: " + (" · ".join(names) if names else "none discovered"))
        support_lines = []
        for plugin in plugins:
            support = getattr(plugin, "version_support", None)
            versions = ""
            if support is not None:
                versions = f"; {support.family}: {', '.join(support.versions)}"
            source = self.engine.registry.source_for(plugin.id)
            support_lines.append(f"{plugin.display_name} ({plugin.id}) [{source}]{versions}")
        self.frontend_status.setToolTip("Currently discovered frontends\n" + "\n".join(support_lines))

    def _action(self, text: str, icon: QStyle.StandardPixmap, callback) -> QAction:
        action = QAction(self.style().standardIcon(icon), text, self, triggered=callback)
        action.setToolTip(text)
        return action

    def _set_diagnostics_visible(self, visible: bool) -> None:
        self.diagnostics.setVisible(visible)

    def open_frontend_manager(self) -> None:
        if self.frontend_manager is not None:
            self.frontend_manager.refresh()
            self.frontend_manager.show()
            self.frontend_manager.raise_()
            self.frontend_manager.activateWindow()
            return
        self.frontend_manager = FrontendManagerDialog(self.engine, self)
        self.frontend_manager.changed.connect(self._update_frontend_status)
        self.frontend_manager.finished.connect(self._frontend_manager_closed)
        self._update_frontend_mutation_enabled()
        self.frontend_manager.show()

    def _frontend_manager_closed(self, _result: int) -> None:
        self.frontend_manager = None

    def _apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        QApplication.instance().setStyleSheet(theme.stylesheet)
        self.pseudocode.set_theme(theme.colors)
        self._syntax_highlighter.set_theme(theme.colors)
        if self.results:
            self._refresh()

    def _load_theme(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Load stylesheet", "", "Qt stylesheets (*.qss)")
        if filename:
            self._apply_theme(external_theme(Path(filename), self._theme))

    def focus_search(self) -> None:
        self.detail_tabs.setCurrentWidget(self.pseudocode)
        self.detail_stack.setCurrentWidget(self.detail_tabs)
        self._hide_go_to_line_panel()
        self.find_panel.show()
        self._position_find_panel()
        self.find_panel.query.setFocus()
        self.find_panel.query.selectAll()

    def focus_global_search(self) -> None:
        self.global_search_dialog.show()
        self.global_search_dialog.raise_()
        self.global_search_dialog.activateWindow()
        self.global_search_query.setFocus()
        self.global_search_query.selectAll()

    def _invalidate_global_search(self, *_unused: object) -> None:
        self.global_search_results.setRowCount(0)

    def _run_global_search(self) -> None:
        query = self.global_search_query.text()
        self.global_search_results.setRowCount(0)
        if not query:
            return
        pattern = query if self.global_search_regex.isChecked() else re.escape(query)
        flags = 0 if self.global_search_case.isChecked() else re.IGNORECASE
        try:
            expression = re.compile(pattern, flags)
        except re.error as error:
            self.statusBar().showMessage(f"Invalid regular expression: {error}")
            return
        scope = self.global_search_scope.currentData()
        for result in self.results:
            function_names = {function.id: function.name for function in result.functions}
            if scope in {"all", "pseudocode"} and result.pseudocode is not None:
                for match in expression.finditer(result.pseudocode.text):
                    if match.start() == match.end():
                        continue
                    mapping = min(
                        (
                            item for item in result.pseudocode.source_map
                            if item.start <= match.start() < item.end
                        ),
                        key=lambda item: item.end - item.start,
                        default=None,
                    )
                    function_id = None if mapping is None else mapping.function_id
                    source = None if mapping is None else mapping.source
                    self._append_global_search_hit(GlobalSearchHit(
                        result=result,
                        kind="Pseudocode",
                        function_id=function_id,
                        offset=None if source is None else source.offset,
                        source=source,
                        text_start=match.start(),
                        text_end=match.end(),
                        preview=_search_preview(result.pseudocode.text, match.start(), match.end()),
                    ), function_names)
            if scope in {"all", "bytecode"}:
                for instruction in result.instructions:
                    text = " ".join((instruction.opcode, ", ".join(instruction.operands), instruction.raw)).strip()
                    for match in expression.finditer(text):
                        if match.start() == match.end():
                            continue
                        self._append_global_search_hit(GlobalSearchHit(
                            result=result,
                            kind="Bytecode",
                            function_id=instruction.function_id,
                            offset=instruction.offset,
                            source=instruction.source,
                            text_start=None,
                            text_end=None,
                            preview=_search_preview(text, match.start(), match.end()),
                        ), function_names)

    def _append_global_search_hit(self, hit: GlobalSearchHit, function_names: dict[str, str]) -> None:
        row = self.global_search_results.rowCount()
        self.global_search_results.insertRow(row)
        values = (
            self._relative_display_path(hit.result.display_path),
            hit.kind,
            function_names.get(hit.function_id, hit.function_id or ""),
            "" if hit.offset is None else str(hit.offset),
            hit.preview,
        )
        for column, value in enumerate(values):
            item = QTableWidgetItem(value)
            item.setData(Qt.ItemDataRole.UserRole, hit)
            self.global_search_results.setItem(row, column, item)

    def _global_search_activated(self, row: int, _column: int) -> None:
        item = self.global_search_results.item(row, 0)
        hit = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(hit, GlobalSearchHit):
            return
        target = self._find_result_item(hit.result, hit.function_id)
        if target is None:
            return
        if self.input_tree.currentItem() is not target:
            self.input_tree.setCurrentItem(target)
        else:
            self._tree_selected()
        if hit.source is not None:
            self._focus_source(hit.result, hit.source, hit.function_id)
        if hit.kind == "Pseudocode" and hit.text_start is not None and hit.text_end is not None:
            self.detail_tabs.setCurrentWidget(self.pseudocode)
            cursor = self.pseudocode.textCursor()
            cursor.setPosition(hit.text_start)
            cursor.setPosition(hit.text_end, QTextCursor.MoveMode.KeepAnchor)
            self.pseudocode.setTextCursor(cursor)
            self.pseudocode.centerCursor()
        elif hit.kind == "Bytecode":
            self.detail_tabs.setCurrentWidget(self.analysis_panel)

    def _find_result_item(self, result: DecompileResult, function_id: str | None) -> QTreeWidgetItem | None:
        for root_index in range(self.input_tree.topLevelItemCount()):
            root = self.input_tree.topLevelItem(root_index)
            if root.data(0, Qt.ItemDataRole.UserRole) is not result:
                continue
            if function_id is None:
                return root
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, tuple) and data[1] == function_id:
                    root.setExpanded(True)
                    return child
            return root
        return None

    def _hide_find_panel(self) -> None:
        self.find_panel.hide()
        self.pseudocode.setFocus()

    def _position_find_panel(self) -> None:
        margin = 8
        for panel in (self.find_panel, self.goto_panel):
            panel.adjustSize()
            panel.move(self.pseudocode.viewport().width() - panel.width() - margin, margin)

    def _find_from_start(self) -> None:
        cursor = QTextCursor(self.pseudocode.document())
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        self._find(False, cursor)

    def find_next(self) -> None:
        self._find(False)

    def find_previous(self) -> None:
        self._find(True)

    def _find(self, backwards: bool, start: QTextCursor | None = None) -> None:
        needle = self.find_panel.query.text()
        if not needle:
            self.find_panel.set_match_count(0, 0)
            return
        flags = QTextDocument.FindFlag.FindBackward if backwards else QTextDocument.FindFlag(0)
        if self.find_panel.case_sensitive.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if self.find_panel.whole_word.isChecked() and not self.find_panel.regular_expression.isChecked():
            flags |= QTextDocument.FindFlag.FindWholeWords
        cursor = self._find_cursor(needle, start or self.pseudocode.textCursor(), flags)
        if cursor.isNull():
            anchor = QTextCursor(self.pseudocode.document())
            anchor.movePosition(QTextCursor.MoveOperation.End if backwards else QTextCursor.MoveOperation.Start)
            cursor = self._find_cursor(needle, anchor, flags)
        if cursor.isNull():
            self.statusBar().showMessage("No matches")
            self.find_panel.set_match_count(0, 0)
            return
        self.detail_tabs.setCurrentWidget(self.pseudocode)
        self.pseudocode.setTextCursor(cursor)
        self._update_match_count(needle)

    def _find_cursor(self, needle: str, origin: QTextCursor, flags):
        if not self.find_panel.regular_expression.isChecked():
            return self.pseudocode.document().find(needle, origin, flags)
        expression = QRegularExpression(needle)
        if self.find_panel.whole_word.isChecked():
            expression.setPattern(rf"\b(?:{needle})\b")
        if not self.find_panel.case_sensitive.isChecked():
            expression.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
        if not expression.isValid():
            self.statusBar().showMessage(expression.errorString())
            return QTextCursor()
        return self.pseudocode.document().find(expression, origin, flags)

    def _update_match_count(self, needle: str) -> None:
        pattern = needle if self.find_panel.regular_expression.isChecked() else QRegularExpression.escape(needle)
        if self.find_panel.whole_word.isChecked():
            pattern = rf"\b(?:{pattern})\b"
        expression = QRegularExpression(pattern)
        if not self.find_panel.case_sensitive.isChecked():
            expression.setPatternOptions(QRegularExpression.PatternOption.CaseInsensitiveOption)
        matches = expression.globalMatch(self.pseudocode.toPlainText())
        offsets: list[int] = []
        while matches.hasNext():
            offsets.append(matches.next().capturedStart())
        selected_start = self.pseudocode.textCursor().selectionStart()
        current = next((index + 1 for index, offset in enumerate(offsets) if offset == selected_start), 0)
        self.find_panel.set_match_count(current, len(offsets))

    def go_to_line(self) -> None:
        self.detail_tabs.setCurrentWidget(self.pseudocode)
        self.detail_stack.setCurrentWidget(self.detail_tabs)
        self._hide_find_panel()
        cursor = self.pseudocode.textCursor()
        self.goto_panel.query.setText(str(cursor.blockNumber() + 1))
        self.goto_panel.show()
        self._position_find_panel()
        self.goto_panel.query.setFocus()
        self.goto_panel.query.selectAll()

    def _hide_go_to_line_panel(self) -> None:
        self.goto_panel.hide()
        self.pseudocode.setFocus()

    def _go_to_requested_line(self) -> None:
        text = self.goto_panel.query.text().strip().replace(",", ":")
        parts = text.split(":", 1)
        try:
            line = int(parts[0])
            column = int(parts[1]) if len(parts) == 2 else 1
        except ValueError:
            self.statusBar().showMessage("Enter a line number or line:column")
            return
        if not 1 <= line <= self.pseudocode.blockCount() or column < 1:
            self.statusBar().showMessage("Line or column is out of range")
            return
        cursor = QTextCursor(self.pseudocode.document().findBlockByNumber(line - 1))
        cursor.movePosition(QTextCursor.MoveOperation.Right, QTextCursor.MoveMode.MoveAnchor, column - 1)
        self.pseudocode.setTextCursor(cursor)
        self.pseudocode.centerCursor()
        self._hide_go_to_line_panel()

    def export_pseudocode(self) -> None:
        result = self._selected_result()
        if result is None or result.pseudocode is None:
            self.statusBar().showMessage("No pseudocode to export")
            return
        filename, _ = QFileDialog.getSaveFileName(self, "Export pseudocode", "pseudocode.txt", "Text files (*.txt);;All files (*)")
        if filename:
            Path(filename).write_text(result.pseudocode.text, encoding="utf-8")

    def _update_cursor_status(self) -> None:
        cursor = self.pseudocode.textCursor()
        location = f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}"
        result = self._selected_result()
        if result is not None and result.pseudocode is not None:
            mappings = [
                item for item in result.pseudocode.source_map
                if item.start <= cursor.position() < item.end and item.source.offset is not None
            ]
            if mappings:
                source = min(mappings, key=lambda item: item.end - item.start).source
                location += f"  |  {source.frontend} @{source.offset}"
        self.statusBar().showMessage(location)

    def open_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open bytecode")
        if name:
            self._open(Path(name))

    def open_directory(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Open directory")
        if name:
            self._open(Path(name))

    def open_archive(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Open archive", "", "Archives (*.zip *.jar);;All files (*)")
        if name:
            self._open(Path(name))

    def open_path(self) -> None:
        name = QFileDialog.getExistingDirectory(self, "Open directory")
        if not name:
            name, _ = QFileDialog.getOpenFileName(self, "Open archive", "", "Archives (*.zip *.jar);;All files (*)")
        if name:
            self._open(Path(name))

    def _open(self, path: Path) -> None:
        self._opened_path = path
        self._remember_recent(path)
        self._history = []
        self._history_index = -1
        self._update_history_actions()
        try:
            self._entries = tuple(iter_input_entries(path))
        except Exception as error:
            self._decompile_failed(f"{type(error).__name__}: {error}")
            return
        self.results = ()
        self._resource_data = {}
        self._simulation_targets = {}
        self._simulation_result = None
        self._simulation_result_path = ""
        self._displayed_result_path = None
        self._displayed_function_id = None
        self._displayed_resource_path = None
        self._open_document_paths = []
        self._closed_document_paths = set()
        self._document_session_started = True
        self._refresh()
        if path.is_dir() or path.suffix.lower() in {".zip", ".jar"}:
            self.statusBar().showMessage(f"Discovered {len(self._entries)} artifact(s); select one to open")
            return
        if self._entries:
            self._load_entry(self._entries[0])

    def decompile_all_pending(self) -> None:
        pending = tuple(
            entry for entry in self._entries
            if not any(result.display_path == entry.display_path for result in self.results)
        )
        if not pending:
            self.statusBar().showMessage("No pending artifacts")
            return
        self._load_entries(pending)

    def _load_entry(self, entry: InputEntry) -> None:
        self._load_entries((entry,))

    def _load_entries(self, entries: tuple[InputEntry, ...]) -> None:
        if self._worker_thread is not None:
            self.statusBar().showMessage("Decompiler is already running")
            return
        if self._simulation_thread is not None:
            self.statusBar().showMessage("Simulation is already running")
            return
        pending = tuple(
            entry for entry in entries
            if not any(result.display_path == entry.display_path for result in self.results)
        )
        if not pending:
            if len(entries) == 1:
                self._refresh(entries[0].display_path)
            return
        self._cancelled = Event()
        if self.frontend_manager is not None:
            self.frontend_manager.set_mutation_enabled(False)
        self._open_completed_documents = len(pending) == 1
        self._worker_thread = QThread(self)
        self._worker = DecompileWorker(self.engine, pending, self._cancelled)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.progress.connect(self._update_progress)
        self._worker.completed.connect(self._decompile_completed)
        self._worker.completed.connect(self._worker_thread.quit)
        self._worker.failed.connect(self._decompile_failed)
        self._worker.failed.connect(self._worker_thread.quit)
        self._worker_thread.finished.connect(self._dispose_worker)
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_button.show()
        self.statusBar().showMessage(f"Decompiling {len(pending)} artifact(s)...")
        self._worker_thread.start()

    def _update_progress(self, current: int, total: int, display_path: str) -> None:
        self.progress.setMaximum(total)
        if total:
            self.progress.setValue(current)
        self.statusBar().showMessage(f"Decompiling {current}/{total}: {Path(display_path).name}")

    def _cancel_decompilation(self) -> None:
        if self._cancelled is not None:
            self._cancelled.set()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling after current artifact...")

    def _decompile_completed(self, result: object, cancelled: bool) -> None:
        completed = tuple(
            item for item in result
            if isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], DecompileResult)
        ) if isinstance(result, tuple) else ()
        results = tuple(item[0] for item in completed)
        for item, data in completed:
            if item.status == "resource" and isinstance(data, bytes):
                self._resource_data[item.display_path] = data
        if results:
            self.results = (*self.results, *results)
            selected_path = None
            if self._open_completed_documents:
                for item in results:
                    if (
                        item.display_path not in self._open_document_paths
                        and item.display_path not in self._closed_document_paths
                    ):
                        self._open_document_paths.append(item.display_path)
                selected_path = results[0].display_path
            self._refresh(selected_path)
        suffix = " (cancelled)" if cancelled else ""
        self.statusBar().showMessage(f"Loaded {len(results)} artifact(s){suffix}")

    def _decompile_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Open failed", message)

    def _dispose_worker(self) -> None:
        if self._worker is not None:
            self._worker.deleteLater()
        if self._worker_thread is not None:
            self._worker_thread.deleteLater()
        self._worker = None
        self._worker_thread = None
        self._cancelled = None
        self.progress.hide()
        self.cancel_button.hide()
        self.cancel_button.setEnabled(True)
        self._update_frontend_mutation_enabled()

    def _update_frontend_mutation_enabled(self) -> None:
        if self.frontend_manager is not None:
            self.frontend_manager.set_mutation_enabled(
                self._worker_thread is None and self._simulation_thread is None
            )

    def _entry_for_display_path(self, display_path: str) -> InputEntry | None:
        return next((entry for entry in self._entries if entry.display_path == display_path), None)

    def _sync_simulation_target(
        self,
        result: DecompileResult,
        function_id: str | None,
        *,
        prefer_tree_target: bool = True,
    ) -> None:
        if result.status != "ok":
            self.simulation_frontend.setText("Simulation is available only for recovered bytecode artifacts")
            self.simulation_target.clear()
            self.simulation_run.setEnabled(False)
            return
        listing = self._simulation_targets.get(result.display_path)
        if listing is None:
            entry = self._entry_for_display_path(result.display_path)
            if entry is None:
                self.simulation_frontend.setText("Simulation input data is unavailable")
                self.simulation_run.setEnabled(False)
                return
            if self._simulation_thread is None:
                self._start_simulation_worker(entry)
            self.simulation_frontend.setText("Discovering simulation targets...")
            self.simulation_run.setEnabled(False)
            return
        frontend = listing.frontend_id or "unavailable"
        self.simulation_frontend.setText(f"Frontend: {frontend}")
        previous = self.simulation_target.currentData()
        preserve_previous = (
            previous if self._simulation_target_path == result.display_path else None
        )
        self.simulation_target.blockSignals(True)
        try:
            self.simulation_target.clear()
            for target in listing.targets:
                self.simulation_target.addItem(target.label, target)
            if prefer_tree_target and function_id is not None:
                index = next(
                    (
                        target.function_index
                        for target in listing.targets
                        if target.function_index < len(result.functions)
                        and result.functions[target.function_index].id == function_id
                    ),
                    None,
                )
                if index is not None:
                    for row in range(self.simulation_target.count()):
                        target = self.simulation_target.itemData(row)
                        if isinstance(target, SimulationTarget) and target.function_index == index:
                            self.simulation_target.setCurrentIndex(row)
                            break
            elif isinstance(preserve_previous, SimulationTarget):
                for row in range(self.simulation_target.count()):
                    target = self.simulation_target.itemData(row)
                    if (
                        isinstance(target, SimulationTarget)
                        and target.query == preserve_previous.query
                        and target.function_index == preserve_previous.function_index
                    ):
                        self.simulation_target.setCurrentIndex(row)
                        break
        finally:
            self.simulation_target.blockSignals(False)
        self._simulation_target_path = result.display_path
        # A healthy target refresh must not erase a completed run status while
        # the worker thread is being disposed.
        if listing.diagnostic:
            self.simulation_status.setText(listing.diagnostic)
        self.simulation_run.setEnabled(bool(listing.targets) and self._simulation_thread is None)
        self._simulation_target_changed()

    def _simulation_tab_changed(self, _index: int) -> None:
        if self.detail_tabs.currentWidget() is not self.simulation_panel:
            return
        result = self._selected_result()
        if not isinstance(result, DecompileResult):
            self.simulation_frontend.setText("Select a recovered artifact to simulate")
            self.simulation_target.clear()
            self.simulation_run.setEnabled(False)
            return
        item = self.input_tree.currentItem()
        data = None if item is None else item.data(0, Qt.ItemDataRole.UserRole)
        self._sync_simulation_target(result, data[1] if isinstance(data, tuple) else None)

    def _simulation_target_changed(self, *_unused: object) -> None:
        target = self.simulation_target.currentData()
        if isinstance(target, SimulationTarget):
            params = ", ".join(target.params) or "no parameters"
            self.simulation_args.setToolTip(f"Target parameters: {params}")
        else:
            self.simulation_args.setToolTip("")

    def _choose_simulation_runtime(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Choose runtime", "", "Python files (*.py)")
        if filename:
            self.simulation_runtime.setText(filename)
            self._remember_simulation_runtime()

    def _remember_simulation_runtime(self) -> None:
        self._settings.setValue("simulation_runtime", self.simulation_runtime.text().strip())

    def _start_simulation_worker(
        self,
        entry: InputEntry,
        *,
        target: SimulationTarget | None = None,
        args: tuple[object, ...] = (),
        runtime_path: Path | None = None,
        limits: SimulationLimits | None = None,
        cancellation: SimulationCancellation | None = None,
    ) -> None:
        if self._simulation_thread is not None:
            return
        self._simulation_job_path = entry.display_path
        self._simulation_thread = QThread(self)
        self._simulation_worker = SimulationWorker(
            self.engine,
            entry,
            target=target,
            args=args,
            runtime_path=runtime_path,
            limits=limits,
            cancellation=cancellation,
        )
        self._simulation_worker.moveToThread(self._simulation_thread)
        self._simulation_thread.started.connect(self._simulation_worker.run)
        self._simulation_worker.completed.connect(self._simulation_completed)
        self._simulation_worker.completed.connect(self._simulation_thread.quit)
        self._simulation_worker.failed.connect(self._simulation_failed)
        self._simulation_worker.failed.connect(self._simulation_thread.quit)
        self._simulation_thread.finished.connect(self._dispose_simulation_worker)
        self._update_frontend_mutation_enabled()
        self._simulation_thread.start()

    def _run_simulation(self) -> None:
        result = self._selected_result()
        target = self.simulation_target.currentData()
        if not isinstance(result, DecompileResult) or not isinstance(target, SimulationTarget):
            self.simulation_status.setText("Select a simulation target first")
            return
        if self._worker_thread is not None:
            self.simulation_status.setText("Wait for decompilation to finish")
            return
        entry = self._entry_for_display_path(result.display_path)
        if entry is None:
            self.simulation_status.setText("Simulation input data is unavailable")
            return
        try:
            parsed_args = json.loads(self.simulation_args.text())
            if not isinstance(parsed_args, list):
                raise ValueError("arguments must be a JSON array")
            runtime = self.simulation_runtime.text().strip()
            runtime_path = Path(runtime) if runtime else None
            limits = SimulationLimits(
                self.simulation_steps.value(),
                self.simulation_depth.value(),
                self.simulation_trace_limit.value(),
            )
            limits.validate()
        except (ValueError, json.JSONDecodeError) as error:
            self.simulation_status.setText(f"Invalid simulation request: {error}")
            return
        self._simulation_cancellation = SimulationCancellation()
        self.simulation_run.setEnabled(False)
        self.simulation_cancel.show()
        self.simulation_status.setText("Simulating...")
        self._start_simulation_worker(
            entry,
            target=target,
            args=tuple(parsed_args),
            runtime_path=runtime_path,
            limits=limits,
            cancellation=self._simulation_cancellation,
        )

    def _cancel_simulation(self) -> None:
        if self._simulation_cancellation is not None:
            self._simulation_cancellation.cancel()
            self.simulation_cancel.setEnabled(False)
            self.simulation_status.setText("Cancelling simulation...")

    def _simulation_completed(self, kind: str, payload: object) -> None:
        if kind == "targets" and isinstance(payload, SimulationTargetListing):
            self._simulation_targets[self._simulation_job_path] = payload
            result = self._selected_result()
            if isinstance(result, DecompileResult) and result.display_path == self._simulation_job_path:
                data = self.input_tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
                function_id = data[1] if isinstance(data, tuple) else None
                self._sync_simulation_target(result, function_id)
            return
        if kind == "run" and isinstance(payload, SimulationResult):
            self._simulation_result = payload
            self._simulation_result_path = self._simulation_job_path
            suffix = " (trace truncated)" if payload.trace_truncated else ""
            outcome = (
                f"returned {payload.values!r}"
                if payload.status.value == "completed"
                else (payload.diagnostic or repr(payload.exception) or "no result")
            )
            self.simulation_status.setText(
                f"{payload.status.value}; {payload.steps} steps; {outcome}{suffix}"
            )
            self.simulation_details.setPlainText(json.dumps({
                "status": payload.status.value,
                "values": payload.values,
                "exception": payload.exception,
                "cause": payload.cause,
                "locals": payload.locals,
                "steps": payload.steps,
                "diagnostic": payload.diagnostic,
                "trace_truncated": payload.trace_truncated,
            }, default=repr, indent=2, sort_keys=True))
            self._refresh_simulation_trace()

    def _simulation_failed(self, message: str) -> None:
        self._simulation_targets.setdefault(
            self._simulation_job_path,
            SimulationTargetListing(None, diagnostic=message),
        )
        self.simulation_status.setText(f"Simulation failed: {message}")

    def _dispose_simulation_worker(self) -> None:
        if self._simulation_worker is not None:
            self._simulation_worker.deleteLater()
        if self._simulation_thread is not None:
            self._simulation_thread.deleteLater()
        self._simulation_worker = None
        self._simulation_thread = None
        self._simulation_cancellation = None
        self.simulation_cancel.hide()
        self.simulation_cancel.setEnabled(True)
        selected = self._selected_result()
        if isinstance(selected, DecompileResult) and self.detail_tabs.currentWidget() is self.simulation_panel:
            data = self.input_tree.currentItem().data(0, Qt.ItemDataRole.UserRole)
            self._sync_simulation_target(
                selected,
                data[1] if isinstance(data, tuple) else None,
                prefer_tree_target=False,
            )
        self._update_frontend_mutation_enabled()

    def _clear_simulation(self) -> None:
        self._simulation_result = None
        self.simulation_trace.setRowCount(0)
        self.simulation_details.clear()
        self.simulation_status.clear()

    def _refresh_simulation_trace(self) -> None:
        self.simulation_trace.setRowCount(0)
        result = self._simulation_result
        if result is None:
            return
        show_all = bool(self.simulation_filter.currentData())
        for index, event in enumerate(result.events):
            if not show_all and event.kind not in {"external-call", "trace-truncated"}:
                continue
            row = self.simulation_trace.rowCount()
            self.simulation_trace.insertRow(row)
            output = " ".join(part for part in (event.stdout.strip(), event.stderr.strip()) if part)
            values = (
                str(index), event.function, event.block or "", event.kind, event.detail,
                "" if getattr(event.source, "offset", None) is None else str(event.source.offset), output,
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, event)
                self.simulation_trace.setItem(row, column, item)

    def _simulation_event_selected(self) -> None:
        item = self.simulation_trace.currentItem()
        event = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        if event is None:
            return
        self.simulation_details.setPlainText(json.dumps({
            "args": event.args,
            "values": event.values,
            "exception": event.exception,
            "locals": event.locals,
            "stdout": event.stdout,
            "stderr": event.stderr,
        }, default=repr, indent=2, sort_keys=True))

    def _simulation_event_activated(self, row: int, _column: int) -> None:
        item = self.simulation_trace.item(row, 0)
        event = None if item is None else item.data(Qt.ItemDataRole.UserRole)
        if event is None:
            return
        result = next(
            (candidate for candidate in self.results if candidate.display_path == self._simulation_result_path),
            None,
        )
        if not isinstance(result, DecompileResult) or event.source is None:
            return
        if 0 <= event.function_index < len(result.functions):
            function_id = result.functions[event.function_index].id
            self._navigate_to_function(function_id, result)
            self.detail_stack.setCurrentWidget(self.detail_tabs)
            self.detail_tabs.setCurrentWidget(self.pseudocode)
            self._focus_source(result, event.source, function_id)
            self.pseudocode.centerCursor()

    def _refresh(self, selected_path: str | None = None) -> None:
        self._update_frontend_status()
        scroll_position = self.input_tree.verticalScrollBar().value()
        self._displayed_result_path = None
        self._displayed_function_id = None
        self._displayed_resource_path = None
        self.input_tree.clear()
        self.diagnostics.setRowCount(0)
        results_by_path = {result.display_path: result for result in self.results}
        if (
            not self._document_session_started
            and len(self.results) == 1
            and self.results[0].display_path not in self._closed_document_paths
        ):
            self._open_document_paths = [self.results[0].display_path]
            self._document_session_started = True
        self._open_document_paths = [
            path for path in self._open_document_paths if path in results_by_path
        ]
        self.document_tabs.blockSignals(True)
        try:
            while self.document_tabs.count():
                self.document_tabs.removeTab(0)
            for path in self._open_document_paths:
                tab_index = self.document_tabs.addTab(self._document_title(results_by_path[path]))
                self.document_tabs.setTabIcon(tab_index, self._result_icon(results_by_path[path]))
        finally:
            self.document_tabs.blockSignals(False)
        self.detail_tabs.setCurrentWidget(self.pseudocode)
        self.detail_stack.setCurrentWidget(self.detail_tabs if self.results else self.welcome_page)
        entries = self._entries or tuple(
            InputEntry(Path(result.display_path.partition("!")[0]), result.display_path)
            for result in self.results
        )
        for entry in entries:
            result = results_by_path.get(entry.display_path)
            status = "pending" if result is None else result.status
            frontend = "" if result is None else (result.frontend_id or "resource")
            node = QTreeWidgetItem([self._relative_display_path(entry.display_path), status, frontend])
            node.setData(0, Qt.ItemDataRole.UserRole, entry if result is None else result)
            node.setIcon(0, self._entry_icon(entry, status))
            node.setToolTip(0, entry.display_path)
            node.setForeground(1, _status_color(status, self._theme.colors))
            self.input_tree.addTopLevelItem(node)
            if result is None:
                continue
            for function in result.functions:
                child = QTreeWidgetItem([function.name, function.status, frontend])
                child.setData(0, Qt.ItemDataRole.UserRole, (result, function.id))
                child.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_CommandLink))
                child.setForeground(1, _status_color(function.status, self._theme.colors))
                node.addChild(child)
            self._add_diagnostics(result)
        self.input_tree.collapseAll()
        if selected_path is None and len(self.results) == 1:
            selected_path = self.results[0].display_path
        if selected_path is not None:
            for index in range(self.input_tree.topLevelItemCount()):
                root = self.input_tree.topLevelItem(index)
                data = root.data(0, Qt.ItemDataRole.UserRole)
                display_path = data.display_path if isinstance(data, (InputEntry, DecompileResult)) else None
                if display_path == selected_path:
                    self.input_tree.setCurrentItem(root)
                    break
        self.input_tree.verticalScrollBar().setValue(
            min(scroll_position, self.input_tree.verticalScrollBar().maximum())
        )
        self._invalidate_global_search()

    def _filter_inputs(self, query: str) -> None:
        needle = query.casefold().strip()
        for root_index in range(self.input_tree.topLevelItemCount()):
            root = self.input_tree.topLevelItem(root_index)
            child_matches = False
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                visible = not needle or needle in child.text(0).casefold() or needle in child.text(1).casefold()
                child.setHidden(not visible)
                child_matches = child_matches or visible
            root_visible = not needle or needle in root.text(0).casefold() or needle in root.text(1).casefold() or child_matches
            root.setHidden(not root_visible)
            root.setExpanded(bool(needle and child_matches))

    def _remember_recent(self, path: Path) -> None:
        paths = [entry for entry in self._settings.value("recent_paths", [], type=list) if entry != str(path)]
        self._settings.setValue("recent_paths", [str(path), *paths][:10])
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self.recent_menu.clear()
        paths = self._settings.value("recent_paths", [], type=list)
        if not paths:
            self.recent_menu.setEnabled(False)
            return
        self.recent_menu.setEnabled(True)
        for value in paths:
            path = Path(value)
            action = QAction(path.name or str(path), self)
            action.setToolTip(str(path))
            action.triggered.connect(lambda _checked=False, selected=path: self._open(selected))
            self.recent_menu.addAction(action)
        if hasattr(self, "welcome_recent_layout"):
            self._rebuild_welcome_recent()

    def _rebuild_welcome_recent(self) -> None:
        while self.welcome_recent_layout.count():
            item = self.welcome_recent_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        paths = [Path(value) for value in self._settings.value("recent_paths", [], type=list)]
        self.welcome_recent.setVisible(bool(paths))
        self.welcome_recent_label.setVisible(bool(paths))
        for path in paths:
            button = QPushButton(path.name or str(path))
            button.setObjectName("welcomeRecentItem")
            button.setToolTip(str(path))
            button.setIcon(self.style().standardIcon(
                QStyle.StandardPixmap.SP_DirIcon if path.is_dir() else QStyle.StandardPixmap.SP_FileIcon
            ))
            button.clicked.connect(lambda _checked=False, selected=path: self._open(selected))
            self.welcome_recent_layout.addWidget(button)

    def _record_location(self, result: DecompileResult, function_id: str | None) -> None:
        if self._restoring_history:
            return
        location = (result.display_path, function_id)
        if self._history_index >= 0 and self._history[self._history_index] == location:
            return
        self._history = self._history[: self._history_index + 1]
        self._history.append(location)
        self._history_index = len(self._history) - 1
        self._update_history_actions()

    def _update_history_actions(self) -> None:
        self.back_action.setEnabled(self._history_index > 0)
        self.forward_action.setEnabled(self._history_index + 1 < len(self._history))

    def go_back(self) -> None:
        if self._history_index > 0:
            self._history_index -= 1
            self._restore_history_location()

    def go_forward(self) -> None:
        if self._history_index + 1 < len(self._history):
            self._history_index += 1
            self._restore_history_location()

    def _restore_history_location(self) -> None:
        display_path, function_id = self._history[self._history_index]
        self._restoring_history = True
        restored = False
        try:
            for root_index in range(self.input_tree.topLevelItemCount()):
                root = self.input_tree.topLevelItem(root_index)
                data = root.data(0, Qt.ItemDataRole.UserRole)
                if not isinstance(data, DecompileResult) or data.display_path != display_path:
                    continue
                target = root
                for child_index in range(root.childCount()):
                    child = root.child(child_index)
                    child_data = child.data(0, Qt.ItemDataRole.UserRole)
                    if isinstance(child_data, tuple) and child_data[1] == function_id:
                        target = child
                        root.setExpanded(True)
                        break
                self.input_tree.blockSignals(True)
                try:
                    self.input_tree.setCurrentItem(target)
                finally:
                    self.input_tree.blockSignals(False)
                self._tree_selected()
                restored = True
                break
        finally:
            self._restoring_history = False
            self._update_history_actions()
        if not restored:
            self._update_history_actions()

    def _document_selected(self, index: int) -> None:
        if not 0 <= index < len(self._open_document_paths):
            return
        display_path = self._open_document_paths[index]
        for root_index in range(self.input_tree.topLevelItemCount()):
            root = self.input_tree.topLevelItem(root_index)
            data = root.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, DecompileResult) and data.display_path == display_path:
                self.input_tree.setCurrentItem(root)
                return

    def _close_document(self, index: int) -> None:
        closed = self._close_documents((index,))
        if not closed and 0 <= index < len(self._open_document_paths):
            self.statusBar().showMessage("Unpin this document before closing it")

    def _close_documents(self, indexes: tuple[int, ...]) -> int:
        closed = 0
        self.document_tabs.blockSignals(True)
        try:
            for index in sorted(set(indexes), reverse=True):
                if not 0 <= index < len(self._open_document_paths):
                    continue
                if self._open_document_paths[index] in self._pinned_documents:
                    continue
                self._closed_document_paths.add(self._open_document_paths[index])
                self._open_document_paths.pop(index)
                self.document_tabs.removeTab(index)
                closed += 1
        finally:
            self.document_tabs.blockSignals(False)
        if not self._open_document_paths:
            self._document_session_started = True
            self.input_tree.blockSignals(True)
            try:
                self.input_tree.clearSelection()
                self.input_tree.setCurrentItem(None)
            finally:
                self.input_tree.blockSignals(False)
            self._displayed_result_path = None
            self._displayed_function_id = None
            self._displayed_resource_path = None
            self.detail_stack.setCurrentWidget(self.welcome_page)
        return closed

    def _document_tab_moved(self, source: int, destination: int) -> None:
        if not (0 <= source < len(self._open_document_paths) and 0 <= destination < len(self._open_document_paths)):
            return
        display_path = self._open_document_paths.pop(source)
        self._open_document_paths.insert(destination, display_path)

    def _move_document(self, source: int, destination: int) -> None:
        if not (0 <= source < self.document_tabs.count() and 0 <= destination < self.document_tabs.count()):
            return
        self.document_tabs.moveTab(source, destination)

    def _document_context_menu(self, position) -> None:
        index = self.document_tabs.tabAt(position)
        if not 0 <= index < len(self._open_document_paths):
            return
        menu = QMenu(self)
        display_path = self._open_document_paths[index]
        pin = QAction("Unpin" if display_path in self._pinned_documents else "Pin", self)
        pin.triggered.connect(lambda: self._toggle_document_pin(index))
        close = QAction("Close", self)
        close.triggered.connect(lambda: self._close_document(index))
        close_others = QAction("Close others", self)
        close_others.triggered.connect(lambda: self._close_documents(tuple(
            item for item in range(self.document_tabs.count()) if item != index
        )))
        close_left = QAction("Close to the left", self)
        close_left.setEnabled(index > 0)
        close_left.triggered.connect(lambda: self._close_documents(tuple(range(index))))
        close_right = QAction("Close to the right", self)
        close_right.setEnabled(index + 1 < self.document_tabs.count())
        close_right.triggered.connect(lambda: self._close_documents(tuple(range(index + 1, self.document_tabs.count()))))
        close_all = QAction("Close all", self)
        close_all.triggered.connect(lambda: self._close_documents(tuple(range(self.document_tabs.count()))))
        move_left = QAction("Move left", self)
        move_left.setEnabled(index > 0)
        move_left.triggered.connect(lambda: self._move_document(index, index - 1))
        move_right = QAction("Move right", self)
        move_right.setEnabled(index + 1 < self.document_tabs.count())
        move_right.triggered.connect(lambda: self._move_document(index, index + 1))
        menu.addActions((pin, close, close_others, close_left, close_right, close_all))
        menu.addSeparator()
        menu.addActions((move_left, move_right))
        menu.exec(self.document_tabs.mapToGlobal(position))

    def _toggle_document_pin(self, index: int) -> None:
        if not 0 <= index < len(self._open_document_paths):
            return
        display_path = self._open_document_paths[index]
        if display_path in self._pinned_documents:
            self._pinned_documents.remove(display_path)
            message = "Document unpinned"
        else:
            self._pinned_documents.add(display_path)
            message = "Document pinned"
        result = next(result for result in self.results if result.display_path == display_path)
        self.document_tabs.setTabText(index, self._document_title(result))
        self.statusBar().showMessage(message)

    def _document_title(self, result: DecompileResult) -> str:
        name = Path(result.display_path.partition("!")[0]).name
        return f"* {name}" if result.display_path in self._pinned_documents else name

    def _entry_icon(self, entry: InputEntry, status: str):
        if status in {"error", "partial", "unsupported", "resource"}:
            return self._status_icon(status)
        path = entry.display_path.partition("!")[0]
        if "!" in entry.display_path or Path(path).suffix.lower() in {".zip", ".jar"}:
            return self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon)
        return self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)

    def _result_icon(self, result: DecompileResult):
        return self._status_icon(result.status)

    def _status_icon(self, status: str):
        icon = {
            "ok": QStyle.StandardPixmap.SP_DialogApplyButton,
            "partial": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "unsupported": QStyle.StandardPixmap.SP_MessageBoxWarning,
            "error": QStyle.StandardPixmap.SP_MessageBoxCritical,
            "resource": QStyle.StandardPixmap.SP_FileIcon,
            "pending": QStyle.StandardPixmap.SP_BrowserReload,
        }.get(status, QStyle.StandardPixmap.SP_FileIcon)
        return self.style().standardIcon(icon)

    def _relative_display_path(self, display_path: str) -> str:
        """Keep the navigation tree concise without changing result provenance."""
        if self._opened_path is None:
            return display_path
        archive, marker, member = display_path.partition("!")
        if marker:
            return f"{Path(archive).name}!{member}"
        artifact = Path(display_path)
        if self._opened_path.is_dir():
            try:
                return str(artifact.relative_to(self._opened_path))
            except ValueError:
                pass
        return artifact.name

    def _add_diagnostics(self, result: DecompileResult) -> None:
        for diagnostic in result.diagnostics:
            row = self.diagnostics.rowCount()
            self.diagnostics.insertRow(row)
            values = (diagnostic.severity, diagnostic.function or "", "" if diagnostic.offset is None else str(diagnostic.offset), diagnostic.message, " | ".join(diagnostic.raw_context))
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setForeground(_diagnostic_color(diagnostic.severity, self._theme.colors))
                self.diagnostics.setItem(row, column, cell)

    def _tree_selected(self) -> None:
        selected = self.input_tree.selectedItems()
        if not selected:
            return
        data = selected[0].data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, InputEntry):
            self._load_entry(data)
            return
        result = data[0] if isinstance(data, tuple) else data
        function_id = data[1] if isinstance(data, tuple) else None
        if not isinstance(result, DecompileResult):
            return
        if result.display_path in self._closed_document_paths:
            return
        root = selected[0]
        while root.parent() is not None:
            root = root.parent()
        if result.display_path not in self._open_document_paths:
            self._closed_document_paths.discard(result.display_path)
            self._open_document_paths.append(result.display_path)
            tab_index = self.document_tabs.addTab(self._document_title(result))
            self.document_tabs.setTabIcon(tab_index, self._result_icon(result))
        tab_index = self._open_document_paths.index(result.display_path)
        if self.document_tabs.currentIndex() != tab_index:
            self.document_tabs.blockSignals(True)
            try:
                self.document_tabs.setCurrentIndex(tab_index)
            finally:
                self.document_tabs.blockSignals(False)
        self._record_location(result, function_id)
        if result.status == "resource":
            if self.detail_tabs.currentWidget() is self.simulation_panel:
                self._sync_simulation_target(result, function_id)
            self._display_resource(result)
            self.detail_stack.setCurrentWidget(self.resource_tabs)
            return
        self.detail_stack.setCurrentWidget(self.detail_tabs)
        result_changed = self._displayed_result_path != result.display_path
        function_changed = result_changed or self._displayed_function_id != function_id
        if result_changed:
            self.pseudocode.setPlainText("" if result.pseudocode is None else result.pseudocode.text)
            self._populate_ast(result.ast, result)
            self._displayed_result_path = result.display_path
        if function_changed:
            self._populate_bytecode(result, function_id)
            self._populate_references(result, function_id)
            self._populate_call_hierarchy(result, function_id)
            self._populate_browse(result, function_id)
            self._populate_control_flow(result, function_id)
            self._displayed_function_id = function_id
        if function_id:
            self._select_function(function_id, result)
        if self.detail_tabs.currentWidget() is self.simulation_panel:
            self._sync_simulation_target(result, function_id)

    def _reopen_closed_tree_item(self, item: QTreeWidgetItem, _column: int) -> None:
        """Only a direct navigation action may reopen a user-closed document."""
        data = item.data(0, Qt.ItemDataRole.UserRole)
        result = data[0] if isinstance(data, tuple) else data
        if not isinstance(result, DecompileResult):
            return
        if result.display_path not in self._closed_document_paths:
            return
        self._closed_document_paths.remove(result.display_path)
        self._tree_selected()

    def _display_resource(self, result: DecompileResult) -> None:
        if self._displayed_resource_path == result.display_path:
            return
        data = self._resource_data.get(result.display_path)
        if data is None:
            self.resource_text.setPlainText("Resource data is unavailable.")
            self.resource_binary.clear()
            self._displayed_resource_path = result.display_path
            return
        text = _decode_resource_text(data)
        if text is None:
            self.resource_text.setPlainText("Binary resource. Open the Binary view to inspect its bytes.")
            self.resource_tabs.setCurrentWidget(self.resource_binary)
        else:
            self.resource_text.setPlainText(text)
            self.resource_tabs.setCurrentWidget(self.resource_text)
        self.resource_binary.setPlainText(_hex_dump(data))
        self._displayed_resource_path = result.display_path

    def _activate_function(self, name: str) -> None:
        result = self._selected_result()
        if not isinstance(result, DecompileResult):
            return
        function = next((item for item in result.functions if item.name == name), None)
        if function is None:
            self.statusBar().showMessage(f"No function named {name}")
            return
        self._navigate_to_function(function.id, result)

    def _navigate_to_function(self, function_id: str, result: DecompileResult) -> None:
        item = self._find_function_item(function_id)
        if item is None:
            return
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)
        if self.input_tree.currentItem() is not item:
            self.input_tree.setCurrentItem(item)
        else:
            self._populate_bytecode(result, function_id)
            self._select_function(function_id, result)
        self.input_tree.scrollToItem(item)

    def _find_function_item(self, function_id: str) -> QTreeWidgetItem | None:
        for root_index in range(self.input_tree.topLevelItemCount()):
            root = self.input_tree.topLevelItem(root_index)
            for child_index in range(root.childCount()):
                child = root.child(child_index)
                data = child.data(0, Qt.ItemDataRole.UserRole)
                if isinstance(data, tuple) and data[1] == function_id:
                    return child
        return None

    def _select_function(self, function_id: str, result: DecompileResult) -> None:
        if result.pseudocode is None:
            return
        mapping = next(
            (item for item in result.pseudocode.source_map if item.function_id == function_id),
            None,
        )
        if mapping is None:
            self.statusBar().showMessage("No associated pseudocode")
            return
        cursor = self.pseudocode.textCursor()
        cursor.setPosition(mapping.start)
        cursor.setPosition(mapping.end, QTextCursor.MoveMode.KeepAnchor)
        self.pseudocode.blockSignals(True)
        try:
            self.pseudocode.setTextCursor(cursor)
        finally:
            self.pseudocode.blockSignals(False)

    def _populate_bytecode(self, result: DecompileResult, function_id: str | None) -> None:
        rows = [row for row in result.instructions if function_id is None or row.function_id == function_id]
        self.bytecode.setRowCount(len(rows))
        for index, row in enumerate(rows):
            values = ("" if row.offset is None else str(row.offset), row.opcode, ", ".join(row.operands), row.raw)
            for column, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setData(
                    Qt.ItemDataRole.UserRole,
                    {"function_id": row.function_id, "source": row.source},
                )
                self.bytecode.setItem(index, column, cell)

    def _populate_ast(self, ast: object | None, result: DecompileResult) -> None:
        self.ast_tree.clear()
        if ast is not None:
            ids_by_name = {function.name: function.id for function in result.functions}
            self.ast_tree.addTopLevelItem(_ast_item(ast, ids_by_name))
            self.ast_tree.expandToDepth(2)

    def _populate_references(self, result: DecompileResult, function_id: str | None) -> None:
        references = [
            reference for reference in result.symbols.references
            if function_id is None or reference.function_id == function_id
        ]
        self.references.setRowCount(len(references))
        function_names = {function.id: function.name for function in result.functions}
        for row, reference in enumerate(references):
            values = (
                reference.kind,
                reference.name,
                function_names.get(reference.function_id, reference.function_id),
                "" if reference.source is None or reference.source.offset is None else str(reference.source.offset),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, reference)
                self.references.setItem(row, column, item)

    def _populate_browse(self, result: DecompileResult, function_id: str | None) -> None:
        self._browse_result = result
        self._browse_function_id = function_id
        self._refresh_browse()

    def _refresh_browse(self) -> None:
        result = getattr(self, "_browse_result", None)
        if not isinstance(result, DecompileResult):
            return
        function_id = getattr(self, "_browse_function_id", None)
        kind = self.browse_filter.currentData()
        entries = [
            entry for entry in result.browse.entries
            if (function_id is None or entry.function_id == function_id)
            and (kind is None or entry.kind == kind)
        ]
        function_names = {function.id: function.name for function in result.functions}
        self.browse.setRowCount(len(entries))
        for row, entry in enumerate(entries):
            values = (
                entry.kind,
                entry.name,
                entry.value,
                function_names.get(entry.function_id, entry.function_id),
                "" if entry.source is None or entry.source.offset is None else str(entry.source.offset),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, entry)
                self.browse.setItem(row, column, item)

    def _populate_call_hierarchy(self, result: DecompileResult, function_id: str | None) -> None:
        self.call_hierarchy.clear()
        if function_id is None:
            self.call_hierarchy.addTopLevelItem(QTreeWidgetItem(["Select a function", ""]))
            return
        function_names = {function.id: function.name for function in result.functions}
        calls = QTreeWidgetItem(["Calls", ""])
        callers = QTreeWidgetItem(["Called by", ""])
        self.call_hierarchy.addTopLevelItems((calls, callers))
        for reference in result.symbols.references:
            if reference.kind != "call" or reference.function_id != function_id:
                continue
            targets = self._call_targets(reference, result)
            if len(targets) == 1:
                target_result, target_id, resolution = targets[0]
                node = QTreeWidgetItem([reference.name, resolution])
                node.setData(0, Qt.ItemDataRole.UserRole, (target_result, target_id))
            elif len(targets) > 1:
                node = QTreeWidgetItem([reference.name, f"ambiguous ({len(targets)} candidates)"])
            else:
                node = QTreeWidgetItem([reference.name, "unresolved"])
            calls.addChild(node)
        source_name = function_names.get(function_id, function_id)
        target_symbol_ids = {symbol.id for symbol in result.symbols.symbols if symbol.kind == "function" and symbol.function_id == function_id}
        for candidate_result in self.results:
            candidate_names = {function.id: function.name for function in candidate_result.functions}
            for reference in candidate_result.symbols.references:
                if reference.kind != "call":
                    continue
                direct = bool(target_symbol_ids.intersection(reference.target_ids))
                candidate = not reference.target_ids and reference.name == source_name
                if not direct and not candidate:
                    continue
                caller_name = candidate_names.get(reference.function_id, reference.function_id)
                node = QTreeWidgetItem([caller_name, "resolved" if direct else "candidate"])
                node.setData(0, Qt.ItemDataRole.UserRole, (candidate_result, reference.function_id))
                callers.addChild(node)
        calls.setExpanded(True)
        callers.setExpanded(True)

    def _call_targets(self, reference: object, result: DecompileResult) -> list[tuple[DecompileResult, str, str]]:
        local_ids = {
            symbol.id: symbol.function_id
            for symbol in result.symbols.symbols
            if symbol.kind == "function"
        }
        direct = [
            (result, local_ids[symbol_id], "resolved")
            for symbol_id in reference.target_ids
            if symbol_id in local_ids
        ]
        if direct:
            return direct
        return [
            (candidate_result, symbol.function_id, "candidate")
            for candidate_result in self.results
            for symbol in candidate_result.symbols.symbols
            if symbol.kind == "function" and symbol.name == reference.name
        ]

    def _populate_control_flow(self, result: DecompileResult, function_id: str | None) -> None:
        graphs = [graph for graph in result.control_flow if function_id is None or graph.function_id == function_id]
        self.control_flow_view.set_graph(graphs[0] if len(graphs) == 1 else None, self._theme.colors)
        rows = [
            (graph, block)
            for graph in graphs
            for block in graph.blocks
        ]
        self.control_flow.setRowCount(len(rows))
        for row, (graph, block) in enumerate(rows):
            edges = ", ".join(
                f"{edge.kind}:{edge.target}"
                for edge in graph.edges
                if edge.source == block.id
            )
            values = (block.id, str(block.statement_count), block.terminator or "", edges)
            for column, value in enumerate(values):
                self.control_flow.setItem(row, column, QTableWidgetItem(value))

    def _bytecode_selected(self) -> None:
        item = self.bytecode.currentItem()
        if item:
            self.detail_tabs.setCurrentWidget(self.bytecode)
            data = item.data(Qt.ItemDataRole.UserRole)
            result = self._selected_result()
            if isinstance(data, dict) and isinstance(result, DecompileResult):
                self._focus_source(result, data["source"], data["function_id"])

    def _ast_selected(self) -> None:
        selected = self.ast_tree.selectedItems()
        result = self._selected_result()
        if selected and isinstance(result, DecompileResult):
            self.detail_tabs.setCurrentWidget(self.ast_tree)
            data = selected[0].data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("source") is not None:
                self._focus_source(result, data["source"], data.get("function_id"), select_ast=False)

    def _reference_selected(self) -> None:
        item = self.references.currentItem()
        result = self._selected_result()
        if item is None or not isinstance(result, DecompileResult):
            return
        reference = item.data(Qt.ItemDataRole.UserRole)
        if reference.source is not None:
            self._focus_source(result, reference.source, reference.function_id)

    def _browse_selected(self) -> None:
        item = self.browse.currentItem()
        result = self._selected_result()
        if item is None or not isinstance(result, DecompileResult):
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        if entry.source is not None:
            self._focus_source(result, entry.source, entry.function_id)

    def _control_flow_activated(self, source: object, function_id: str) -> None:
        result = self._selected_result()
        if isinstance(result, DecompileResult):
            self._focus_source(result, source, function_id)

    def _call_hierarchy_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2 and isinstance(data[0], DecompileResult):
            self._navigate_to_function(data[1], data[0])

    def _reference_activated(self, row: int, _column: int) -> None:
        result = self._selected_result()
        item = self.references.item(row, 0)
        if item is None or not isinstance(result, DecompileResult):
            return
        reference = item.data(Qt.ItemDataRole.UserRole)
        targets = self._call_targets(reference, result)
        if len(targets) == 1:
            self._navigate_to_function(targets[0][1], targets[0][0])
        elif len(targets) > 1:
            self.statusBar().showMessage(f"Ambiguous cross-artifact target: {reference.name} ({len(targets)} candidates)")
        else:
            self.statusBar().showMessage(f"No known target for {reference.name}")

    def _pseudocode_selected(self) -> None:
        result = self._selected_result()
        if not isinstance(result, DecompileResult) or result.pseudocode is None:
            return
        position = self.pseudocode.textCursor().position()
        candidates = [item for item in result.pseudocode.source_map if item.start <= position < item.end]
        if not candidates:
            self.statusBar().showMessage("No associated bytecode")
            return
        mapping = min(candidates, key=lambda item: item.end - item.start)
        if mapping.source.offset is None:
            self.statusBar().showMessage("No associated bytecode")
            return
        self._focus_source(result, mapping.source, mapping.function_id)

    def _focus_source(
        self,
        result: DecompileResult,
        source: object,
        function_id: str | None,
        *,
        select_ast: bool = True,
    ) -> None:
        if source is None or function_id is None:
            self.statusBar().showMessage("No associated bytecode")
            return
        self._populate_bytecode(result, function_id)
        for row in range(self.bytecode.rowCount()):
            data = self.bytecode.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and _same_source(data["source"], source):
                self.bytecode.blockSignals(True)
                try:
                    self.bytecode.selectRow(row)
                finally:
                    self.bytecode.blockSignals(False)
                break
        else:
            self.statusBar().showMessage("No associated bytecode")
            return
        if result.pseudocode is not None:
            ranges = [
                item for item in result.pseudocode.source_map
                if _same_source(item.source, source) and item.function_id == function_id
            ]
            if not ranges:
                source_offset = getattr(source, "offset", None)
                source_frontend = getattr(source, "frontend", None)
                nearby = [
                    item
                    for item in result.pseudocode.source_map
                    if item.function_id == function_id
                    and getattr(item.source, "frontend", None) == source_frontend
                    and isinstance(getattr(item.source, "offset", None), int)
                    and isinstance(source_offset, int)
                ]
                if nearby:
                    ranges = [
                        min(
                            nearby,
                            key=lambda item: abs(item.source.offset - source_offset),
                        )
                    ]
            if ranges:
                mapping = min(ranges, key=lambda item: item.end - item.start)
                cursor = self.pseudocode.textCursor()
                cursor.setPosition(mapping.start)
                cursor.setPosition(mapping.end, QTextCursor.MoveMode.KeepAnchor)
                self.pseudocode.blockSignals(True)
                try:
                    self.pseudocode.setTextCursor(cursor)
                finally:
                    self.pseudocode.blockSignals(False)
                self.pseudocode.centerCursor()
        if select_ast:
            ast_item = self._find_ast_item(source, function_id)
            if ast_item is not None:
                self.ast_tree.blockSignals(True)
                try:
                    self.ast_tree.setCurrentItem(ast_item)
                    self.ast_tree.scrollToItem(ast_item)
                finally:
                    self.ast_tree.blockSignals(False)
            else:
                self.statusBar().showMessage("No exact AST node")

    def _find_ast_item(self, source: object, function_id: str) -> QTreeWidgetItem | None:
        def visit(item: QTreeWidgetItem) -> QTreeWidgetItem | None:
            data = item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, dict) and data.get("function_id") == function_id:
                if _same_source(data.get("source"), source):
                    return item
            for index in range(item.childCount()):
                found = visit(item.child(index))
                if found is not None:
                    return found
            return None

        for index in range(self.ast_tree.topLevelItemCount()):
            found = visit(self.ast_tree.topLevelItem(index))
            if found is not None:
                return found
        return None

    def _selected_result(self) -> DecompileResult | None:
        selected = self.input_tree.selectedItems()
        if not selected:
            return None
        data = selected[0].data(0, Qt.ItemDataRole.UserRole)
        result = data[0] if isinstance(data, tuple) else data
        return result if isinstance(result, DecompileResult) else None


def _ast_item(
    value: Any,
    function_ids_by_name: dict[str, str],
    function_id: str | None = None,
) -> QTreeWidgetItem:
    item = QTreeWidgetItem([type(value).__name__])
    source = getattr(value, "source", None)
    if type(value).__name__ == "FunctionDecl":
        function_id = function_ids_by_name.get(getattr(value, "name", ""), function_id)
    item.setData(0, Qt.ItemDataRole.UserRole, {"function_id": function_id, "source": source})
    if is_dataclass(value):
        for field in fields(value):
            child = QTreeWidgetItem([field.name])
            child.addChild(_ast_item(getattr(value, field.name), function_ids_by_name, function_id))
            item.addChild(child)
    elif isinstance(value, (tuple, list)):
        for member in value:
            item.addChild(_ast_item(member, function_ids_by_name, function_id))
    else:
        item.setText(0, repr(value))
    return item


def _diagnostic_color(severity: str, colors: dict[str, str]) -> QColor:
    if severity == "error":
        return QColor(colors["status_error"])
    if severity == "warning":
        return QColor(colors["status_warning"])
    return QColor(colors["status_info"])


def _search_preview(text: str, start: int, end: int, limit: int = 120) -> str:
    """Render a compact, single-line excerpt around a global-search match."""
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start:] if line_end < 0 else text[line_start:line_end]
    line = " ".join(line.split())
    if len(line) <= limit:
        return line
    return f"{line[:limit - 3]}..."


def _decode_resource_text(data: bytes) -> str | None:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        encoding = "utf-8-sig"
    try:
        text = data.decode(encoding)
    except UnicodeDecodeError:
        return None
    if "\x00" in text:
        return None
    controls = sum(character.isascii() and character < " " and character not in "\n\r\t" for character in text)
    return None if text and controls / len(text) > 0.02 else text


def _hex_dump(data: bytes, width: int = 16) -> str:
    lines: list[str] = []
    for offset in range(0, len(data), width):
        chunk = data[offset:offset + width]
        values = " ".join(f"{value:02x}" for value in chunk)
        ascii_text = "".join(chr(value) if 32 <= value < 127 else "." for value in chunk)
        lines.append(f"{offset:08x}  {values:<{width * 3 - 1}}  |{ascii_text}|")
    return "\n".join(lines)


def _status_color(status: str, colors: dict[str, str]) -> QColor:
    if status == "ok":
        return QColor(colors["status_ok"])
    if status in {"partial", "unsupported"}:
        return QColor(colors["status_warning"])
    if status == "error":
        return QColor(colors["status_error"])
    return QColor(colors["syntax_comment"])


def _same_source(left: object, right: object) -> bool:
    """Compare the stable bytecode identity, excluding optional debug fields."""
    return (
        getattr(left, "frontend", None) == getattr(right, "frontend", None)
        and getattr(left, "offset", None) is not None
        and getattr(left, "offset", None) == getattr(right, "offset", None)
    )


class PseudocodeEditor(QPlainTextEdit):
    """Read-only editor with Ctrl+click navigation for recovered functions."""

    function_activated = Signal(str)
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        self._theme_colors: dict[str, str] = {}
        self.line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.update_line_number_area_width(0)

    def set_theme(self, colors: dict[str, str]) -> None:
        self._theme_colors = colors
        self.line_number_area.update()

    def line_number_area_width(self) -> int:
        digits = len(str(max(1, self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def update_line_number_area_width(self, _count: int) -> None:
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def update_line_number_area(self, rect, dy: int) -> None:
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(0, rect.y(), self.line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        contents = self.contentsRect()
        self.line_number_area.setGeometry(QRect(contents.left(), contents.top(), self.line_number_area_width(), contents.height()))
        self.resized.emit()

    def paint_line_numbers(self, event) -> None:
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), QColor(self._theme_colors["line_number_background"]))
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        painter.setPen(QColor(self._theme_colors["line_number_foreground"]))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, self.line_number_area.width() - 6, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1

    def mousePressEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            cursor = self.cursorForPosition(event.position().toPoint())
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
            name = cursor.selectedText()
            if name:
                self.function_activated.emit(name)
                event.accept()
                return
        super().mousePressEvent(event)


class GenericPseudocodeHighlighter(QSyntaxHighlighter):
    """Small grammar-aware palette for the generic pseudocode backend."""

    def __init__(self, document) -> None:
        super().__init__(document)
        self._rules: list[tuple[QRegularExpression, QTextCharFormat, int]] = []
        self._comment = QTextCharFormat()
        self._string = QTextCharFormat()
        self.set_theme(builtin_themes()[0].colors)

    def set_theme(self, colors: dict[str, str]) -> None:
        self._rules = []
        self._add_rule(r"\b(function|let|return|if|else|while|for|in|break|continue|switch|case|default|try|catch|throw|raise|goto)\b", colors["syntax_keyword"], bold=True)
        self._add_rule(r"\b(true|false|null|None)\b", colors["syntax_literal"])
        self._add_rule(r"\b\d+(?:\.\d+)?\b", colors["syntax_number"])
        self._add_rule(r"\bfunction\s+([A-Za-z_][A-Za-z0-9_]*)", colors["syntax_function"], group=1, bold=True)
        self._comment = QTextCharFormat()
        self._comment.setForeground(QColor(colors["syntax_comment"]))
        self._string = QTextCharFormat()
        self._string.setForeground(QColor(colors["syntax_string"]))
        self.rehighlight()

    def _add_rule(self, pattern: str, color: str, *, group: int = 0, bold: bool = False) -> None:
        style = QTextCharFormat()
        style.setForeground(QColor(color))
        if bold:
            style.setFontWeight(700)
        expression = QRegularExpression(pattern)
        expression.setPatternOptions(QRegularExpression.PatternOption.UseUnicodePropertiesOption)
        self._rules.append((expression, style, group))

    def highlightBlock(self, text: str) -> None:
        comment_start = text.find("//")
        code = text if comment_start < 0 else text[:comment_start]
        for expression, style, group in self._rules:
            match = expression.globalMatch(code)
            while match.hasNext():
                current = match.next()
                self.setFormat(current.capturedStart(group), current.capturedLength(group), style)
        strings = QRegularExpression(r"(?:'[^'\\]*(?:\\.[^'\\]*)*'|\"[^\"\\]*(?:\\.[^\"\\]*)*\")").globalMatch(code)
        while strings.hasNext():
            current = strings.next()
            self.setFormat(current.capturedStart(), current.capturedLength(), self._string)
        if comment_start >= 0:
            self.setFormat(comment_start, len(text) - comment_start, self._comment)


def main(argv: list[str] | None = None) -> int:
    app = QApplication([sys.argv[0], *(argv or [])])
    window = Workbench()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
