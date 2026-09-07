"""Core-owned orchestration for conservative CFG region reduction.

The reducer is deliberately unaware of VM frontends and AST renderers.  It
only coordinates VM-neutral matchers, the shared CFG rewrite validator, and a
host-provided semantic safety check.  Matchers remain small adapters at this
seam; all accepted candidates are still fail-closed by ``validate_cfg_rewrite``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TypeVar

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.cfg_rewrite import (
    CFGRewriteCandidate,
    RewriteEvidence,
    validate_cfg_rewrite,
)


FunctionT = TypeVar("FunctionT")
Reducer = Callable[[FunctionT], FunctionT | None]
SafetyCheck = Callable[[FunctionT], bool]


@dataclass(frozen=True)
class RegionReducer:
    """Run deterministic, fail-closed CFG region reducers.

    ``reducers`` are tried in registration order.  A reducer listed in
    ``normalizers`` may be applied repeatedly before the next structural
    matcher is attempted.  Structural candidates return immediately after
    validation, preserving the existing one-rewrite-at-a-time fixed point.
    """

    reducers: tuple[Reducer[FunctionT], ...]
    normalizers: tuple[Reducer[FunctionT], ...] = ()

    def reduce(
        self,
        function: FunctionT,
        *,
        is_safe: SafetyCheck[FunctionT] | None = None,
    ) -> FunctionT | None:
        current = function
        normalized = False
        # Keep tuple membership rather than hashing callbacks: callers may
        # register callable instances that intentionally define no hash.
        normalizer_set = self.normalizers

        while True:
            for reducer in self.reducers:
                candidate = reducer(current)
                if candidate is None:
                    continue

                if _is_low_level_function(current):
                    snapshot = build_cfg(current)
                    proof = (
                        "lossless-normalization"
                        if reducer in normalizer_set
                        else "exact-topology"
                    )
                    decision = validate_cfg_rewrite(
                        CFGRewriteCandidate(
                            original=current,
                            rewritten=candidate,
                            rule=_rule_name(candidate, reducer),
                            proof=proof,
                            evidence=RewriteEvidence(
                                edge_ids=tuple(edge.edge_id for edge in snapshot.edges),
                                raw_context=tuple(
                                    current.metadata.get("unsupported_context", ())
                                )
                                or tuple(
                                    str(row)
                                    for row in current.metadata.get(
                                        "bytecode_instructions", ()
                                    )
                                ),
                                snapshot_key=tuple(
                                    (edge.source, edge.target, edge.kind, edge.ordinal)
                                    for edge in snapshot.edges
                                ),
                            ),
                        )
                    )
                    if not decision.accepted:
                        continue
                    candidate = decision.function
                else:
                    # Retain the historical extension seam for non-VM
                    # FunctionIR callers.  VM preservation views always go
                    # through the validator above.
                    candidate = replace(
                        candidate,
                        control_provenance=tuple(
                            dict.fromkeys(
                                (
                                    *getattr(current, "control_provenance", ()),
                                    *getattr(candidate, "control_provenance", ()),
                                )
                            )
                        ),
                        bytecode_control_flow=tuple(
                            dict.fromkeys(
                                (
                                    *getattr(current, "bytecode_control_flow", ()),
                                    *getattr(candidate, "bytecode_control_flow", ()),
                                )
                            )
                        ),
                    )

                if is_safe is not None and not is_safe(candidate):
                    continue
                if reducer in normalizer_set:
                    current = candidate
                    normalized = True
                    break
                return candidate
            else:
                return current if normalized else None


def _is_low_level_function(function: object) -> bool:
    return getattr(function, "recovery_kind", None) in {
        "generic-vm-low-level-cfg",
        "generic-vm-low-level-cfg-structured",
    }


def _rule_name(function: object, reducer: Reducer[FunctionT]) -> str:
    metadata = getattr(function, "metadata", {})
    return metadata.get("low_level_cfg_structured", getattr(reducer, "__name__", "region-reducer"))
