"""Export self-contained VM frontend and GUI plugin starter projects."""
from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import re
import tempfile


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXTENSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


class TemplateExportError(ValueError):
    """The requested extension project cannot be created safely."""


@dataclass(frozen=True)
class TemplateRequest:
    kind: str
    project_id: str
    package_name: str
    display_name: str
    author: str
    description: str
    requirements: str
    output_directory: Path
    vm_name: str = ""
    suffixes: tuple[str, ...] = ()
    versions: tuple[str, ...] = ()
    include_simulation: bool = False


def export_template(request: TemplateRequest) -> Path:
    """Render an extension project atomically into a new directory."""
    _validate(request)
    destination = request.output_directory.expanduser().resolve()
    if destination.exists():
        raise TemplateExportError(f"output directory already exists: {destination}")
    parent = destination.parent
    if not parent.is_dir():
        raise TemplateExportError(f"output directory parent does not exist: {parent}")

    with tempfile.TemporaryDirectory(prefix="unidecompiler-template-", dir=parent) as temporary:
        staged = Path(temporary) / destination.name
        _render_tree(request, staged)
        _validate_rendered_project(request, staged)
        staged.replace(destination)
    return destination


def _validate(request: TemplateRequest) -> None:
    if request.kind not in {"frontend", "gui_plugin"}:
        raise TemplateExportError("template kind must be 'frontend' or 'gui_plugin'")
    if not _EXTENSION_ID.fullmatch(request.project_id):
        raise TemplateExportError("project ID must contain only letters, digits, '.', '_' or '-'")
    if not _IDENTIFIER.fullmatch(request.package_name):
        raise TemplateExportError("package name must be a valid Python identifier")
    for label, value in (("display name", request.display_name), ("author", request.author), ("description", request.description), ("requirements", request.requirements)):
        if not value.strip():
            raise TemplateExportError(f"{label} is required")
    if request.kind == "frontend":
        if not request.vm_name.strip():
            raise TemplateExportError("VM name is required for a frontend template")
        if not request.suffixes or not all(item.startswith(".") and len(item) > 1 for item in request.suffixes):
            raise TemplateExportError("frontend suffixes must be comma-separated extensions beginning with '.'")
        if not request.versions:
            raise TemplateExportError("at least one bytecode version is required")


def _render_tree(request: TemplateRequest, destination: Path) -> None:
    source_root = resources.files("unidecompiler_gui").joinpath("template_assets", request.kind)
    values = {
        "__PROJECT_ID__": request.project_id,
        "__PACKAGE__": request.package_name,
        "__DISPLAY_NAME__": request.display_name,
        "__AUTHOR__": request.author,
        "__DESCRIPTION__": request.description,
        "__USER_REQUIREMENTS__": request.requirements.strip(),
        "__VM_NAME__": request.vm_name,
        "__SUFFIXES__": repr(request.suffixes),
        "__FIRST_SUFFIX__": request.suffixes[0] if request.suffixes else ".vm",
        "__VERSIONS__": repr(request.versions),
        "__DEPENDENCIES__": _toml_array(
            ("unidecompiler>=0.1.3,<0.2.0", "unidecompiler-simulator>=0.1.1,<0.2.0")
            if request.include_simulation else ("unidecompiler>=0.1.3,<0.2.0",)
        ),
    }
    for source, relative in _walk_assets(source_root):
        if relative.parts and relative.parts[0] == "simulation" and not request.include_simulation:
            continue
        if relative.parts and relative.parts[0] == "simulation":
            relative = relative.relative_to("simulation")
        target = destination / _replace(str(relative), values)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_replace(source.read_text(encoding="utf-8"), values), encoding="utf-8")


def _walk_assets(root):
    for item in root.iterdir():
        yield item, Path(item.name)
        if item.is_dir():
            for nested, relative in _walk_assets(item):
                yield nested, Path(item.name) / relative


def _replace(value: str, values: dict[str, str]) -> str:
    for token, replacement in values.items():
        value = value.replace(token, replacement)
    return value


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(repr(value) for value in values) + "]"


def _validate_rendered_project(request: TemplateRequest, root: Path) -> None:
    required = ("AGENTS.md", "README.md", "pyproject.toml")
    if request.kind == "frontend":
        required += ("unidecompiler-plugin.toml", "docs/NEW_VM_FRONTEND.md")
    else:
        required += ("plugin.toml", "docs/GUI_PLUGIN_DEVELOPMENT.md")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise TemplateExportError("template assets are incomplete: " + ", ".join(missing))
