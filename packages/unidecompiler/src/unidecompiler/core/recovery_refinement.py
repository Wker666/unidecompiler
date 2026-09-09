from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace

from unidecompiler.core.cfg import build_cfg, validate_cfg_consistency
from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    BasicBlock,
    BinaryOp,
    Const,
    DoWhile,
    Expr,
    FunctionIR,
    ForEach,
    ForRange,
    If,
    Jump,
    Phi,
    SourceRef,
    Stmt,
    Var,
    While,
    exceptional_transfers,
)


SemanticSafetyCheck = Callable[[FunctionIR], bool]

_LOW_LEVEL_RECOVERY_KINDS = {
    "generic-vm-low-level-cfg",
    "generic-vm-low-level-cfg-structured",
}


def refine_recovered_function(
    function: FunctionIR,
    *,
    is_safe: SemanticSafetyCheck,
) -> FunctionIR:
    """Refine and re-structure a recovered VM function to a fixed point.

    Every accepted refinement has a local equivalence proof and crosses the
    caller's whole-function safety check.  CFG analyses are rebuilt after
    each accepted candidate.  A repeated state or exhausted safety bound is
    treated as an internal refinement failure and returns the untouched
    preservation function rather than exposing an unverified intermediate.
    """

    if function.recovery_kind not in _LOW_LEVEL_RECOVERY_KINDS:
        return function
    if not function.blocks or not is_safe(function):
        return function

    # Validate only representation-level value invariants here.  Structured
    # regions may use logical labels, while duplicate/invalid block-entry
    # Phis are never safe to rewrite or render as if they were valid SSA.
    from unidecompiler.core.value_validation import validate_value_invariants

    value_diagnostics = tuple(
        diagnostic
        for diagnostic in validate_value_invariants(function)
        if "lacks concrete edge identity" not in diagnostic
    )
    if value_diagnostics:
        return replace(
            function,
            metadata={
                **function.metadata,
                "recovery_value_diagnostics": value_diagnostics,
            },
        )

    current = function
    needs_restructure = current.recovery_kind == "generic-vm-low-level-cfg"
    seen: set[object] = set()
    max_rounds = max(32, len(function.blocks) * 16 + _statement_count(function) * 4)

    for _round in range(max_rounds):
        fingerprint = _function_fingerprint(current)
        if fingerprint in seen:
            return _refinement_diagnostic(
                current,
                code="non_progress",
                message="recovery refinement reached a repeated function state",
                rounds=_round,
            )
        seen.add(fingerprint)

        # Establish the existing preservation-floor structure first.  Some
        # matchers intentionally consume explicit jumps or Phis as proof
        # facts; deleting those facts before the initial pass can make a
        # semantically equivalent graph less recognizable.  Refinement still
        # re-enters this same path after every later accepted cleanup.
        if needs_restructure:
            from unidecompiler.core.low_level_cfg_structuring import structure_low_level_cfg

            structured = structure_low_level_cfg(current, is_safe=is_safe)
            if structured is not None and (
                _function_fingerprint(structured) != fingerprint
                # Rejection/proof records do not alter the scheduling key,
                # but they are required diagnostics for a preserved CFG and
                # must survive this coordinator boundary.
                or structured.metadata != current.metadata
            ):
                if (
                    current.recovery_kind == "generic-vm-low-level-cfg-structured"
                    and structured.recovery_kind != "generic-vm-low-level-cfg-structured"
                ):
                    return current
                if not is_safe(structured):
                    return current
                current = structured
            needs_restructure = False
            # Materialize conservative SSA join facts before the first
            # recovery had finished, which made trivial loop Phis impossible
            # to remove and prevented them from exposing another CFG shape.
            if (
                current.recovery_kind == "generic-vm-low-level-cfg"
                and not current.metadata.get("recovery_phi_materialized")
            ):
                from unidecompiler.core.ssa import insert_phi_nodes

                materialized = insert_phi_nodes(current)
                if materialized != current:
                    materialized = replace(
                        materialized,
                        metadata={
                            **materialized.metadata,
                            "recovery_phi_materialized": True,
                        },
                    )
                if (
                    not _refinement_is_valid(current, materialized)
                    or not is_safe(materialized)
                ):
                    return current
                current = materialized
            # A structurer may legitimately have no matching rule.  That is
            # not a reason to abandon expression/SSA cleanup: refinement is
            # also defined on the preservation-floor CFG itself.  The next
            # phase therefore runs even when ``structured`` is None or is an
            # unchanged function.

        # Keep pass scheduling in the shared core manager so fixed-point
        # budgets and non-progress diagnostics use one deterministic policy.
        from unidecompiler.core.pass_manager import PassSpec, run_passes

        pass_result = run_passes(
            current,
            (PassSpec("recovery-refinement", _next_refinement),),
            fingerprint=_function_fingerprint,
        )
        refined = pass_result.function if pass_result.changed else None
        if pass_result.diagnostics:
            current = _refinement_diagnostic(
                current,
                code=pass_result.diagnostics[0].code,
                message=pass_result.diagnostics[0].message,
                rounds=_round,
            )
        if refined is not None:
            if not _refinement_is_valid(current, refined) or not is_safe(refined):
                # A rejected atomic candidate must not prevent a later CFG
                # structurer from seeing the original preservation graph.
                refined = None
            else:
                current = refined
                needs_restructure = True
                continue
        if not needs_restructure:
            return current

    return _refinement_diagnostic(
        current,
        code="budget_exhausted",
        message="recovery refinement reached its fixed-point budget",
        rounds=max_rounds,
    )


