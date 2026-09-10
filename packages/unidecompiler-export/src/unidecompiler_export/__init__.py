"""Host-side export helpers for recovered unidecompiler results."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Iterable


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")
_VSCODE_METADATA_SUFFIX = ".unidec.json"


@dataclass(frozen=True)
class PseudocodeMetadataExport:
    """Paths written for one pseudocode and VS Code metadata export."""

    pseudocode_path: Path
    metadata_path: Path


class VscodeMetadataExportError(OSError):
    """Metadata export failed after its pseudocode file was written."""

    def __init__(
        self,
        pseudocode_path: Path,
        metadata_path: Path,
        cause: Exception,
        *,
        completed: tuple[PseudocodeMetadataExport, ...] = (),
    ) -> None:
        self.pseudocode_path = pseudocode_path
        self.metadata_path = metadata_path
        self.cause = cause
        self.completed = completed
        super().__init__(
            f"pseudocode was exported to {pseudocode_path}, but VS Code metadata "
            f"could not be exported to {metadata_path}: {cause}"
        )


def pseudocode_text(result: object) -> str:
    pseudocode = getattr(result, "pseudocode", None)
    text = getattr(pseudocode, "text", None)
    if not isinstance(text, str):
        raise ValueError("result does not contain pseudocode")
    return text


def write_pseudocode(result: object, destination: Path) -> None:
    """Write one result's pseudocode to *destination*."""
    destination.write_text(pseudocode_text(result), encoding="utf-8")


def build_vscode_metadata(result: object) -> dict[str, Any]:
    """Build the minimal, path-free VS Code navigation sidecar payload."""
    pseudocode = getattr(result, "pseudocode", None)
    text = pseudocode_text(result)
    instructions: list[dict[str, Any]] = []
    instruction_keys: set[tuple[str, int]] = set()
    for instruction in getattr(result, "instructions", ()):
        function_id = getattr(instruction, "function_id", None)
        offset = getattr(instruction, "offset", None)
        if not isinstance(function_id, str) or not function_id or not _is_int(offset):
            continue
        key = (function_id, offset)
        if key in instruction_keys:
            continue
        item: dict[str, Any] = {"function_id": function_id, "offset": offset}
        raw = getattr(instruction, "raw", None)
        if isinstance(raw, str):
            item["raw"] = raw
        byte_range = getattr(instruction, "byte_range", None)
        if byte_range is not None:
            byte_start = getattr(byte_range, "start", None)
            byte_size = getattr(byte_range, "size", None)
            if _is_int(byte_start) and byte_start >= 0 and _is_int(byte_size) and byte_size > 0:
                item["byte_range"] = {"start": byte_start, "size": byte_size}
        instructions.append(item)
        instruction_keys.add(key)

    source_map: list[dict[str, Any]] = []
    for mapping in getattr(pseudocode, "source_map", ()):
        start = getattr(mapping, "start", None)
        end = getattr(mapping, "end", None)
        function_id = getattr(mapping, "function_id", None)
        source = getattr(mapping, "source", None)
        offset = getattr(source, "offset", None)
        if not (
            _is_int(start)
            and _is_int(end)
            and 0 <= start < end <= len(text)
            and isinstance(function_id, str)
            and function_id
            and _is_int(offset)
            and (function_id, offset) in instruction_keys
        ):
            continue
        source_map.append(
            {
                "start": _utf16_offset(text, start),
                "end": _utf16_offset(text, end),
                "function_id": function_id,
                "offset": offset,
            }
        )

    return {
        "schema_version": 1,
        "pseudocode": {
            "encoding": "utf-8",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        },
        "source_map": source_map,
        "instructions": instructions,
    }


def write_vscode_metadata(result: object, destination: Path) -> Path:
    """Write one stable UTF-8 VS Code navigation sidecar."""
    destination.write_text(_vscode_metadata_json(result), encoding="utf-8")
    return destination


def write_pseudocode_with_vscode_metadata(
    result: object,
    pseudocode_path: Path,
    metadata_path: Path | None = None,
) -> PseudocodeMetadataExport:
    """Write pseudocode first, followed by its adjacent or explicit sidecar."""
    metadata_path = metadata_path or _metadata_path(pseudocode_path)
    if pseudocode_path.resolve() == metadata_path.resolve():
        raise ValueError("pseudocode and VS Code metadata destinations must differ")
    write_pseudocode(result, pseudocode_path)
    try:
        write_vscode_metadata(result, metadata_path)
    except (OSError, TypeError, ValueError) as error:
        raise VscodeMetadataExportError(pseudocode_path, metadata_path, error) from error
    return PseudocodeMetadataExport(pseudocode_path, metadata_path)


