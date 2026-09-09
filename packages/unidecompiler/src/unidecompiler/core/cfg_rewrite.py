from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Literal

from unidecompiler.core.cfg import build_cfg, validate_cfg_consistency
from unidecompiler.core.value_validation import validate_value_invariants
from unidecompiler.core.ir import (
    BasicBlock,
    Branch,
    Break,
    Const,
    Continue,
    DoWhile,
    Fallthrough,
    ForEach,
    ForRange,
    FunctionIR,
    If,
    Jump,
    MultiBranch,
    Raise,
    Reraise,
    Return,
    Switch,
    Try,
    Unsupported,
    While,
    exceptional_transfers,
)


CFGRewriteProof = Literal["lossless-normalization", "exact-topology"]


@dataclass(frozen=True)
class RewriteEvidence:
    """Auditable facts used to admit one VM-neutral CFG rewrite.

    Matchers retain their local proof logic, but recording the exact concrete
    edges and source context makes a rejected or later-regressed rewrite
    diagnosable without referring to a frontend-private model.
    """

    edge_ids: tuple[str, ...] = ()
    value_ids: tuple[str, ...] = ()
    raw_context: tuple[str, ...] = ()
    # Optional immutable identity of the CFG snapshot used by the matcher.
    # When supplied, the common gate recomputes the key from ``original`` and
    # rejects stale evidence instead of trusting a block-id comparison.
    snapshot_key: tuple[tuple[object, ...], ...] | None = None
    # Full block identity is kept separately so adding this evidence does not
    # change the legacy positional order of ``snapshot_key``.
    block_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class CFGRewriteCandidate:
    """One core-owned CFG rewrite awaiting fail-closed validation.

    ``proof`` describes how the producing matcher established its local
    preconditions.  It is not a waiver: every candidate also crosses the
    common invariants below before it can replace the preservation CFG.
    """

    original: FunctionIR
    rewritten: FunctionIR
    rule: str
    proof: CFGRewriteProof
    evidence: RewriteEvidence = RewriteEvidence()


@dataclass(frozen=True)
class CFGRewriteDecision:
    accepted: bool
    function: FunctionIR
    reasons: tuple[str, ...] = ()
    evidence: RewriteEvidence = RewriteEvidence()
    # Keep the producer identity alongside evidence so rejection/acceptance
    # records remain attributable even when the candidate's metadata is
    # merged with the preservation function.
    rule: str = ""


@dataclass(frozen=True)
class RecoveryDecline:
    """A matcher-owned, auditable refusal to propose a CFG rewrite.

    ``None`` remains a backwards-compatible return value for older matchers,
    but new core matchers should return this value when they can explain why
    a local candidate was not safe.  The reducer enriches the evidence with
    the current immutable CFG snapshot before storing it in recovery
    metadata.  This type deliberately contains no frontend-specific data.
    """

    rule: str
    reasons: tuple[str, ...]
    evidence: RewriteEvidence = RewriteEvidence()