def _refinement_diagnostic(
    function: FunctionIR,
    *,
    code: str,
    message: str,
    rounds: int,
) -> FunctionIR:
    """Attach an analyzable fixed-point stop reason to the preservation IR."""

    existing = tuple(function.metadata.get("recovery_refinement_diagnostics", ()))
    context = tuple(function.metadata.get("unsupported_context", ()))
    rows = tuple(function.metadata.get("bytecode_instructions", ()))
    if rows and not context:
        context = tuple(
            f"offset={row.get('offset')!r}; opcode={row.get('opcode')}; "
            f"operands={[item.get('text') for item in row.get('operands', ())]}"
            for row in rows[-8:]
            if isinstance(row, dict)
        )
    diagnostic = {
        "code": code,
        "message": message,
        "rounds": rounds,
        "context": context,
    }
    if diagnostic in existing:
        return function
    return replace(
        function,
        metadata={
            **function.metadata,
            "recovery_refinement_diagnostics": (*existing, diagnostic),
        },
    )


def _next_refinement(function: FunctionIR) -> FunctionIR | None:
    """Return one deterministic, locally proven refinement candidate."""

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    available_in, available_out = _definitely_available_names(function)
    defined_anywhere = frozenset().union(*available_out.values()) if available_out else frozenset()

    # Expression refinement runs before statement deletion so
    # ``x = phi(...x...)`` becomes ``x = x`` in one round and is removed in
    # the next.  This makes every state transition small and auditable.
    for index, block in enumerate(function.blocks):
        predecessors = frozenset(cfg.predecessors(block.id))
        rewritten = _rewrite_first_block_expression(
            block,
            predecessors=predecessors,
            edge_ambiguous=len(cfg.incoming_edges(block.id)) != len(predecessors),
            available_out=available_out,
            defined_anywhere=defined_anywhere,
        )
        if rewritten is not None:
            return _replace_block(
                function,
                index,
                rewritten,
            )

    # VM string builders are often lowered into a straight-line sequence such
    # as ``s = ''; s = s + 'ver'; s = s + 'sio'``.  Track only constants
    # defined earlier in the same block and fold that proven string expression
    # to one literal.  We deliberately do not propagate across CFG edges or
    # through unknown assignments/calls.
    for block_index, block in enumerate(function.blocks):
        known_strings: dict[str, str] = {}
        string_run_target: str | None = None
        string_run_start: int | None = None
        for statement_index, statement in enumerate(block.statements):
            if not isinstance(statement, Assign) or not isinstance(statement.target, Var):
                string_run_target = None
                string_run_start = None
                known_strings.clear()
                continue
            target_name = statement.target.name
            if string_run_target != target_name:
                known_strings.clear()
                string_run_target = target_name
                string_run_start = (
                    statement_index
                    if isinstance(statement.value, Const)
                    and isinstance(statement.value.value, str)
                    else None
                )
            value = _constant_string_value(statement.value, known_strings)
            if value is not None and not isinstance(statement.value, Const):
                rewritten = replace(
                    statement,
                    value=Const(
                        value=value,
                        source=statement.value.source,
                        type=statement.value.type,
                    ),
                )
                start = string_run_start
                if start is not None:
                    return _replace_block(
                        function,
                        block_index,
                        replace(
                            block,
                            statements=(
                                *block.statements[:start],
                                rewritten,
                                *block.statements[statement_index + 1 :],
                            ),
                        ),
                    )
                return _replace_block(
                    function,
                    block_index,
                    replace(
                        block,
                        statements=(
                            *block.statements[:statement_index],
                            rewritten,
                            *block.statements[statement_index + 1 :],
                        ),
                    ),
                )
            if isinstance(statement.value, Const) and isinstance(
                statement.value.value, str
            ):
                known_strings[statement.target.name] = statement.value.value
            else:
                known_strings.pop(statement.target.name, None)
                string_run_target = None
                string_run_start = None

    # Delete only a side-effect-free identity assignment whose read is
    # definitely valid at that exact program point.  The walk includes
    # already-structured nested bodies; it never crosses an expression or
    # changes branch/loop control.
    for block_index, block in enumerate(function.blocks):
        statements, changed = _remove_first_identity(
            block.statements,
            available=set(available_in.get(block.id, frozenset())),
        )
        if changed:
            return _replace_block(
                function,
                block_index,
                replace(block, statements=statements),
            )

    # An explicit jump to the physically next block is exactly the existing
    # implicit fallthrough edge.  On the low-level preservation floor we only
    # remove it when the successor has one concrete predecessor.  That extra
    # guard keeps normalization from changing which shared join a later
    # structurer sees; the structured path already has its own topology proof.
    # In an exceptional CFG, retain empty routing blocks as the preservation
    # floor; a non-empty block's jump is still a proven no-op, and removing it
    # does not touch its exceptional edge.
    exceptional_context = _has_exceptional_context(function)
    allow_fallthrough_cleanup = (
        function.recovery_kind == "generic-vm-low-level-cfg"
        or (
            function.recovery_kind == "generic-vm-low-level-cfg-structured"
            and not exceptional_context
        )
    )
    if allow_fallthrough_cleanup:
        for index, block in enumerate(function.blocks[:-1]):
            if (
                (index > 0 or function.recovery_kind != "generic-vm-low-level-cfg")
                and
                (not exceptional_context or block.statements)
                and isinstance(block.terminator, Jump)
                and block.terminator.target == function.blocks[index + 1].id
                and (
                    function.recovery_kind != "generic-vm-low-level-cfg"
                    or len(cfg.predecessors(function.blocks[index + 1].id)) == 1
                )
            ):
                provenance = function.control_provenance
                if block.terminator.source is not None:
                    provenance = tuple(
                        dict.fromkeys((*provenance, block.terminator.source))
                    )
                return replace(
                    _replace_block(
                        function,
                        index,
                        replace(block, terminator=None),
                    ),
                    control_provenance=provenance,
                )

    return None


