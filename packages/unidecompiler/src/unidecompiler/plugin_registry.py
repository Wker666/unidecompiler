from __future__ import annotations

from dataclasses import dataclass
from importlib.metadata import entry_points
from importlib import import_module
from pathlib import Path
import sys
import tomllib
from typing import Iterable

from unidecompiler.plugins import FrontendPlugin, FrontendVersionSupport


class FrontendSelectionError(ValueError):
    pass


class FrontendRegistrationError(FrontendSelectionError):
    """Raised when an external frontend manifest cannot be loaded safely."""


@dataclass
class FrontendRegistry:
    _plugins: list[FrontendPlugin]

    def __init__(self) -> None:
        self._plugins = []
        self._sources: dict[str, str] = {}

    @classmethod
    def discover(cls, group: str = "unidecompiler.frontends") -> "FrontendRegistry":
        """Load installed frontend adapters from a Python entry-point group.

        The core never imports a concrete frontend.  Hosts may instead use
        :meth:`from_plugins` for an explicitly supplied set of adapters.
        """
        selected = sorted(entry_points().select(group=group), key=lambda entry_point: entry_point.name)
        return cls.from_plugins(_load_entry_point(entry_point) for entry_point in selected)

    @classmethod
    def from_plugins(cls, plugins: Iterable[FrontendPlugin]) -> "FrontendRegistry":
        registry = cls()
        for plugin in plugins:
            registry.register(plugin)
        return registry

    def register(self, plugin: FrontendPlugin, *, source: str | None = None) -> None:
        _validate_plugin(plugin)
        if any(existing.id == plugin.id for existing in self._plugins):
            raise FrontendSelectionError(f"duplicate frontend plugin id: {plugin.id}")
        self._plugins.append(plugin)
        self._sources[plugin.id] = source or "built-in"

    def register_directory(self, directory: Path | str) -> FrontendPlugin:
        root = Path(directory).expanduser().resolve()
        manifest_path = root / "unidecompiler-plugin.toml"
        if not manifest_path.is_file():
            raise FrontendRegistrationError(f"missing manifest: {manifest_path}")
        try:
            manifest = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as error:
            raise FrontendRegistrationError(f"invalid manifest: {error}") from error
        config = manifest.get("frontend")
        if not isinstance(config, dict):
            raise FrontendRegistrationError("manifest must contain [frontend]")
        plugin_id = config.get("id")
        target = config.get("module")
        if not isinstance(plugin_id, str) or not plugin_id:
            raise FrontendRegistrationError("manifest frontend.id must be a non-empty string")
        if not isinstance(target, str) or ":" not in target:
            raise FrontendRegistrationError("manifest frontend.module must be 'module:attribute'")
        module_name, attribute_name = target.split(":", 1)
        import_roots = [root, root / "src"]
        for import_root in import_roots:
            if import_root.is_dir() and str(import_root) not in sys.path:
                sys.path.insert(0, str(import_root))
        try:
            loaded = getattr(import_module(module_name), attribute_name)
            plugin = loaded() if isinstance(loaded, type) else loaded
            _validate_plugin(plugin, require_metadata=True)
        except Exception as error:
            if isinstance(error, FrontendRegistrationError):
                raise
            raise FrontendRegistrationError(f"could not load {target}: {error}") from error
        if plugin.id != plugin_id:
            raise FrontendRegistrationError(
                f"manifest id {plugin_id!r} does not match plugin id {plugin.id!r}"
            )
        self.register(plugin, source=str(root))
        return plugin

    def unregister(self, plugin_id: str) -> FrontendPlugin:
        for index, plugin in enumerate(self._plugins):
            if plugin.id == plugin_id:
                removed = self._plugins.pop(index)
                self._sources.pop(plugin_id, None)
                return removed
        raise FrontendSelectionError(f"unknown frontend plugin: {plugin_id}")

    def list(self) -> tuple[FrontendPlugin, ...]:
        return tuple(self._plugins)

    def source_for(self, plugin_id: str) -> str:
        return self._sources.get(plugin_id, "built-in")

    def version_support(self) -> tuple[tuple[FrontendPlugin, FrontendVersionSupport], ...]:
        return tuple((plugin, plugin.version_support) for plugin in self._plugins)

    def get(self, plugin_id: str) -> FrontendPlugin:
        for plugin in self._plugins:
            if plugin.id == plugin_id:
                return plugin
        raise FrontendSelectionError(f"unknown frontend plugin: {plugin_id}")

    def select(
        self,
        data: bytes,
        filename: str | None = None,
        explicit_id: str | None = None,
    ) -> FrontendPlugin:
        if explicit_id is not None:
            plugin = self.get(explicit_id)
            if not plugin.can_load(data, filename):
                raise FrontendSelectionError(
                    f"frontend {explicit_id!r} cannot load this input"
                )
            return plugin

        matches = [
            plugin for plugin in self._plugins if plugin.can_load(data, filename)
        ]
        if not matches:
            raise FrontendSelectionError("no frontend plugin can load this input")
        if len(matches) > 1:
            ids = ", ".join(plugin.id for plugin in matches)
            raise FrontendSelectionError(f"ambiguous frontend plugins: {ids}")
        return matches[0]


def _load_entry_point(entry_point) -> FrontendPlugin:
    loaded = entry_point.load()
    plugin = loaded() if isinstance(loaded, type) else loaded
    try:
        _validate_plugin(plugin)
    except FrontendRegistrationError as error:
        raise FrontendSelectionError(
            f"frontend entry point {entry_point.name!r} did not load a FrontendPlugin: {error}"
        ) from error
    return plugin


def _validate_plugin(plugin: FrontendPlugin, *, require_metadata: bool = False) -> None:
    required = ("id", "can_load", "decode", "lift")
    if require_metadata:
        required = ("id", "display_name", "supported_inputs", "version_support", "can_load", "decode", "lift")
    if not all(hasattr(plugin, attribute) for attribute in required):
        missing = ", ".join(attribute for attribute in required if not hasattr(plugin, attribute))
        raise FrontendRegistrationError(f"frontend is missing required attributes: {missing}")