def validate_cfg_rewrite(candidate: CFGRewriteCandidate) -> CFGRewriteDecision:
    """Accept only a complete, progressing, VM-neutral CFG rewrite.

    Exact topology matchers remain responsible for proving their local graph
    equivalence.  This shared gate enforces the cross-rule invariants that are
    easy to lose when a matcher is maintained in isolation.  Rejection always
    returns the untouched preservation CFG.
    """

    original = candidate.original
    rewritten = _merge_context(original, candidate.rewritten)
    reasons: list[str] = []

    if original.recovery_kind not in {
        "generic-vm-low-level-cfg",
        "generic-vm-low-level-cfg-structured",
    }:
        reasons.append("original is not a low-level preservation CFG or derived structured view")
    if candidate.proof not in {"lossless-normalization", "exact-topology"}:
        reasons.append("unknown CFG rewrite proof kind")
    if not candidate.rule:
        reasons.append("CFG rewrite rule has no stable identity")

    original_cfg = build_cfg(original)
    original_consistency = validate_cfg_consistency(original_cfg)
    original_value_diagnostics = validate_value_invariants(original)
    original_edge_ids = {edge.edge_id for edge in original_cfg.edges}
    if any(edge_id not in original_edge_ids for edge_id in candidate.evidence.edge_ids):
        reasons.append("rewrite evidence references an edge outside the original CFG snapshot")
    if (
        candidate.evidence.snapshot_key is not None
        and candidate.evidence.snapshot_key != _cfg_snapshot_key(original_cfg)
    ):
        reasons.append("rewrite evidence does not match the original CFG snapshot")
    if candidate.evidence.block_ids and candidate.evidence.block_ids != original_cfg.block_ids:
        reasons.append("rewrite evidence does not match the original CFG block snapshot")

    if (
        rewritten.name != original.name
        or rewritten.params != original.params
        or rewritten.nested_functions != original.nested_functions
        or rewritten.source != original.source
    ):
        reasons.append("function identity changed")
    if not original.blocks or not rewritten.blocks:
        reasons.append("CFG rewrite cannot add or remove an empty function body")
    if _has_duplicate_block_ids(original.blocks):
        reasons.append("original CFG contains duplicate block ids")
    if _has_duplicate_block_ids(rewritten.blocks):
        reasons.append("rewritten CFG contains duplicate block ids")
    original_exceptional = _exceptional_context_signature(original)
    rewritten_exceptional = _exceptional_context_signature(rewritten)
    if original_exceptional != rewritten_exceptional:
        reasons.append("rewrite changed exceptional CFG context")
    elif original_exceptional and not _exception_blocks_unchanged(original, rewritten):
        reasons.append("rewrite changed an exception-bearing block")
    elif original_exceptional and not _exception_boundary_edges_unchanged(original, rewritten):
        reasons.append("rewrite changed CFG edges at an exception boundary")
    elif original_exceptional and rewritten == original:
        # Preserve the historical diagnostic for callers that submit an
        # exceptional CFG as an ordinary no-op candidate.  Real ordinary
        # region rewrites are allowed only when this signature is unchanged.
        reasons.append("ordinary CFG rewriting cannot own exceptional context")

    rewritten_cfg = build_cfg(rewritten)
    rewritten_consistency = validate_cfg_consistency(rewritten_cfg)
    if original_cfg.diagnostics:
        reasons.append("original CFG has unresolved targets")
    if rewritten_cfg.diagnostics:
        reasons.append("rewritten CFG has unresolved targets")
    if original_consistency:
        reasons.append("original CFG consistency check failed: " + "; ".join(original_consistency))
    if original_value_diagnostics:
        reasons.append(
            "original violates generic value invariants: "
            + "; ".join(original_value_diagnostics)
        )
    if rewritten_consistency:
        reasons.append("rewritten CFG consistency check failed: " + "; ".join(rewritten_consistency))
    rewritten_value_diagnostics = validate_value_invariants(rewritten)
    if rewritten_value_diagnostics:
        reasons.append(
            "rewrite violates generic value invariants: "
            + "; ".join(rewritten_value_diagnostics)
        )
    if _metadata_was_lost(original, rewritten):
        reasons.append("frontend or recovery metadata was lost")
    if not set(original.control_provenance).issubset(rewritten.control_provenance):
        reasons.append("control provenance was lost")
    if not set(original.bytecode_control_flow).issubset(rewritten.bytecode_control_flow):
        reasons.append("bytecode control-flow facts were lost")
    # Structured rewrites deliberately move branch expressions from CFG
    # terminators into If/While/Switch nodes, and a loop may evaluate one
    # source condition at a different syntactic site.  A generic expression
    # counter cannot prove equivalence and would reject valid transformations.
    # Exact matchers therefore own their local expression/order proof; this
    # gate checks only the representation-independent facts below.
    if _contains_unsupported(rewritten) and not _contains_unsupported(original):
        reasons.append("rewrite introduced unsupported IR")
    if _has_invalid_structured_control(rewritten):
        reasons.append("rewrite introduced invalid structured loop control")
    if _has_nested_low_level_cfg_transfer(rewritten):
        reasons.append(
            "rewrite leaves an explicit CFG transfer inside a structured region"
        )
    if _semantic_literal_atoms(original) != _semantic_literal_atoms(rewritten):
        reasons.append("rewrite changed executable literal content")
    if _terminal_behavior_signature(original) != _terminal_behavior_signature(rewritten):
        reasons.append("rewrite changed terminal behavior")

    block_progress = len(rewritten.blocks) < len(original.blocks)
    edge_progress = len(rewritten_cfg.edges) < len(original_cfg.edges)
    representation_progress = (
        len(original.blocks) == 1
        and not original_cfg.edges
        and rewritten.blocks == original.blocks
        and rewritten.recovery_kind == "generic-vm-low-level-cfg-structured"
    )
    # Some lossless normalizers only change control-flow representation inside
    # a structured statement (for example, hoisting a common Try join).  Those
    # transfers are intentionally not part of the block CFG edge count, but a
    # strict decrease still provides a generic, observable progress measure.
    nested_transfer_progress = (
        candidate.proof == "lossless-normalization"
        and _nested_control_transfer_count(rewritten)
        < _nested_control_transfer_count(original)
    )
    if not (
        block_progress
        or edge_progress
        or representation_progress
        or nested_transfer_progress
    ):
        reasons.append("rewrite made no provable CFG progress")

    if reasons:
        return CFGRewriteDecision(
            False,
            original,
            tuple(dict.fromkeys(reasons)),
            candidate.evidence,
            candidate.rule,
        )
    return CFGRewriteDecision(
        True,
        rewritten,
        evidence=candidate.evidence,
        rule=candidate.rule,
    )