def _rewrite_first_block_expression(
    block: BasicBlock,
    *,
    predecessors: frozenset[str],
    edge_ambiguous: bool,
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
    nested_control: bool = False,
) -> BasicBlock | None:
    for index, statement in enumerate(block.statements):
        rewritten, changed = _rewrite_first_expression(
            statement,
            predecessors=predecessors,
            edge_ambiguous=edge_ambiguous,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
            nested_control=nested_control,
        )
        if changed:
            return replace(
                block,
                statements=(
                    *block.statements[:index],
                    rewritten,
                    *block.statements[index + 1 :],
                ),
            )
    if block.terminator is None:
        return None
    terminator, changed = _rewrite_first_expression(
        block.terminator,
        predecessors=predecessors,
        edge_ambiguous=edge_ambiguous,
        available_out=available_out,
        defined_anywhere=defined_anywhere,
        nested_control=nested_control,
    )
    if not changed:
        return None
    return replace(block, terminator=terminator)


def _rewrite_first_expression(
    value: object,
    *,
    predecessors: frozenset[str],
    edge_ambiguous: bool,
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
    nested_control: bool = False,
) -> tuple[object, bool]:
    if isinstance(value, Phi):
        replacement = _identical_phi_value(
            value,
            predecessors=predecessors,
            edge_ambiguous=edge_ambiguous,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
            allow_partial=nested_control,
        )
        if replacement is not None:
            return replacement, True

    if isinstance(value, tuple):
        for index, item in enumerate(value):
            rewritten, changed = _rewrite_first_expression(
                item,
                predecessors=predecessors,
                edge_ambiguous=edge_ambiguous,
                available_out=available_out,
                defined_anywhere=defined_anywhere,
                nested_control=nested_control,
            )
            if changed:
                return (*value[:index], rewritten, *value[index + 1 :]), True
        return value, False

    if not is_dataclass(value) or isinstance(value, SourceRef):
        return value, False

    for field in fields(value):
        if field.name in {"source", "type"}:
            continue
        field_value = getattr(value, field.name)
        child_nested_control = nested_control or isinstance(
            value, (If, While, DoWhile, ForEach, ForRange)
        )
        rewritten, changed = _rewrite_first_expression(
            field_value,
            predecessors=predecessors,
            edge_ambiguous=edge_ambiguous,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
            nested_control=child_nested_control,
        )
        if changed:
            return replace(value, **{field.name: rewritten}), True
    return value, False


