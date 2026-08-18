from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from unidecompiler.core.ir import ModuleIR


@dataclass(frozen=True)
class OutputArtifact:
    kind: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    id: str
    display_name: str

    def emit(self, module: ModuleIR) -> OutputArtifact:
        """Emit a user-facing artifact from Universal IR."""

