from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass, replace
from typing import Literal

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import (
    BasicBlock,
    Branch,
    Break,
    Const,
    Continue,
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
)


CFGRewriteProof = Literal["lossless-normalization", "exact-topology"]


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


@dataclass(frozen=True)
class CFGRewriteDecision:
    accepted: bool
    function: FunctionIR
    reasons: tuple[str, ...] = ()


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
    if _has_exceptional_context(original) or _has_exceptional_context(rewritten):
        reasons.append("ordinary CFG rewriting cannot own exceptional context")

    original_cfg = build_cfg(original)
    rewritten_cfg = build_cfg(rewritten)
    if original_cfg.diagnostics:
        reasons.append("original CFG has unresolved targets")
    if rewritten_cfg.diagnostics:
        reasons.append("rewritten CFG has unresolved targets")
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
        return CFGRewriteDecision(False, original, tuple(dict.fromkeys(reasons)))
    return CFGRewriteDecision(True, rewritten)


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


def _has_exceptional_context(function: FunctionIR) -> bool:
    return any(
        block.exception_edge is not None or block.active_exception_handlers
        for block in function.blocks
    )


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


def _valid_statement_sequence(statements: tuple[object, ...], *, loop_depth: int) -> bool:
    for statement in statements:
        if isinstance(statement, (Break, Continue)) and loop_depth == 0:
            return False
        if isinstance(statement, If):
            if not _valid_statement_sequence(statement.then_body, loop_depth=loop_depth):
                return False
            if not _valid_statement_sequence(statement.else_body, loop_depth=loop_depth):
                return False
        elif isinstance(statement, Switch):
            if any(
                not _valid_statement_sequence(body, loop_depth=loop_depth)
                for _value, body in statement.cases
            ):
                return False
            if not _valid_statement_sequence(statement.default_body, loop_depth=loop_depth):
                return False
        elif isinstance(statement, (While, ForEach, ForRange)):
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
