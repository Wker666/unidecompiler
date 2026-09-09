"""Runtime-only progress events for decompilation hosts.

Progress is deliberately kept outside generic IR and frontend metadata.  It is
an observation seam for hosts such as the CLI and GUI and must never influence
recovery decisions or the returned decompilation result.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal, Protocol


ProgressPhase = Literal[
    "discover",
    "decode",
    "lift",
    "recover",
    "render",
    "complete",
]
ProgressStatus = Literal["started", "progress", "completed", "failed", "cancelled"]
ProgressUnit = Literal["artifact", "function", "instruction", "phase"]


@dataclass(frozen=True)
class ProgressEvent:
    """Immutable, frontend-neutral progress snapshot."""

    artifact_label: str
    batch_index: int | None = None
    batch_total: int | None = None
    phase: ProgressPhase = "discover"
    status: ProgressStatus = "progress"
    completed: int | None = None
    total: int | None = None
    unit: ProgressUnit = "phase"
    fraction: float | None = None
    message: str = ""

    def __post_init__(self) -> None:
        if self.batch_index is not None and self.batch_index < 1:
            raise ValueError("batch_index must be positive")
        if self.batch_total is not None and self.batch_total < 0:
            raise ValueError("batch_total must be non-negative")
        if self.batch_index is not None and self.batch_total is not None:
            if self.batch_index > self.batch_total and self.batch_total:
                raise ValueError("batch_index cannot exceed batch_total")
        if self.completed is not None and self.completed < 0:
            raise ValueError("completed must be non-negative")
        if self.total is not None and self.total < 0:
            raise ValueError("total must be non-negative")
        if self.completed is not None and self.total is not None and self.completed > self.total:
            raise ValueError("completed cannot exceed total")
        if self.fraction is not None and not 0.0 <= self.fraction <= 1.0:
            raise ValueError("fraction must be between zero and one")
        if self.fraction is not None and self.total is None:
            raise ValueError("fraction requires a total")


class ProgressReporter(Protocol):
    """Consumer of runtime progress snapshots."""

    def report(self, event: ProgressEvent) -> None:
        ...


ProgressCallback = Callable[[ProgressEvent], None]


def report_progress(
    reporter: ProgressReporter | None,
    *,
    phase: ProgressPhase,
    status: ProgressStatus = "progress",
    completed: int | None = None,
    total: int | None = None,
    unit: ProgressUnit = "phase",
    message: str = "",
) -> None:
    """Best-effort helper for optional frontend/core instrumentation."""

    if reporter is None:
        return
    reporter.report(
        ProgressEvent(
            artifact_label="",
            phase=phase,
            status=status,
            completed=completed,
            total=total,
            unit=unit,
            fraction=fraction_for(completed, total),
            message=message,
        )
    )


class NullProgressReporter:
    """No-op reporter used by the backwards-compatible default path."""

    def report(self, event: ProgressEvent) -> None:
        return None


class SafeProgressReporter:
    """Adapter that isolates observer failures from decompilation."""

    def __init__(self, reporter: ProgressReporter | ProgressCallback | None) -> None:
        self._reporter = reporter
        self._disabled = reporter is None

    @property
    def enabled(self) -> bool:
        return not self._disabled

    def report(self, event: ProgressEvent) -> None:
        if self._disabled or self._reporter is None:
            return
        try:
            target = self._reporter
            if callable(target):
                target(event)
            else:
                target.report(event)
        except Exception:
            # Progress is observational only.  A broken host observer must
            # never turn a successful decompilation into a failed one.
            self._disabled = True


def fraction_for(completed: int | None, total: int | None) -> float | None:
    """Return a proven fraction, or ``None`` when the total is unknown."""

    if completed is None or total is None or total <= 0:
        return None
    return min(1.0, max(0.0, completed / total))


__all__ = (
    "NullProgressReporter",
    "ProgressCallback",
    "ProgressEvent",
    "ProgressPhase",
    "ProgressReporter",
    "ProgressStatus",
    "ProgressUnit",
    "SafeProgressReporter",
    "fraction_for",
    "report_progress",
)
