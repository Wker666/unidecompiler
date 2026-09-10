"""Qt dialog for collecting inputs for the GUI-only template exporter."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from unidecompiler_gui.template_export import (
    TemplateExportError,
    TemplateRequest,
    build_ai_goal_prompt,
    derive_project_names,
    export_template,
)


class TemplateDialog(QDialog):
    """Collect project metadata and export one new extension directory."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export extension template")
        self.resize(620, 560)
        self._form = QFormLayout()

        self.kind = QComboBox()
        self.kind.addItem("VM frontend", "frontend")
        self.kind.addItem("GUI plugin", "gui_plugin")
        self.kind.currentIndexChanged.connect(self._update_kind_fields)
        self._form.addRow("Template type", self.kind)

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("My VM frontend")
        self._form.addRow("Project name", self.project_name)
        self.author = QLineEdit()
        self._form.addRow("Author", self.author)
        self.description = QLineEdit()
        self._form.addRow("Description", self.description)
        self.requirements = QPlainTextEdit()
        self.requirements.setPlaceholderText("Describe the feature this project must implement.")
        self.requirements.setFixedHeight(90)
        self._form.addRow("Requested feature", self.requirements)

        self.suffixes = QLineEdit()
        self.suffixes.setPlaceholderText(".vm, .bytecode")
        self._form.addRow("Input suffixes", self.suffixes)
        self.versions = QLineEdit()
        self.versions.setPlaceholderText("1, 2")
        self._form.addRow("Bytecode versions", self.versions)
        self.simulation = QCheckBox("Generate optional data-only simulation adapter")
        self._form.addRow("Simulation", self.simulation)

        self.ai_guidance = QCheckBox("Include AI development kit")
        self.ai_guidance.toggled.connect(self._update_ai_fields)
        self._form.addRow("AI assistance", self.ai_guidance)

        self.interpreter_source, self._interpreter_row = self._file_picker("Choose interpreter source")
        self._form.addRow("VM interpreter", self._interpreter_row)
        self.bytecode_sample, self._bytecode_row = self._file_picker("Choose bytecode sample")
        self._form.addRow("Bytecode sample", self._bytecode_row)
        self.entry_kind = QComboBox()
        self.entry_kind.addItem("Symbol", "symbol")
        self.entry_kind.addItem("Offset", "offset")
        self.entry_kind.addItem("Exported function", "exported_function")
        self._form.addRow("VM entry kind", self.entry_kind)
        self.entry_value = QLineEdit()
        self.entry_value.setPlaceholderText("main or 0x1000")
        self._form.addRow("VM entry", self.entry_value)
        self.entry_context = QPlainTextEdit()
        self.entry_context.setPlaceholderText("Initial cursor/base, arguments, and setup facts")
        self.entry_context.setFixedHeight(70)
        self._form.addRow("Entry context", self.entry_context)

        output_row = QVBoxLayout()
        self.output = QLineEdit()
        browse = QDialogButtonBox()
        browse.addButton("Choose directory", QDialogButtonBox.ButtonRole.ActionRole).clicked.connect(self._choose_output)
        output_row.addWidget(self.output)
        output_row.addWidget(browse)
        self._form.addRow("Output directory", output_row)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        export_button = buttons.addButton("Export", QDialogButtonBox.ButtonRole.AcceptRole)
        export_button.clicked.connect(self._export)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(self._form)
        layout.addWidget(buttons)
        self._update_kind_fields()
        self._update_ai_fields()

    def _file_picker(self, title: str) -> tuple[QLineEdit, QWidget]:
        field = QLineEdit()
        field.setReadOnly(True)
        button = QPushButton("Choose")
        button.clicked.connect(lambda: self._choose_file(field, title))
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field)
        layout.addWidget(button)
        return field, row

    def _choose_file(self, field: QLineEdit, title: str) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, title)
        if filename:
            field.setText(filename)

    def _update_kind_fields(self) -> None:
        frontend = self.kind.currentData() == "frontend"
        for widget in (self.suffixes, self.versions, self.simulation):
            self._form.setRowVisible(widget, frontend)
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(frontend)
        self._form.setRowVisible(self.ai_guidance, frontend)
        label = self._form.labelForField(self.ai_guidance)
        if label is not None:
            label.setVisible(frontend)
        self._update_ai_fields()

    def _update_ai_fields(self) -> None:
        visible = self.kind.currentData() == "frontend" and self.ai_guidance.isChecked()
        for widget in (self._interpreter_row, self._bytecode_row, self.entry_kind, self.entry_value, self.entry_context):
            self._form.setRowVisible(widget, visible)
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(visible)

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose parent directory")
        if directory:
            try:
                _, project_id, _ = derive_project_names(self.project_name.text())
            except TemplateExportError:
                project_id = "extension-template"
            self.output.setText(str(Path(directory) / project_id))

    def _export(self) -> None:
        kind = self.kind.currentData()
        try:
            display_name, project_id, package_name = derive_project_names(self.project_name.text())
            ai_enabled = self.kind.currentData() == "frontend" and self.ai_guidance.isChecked()
            request = TemplateRequest(
                kind=kind,
                project_id=project_id,
                package_name=package_name,
                display_name=display_name,
                author=self.author.text().strip(),
                description=self.description.text().strip(),
                requirements=self.requirements.toPlainText(),
                output_directory=Path(self.output.text().strip()),
                vm_name=display_name,
                suffixes=tuple(item.strip() for item in self.suffixes.text().split(",") if item.strip()),
                versions=tuple(item.strip() for item in self.versions.text().split(",") if item.strip()),
                include_simulation=self.simulation.isChecked(),
                include_ai_guidance=ai_enabled,
                interpreter_source=Path(self.interpreter_source.text()) if ai_enabled and self.interpreter_source.text() else None,
                bytecode_sample=Path(self.bytecode_sample.text()) if ai_enabled and self.bytecode_sample.text() else None,
                entry_kind=self.entry_kind.currentData() if ai_enabled else "",
                entry_value=self.entry_value.text() if ai_enabled else "",
                entry_context=self.entry_context.toPlainText() if ai_enabled else "",
            )
            destination = export_template(request)
        except (TemplateExportError, OSError) as error:
            QMessageBox.critical(self, "Template export failed", str(error))
            return
        QMessageBox.information(self, "Template exported", f"Template exported to:\n{destination}")
        if ai_enabled:
            self._show_ai_goal_prompt(request, destination)
        self.accept()

    def _show_ai_goal_prompt(self, request: TemplateRequest, destination: Path) -> None:
        prompt = build_ai_goal_prompt(request, destination)
        dialog = QDialog(self)
        dialog.setWindowTitle("Copy AI goal prompt")
        dialog.resize(760, 520)
        text = QPlainTextEdit(dialog)
        text.setReadOnly(True)
        text.setPlainText(prompt)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        copy_button = buttons.addButton("Copy", QDialogButtonBox.ButtonRole.ActionRole)
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(prompt))
        buttons.rejected.connect(dialog.reject)
        layout = QVBoxLayout(dialog)
        layout.addWidget(text)
        layout.addWidget(buttons)
        dialog.exec()
