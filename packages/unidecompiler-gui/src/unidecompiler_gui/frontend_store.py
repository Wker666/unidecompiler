"""Persistent user frontend directory records for the GUI host."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import sys
from typing import Any


@dataclass(frozen=True)
class InstalledFrontend:
    id: str
    path: str


@dataclass(frozen=True)
class FrontendRestoreFailure:
    id: str
    path: str
    diagnostic: str


def user_frontend_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "unidecompiler"


class FrontendStore:
    """Stores only external frontend source paths; runtime objects stay in core."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_frontend_root()
        self.path = self.root / "frontends.json"
        self.load_diagnostic: str | None = None

    def list(self) -> tuple[InstalledFrontend, ...]:
        self.load_diagnostic = None
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            self.load_diagnostic = f"could not read frontend configuration: {type(error).__name__}: {error}"
            return ()
        if not isinstance(raw, dict) or not isinstance(raw.get("frontends"), list):
            self.load_diagnostic = "could not read frontend configuration: expected a frontends list"
            return ()
        records: list[InstalledFrontend] = []
        for item in raw["frontends"]:
            if not isinstance(item, dict):
                continue
            frontend_id = item.get("id")
            frontend_path = item.get("path")
            if isinstance(frontend_id, str) and frontend_id and isinstance(frontend_path, str) and frontend_path:
                records.append(InstalledFrontend(frontend_id, frontend_path))
        return tuple(records)

    def add(self, frontend_id: str, directory: Path | str) -> InstalledFrontend:
        record = InstalledFrontend(frontend_id, str(Path(directory).expanduser().resolve()))
        records = [item for item in self.list() if item.id != frontend_id and item.path != record.path]
        self._write([*records, record])
        return record

    def remove(self, frontend_id: str) -> bool:
        records = list(self.list())
        remaining = [item for item in records if item.id != frontend_id]
        if len(remaining) == len(records):
            return False
        self._write(remaining)
        return True

    def record_for(self, frontend_id: str) -> InstalledFrontend | None:
        return next((item for item in self.list() if item.id == frontend_id), None)

    def restore(self, engine: Any) -> tuple[FrontendRestoreFailure, ...]:
        failures: list[FrontendRestoreFailure] = []
        for record in self.list():
            try:
                engine.register_frontend_directory(record.path)
            except Exception as error:
                failures.append(FrontendRestoreFailure(record.id, record.path, f"{type(error).__name__}: {error}"))
        return tuple(failures)

    def _write(self, records: list[InstalledFrontend]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"frontends": [asdict(item) for item in records]}, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(self.path)