def _identical_phi_value(
    phi: Phi,
    *,
    predecessors: frozenset[str],
    edge_ambiguous: bool,
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
    allow_partial: bool = False,
) -> Expr | None:
    if not phi.incoming:
        return None
    # A block-id keyed Phi cannot represent which value belongs to each of
    # several parallel edges from the same predecessor.  Keep it intact until
    # an edge-aware Phi representation is available instead of guessing.
    if edge_ambiguous:
        return None

    by_predecessor: dict[str, Expr] = {}
    for predecessor, incoming in phi.incoming:
        previous = by_predecessor.get(predecessor)
        if previous is not None and not _pure_values_equal(previous, incoming):
            return None
        by_predecessor[predecessor] = incoming
    incoming_predecessors = frozenset(by_predecessor)
    if predecessors:
        if incoming_predecessors != predecessors and not (
            allow_partial
            and len(incoming_predecessors) == 1
            and incoming_predecessors.issubset(predecessors)
        ):
            return None
    elif len(incoming_predecessors) != 1:
        return None

    values = tuple(by_predecessor.values())
    first = values[0]
    if not isinstance(first, (Var, Const)):
        return None
    if not all(_pure_values_equal(first, value) for value in values[1:]):
        return None
    if isinstance(first, Var):
        if len(set(by_predecessor)) == 1:
            # A duplicate predecessor is already a single incoming edge.  If
            # it names a CFG block, require the value to be available on that
            # edge; otherwise retain the conservative fallback for structured
            # nested expressions whose provenance id is not a top-level block.
            predecessor = next(iter(by_predecessor))
            if predecessor in available_out:
                if first.name not in available_out[predecessor]:
                    return None
            elif first.name not in defined_anywhere:
                return None
        elif any(
            first.name not in available_out.get(predecessor, frozenset())
            for predecessor in predecessors
        ):
            return None
    return first