def _cfg_snapshot_key(cfg) -> tuple[tuple[object, ...], ...]:
    return cfg.snapshot_key


def _merge_context(original: FunctionIR, rewritten: FunctionIR) -> FunctionIR:
    return replace(
        rewritten,
        control_provenance=tuple(
            dict.fromkeys((*original.control_provenance, *rewritten.control_provenance))
        ),
        bytecode_control_flow=tuple(
            dict.fromkeys((*original.bytecode_control_flow, *rewritten.bytecode_control_flow))
        ),
    )


def _has_duplicate_block_ids(blocks: tuple[BasicBlock, ...]) -> bool:
    block_ids = tuple(block.id for block in blocks)
    return len(block_ids) != len(set(block_ids))


def _exceptional_context_signature(function: FunctionIR) -> tuple[tuple[object, ...], ...]:
    """Return the immutable exception facts ordinary rewrites must preserve.

    Ordinary region reduction may now proceed around handlers, but it may not
    move, delete, retarget, or alter a block's exception state.  Comparing the
    complete signature keeps this rule independent of block ordering and
    prevents a rewrite from silently changing handler ownership.
    """

    return tuple(
        sorted(
            (
                (
                    block.id,
                    tuple(exceptional_transfers(block)),
                    tuple(block.active_exception_handlers),
                )
                for block in function.blocks
                if exceptional_transfers(block) or block.active_exception_handlers
            ),
            key=lambda item: item[0],
        )
    )


def _exception_blocks_unchanged(original: FunctionIR, rewritten: FunctionIR) -> bool:
    """Require exact preservation of blocks carrying exception state.

    A rewrite may simplify a disconnected ordinary region in a function that
    also contains handlers, but it must not retarget or rewrite the blocks
    that establish/receive exception control flow.  Comparing the complete
    ``BasicBlock`` value also catches accidental changes to their terminators
    and statements, not just changes to the exception metadata.
    """

    original_blocks = {block.id: block for block in original.blocks}
    rewritten_blocks = {block.id: block for block in rewritten.blocks}
    protected_ids = _exception_protected_block_ids(original)
    return all(
        rewritten_blocks.get(block_id) == original_blocks[block_id]
        for block_id in protected_ids
    )


def _block_source_offsets(block: BasicBlock) -> frozenset[int]:
    """Return direct executable source offsets carried by one basic block."""

    offsets: set[int] = set()
    for value in (*block.statements, block.terminator):
        source = getattr(value, "source", None)
        offset = getattr(source, "offset", None)
        if isinstance(offset, int):
            offsets.add(offset)
    return frozenset(offsets)


