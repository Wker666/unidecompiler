"""Helpers for exporting one or more recovered pseudocode documents."""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Iterable


_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


def write_pseudocode(result: object, destination: Path) -> None:
    """Write one result's pseudocode to *destination* without extra metadata."""
    pseudocode = getattr(result, "pseudocode", None)
    destination.write_text(_pseudocode_text(result), encoding="utf-8")


def export_pseudocode_documents(results: Iterable[object], directory: Path) -> tuple[Path, ...]:
    """Export every result with pseudocode into *directory* as separate text files.

    Names are derived only from the final path component, so absolute source
    paths are never copied into filenames. Existing files are preserved by
    adding a numeric suffix instead of overwriting them.
    """
    directory = directory.expanduser().resolve()
    if not directory.is_dir():
        raise ValueError(f"output directory does not exist: {directory}")

    exported: list[Path] = []
    used: set[str] = set()
    for result in results:
        if getattr(result, "pseudocode", None) is None:
            continue
        display_path = str(getattr(result, "display_path", "artifact"))
        candidate = directory / _export_filename(display_path, used, directory)
        _write_new_pseudocode(result, candidate)
        used.add(candidate.name)
        exported.append(candidate)
    return tuple(exported)


def _pseudocode_text(result: object) -> str:
    pseudocode = getattr(result, "pseudocode", None)
    text = getattr(pseudocode, "text", None)
    if not isinstance(text, str):
        raise ValueError("result does not contain pseudocode")
    return text


def _write_new_pseudocode(result: object, destination: Path) -> None:
    """Create a new file without following or replacing an existing path."""
    descriptor = os.open(
        destination,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o644,
    )
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(_pseudocode_text(result))


def _export_filename(display_path: str, used: set[str], directory: Path) -> str:
    source_name = Path(display_path.rsplit("!", 1)[-1]).name
    stem = Path(source_name).stem or source_name
    stem = _UNSAFE_FILENAME_CHARS.sub("_", stem).strip("._-") or "artifact"
    base = f"{stem}.pseudocode.txt"
    candidate = base
    index = 2
    while candidate in used or (directory / candidate).exists():
        candidate = f"{stem}-{index}.pseudocode.txt"
        index += 1
    return candidate
