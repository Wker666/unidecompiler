"""Generic call side-effect summaries for core analyses.

The summary is descriptive only: it never executes a call or asks a frontend
to interpret one.  Unknown calls remain conservative, which lets stack/value
passes preserve ordering without embedding language-specific rules.
"""

from __future__ import annotations

from typing import Mapping, Protocol

from unidecompiler.core.ir import Call, CallEffectSummary


class CallEffectProvider(Protocol):
    def summary(self, call: Call) -> CallEffectSummary | None: ...


PURE_CALL = CallEffectSummary(may_raise=False, returns=1, unknown=False)
UNKNOWN_CALL = CallEffectSummary()


def summarize_call(
    call: Call,
    summaries: Mapping[str, CallEffectSummary] | CallEffectProvider | None = None,
) -> CallEffectSummary:
    """Resolve a summary by static global callee name, failing closed."""

    if summaries is None:
        return call.effect_summary or UNKNOWN_CALL
    if hasattr(summaries, "summary"):
        result = summaries.summary(call)  # type: ignore[attr-defined]
        return result if result is not None else UNKNOWN_CALL
    callee = call.callee
    name = getattr(callee, "name", None)
    if isinstance(name, str):
        return summaries.get(name, UNKNOWN_CALL)
    return UNKNOWN_CALL


def call_preserves_storage(
    call: Call,
    storage: str,
    summaries: Mapping[str, CallEffectSummary] | CallEffectProvider | None = None,
) -> bool:
    """Whether a call summary proves it cannot write the given storage."""

    summary = summarize_call(call, summaries)
    return not summary.unknown and storage not in summary.writes
