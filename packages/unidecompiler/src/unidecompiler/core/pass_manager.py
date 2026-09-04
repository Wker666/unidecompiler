"""Small, VM-neutral pass scheduling primitives.

Passes are deliberately value based: a pass receives an immutable function and
returns a new function (or ``None`` when it has no candidate).  The manager
never exposes partially-mutated state and records why a budget or fixed point
stopped.  Frontends and renderers must not depend on this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from unidecompiler.core.ir import FunctionIR
PassFn = Callable[[FunctionIR], FunctionIR | None]


@dataclass(frozen=True)
class PassSpec:
    name: str
    run: PassFn
    max_rounds: int = 1
    repeat: bool = False

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("pass name must not be empty")
        if not callable(self.run):
            raise TypeError("pass run must be callable")
        if self.max_rounds < 1:
            raise ValueError("pass max_rounds must be positive")


@dataclass(frozen=True)
class PassDiagnostic:
    pass_name: str
    code: str
    message: str
    before: object | None = None
    after: object | None = None


@dataclass(frozen=True)
class PassRunResult:
    function: FunctionIR
    changed: bool
    diagnostics: tuple[PassDiagnostic, ...] = ()


def run_passes(
    function: FunctionIR,
    passes: Iterable[PassSpec],
    *,
    fingerprint: Callable[[FunctionIR], object] | None = None,
) -> PassRunResult:
    """Run deterministic passes with fail-closed fixed-point handling.

    A pass may return the same value or ``None``.  A repeated fingerprint is
    treated as non-progress and leaves the last known-good function intact.
    Exceptions are intentionally not swallowed: malformed IR is an internal
    error and must be fixed by the caller rather than converted to a guess.
    """

    identify = fingerprint or _default_fingerprint
    current = function
    diagnostics: list[PassDiagnostic] = []
    changed = False
    for spec in passes:
        seen: set[object] = {identify(current)}
        limit = spec.max_rounds if spec.repeat else 1
        stopped = False
        for _ in range(limit):
            before = identify(current)
            candidate = spec.run(current)
            if candidate is None or candidate == current:
                stopped = True
                break
            after = identify(candidate)
            if after == before or after in seen:
                diagnostics.append(
                    PassDiagnostic(
                        spec.name,
                        "non_progress",
                        "pass produced a repeated state; preserving last known-good IR",
                        before,
                        after,
                    )
                )
                stopped = True
                break
            seen.add(after)
            current = candidate
            changed = True
        if not stopped and spec.repeat:
            diagnostics.append(
                PassDiagnostic(
                    spec.name,
                    "budget_exhausted",
                    "pass reached its fixed-point budget; preserving the latest verified IR",
                    identify(current),
                    identify(current),
                )
            )
    return PassRunResult(current, changed, tuple(diagnostics))


def _default_fingerprint(function: FunctionIR) -> object:
    return (
        function.recovery_kind,
        function.blocks,
        function.control_provenance,
        function.bytecode_control_flow,
        tuple(sorted((str(key), repr(value)) for key, value in function.metadata.items())),
    )
