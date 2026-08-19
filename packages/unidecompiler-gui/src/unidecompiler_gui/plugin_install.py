"""Install and load trusted GUI plugins outside the frontend registry."""
from __future__ import annotations

import importlib
from importlib import metadata
import json
import os
from dataclasses import dataclass, asdict
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib
import re
from urllib.parse import urlparse
from urllib.request import urlopen
import zipfile

from unidecompiler_gui_sdk.api import API_VERSION


_PLUGIN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


@dataclass(frozen=True)
class GuiPluginManifest:
    id: str
    name: str
    version: str
    api: str
    entry: str
    description: str = ""
    requires: tuple[str, ...] = ()


@dataclass(frozen=True)
class InstalledGuiPlugin:
    id: str
    source_kind: str
    source: str
    install_path: str
    enabled: bool = True
    ref: str | None = None


def user_plugin_root() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "unidecompiler" / "plugins"


class GuiPluginStore:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or user_plugin_root()
        self.packages = self.root / "packages"
        self.index_path = self.root / "installed.json"

    def list(self) -> tuple[InstalledGuiPlugin, ...]:
        if not self.index_path.exists():
            return ()
        raw = json.loads(self.index_path.read_text(encoding="utf-8"))
        return tuple(InstalledGuiPlugin(**item) for item in raw if isinstance(item, dict))

    def install_local(self, source: Path) -> InstalledGuiPlugin:
        manifest = load_manifest(source)
        return self._install(manifest, source, "local", str(source))

    def install_github(self, source: str) -> InstalledGuiPlugin:
        owner, repo, ref = parse_github_source(source)
        archive = f"https://github.com/{owner}/{repo}/archive/{ref}.zip"
        with tempfile.TemporaryDirectory(prefix="unidecompiler-gui-plugin-") as temporary:
            archive_path = Path(temporary) / "plugin.zip"
            with urlopen(archive, timeout=30) as response:
                archive_path.write_bytes(response.read())
            extracted = Path(temporary) / "source"
            extract_archive(archive_path, extracted)
            roots = [item for item in extracted.iterdir() if item.is_dir()]
            source_root = roots[0] if len(roots) == 1 else extracted
            manifest = load_manifest(source_root)
            return self._install(manifest, source_root, "github", f"https://github.com/{owner}/{repo}", ref)

    def update(self, plugin_id: str) -> InstalledGuiPlugin:
        record = next((item for item in self.list() if item.id == plugin_id), None)
        if record is None:
            raise KeyError(plugin_id)
        if record.source_kind == "local":
            return self.install_local(Path(record.source))
        if record.source_kind == "github":
            source = record.source if record.ref is None else f"{record.source}/tree/{record.ref}"
            return self.install_github(source)
        raise ValueError(f"plugin {plugin_id!r} has an unsupported source type")

    def set_enabled(self, plugin_id: str, enabled: bool) -> None:
        records = [item if item.id != plugin_id else InstalledGuiPlugin(**{**asdict(item), "enabled": enabled}) for item in self.list()]
        self._write(records)

    def remove(self, plugin_id: str) -> None:
        records = [item for item in self.list() if item.id != plugin_id]
        path = self.packages / plugin_id
        if path.exists():
            shutil.rmtree(path)
        self._write(records)

    def _install(self, manifest: GuiPluginManifest, source: Path, kind: str, origin: str, ref: str | None = None) -> InstalledGuiPlugin:
        self.packages.mkdir(parents=True, exist_ok=True)
        target = self.packages / manifest.id
        temporary = self.packages / f".{manifest.id}.tmp"
        previous = self.packages / f".{manifest.id}.previous"
        if temporary.exists():
            shutil.rmtree(temporary)
        if previous.exists():
            shutil.rmtree(previous)
        shutil.copytree(source, temporary)
        try:
            if target.exists():
                target.replace(previous)
            temporary.replace(target)
        except Exception:
            if previous.exists() and not target.exists():
                previous.replace(target)
            raise
        if previous.exists():
            shutil.rmtree(previous)
        record = InstalledGuiPlugin(manifest.id, kind, origin, str(target), True, ref)
        records = [item for item in self.list() if item.id != manifest.id]
        self._write([*records, record])
        return record

    def _write(self, records: list[InstalledGuiPlugin]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.index_path.with_suffix(".tmp")
        temporary.write_text(json.dumps([asdict(item) for item in records], indent=2, sort_keys=True), encoding="utf-8")
        temporary.replace(self.index_path)


def load_manifest(root: Path) -> GuiPluginManifest:
    path = root / "plugin.toml"
    if not path.is_file():
        raise ValueError("GUI plugin requires plugin.toml")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    section = config.get("plugin")
    if not isinstance(section, dict):
        raise ValueError("plugin.toml requires a [plugin] section")
    required = ("id", "name", "version", "api", "entry")
    if not all(isinstance(section.get(name), str) and section[name] for name in required):
        raise ValueError("[plugin] requires non-empty id, name, version, api, and entry")
    if section["api"] != API_VERSION:
        raise ValueError(f"plugin API {section['api']!r} is incompatible with GUI API {API_VERSION!r}")
    if not _PLUGIN_ID.fullmatch(section["id"]):
        raise ValueError("plugin id must contain only letters, digits, '.', '_' or '-'")
    module_name, separator, attribute = section["entry"].partition(":")
    if not separator or not module_name or not attribute or not all(part.isidentifier() for part in module_name.split(".")) or not attribute.isidentifier():
        raise ValueError("plugin entry must be a Python module:callable")
    requires = config.get("python", {}).get("requires", []) if isinstance(config.get("python", {}), dict) else []
    if not isinstance(requires, (list, tuple)) or not all(isinstance(item, str) for item in requires):
        raise ValueError("[python].requires must be a list of strings")
    return GuiPluginManifest(section["id"], section["name"], section["version"], section["api"], section["entry"], section.get("description", ""), tuple(requires))


def validate_requirements(requirements: tuple[str, ...]) -> None:
    missing = []
    for requirement in requirements:
        distribution = re.split(r"[<>=!~ ;\[]", requirement, maxsplit=1)[0].strip()
        if not distribution:
            raise ValueError(f"invalid Python dependency {requirement!r}")
        try:
            metadata.version(distribution)
        except metadata.PackageNotFoundError:
            missing.append(requirement)
    if missing:
        raise ValueError("missing plugin Python dependencies: " + ", ".join(missing))


def parse_github_source(source: str) -> tuple[str, str, str]:
    value = source.strip()
    if "://" not in value:
        value = f"https://github.com/{value}"
    parsed = urlparse(value)
    if parsed.netloc.lower() != "github.com":
        raise ValueError("only github.com plugin sources are supported")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise ValueError("GitHub source must identify owner/repository")
    ref = "main"
    if len(parts) >= 4 and parts[2] in {"tree", "commit"}:
        ref = parts[3]
    return parts[0], parts[1].removesuffix(".git"), ref


def extract_archive(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as contents:
        for member in contents.infolist():
            target = destination / member.filename
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError("GitHub plugin archive contains an unsafe path")
        contents.extractall(destination)


def load_enabled_plugins(store: GuiPluginStore, host) -> tuple[str, ...]:
    loaded: list[str] = []
    for record in store.list():
        if not record.enabled:
            continue
        root = Path(record.install_path)
        try:
            manifest = load_manifest(root)
            validate_requirements(manifest.requires)
            module_name, separator, attribute = manifest.entry.partition(":")
            if not separator:
                raise ValueError("plugin entry must be module:attribute")
            sys.path.insert(0, str(root))
            try:
                callback = getattr(importlib.import_module(module_name), attribute)
            finally:
                sys.path.pop(0)
            if not callable(callback):
                raise ValueError("plugin entry is not callable")
            callback(host.for_plugin(manifest.id))
            loaded.append(manifest.id)
        except Exception as error:
            host.emit("plugin_failed", f"{record.id}: {type(error).__name__}: {error}")
    return tuple(loaded)