def _exception_protected_block_ids(function: FunctionIR) -> set[str]:
    """Return exception sources, handler entries, and handler-state clones.

    A handler target does not necessarily carry ``active_exception_handlers``
    itself: that metadata can begin at a successor, or be unavailable in a
    preservation CFG.  It is nevertheless the receiving endpoint of an
    exceptional transfer, so an ordinary rewrite must not consume or modify
    it.  Protect the concrete target directly rather than inferring handler
    ownership from frontend-specific metadata.
    """

    protected_ids = {
        block.id
        for block in function.blocks
        if exceptional_transfers(block) or block.active_exception_handlers
    }
    protected_ids.update(
        transfer.target
        for block in function.blocks
        for transfer in exceptional_transfers(block)
    )
    exceptional_offsets = {
        getattr(transfer.source, "offset", None)
        for block in function.blocks
        for transfer in exceptional_transfers(block)
        if getattr(transfer.source, "offset", None) is not None
    }
    if exceptional_offsets:
        protected_ids.update(
            block.id
            for block in function.blocks
            if _block_source_offsets(block) & exceptional_offsets
        )
    return protected_ids


def _exception_boundary_edges_unchanged(original: FunctionIR, rewritten: FunctionIR) -> bool:
    """Require exact concrete CFG edges at protected block boundaries."""

    protected_ids = _exception_protected_block_ids(original)
    if not protected_ids:
        return True
    original_cfg = build_cfg(original)
    rewritten_cfg = build_cfg(rewritten)
    original_boundary = {
        (edge.source, edge.target, edge.kind, edge.ordinal)
        for edge in original_cfg.edges
        if edge.source in protected_ids or edge.target in protected_ids
    }
    rewritten_boundary = {
        (edge.source, edge.target, edge.kind, edge.ordinal)
        for edge in rewritten_cfg.edges
        if edge.source in protected_ids or edge.target in protected_ids
    }
    return original_boundary == rewritten_boundary


def _metadata_was_lost(original: FunctionIR, rewritten: FunctionIR) -> bool:
    return any(
        key not in rewritten.metadata or rewritten.metadata[key] != value
        for key, value in original.metadata.items()
        if key not in {"structured_lift", "low_level_cfg_structured"}
    )


def _contains_unsupported(function: FunctionIR) -> bool:
    return any(
        isinstance(value, Unsupported)
        for block in function.blocks
        for value in _walk_values((*block.statements, block.terminator))
    )


def _semantic_literal_atoms(function: FunctionIR) -> frozenset[object]:
    """Return immutable literal facts that a CFG rewrite cannot change.

    Structuring is allowed to move expressions, materialize phis, and create
    synthetic loop carriers.  It is not allowed to change the literal values
    used by executable expressions.  Local topology proofs cover variable
    flow and evaluation order; this shared check catches accidental value
    substitution without rejecting lossless presentation changes.
    """

    atoms: list[object] = []
    for block in function.blocks:
        for statement in block.statements:
            _collect_semantic_literal_atoms(statement, atoms)
        _collect_semantic_literal_atoms(block.terminator, atoms)
    return frozenset(atoms)


def _terminal_behavior_signature(function: FunctionIR) -> frozenset[tuple[str, int | None]]:
    """Capture terminal kinds and return arity without tying a rule to names.

    Structured regions move terminators into nested statement bodies, so the
    signature walks the full IR tree.  It is intentionally weaker than a
    complete semantic proof; individual topology matchers still own data-flow
    and side-effect equivalence.
    """

    signature: list[tuple[str, int | None]] = []
    for block in function.blocks:
        values: tuple[object, ...] = block.statements
        # Structured rewrites use an empty top-level Return as a carrier when
        # all real terminal arms live inside an If/Switch.  It is not an
        # additional execution outcome and must not make an exact rewrite look
        # semantically different from the original terminal arms.
        if not (
            isinstance(block.terminator, Return)
            and not block.terminator.values
            and any(isinstance(value, (Return, Raise, Reraise)) for value in _walk_values(block.statements))
        ):
            values = (*values, block.terminator)
        for value in _walk_values(values):
            if isinstance(value, Return):
                signature.append(("return", len(value.values)))
            elif isinstance(value, Raise):
                signature.append(("raise", None))
            elif isinstance(value, Reraise):
                signature.append(("reraise", None))
    return frozenset(signature)


