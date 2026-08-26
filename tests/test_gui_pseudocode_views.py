from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QMessageBox

from unidecompiler.engine import DecompilerEngine, PseudocodeDocument
from unidecompiler_gui.app import Workbench

from tests.plugins import create_plugin_registry


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def _result():
    path = Path("opcode_projects/generate/java-stress/stress/DPStress.class")
    return DecompilerEngine.from_registry(create_plugin_registry()).decompile_bytes(
        path.read_bytes(), str(path)
    )


def test_function_selection_loads_only_that_function(app) -> None:
    result = _result()
    window = Workbench(DecompilerEngine.from_registry(create_plugin_registry()))
    window.results = (result,)
    window._refresh()
    root = window.input_tree.topLevelItem(0)
    item = root.child(1)
    function_id = item.data(0, Qt.ItemDataRole.UserRole)[1]
    start, end = window._function_pseudocode_range(result, function_id)

    window.input_tree.setCurrentItem(item)

    assert window.pseudocode.toPlainText() == result.pseudocode.text[start:end]
    assert window._pseudocode_view_mode == "function"


def test_large_module_selection_can_be_cancelled_without_loading_full_text(app, monkeypatch) -> None:
    result = _result()
    assert result.pseudocode is not None
    padded = PseudocodeDocument(
        result.pseudocode.text + ("\n// padding" * 60_000),
        result.pseudocode.source_map,
    )
    window = Workbench(DecompilerEngine.from_registry(create_plugin_registry()))
    window.results = (replace(result, pseudocode=padded),)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.StandardButton.No)

    window._refresh()

    assert window._pseudocode_view_mode == "unavailable"
    assert "Complete pseudocode was not loaded" in window.pseudocode.toPlainText()