def _pure_values_equal(left: Expr, right: Expr) -> bool:
    if type(left) is not type(right) or left.type != right.type:
        return False
    if isinstance(left, Var) and isinstance(right, Var):
        return left.name == right.name
    if isinstance(left, Const) and isinstance(right, Const):
        return type(left.value) is type(right.value) and left.value == right.value
    return False


def _constant_string_value(value: Expr, known_strings: dict[str, str]) -> str | None:
    if isinstance(value, Const):
        return value.value if isinstance(value.value, str) else None
    if isinstance(value, Var):
        return known_strings.get(value.name)
    if isinstance(value, BinaryOp) and value.op == "+":
        left = _constant_string_value(value.left, known_strings)
        right = _constant_string_value(value.right, known_strings)
        if left is not None and right is not None:
            return left + right
    return None


def _definitely_available_names(
    function: FunctionIR,
) -> tuple[dict[str, frozenset[str]], dict[str, frozenset[str]]]:
    cfg = build_cfg(function)
    block_defs = {
        block.id: frozenset(
            name
            for statement in block.statements
            for name in _assigned_names(statement)
        )
        for block in function.blocks
    }
    entry = cfg.entry
    universe = frozenset(function.params).union(*block_defs.values())
    available_in = {
        block.id: (frozenset(function.params) if block.id == entry else universe)
        for block in function.blocks
    }
    available_out = {
        block.id: available_in[block.id] | block_defs[block.id]
        for block in function.blocks
    }

    changed = True
    while changed:
        changed = False
        for block in function.blocks:
            if block.id == entry:
                incoming = frozenset(function.params)
            else:
                predecessors = cfg.predecessors(block.id)
                incoming = (
                    frozenset.intersection(
                        *(available_out[predecessor] for predecessor in predecessors)
                    )
                    if predecessors
                    else frozenset()
                )
            outgoing = incoming | block_defs[block.id]
            if incoming != available_in[block.id] or outgoing != available_out[block.id]:
                available_in[block.id] = incoming
                available_out[block.id] = outgoing
                changed = True
    return available_in, available_out


def _assigned_names(statement: Stmt) -> frozenset[str]:
    if isinstance(statement, Assign) and isinstance(statement.target, Var):
        return frozenset({statement.target.name})
    if isinstance(statement, AssignMany):
        return frozenset(target.name for target in statement.targets)
    return frozenset()


def _remove_first_identity(
    statements: tuple[Stmt, ...],
    *,
    available: set[str],
) -> tuple[tuple[Stmt, ...], bool]:
    """Remove one proven identity assignment from a statement tree."""

    for index, statement in enumerate(statements):
        if (
            isinstance(statement, Assign)
            and isinstance(statement.target, Var)
            and isinstance(statement.value, Var)
            and statement.target.name == statement.value.name
            and statement.target.name in available
        ):
            return (
                (*statements[:index], *statements[index + 1 :]),
                True,
            )

        nested = _nested_statement_sequences(statement)
        for field_name, body in nested:
            rewritten, changed = _remove_first_identity(
                body,
                available=set(available),
            )
            if changed:
                return (
                    (
                        *statements[:index],
                        replace(statement, **{field_name: rewritten}),
                        *statements[index + 1 :],
                    ),
                    True,
                )
        available.update(_assigned_names(statement))
    return statements, False


def _nested_statement_sequences(
    statement: Stmt,
) -> tuple[tuple[str, tuple[Stmt, ...]], ...]:
    if isinstance(statement, If):
        return (("then_body", statement.then_body), ("else_body", statement.else_body))
    if isinstance(statement, (While, DoWhile, ForEach, ForRange)):
        return (("body", statement.body),)
    return ()


