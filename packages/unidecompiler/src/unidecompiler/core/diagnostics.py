from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from unidecompiler.core.ir import SourceRef


Severity = Literal["info", "warning", "error"]
ConfidenceLevel = Literal["none", "low", "medium", "high"]


@dataclass(frozen=True)
class Diagnostic:
    code: str
    message: str
    severity: Severity = "warning"
    frontend: str | None = None
    function: str | None = None
    offset: int | None = None
    source: SourceRef | None = None
    raw_context: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "code": self.code,
            "message": self.message,
            "severity": self.severity,
            "frontend": self.frontend,
            "function": self.function,
            "offset": self.offset,
            "source": None if self.source is None else {
                "frontend": self.source.frontend,
                "offset": self.source.offset,
                "line": self.source.line,
                "detail": self.source.detail,
            },
            "raw_context": list(self.raw_context),
        }


@dataclass(frozen=True)
class Confidence:
    level: ConfidenceLevel
    reason: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "reason": self.reason,
        }


def unsupported_diagnostic(
    *,
    code: str,
    message: str,
    frontend: str | None = None,
    function: str | None = None,
    offset: int | None = None,
    source: SourceRef | None = None,
    raw_context: tuple[str, ...] = (),
) -> Diagnostic:
    return Diagnostic(
        code=code,
        message=message,
        severity="warning",
        frontend=frontend,
        function=function,
        offset=offset,
        source=source,
        raw_context=raw_context,
    )
