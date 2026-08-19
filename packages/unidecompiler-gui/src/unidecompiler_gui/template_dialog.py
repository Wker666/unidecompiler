"""Qt dialog for collecting inputs for the GUI-only template exporter."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from unidecompiler_gui.template_export import TemplateExportError, TemplateRequest, export_template


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

        self.project_id = QLineEdit()
        self.project_id.setPlaceholderText("my-vm-frontend")
        self._form.addRow("Project ID", self.project_id)
        self.package_name = QLineEdit()
        self.package_name.setPlaceholderText("my_vm_frontend")
        self._form.addRow("Python package", self.package_name)
        self.display_name = QLineEdit()
        self._form.addRow("Display name", self.display_name)
        self.author = QLineEdit()
        self._form.addRow("Author", self.author)
        self.description = QLineEdit()
        self._form.addRow("Description", self.description)
        self.requirements = QPlainTextEdit()
        self.requirements.setPlaceholderText("Describe the feature this project must implement.")
        self.requirements.setFixedHeight(90)
        self._form.addRow("Requested feature", self.requirements)

        self.vm_name = QLineEdit()
        self._form.addRow("VM name", self.vm_name)
        self.suffixes = QLineEdit()
        self.suffixes.setPlaceholderText(".vm, .bytecode")
        self._form.addRow("Input suffixes", self.suffixes)
        self.versions = QLineEdit()
        self.versions.setPlaceholderText("1, 2")
        self._form.addRow("Bytecode versions", self.versions)
        self.simulation = QCheckBox("Generate optional data-only simulation adapter")
        self._form.addRow("Simulation", self.simulation)

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

    def _update_kind_fields(self) -> None:
        frontend = self.kind.currentData() == "frontend"
        for widget in (self.vm_name, self.suffixes, self.versions, self.simulation):
            self._form.setRowVisible(widget, frontend)
            label = self._form.labelForField(widget)
            if label is not None:
                label.setVisible(frontend)

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Choose parent directory")
        if directory:
            project = self.project_id.text().strip() or "extension-template"
            self.output.setText(str(Path(directory) / project))

    def _export(self) -> None:
        kind = self.kind.currentData()
        try:
            request = TemplateRequest(
                kind=kind,
                project_id=self.project_id.text().strip(),
                package_name=self.package_name.text().strip(),
                display_name=self.display_name.text().strip(),
                author=self.author.text().strip(),
                description=self.description.text().strip(),
                requirements=self.requirements.toPlainText(),
                output_directory=Path(self.output.text().strip()),
                vm_name=self.vm_name.text().strip(),
                suffixes=tuple(item.strip() for item in self.suffixes.text().split(",") if item.strip()),
                versions=tuple(item.strip() for item in self.versions.text().split(",") if item.strip()),
                include_simulation=self.simulation.isChecked(),
            )
            destination = export_template(request)
        except (TemplateExportError, OSError) as error:
            QMessageBox.critical(self, "Template export failed", str(error))
            return
        QMessageBox.information(self, "Template exported", f"Template exported to:\n{destination}")
        self.accept()
