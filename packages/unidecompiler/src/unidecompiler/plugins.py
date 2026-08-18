from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from unidecompiler.core.ir import ModuleIR


class FrontendDecodeError(ValueError):
    """A plugin could identify but not decode its input artifact."""


@dataclass(frozen=True)
class FrontendVersionSupport:
    """Frontend-owned declaration of supported bytecode versions."""

    family: str
    versions: tuple[str, ...]
    parser: str
    status: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrontendModule:
    """Decoded frontend-specific module.

    ``payload`` intentionally remains frontend-owned. Core code must not inspect
    it; only the same frontend plugin should lift it into ``ModuleIR``.
    """

    frontend_id: str
    payload: Any
    metadata: dict[str, Any]


class FrontendPlugin(Protocol):
    id: str
    display_name: str
    supported_inputs: tuple[str, ...]
    version_support: FrontendVersionSupport

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        """Return whether this plugin can decode the given bytes."""

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        """Decode raw bytes into a frontend-owned model."""

    def lift(self, module: FrontendModule) -> ModuleIR:
        """Lift the frontend-owned model into Universal IR."""