def _replace_block(
    function: FunctionIR,
    index: int,
    block: BasicBlock,
) -> FunctionIR:
    return replace(
        function,
        blocks=(*function.blocks[:index], block, *function.blocks[index + 1 :]),
    )


def _refinement_is_valid(original: FunctionIR, rewritten: FunctionIR) -> bool:
    if (
        rewritten.name != original.name
        or rewritten.params != original.params
        or rewritten.nested_functions != original.nested_functions
        or rewritten.source != original.source
        or rewritten.recovery_kind != original.recovery_kind
        or rewritten.bytecode_control_flow != original.bytecode_control_flow
    ):
        return False
    if any(
        key not in rewritten.metadata or rewritten.metadata[key] != value
        for key, value in original.metadata.items()
    ):
        return False
    if not set(original.control_provenance).issubset(rewritten.control_provenance):
        return False
    if tuple(block.id for block in rewritten.blocks) != tuple(
        block.id for block in original.blocks
    ):
        return False
    if any(
        exceptional_transfers(left) != exceptional_transfers(right)
        or left.active_exception_handlers != right.active_exception_handlers
        for left, right in zip(original.blocks, rewritten.blocks)
    ):
        return False
    if _contains_empty_control_body(rewritten):
        return False

    from unidecompiler.core.value_validation import validate_value_invariants

    if any(
        "lacks concrete edge identity" not in diagnostic
        for diagnostic in validate_value_invariants(rewritten)
    ):
        return False

    original_cfg = build_cfg(original)
    rewritten_cfg = build_cfg(rewritten)
    if (
        original_cfg.diagnostics
        or rewritten_cfg.diagnostics
        or validate_cfg_consistency(original_cfg)
        or validate_cfg_consistency(rewritten_cfg)
    ):
        return False
    if original_cfg.block_ids != rewritten_cfg.block_ids:
        return False
    return _semantic_edges(original_cfg) == _semantic_edges(rewritten_cfg)


def _semantic_edges(cfg) -> tuple[tuple[str, str, str, int, object], ...]:
    return tuple(
        sorted(
            (
                edge.source,
                edge.target,
                "unconditional" if edge.kind in {"jump", "fallthrough"} else edge.kind,
                edge.ordinal,
                edge.provenance,
            )
            for edge in cfg.edges
        )
    )


def _has_exceptional_context(function: FunctionIR) -> bool:
    return any(
        exceptional_transfers(block) or block.active_exception_handlers
        for block in function.blocks
    )


def _contains_empty_control_body(function: FunctionIR) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, (While, DoWhile, ForEach, ForRange)) and not value.body:
            return True
        if isinstance(value, If) and not value.then_body and not value.else_body:
            return True
        if isinstance(value, tuple):
            return any(visit(item) for item in value)
        if not is_dataclass(value) or isinstance(value, SourceRef):
            return False
        return any(
            visit(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"source", "type"}
        )

    return any(visit(block.statements) for block in function.blocks)


def _function_fingerprint(function: FunctionIR) -> object:
    # Audit metadata (proofs, matcher attempts, bytecode context, and
    # diagnostics) is intentionally append-only and can become large on a
    # handler-heavy CFG.  It does not affect any recovery decision, so
    # serializing it into every fixed-point key both wastes time and can turn
    # a finite refinement into quadratic/exponential ``repr`` work.  Include
    # only the small flags that alter pass scheduling.
    metadata = function.metadata
    scheduling_metadata = tuple(
        sorted(
            (key, repr(metadata[key]))
            for key in ("recovery_phi_materialized", "low_level_cfg_structured")
            if key in metadata
        )
    )
    return (
        function.recovery_kind,
        function.blocks,
        function.control_provenance,
        function.bytecode_control_flow,
        scheduling_metadata,
    )


def _statement_count(function: FunctionIR) -> int:
    return sum(len(block.statements) + (block.terminator is not None) for block in function.blocks)
