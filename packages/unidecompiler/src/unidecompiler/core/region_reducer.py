"""Core-owned orchestration for conservative CFG region reduction.

The reducer is deliberately unaware of VM frontends and AST renderers.  It
only coordinates VM-neutral matchers, the shared CFG rewrite validator, and a
host-provided semantic safety check.  Matchers remain small adapters at this
seam; all accepted candidates are still fail-closed by ``validate_cfg_rewrite``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, is_dataclass, replace
from typing import TypeVar

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.cfg_rewrite import (
    CFGRewriteCandidate,
    RecoveryDecline,
    RewriteEvidence,
    validate_cfg_rewrite,
)
from unidecompiler.core.ir import exceptional_transfers


FunctionT = TypeVar("FunctionT")
Reducer = Callable[[FunctionT], FunctionT | RecoveryDecline | None]
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
    max_rounds: int | None = None
    # Structural reducers historically returned after their first accepted
    # candidate.  Some core-owned worklists need a fixed point instead:
    # every accepted candidate is one atomic rewrite, after which the CFG and
    # all derived facts must be rebuilt before another matcher runs.  Keep the
    # switch explicit so non-worklist callers retain the old one-shot contract.
    continue_after_structural: bool = False
    # ``None`` means that a matcher found no candidate; it is not itself a
    # rejected rewrite.  The legacy reducer keeps a summary rejection for
    # preservation-floor callers, while local worklists can disable that
    # summary and only record explicit ``RecoveryDecline``/validator rejects.
    record_no_candidate_rejection: bool = True

    def reduce(
        self,
        function: FunctionT,
        *,
        is_safe: SafetyCheck[FunctionT] | None = None,
    ) -> FunctionT | None:
        current = function
        normalized = False
        rejected = False
        declined_rules: list[str] = []
        declined_attempts: list[dict[str, object]] = []
        explicit_decline = False
        rounds = 0
        seen: set[str] = set()
        budget = self.max_rounds
        if budget is None:
            block_count = len(getattr(function, "blocks", ()))
            budget = max(64, block_count * 32 + len(self.reducers) * 2)
        if budget < 1:
            raise ValueError("max_rounds must be positive")
        # Keep tuple membership rather than hashing callbacks: callers may
        # register callable instances that intentionally define no hash.
        normalizer_set = self.normalizers

        while True:
            fingerprint = _reducer_fingerprint(current)
            if fingerprint in seen:
                current = _record_reducer_diagnostic(
                    current,
                    code="non_progress",
                    message="region reducer reached a repeated function state",
                    rounds=rounds,
                )
                return current if (normalized or rejected or _is_low_level_function(current)) else None
            seen.add(fingerprint)
            rounds += 1
            if rounds > budget:
                current = _record_reducer_diagnostic(
                    current,
                    code="budget_exhausted",
                    message="region reducer reached its fixed-point budget",
                    rounds=rounds - 1,
                )
                return current if (normalized or rejected or _is_low_level_function(current)) else None
            for reducer in self.reducers:
                result = reducer(current)
                decline: RecoveryDecline | None = (
                    result if isinstance(result, RecoveryDecline) else None
                )
                candidate = None if decline is not None else result
                if candidate is None:
                    if _is_low_level_function(current):
                        explicit_decline = explicit_decline or decline is not None
                        rule = (
                            decline.rule
                            if decline is not None and decline.rule
                            else getattr(reducer, "__name__", "region-reducer")
                        )
                        if rule not in declined_rules:
                            declined_rules.append(rule)
                            decline_evidence = _complete_decline_evidence(
                                current,
                                decline.evidence if decline is not None else None,
                            )
                            reasons = (
                                decline.reasons
                                if decline is not None and decline.reasons
                                else (
                                    "matcher returned no candidate; local "
                                    "topology/value/exception preconditions "
                                    "were not proven",
                                )
                            )
                            declined_attempts.append(
                                {
                                    "rule": rule,
                                    "classification": (
                                        "explicit-decline"
                                        if decline is not None
                                        else "no-candidate"
                                    ),
                                    "block_ids": tuple(decline_evidence.block_ids),
                                    "edge_ids": tuple(decline_evidence.edge_ids),
                                    "snapshot_key": tuple(
                                        decline_evidence.snapshot_key or ()
                                    ),
                                    "raw_context": tuple(decline_evidence.raw_context),
                                    "reasons": tuple(reasons),
                                    # Keep a singular, human-readable reason
                                    # alongside the structured tuple.  The
                                    # matcher-attempt record is public audit
                                    # metadata and older consumers expect a
                                    # non-empty ``reason`` field when a
                                    # candidate is declined.
                                    "reason": "; ".join(reasons),
                                }
                            )
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
                                edge_ids=_rewrite_edge_evidence(snapshot, candidate),
                                raw_context=tuple(
                                    current.metadata.get("unsupported_context", ())
                                )
                                or tuple(
                                    str(row)
                                    for row in current.metadata.get(
                                        "bytecode_instructions", ()
                                    )
                                ),
                                snapshot_key=snapshot.snapshot_key,
                                block_ids=snapshot.block_ids,
                            ),
                        )
                    )
                    if not decision.accepted:
                        # Rejection evidence is metadata only; the CFG blocks,
                        # edges, and handler state remain untouched.
                        current = _record_rejection(
                            current,
                            decision,
                            classification="validator-rejection",
                        )
                        rejected = True
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
                    if _is_low_level_function(current):
                        current = _record_rejection(
                            current,
                            None,
                            rule=_rule_name(candidate, reducer),
                            reason="whole-function semantic safety check rejected candidate",
                            evidence=_rejection_evidence(current),
                            classification="semantic-safety-rejection",
                        )
                        rejected = True
                    continue
                if _is_low_level_function(current):
                    candidate = _record_acceptance(candidate, decision)
                    candidate = _record_attempts(candidate, declined_attempts)
                if reducer in normalizer_set:
                    current = candidate
                    normalized = True
                    break
                if self.continue_after_structural:
                    current = candidate
                    normalized = True
                    break
                return candidate
            else:
                # Preserve rejection evidence for low-level VM functions even
                # when no candidate was accepted.  Returning ``None`` here
                # would discard the auditable reasons attached above and make
                # the next caller believe that no recovery attempt happened.
                if (
                    _is_low_level_function(current)
                    and declined_attempts
                    and (self.record_no_candidate_rejection or explicit_decline)
                ):
                    current = _record_rejection(
                        current,
                        None,
                        rule="region-reducer",
                        reason=(
                            "no candidate passed validation; preservation CFG "
                            "retained after all registered matcher attempts"
                        ),
                        evidence=_rejection_evidence(current),
                        attempted_rules=tuple(declined_rules),
                        attempts=tuple(declined_attempts),
                        classification="no-candidate",
                    )
                    rejected = True
                if normalized or (rejected and _is_low_level_function(current)):
                    return current
                return None


def _reducer_fingerprint(function: object) -> str:
    """Return a deterministic state key without relying on object hashing."""

    # Proof/rejection/context metadata is append-only audit data and may be
    # large for handler-heavy graphs.  It must not participate in structural
    # fixed-point identity: serializing it on every matcher round can dominate
    # recovery time.  Keep only the compact fields that affect rule dispatch.
    metadata = getattr(function, "metadata", {})
    scheduling_metadata = tuple(
        sorted(
            (key, repr(metadata[key]))
            for key in ("low_level_cfg_structured", "recovery_phi_materialized")
            if key in metadata
        )
    )
    return repr(
        (
            getattr(function, "recovery_kind", None),
            getattr(function, "blocks", ()),
            getattr(function, "control_provenance", ()),
            getattr(function, "bytecode_control_flow", ()),
            scheduling_metadata,
        )
    )


def _rewrite_edge_evidence(original_cfg, rewritten: FunctionT) -> tuple[str, ...]:
    """Identify the concrete original edges affected by one candidate.

    Matchers remain responsible for proving their local topology.  The common
    coordinator records only edges that disappear or change identity, rather
    than claiming that every function edge was consumed by every rule.  This
    makes proof metadata useful for diagnosing a remaining goto while keeping
    the validator conservative.
    """

    rewritten_cfg = build_cfg(rewritten)
    rewritten_ids = {edge.edge_id for edge in rewritten_cfg.edges}
    affected = [edge.edge_id for edge in original_cfg.edges if edge.edge_id not in rewritten_ids]
    if affected:
        return tuple(affected)
    # A one-block representation transition has no CFG edge to consume.  Keep
    # an empty edge list rather than inventing an edge identity.
    return ()


def _record_reducer_diagnostic(
    function: FunctionT,
    *,
    code: str,
    message: str,
    rounds: int,
) -> FunctionT:
    if not hasattr(function, "metadata") or not is_dataclass(function):
        return function
    metadata = dict(getattr(function, "metadata", {}))
    existing = tuple(metadata.get("recovery_reducer_diagnostics", ()))
    diagnostic = {"code": code, "message": message, "rounds": rounds}
    if diagnostic in existing:
        return function
    return replace(
        function,
        metadata={
            **metadata,
            "recovery_reducer_diagnostics": (*existing, diagnostic),
        },
    )


def _is_low_level_function(function: object) -> bool:
    return getattr(function, "recovery_kind", None) in {
        "generic-vm-low-level-cfg",
        "generic-vm-low-level-cfg-structured",
    }


def _rule_name(function: object, reducer: Reducer[FunctionT]) -> str:
    metadata = getattr(function, "metadata", {})
    return metadata.get("low_level_cfg_structured", getattr(reducer, "__name__", "region-reducer"))


def _record_acceptance(function: FunctionT, decision) -> FunctionT:
    metadata = dict(getattr(function, "metadata", {}))
    evidence = decision.evidence
    proof = {
        "rule": decision.rule or metadata.get("low_level_cfg_structured"),
        "block_ids": tuple(evidence.block_ids),
        "edge_ids": tuple(evidence.edge_ids),
        "snapshot_key": tuple(evidence.snapshot_key or ()),
        "raw_context": tuple(evidence.raw_context),
    }
    existing = tuple(metadata.get("recovery_proofs", ()))
    if proof not in existing:
        metadata["recovery_proofs"] = (*existing, proof)
    return replace(function, metadata=metadata)


def _record_attempts(function: FunctionT, attempts: list[dict[str, object]]) -> FunctionT:
    """Attach deterministic matcher attempts without changing executable IR."""

    if not attempts or not hasattr(function, "metadata") or not is_dataclass(function):
        return function
    metadata = dict(getattr(function, "metadata", {}))
    existing = tuple(metadata.get("recovery_matcher_attempts", ()))
    additions = tuple(item for item in attempts if item not in existing)
    if not additions:
        return function
    return replace(
        function,
        metadata={
            **metadata,
            "recovery_matcher_attempts": (*existing, *additions),
        },
    )


def _record_rejection(
    function: FunctionT,
    decision,
    *,
    rule: str | None = None,
    reason: str | None = None,
    evidence: RewriteEvidence | None = None,
    attempted_rules: tuple[str, ...] = (),
    attempts: tuple[dict[str, object], ...] = (),
    classification: str | None = None,
) -> FunctionT:
    if not hasattr(function, "metadata") or not is_dataclass(function):
        return function
    metadata = dict(getattr(function, "metadata", {}))
    evidence_obj = evidence if evidence is not None else (
        None if decision is None else decision.evidence
    )
    edge_ids = () if evidence_obj is None else evidence_obj.edge_ids
    reasons = () if decision is None else decision.reasons
    item = {
        "rule": rule or getattr(decision, "rule", "") or metadata.get("low_level_cfg_structured"),
        "block_ids": () if evidence_obj is None else tuple(evidence_obj.block_ids),
        "edge_ids": tuple(edge_ids),
        "snapshot_key": () if evidence_obj is None else tuple(evidence_obj.snapshot_key or ()),
        "raw_context": () if evidence_obj is None else tuple(evidence_obj.raw_context),
        "reasons": tuple(reasons) or ((reason,) if reason else ()),
        "classification": classification or (
            "validator-rejection" if decision is not None else "rejection"
        ),
        "attempted_rules": tuple(attempted_rules),
        "attempts": tuple(attempts),
    }
    existing = tuple(metadata.get("recovery_rejections", ()))
    if item not in existing:
        metadata["recovery_rejections"] = (*existing, item)
    return replace(function, metadata=metadata)


def _has_exceptional_context(function: object) -> bool:
    return any(
        exceptional_transfers(block)
        or bool(getattr(block, "active_exception_handlers", ()))
        for block in getattr(function, "blocks", ())
    )


def _rejection_evidence(function: FunctionT) -> RewriteEvidence:
    cfg = build_cfg(function)
    return RewriteEvidence(
        edge_ids=tuple(edge.edge_id for edge in cfg.edges if edge.kind == "exception"),
        snapshot_key=cfg.snapshot_key,
        block_ids=cfg.block_ids,
        raw_context=tuple(
            str(row)
            for row in getattr(function, "metadata", {}).get("bytecode_instructions", ())
        ),
    )


def _complete_decline_evidence(
    function: FunctionT,
    evidence: RewriteEvidence | None,
) -> RewriteEvidence:
    """Fill matcher evidence from the current immutable CFG snapshot.

    A matcher may provide a smaller local evidence set, but it must never be
    allowed to omit the snapshot identity or source context from the audit
    record.  Evidence is completed here, at the core boundary, so frontend
    matchers cannot accidentally invent block or edge identities.
    """

    baseline = _rejection_evidence(function)
    if evidence is None:
        return baseline
    return RewriteEvidence(
        edge_ids=tuple(evidence.edge_ids) or baseline.edge_ids,
        value_ids=tuple(evidence.value_ids),
        raw_context=tuple(evidence.raw_context) or baseline.raw_context,
        snapshot_key=evidence.snapshot_key or baseline.snapshot_key,
        block_ids=tuple(evidence.block_ids) or baseline.block_ids,
    )