def _collect_semantic_literal_atoms(value: object, atoms: list[object]) -> None:
    if value is None:
        return
    if isinstance(value, Const):
        # ``null`` is used by generic structured-return carriers as an
        # initialization value; it is not an input literal whose substitution
        # would change the recovered computation.
        if value.value is not None:
            atoms.append(_semantic_value_key(value.value))
        return
    if isinstance(value, While):
        # ``while true`` is a structural carrier for a proven backedge.
        if not (isinstance(value.condition, Const) and value.condition.value is True):
            _collect_semantic_literal_atoms(value.condition, atoms)
        _collect_semantic_literal_atoms(value.body, atoms)
        return
    if is_dataclass(value):
        for field in fields(value):
            if field.name not in {"source", "type"}:
                _collect_semantic_literal_atoms(getattr(value, field.name), atoms)
        return
    if isinstance(value, tuple):
        for item in value:
            _collect_semantic_literal_atoms(item, atoms)
        return


def _semantic_value_key(value: object) -> object:
    if isinstance(value, tuple):
        return tuple(_semantic_value_key(item) for item in value)
    if isinstance(value, list):
        return tuple(_semantic_value_key(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _semantic_value_key(item)) for key, item in value.items()))
    if not is_dataclass(value):
        return value
    return (
        type(value).__name__,
        tuple(
            (field.name, _semantic_value_key(getattr(value, field.name)))
            for field in fields(value)
            if field.name not in {"source", "type"}
        ),
    )


def _nested_control_transfer_count(function: FunctionIR) -> int:
    """Count control transfers embedded in statement trees, excluding blocks."""

    control_types = (Jump, Branch, MultiBranch)
    return sum(
        isinstance(value, control_types)
        for block in function.blocks
        for statement in block.statements
        for value in _walk_values(statement)
    )


def _has_nested_low_level_cfg_transfer(function: FunctionIR) -> bool:
    """Reject a structured region which still carries an unlabelled CFG edge.

    A top-level block terminator remains representable by the preservation CFG
    renderer, which emits its block label.  A ``Jump``/``Branch``/``MultiBranch``
    nested under ``If``/``Try``/loop statements has no independently retained
    block presentation, so its target can become dangling when a matcher
    removes the original target block.  Recovery must either consume that edge
    into a proved structured construct or keep the complete low-level CFG.
    """

    control_types = (Jump, Branch, MultiBranch)
    return any(
        isinstance(value, control_types)
        for block in function.blocks
        for statement in block.statements
        for value in _walk_values(statement)
    )


def _walk_values(value: object):
    if isinstance(value, tuple):
        for item in value:
            yield from _walk_values(item)
        return
    if value is None:
        return
    yield value
    if not is_dataclass(value):
        return
    for field in fields(value):
        if field.name in {"source", "type"}:
            continue
        yield from _walk_values(getattr(value, field.name))


def _has_invalid_structured_control(function: FunctionIR) -> bool:
    return any(
        not _valid_statement_sequence(block.statements, loop_depth=0)
        for block in function.blocks
    )


def _valid_statement_sequence(
    statements: tuple[object, ...], *, loop_depth: int, switch_arm: bool = False
) -> bool:
    for index, statement in enumerate(statements):
        if isinstance(statement, (Break, Continue)) and loop_depth == 0:
            return False
        if isinstance(statement, Fallthrough):
            if not switch_arm or index != len(statements) - 1:
                return False
            continue
        if isinstance(statement, If):
            if not _valid_statement_sequence(statement.then_body, loop_depth=loop_depth):
                return False
            if not _valid_statement_sequence(statement.else_body, loop_depth=loop_depth):
                return False
        elif isinstance(statement, Switch):
            if any(
                not _valid_statement_sequence(body, loop_depth=loop_depth, switch_arm=True)
                for _value, body in statement.cases
            ):
                return False
            # ``default_body`` is always the final arm in the generic Switch
            # execution order.  A fallthrough there has no target and the
            # simulator correctly treats it as unsupported, so reject it at
            # the common rewrite boundary as well.
            if not _valid_statement_sequence(
                statement.default_body, loop_depth=loop_depth, switch_arm=False
            ):
                return False
        elif isinstance(statement, (While, DoWhile, ForEach, ForRange)):
            if not statement.body:
                return False
            if not _valid_statement_sequence(statement.body, loop_depth=loop_depth + 1):
                return False
        elif isinstance(statement, Try):
            if not _valid_statement_sequence(statement.body, loop_depth=loop_depth):
                return False
            if any(
                not _valid_statement_sequence(handler.body, loop_depth=loop_depth)
                for handler in statement.handlers
            ):
                return False
    return True