def export_pseudocode_documents(results: Iterable[object], directory: Path) -> tuple[Path, ...]:
    """Export each result with pseudocode using collision-safe filenames."""
    return export_text_documents(
        (
            (str(getattr(result, "display_path", "artifact")), pseudocode_text(result))
            for result in results
            if getattr(result, "pseudocode", None) is not None
        ),
        directory,
        suffix=".pse",
    )


def export_pseudocode_documents_with_vscode_metadata(
    results: Iterable[object],
    directory: Path,
) -> tuple[PseudocodeMetadataExport, ...]:
    """Export collision-safe pseudocode files with adjacent sidecars."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"output directory does not exist: {directory}")

    exported: list[PseudocodeMetadataExport] = []
    used: set[str] = set()
    for result in results:
        if getattr(result, "pseudocode", None) is None:
            continue
        display_path = str(getattr(result, "display_path", "artifact"))
        filename = _export_filename(
            display_path,
            used,
            directory,
            suffix=".pse",
            companion_suffixes=(_VSCODE_METADATA_SUFFIX,),
        )
        pseudocode_path = directory / filename
        metadata_path = _metadata_path(pseudocode_path)
        try:
            _write_new_text(pseudocode_text(result), pseudocode_path)
            try:
                _write_new_text(_vscode_metadata_json(result), metadata_path)
            except (OSError, TypeError, ValueError) as error:
                raise VscodeMetadataExportError(pseudocode_path, metadata_path, error) from error
            pair = PseudocodeMetadataExport(pseudocode_path, metadata_path)
        except VscodeMetadataExportError as error:
            raise VscodeMetadataExportError(
                error.pseudocode_path,
                error.metadata_path,
                error.cause,
                completed=tuple(exported),
            ) from error
        used.add(filename)
        exported.append(pair)
    return tuple(exported)


def export_text_documents(
    documents: Iterable[tuple[str, str]],
    directory: Path,
    *,
    suffix: str,
) -> tuple[Path, ...]:
    """Write named text documents without overwriting existing files."""
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"output directory does not exist: {directory}")

    exported: list[Path] = []
    used: set[str] = set()
    for display_path, text in documents:
        candidate = directory / _export_filename(display_path, used, directory, suffix=suffix)
        _write_new_text(text, candidate)
        used.add(candidate.name)
        exported.append(candidate)
    return tuple(exported)


def _write_new_text(text: str, destination: Path) -> None:
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(text)


def _export_filename(
    display_path: str,
    used: set[str],
    directory: Path,
    *,
    suffix: str,
    companion_suffixes: tuple[str, ...] = (),
) -> str:
    source_name = Path(display_path.rsplit("!", 1)[-1]).name
    stem = Path(source_name).stem or source_name
    stem = _UNSAFE_FILENAME_CHARS.sub("_", stem).strip("._-") or "artifact"
    candidate = f"{stem}{suffix}"
    index = 2
    while (
        candidate in used
        or (directory / candidate).exists()
        or any((directory / f"{candidate}{item}").exists() for item in companion_suffixes)
    ):
        candidate = f"{stem}-{index}{suffix}"
        index += 1
    return candidate


def _metadata_path(pseudocode_path: Path) -> Path:
    return Path(f"{pseudocode_path}{_VSCODE_METADATA_SUFFIX}")


def _vscode_metadata_json(result: object) -> str:
    return json.dumps(
        build_vscode_metadata(result),
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"


def _utf16_offset(text: str, offset: int) -> int:
    return len(text[:offset].encode("utf-16-le")) // 2


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


__all__ = (
    "PseudocodeMetadataExport",
    "VscodeMetadataExportError",
    "build_vscode_metadata",
    "export_pseudocode_documents",
    "export_pseudocode_documents_with_vscode_metadata",
    "export_text_documents",
    "pseudocode_text",
    "write_pseudocode",
    "write_pseudocode_with_vscode_metadata",
    "write_vscode_metadata",
)

from .templates import (
    TemplateExportError,
    TemplateRequest,
    build_ai_goal_prompt,
    derive_project_names,
    export_template,
)

__all__ += (
    "TemplateExportError",
    "TemplateRequest",
    "build_ai_goal_prompt",
    "derive_project_names",
    "export_template",
)
