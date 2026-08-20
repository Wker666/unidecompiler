"""Public, VM-neutral provenance facts for artifact-backed analysis."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ByteRange:
    """An exact absolute byte range in the original input artifact."""

    start: int
    size: int

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("byte range start must be non-negative")
        if self.size <= 0:
            raise ValueError("byte range size must be positive")

    @property
    def end(self) -> int:
        return self.start + self.size
