"""Read-only expansion of files, directories, and ZIP-compatible archives."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
import zipfile


ARCHIVE_SUFFIXES = frozenset({".zip", ".jar"})


@dataclass(frozen=True)
class InputArtifact:
    """One input member ready for frontend selection.

    ``display_path`` remains meaningful for archive members without exposing a
    mutable archive handle to callers.
    """

    path: Path
    display_path: str
    data: bytes
    kind: str = "bytecode"


@dataclass(frozen=True)
class InputEntry:
    """A discoverable input member that has not been read or decoded yet."""

    path: Path
    display_path: str
    archive_member: str | None = None


def expand_input_path(path: Path) -> tuple[InputArtifact, ...]:
    """Return every file represented by *path* in deterministic order."""
    return tuple(iter_input_path(path))


def iter_input_path(path: Path) -> Iterator[InputArtifact]:
    """Yield artifacts represented by *path* in deterministic order.

    This is a read-only transport helper. It has no frontend selection or
    decompilation policy, allowing hosts to choose eager or incremental use.
    """
    for entry in iter_input_entries(path):
        yield load_input_entry(entry)


def iter_input_entries(path: Path) -> Iterator[InputEntry]:
    """Yield input member identities without reading their contents."""
    if path.is_dir():
        for child in sorted(candidate for candidate in path.rglob("*") if candidate.is_file()):
            yield InputEntry(child, str(child))
        return
    if _looks_like_archive(path):
        with zipfile.ZipFile(path) as archive:
            for name in sorted(info.filename for info in archive.infolist() if not info.is_dir()):
                yield InputEntry(path, f"{path}!{name}", name)
        return
    yield InputEntry(path, str(path))


def load_input_entry(entry: InputEntry) -> InputArtifact:
    """Read one previously discovered member without selecting a frontend."""
    if entry.archive_member is None:
        return _read_file_artifact(entry.path)
    with zipfile.ZipFile(entry.path) as archive:
        data = archive.read(entry.archive_member)
    return InputArtifact(
        path=entry.path,
        display_path=entry.display_path,
        data=data,
        kind="bytecode" if _looks_like_bytecode(data, Path(entry.archive_member)) else "resource",
    )


def _looks_like_archive(path: Path) -> bool:
    return path.suffix.lower() in ARCHIVE_SUFFIXES or zipfile.is_zipfile(path)


def _read_file_artifact(path: Path) -> InputArtifact:
    data = path.read_bytes()
    return InputArtifact(path, str(path), data, "bytecode" if _looks_like_bytecode(data, path) else "resource")


def _looks_like_bytecode(data: bytes, path: Path) -> bool:
    if not data:
        return False
    return path.suffix.lower() in {".pyc", ".class", ".luac", ".dll", ".exe", ".wasm"} or data.startswith(
        (b"\x1bLua", b"\xca\xfe\xba\xbe", b"MZ", b"\x00asm")
    )
