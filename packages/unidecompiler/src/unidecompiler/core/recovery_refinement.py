from __future__ import annotations

from collections.abc import Callable
from dataclasses import fields, is_dataclass, replace

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    BasicBlock,
    Const,
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

    original = function
    current = function
    needs_restructure = current.recovery_kind == "generic-vm-low-level-cfg"
    seen: set[object] = set()
    max_rounds = max(32, len(function.blocks) * 16 + _statement_count(function) * 4)

    for _round in range(max_rounds):
        fingerprint = _function_fingerprint(current)
        if fingerprint in seen:
            return original
        seen.add(fingerprint)

        # Establish the existing preservation-floor structure first.  Some
        # matchers intentionally consume explicit jumps or Phis as proof
        # facts; deleting those facts before the initial pass can make a
        # semantically equivalent graph less recognizable.  Refinement still
        # re-enters this same path after every later accepted cleanup.
        if needs_restructure:
            from unidecompiler.core.low_level_cfg_structuring import structure_low_level_cfg

            structured = structure_low_level_cfg(current, is_safe=is_safe)
            if structured is None or _function_fingerprint(structured) == fingerprint:
                return current
            if (
                current.recovery_kind == "generic-vm-low-level-cfg-structured"
                and structured.recovery_kind != "generic-vm-low-level-cfg-structured"
            ):
                return current
            if not is_safe(structured):
                return current
            current = structured
            needs_restructure = False
            continue

        refined = _next_refinement(current)
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

    return original


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
            available_out=available_out,
            defined_anywhere=defined_anywhere,
        )
        if rewritten is not None:
            return _replace_block(
                function,
                index,
                rewritten,
            )

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
    # implicit fallthrough edge.  Exceptional contexts remain untouched until
    # a dedicated exceptional-CFG proof owns them.
    if (
        function.recovery_kind == "generic-vm-low-level-cfg-structured"
        and not _has_exceptional_context(function)
    ):
        for index, block in enumerate(function.blocks[:-1]):
            if (
                isinstance(block.terminator, Jump)
                and block.terminator.target == function.blocks[index + 1].id
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
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
) -> BasicBlock | None:
    for index, statement in enumerate(block.statements):
        rewritten, changed = _rewrite_first_expression(
            statement,
            predecessors=predecessors,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
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
        available_out=available_out,
        defined_anywhere=defined_anywhere,
    )
    if not changed:
        return None
    return replace(block, terminator=terminator)


def _rewrite_first_expression(
    value: object,
    *,
    predecessors: frozenset[str],
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
) -> tuple[object, bool]:
    if isinstance(value, Phi):
        replacement = _identical_phi_value(
            value,
            predecessors=predecessors,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
        )
        if replacement is not None:
            return replacement, True

    if isinstance(value, tuple):
        for index, item in enumerate(value):
            rewritten, changed = _rewrite_first_expression(
                item,
                predecessors=predecessors,
                available_out=available_out,
                defined_anywhere=defined_anywhere,
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
        rewritten, changed = _rewrite_first_expression(
            field_value,
            predecessors=predecessors,
            available_out=available_out,
            defined_anywhere=defined_anywhere,
        )
        if changed:
            return replace(value, **{field.name: rewritten}), True
    return value, False


def _identical_phi_value(
    phi: Phi,
    *,
    predecessors: frozenset[str],
    available_out: dict[str, frozenset[str]],
    defined_anywhere: frozenset[str],
) -> Expr | None:
    if not phi.incoming:
        return None

    by_predecessor: dict[str, Expr] = {}
    for predecessor, incoming in phi.incoming:
        previous = by_predecessor.get(predecessor)
        if previous is not None and not _pure_values_equal(previous, incoming):
            return None
        by_predecessor[predecessor] = incoming
    incoming_predecessors = frozenset(by_predecessor)
    if predecessors:
        if incoming_predecessors != predecessors:
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
            # A duplicate predecessor is already a single incoming edge.  The
            # generic IR has no edge-sensitive variable object, so require
            # only that the name is proven to be defined somewhere in this
            # function; this covers structured nested expressions whose
            # predecessor id is not a top-level CFG block.
            if first.name not in defined_anywhere:
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
    if isinstance(statement, (While, ForEach, ForRange)):
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
        left.exception_edge != right.exception_edge
        or left.active_exception_handlers != right.active_exception_handlers
        for left, right in zip(original.blocks, rewritten.blocks)
    ):
        return False
    if _contains_empty_control_body(rewritten):
        return False

    original_cfg = build_cfg(original)
    rewritten_cfg = build_cfg(rewritten)
    if original_cfg.diagnostics or rewritten_cfg.diagnostics:
        return False
    return _semantic_edges(original_cfg) == _semantic_edges(rewritten_cfg)


def _semantic_edges(cfg) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted(
            (
                edge.source,
                edge.target,
                "unconditional" if edge.kind in {"jump", "fallthrough"} else edge.kind,
            )
            for edge in cfg.edges
        )
    )


def _has_exceptional_context(function: FunctionIR) -> bool:
    return any(
        block.exception_edge is not None or block.active_exception_handlers
        for block in function.blocks
    )


def _contains_empty_control_body(function: FunctionIR) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, (While, ForEach, ForRange)) and not value.body:
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
    return (
        function.recovery_kind,
        function.blocks,
        function.control_provenance,
        function.bytecode_control_flow,
        tuple(sorted((str(key), repr(value)) for key, value in function.metadata.items())),
    )


def _statement_count(function: FunctionIR) -> int:
    return sum(len(block.statements) + (block.terminator is not None) for block in function.blocks)
