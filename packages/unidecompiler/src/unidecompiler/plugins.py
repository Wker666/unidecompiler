from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from unidecompiler.core.ir import ModuleIR
from unidecompiler.progress import ProgressReporter


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


class ProgressiveFrontend(Protocol):
    """Optional progress-aware frontend capability.

    Existing frontends remain valid ``FrontendPlugin`` implementations.  A
    host uses these methods only when present and otherwise falls back to
    phase-level progress around the regular ``decode``/``lift`` methods.
    """

    def decode_with_progress(
        self,
        data: bytes,
        filename: str | None,
        reporter: ProgressReporter,
    ) -> FrontendModule:
        ...

    def lift_with_progress(
        self,
        module: FrontendModule,
        reporter: ProgressReporter,
    ) -> ModuleIR:
        ...
