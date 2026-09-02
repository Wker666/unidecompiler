"""Export self-contained VM frontend and GUI plugin starter projects."""
from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
from importlib import resources
from pathlib import Path
import re
import shutil
import tempfile
import tomllib
import unicodedata


_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_EXTENSION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PYTHON_KEYWORDS = frozenset({
    "False", "None", "True", "and", "as", "assert", "async", "await", "break", "case", "class", "continue",
    "def", "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is",
    "lambda", "match", "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
})
_TEMPLATE_TOKEN = re.compile(
    r"__(?:PROJECT_ID|PACKAGE|DISPLAY_NAME|AUTHOR|DESCRIPTION|USER_REQUIREMENTS|VM_NAME|SUFFIXES|FIRST_SUFFIX|VERSIONS|DEPENDENCIES|"
    r"INTERPRETER_FILE|BYTECODE_FILE|ENTRY_KIND|ENTRY_VALUE|ENTRY_CONTEXT|SIMULATION_GUIDANCE|SIMULATION_DELIVERABLE)__"
)
_AI_ENTRY_KINDS = frozenset({"symbol", "offset", "exported_function"})
_MAX_AI_INPUT_BYTES = 64 * 1024 * 1024
_HIGH_CONFIDENCE_SECRET = re.compile(
    rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----|"
    rb"\b(?:gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b"
)
_MACHINE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|Users|private|root|mnt)/)[^\s`\"']+")


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
    include_ai_guidance: bool = False
    interpreter_source: Path | None = None
    bytecode_sample: Path | None = None
    entry_kind: str = ""
    entry_value: str = ""
    entry_context: str = ""


def derive_project_names(project_name: str) -> tuple[str, str, str]:
    """Return display name, manifest ID, and safe Python package name."""
    display_name = project_name.strip()
    if not display_name:
        raise TemplateExportError("project name is required")
    normalized = unicodedata.normalize("NFKC", display_name)
    project_id = re.sub(r"[^A-Za-z0-9._-]+", "-", normalized)
    project_id = re.sub(r"[-_.]+", "-", project_id).strip("-_")
    project_id = project_id.lower() or "extension"
    if not project_id[0].isalnum():
        project_id = f"extension-{project_id}"
    package_name = re.sub(r"[^A-Za-z0-9]+", "_", normalized).strip("_").lower()
    package_name = package_name or "extension"
    if package_name[0].isdigit() or package_name in _PYTHON_KEYWORDS:
        package_name = f"extension_{package_name}"
    return display_name, project_id, package_name


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
        if request.include_ai_guidance:
            _copy_ai_inputs(request, staged)
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
    if request.include_ai_guidance:
        if request.kind != "frontend":
            raise TemplateExportError("AI development guidance is available only for frontend templates")
        if request.entry_kind not in _AI_ENTRY_KINDS:
            raise TemplateExportError("VM entry kind must be symbol, offset, or exported function")
        if not request.entry_value.strip():
            raise TemplateExportError("VM entry is required when AI development guidance is enabled")
        _validate_ai_input(request.interpreter_source, "VM interpreter source", scan_secrets=True)
        _validate_ai_input(request.bytecode_sample, "bytecode sample", scan_secrets=True)


def _render_tree(request: TemplateRequest, destination: Path) -> None:
    source_root = resources.files("unidecompiler_gui").joinpath("template_assets", request.kind)
    interpreter_name = _safe_input_name(request.interpreter_source, "interpreter")
    bytecode_name = _safe_input_name(request.bytecode_sample, "sample")
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
            ("unidecompiler>=0.1.7,<0.2.0", "unidecompiler-simulator>=0.1.2,<0.2.0")
            if request.include_simulation else ("unidecompiler>=0.1.7,<0.2.0",)
        ),
        "__INTERPRETER_FILE__": interpreter_name,
        "__BYTECODE_FILE__": bytecode_name,
        "__ENTRY_KIND__": request.entry_kind,
        "__ENTRY_VALUE__": _redact_machine_paths(request.entry_value.strip()),
        "__ENTRY_CONTEXT__": _redact_machine_paths(request.entry_context.strip()) or "Not provided; establish this from static evidence before lifting.",
        "__SIMULATION_GUIDANCE__": (
            "Simulation is in scope: implement only the optional data-only simulation adapter, resolve targets to functions in the lifted module, and add focused simulator tests."
            if request.include_simulation else
            "Simulation is out of scope for this generated project. Do not add a simulation adapter or execute frontend bytecode; keep the frontend/core boundary focused on decoding and lifting."
        ),
        "__SIMULATION_DELIVERABLE__": (
            "Implement the optional data-only simulation adapter and tests for target discovery, query resolution, arguments, returns, and unhandled external calls."
            if request.include_simulation else
            "Do not implement a simulation adapter in this project. Leave simulation unsupported rather than adding frontend execution behavior."
        ),
    }
    for source, relative in _walk_assets(source_root):
        if "__pycache__" in relative.parts or source.name.endswith((".pyc", ".pyo")):
            continue
        if relative.parts and relative.parts[0] == "simulation" and not request.include_simulation:
            continue
        if relative.parts and relative.parts[0] == "simulation":
            relative = relative.relative_to("simulation")
        if relative.parts and relative.parts[0] == "ai" and not request.include_ai_guidance:
            continue
        if relative.parts and relative.parts[0] == "ai":
            relative = relative.relative_to("ai")
        target = destination / _replace(str(relative), values)
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_bytes()
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            # Template assets may include images or other deliberate binary
            # files. They are copied unchanged and never treated as tokens.
            target.write_bytes(content)
        else:
            target.write_text(_replace(text, values, suffix=target.suffix), encoding="utf-8")


def _validate_ai_input(path: Path | None, label: str, *, scan_secrets: bool) -> None:
    if path is None:
        raise TemplateExportError(f"{label} is required when AI development guidance is enabled")
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise TemplateExportError(f"{label} must be a regular file, not a symbolic link")
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as error:
        raise TemplateExportError(f"{label} does not exist") from error
    if not candidate.is_file():
        raise TemplateExportError(f"{label} must be a regular file")
    if candidate.stat().st_size == 0:
        raise TemplateExportError(f"{label} must not be empty")
    if candidate.stat().st_size > _MAX_AI_INPUT_BYTES:
        raise TemplateExportError(f"{label} exceeds the 64 MiB safety limit")
    if scan_secrets:
        with candidate.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                if _HIGH_CONFIDENCE_SECRET.search(chunk):
                    raise TemplateExportError(f"{label} contains a likely credential or private key")


def _safe_input_name(path: Path | None, fallback: str) -> str:
    if path is None:
        return fallback
    name = unicodedata.normalize("NFKC", path.name)
    suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(name).suffix)[:20]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(name).stem).strip("_-")
    return f"{stem or fallback}{suffix}"


def _copy_ai_inputs(request: TemplateRequest, destination: Path) -> None:
    sources = (
        ("vm_interpreter_source", request.interpreter_source, Path("analysis_inputs/interpreter") / _safe_input_name(request.interpreter_source, "interpreter")),
        ("bytecode_sample", request.bytecode_sample, Path("analysis_inputs/samples") / _safe_input_name(request.bytecode_sample, "sample")),
    )
    records = []
    for role, source, relative in sources:
        assert source is not None
        resolved = source.expanduser().resolve(strict=True)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resolved, target)
        records.append({
            "role": role,
            "path": relative.as_posix(),
            "original_filename": source.name,
            "size": target.stat().st_size,
            "sha256": _sha256(target),
        })
    manifest = {
        "schema_version": 1,
        "project_id": request.project_id,
        "execution_policy": "static-analysis-only",
        "inputs": records,
        "entry": {
            "kind": request.entry_kind,
            "value": _redact_machine_paths(request.entry_value.strip()),
            "context": _redact_machine_paths(request.entry_context.strip()) or None,
            "status": "user-specified-unverified",
        },
    }
    (destination / "analysis_inputs/manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_machine_paths(value: str) -> str:
    return _MACHINE_PATH.sub("<absolute-path-redacted>", value)


def _walk_assets(root):
    for item in root.iterdir():
        yield item, Path(item.name)
        if item.is_dir():
            for nested, relative in _walk_assets(item):
                yield nested, Path(item.name) / relative


def _replace(value: str, values: dict[str, str], *, suffix: str = "") -> str:
    if suffix == ".py":
        values = {
            **values,
            "__PROJECT_ID__": _escape_quoted(values["__PROJECT_ID__"]),
            "__DISPLAY_NAME__": _escape_quoted(values["__DISPLAY_NAME__"]),
            "__VM_NAME__": _escape_quoted(values["__VM_NAME__"]),
            "__FIRST_SUFFIX__": _escape_quoted(values["__FIRST_SUFFIX__"]),
            "__AUTHOR__": _escape_quoted(values["__AUTHOR__"]),
            "__DESCRIPTION__": _escape_quoted(values["__DESCRIPTION__"]),
            "__USER_REQUIREMENTS__": _escape_quoted(values["__USER_REQUIREMENTS__"]),
        }
    elif suffix == ".toml":
        values = {
            **values,
            "__PROJECT_ID__": _escape_quoted(values["__PROJECT_ID__"]),
            "__DISPLAY_NAME__": _escape_quoted(values["__DISPLAY_NAME__"]),
            "__AUTHOR__": _escape_quoted(values["__AUTHOR__"]),
            "__DESCRIPTION__": _escape_quoted(values["__DESCRIPTION__"]),
        }
    return _TEMPLATE_TOKEN.sub(lambda match: values[match.group(0)], value)


def _escape_quoted(value: str) -> str:
    encoded = json.dumps(value, ensure_ascii=False)
    return encoded[1:-1]


def _toml_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(repr(value) for value in values) + "]"


def _validate_rendered_project(request: TemplateRequest, root: Path) -> None:
    required = ("AGENTS.md", "README.md", "pyproject.toml")
    if request.kind == "frontend":
        required += ("unidecompiler-plugin.toml", "docs/NEW_VM_FRONTEND.md")
        if request.include_ai_guidance:
            required += (
                "analysis_inputs/manifest.json",
                "docs/AI_CONTEXT.md",
                "docs/VM_ANALYSIS.md",
                "skills/vm-frontend-development/SKILL.md",
            )
    else:
        required += ("plugin.toml", "docs/GUI_PLUGIN_DEVELOPMENT.md")
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise TemplateExportError("template assets are incomplete: " + ", ".join(missing))
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in {".py", ".toml"}:
            continue
        text = path.read_text(encoding="utf-8")
        if _TEMPLATE_TOKEN.search(text):
            raise TemplateExportError(f"template contains unreplaced placeholder: {path.name}")
        try:
            if path.suffix == ".py":
                ast.parse(text, filename=str(path))
            else:
                tomllib.loads(text)
        except (SyntaxError, tomllib.TOMLDecodeError) as error:
            raise TemplateExportError(f"generated project file is invalid: {path.name}") from error
