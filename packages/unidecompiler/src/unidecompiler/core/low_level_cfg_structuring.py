from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from collections.abc import Callable
from types import SimpleNamespace

from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    BasicBlock,
    BinaryOp,
    Branch,
    Break,
    Const,
    CapturedVar,
    Continue,
    ExceptHandler,
    Expr,
    ExprStmt,
    ForEach,
    ForRange,
    FunctionIR,
    GetItem,
    Global,
    If,
    Jump,
    MultiBranch,
    Phi,
    Raise,
    Reraise,
    Return,
    Stmt,
    StoreAttr,
    StoreItem,
    Switch,
    Terminator,
    UnaryOp,
    Try,
    Var,
    While,
    Yield,
)
from unidecompiler.core.structuring import (
    StructuredWhile,
    structure_function,
)
from unidecompiler.core.cfg import (
    build_cfg,
    compute_immediate_postdominators,
    find_natural_loops,
)
from unidecompiler.core.cfg_rewrite import CFGRewriteCandidate, validate_cfg_rewrite
from unidecompiler.core.region import RegionGraph


LowLevelCfgStructurer = Callable[[FunctionIR], FunctionIR | None]

_LOW_LEVEL_CFG_STRUCTURERS: list[LowLevelCfgStructurer] = []
_LOW_LEVEL_CFG_NORMALIZERS: tuple[LowLevelCfgStructurer, ...] = ()


def register_low_level_cfg_structurer(
    structurer: LowLevelCfgStructurer,
    *,
    prepend: bool = False,
) -> LowLevelCfgStructurer:
    if structurer in _LOW_LEVEL_CFG_STRUCTURERS:
        return structurer
    if prepend:
        _LOW_LEVEL_CFG_STRUCTURERS.insert(0, structurer)
    else:
        _LOW_LEVEL_CFG_STRUCTURERS.append(structurer)
    return structurer


def unregister_low_level_cfg_structurer(structurer: LowLevelCfgStructurer) -> None:
    if structurer in _LOW_LEVEL_CFG_STRUCTURERS:
        _LOW_LEVEL_CFG_STRUCTURERS.remove(structurer)


def low_level_cfg_structurers() -> tuple[LowLevelCfgStructurer, ...]:
    return tuple(_LOW_LEVEL_CFG_STRUCTURERS)


def apply_low_level_cfg_structuring(
    function: FunctionIR,
    *,
    is_safe: Callable[[FunctionIR], bool] | None = None,
) -> FunctionIR:
    if function.recovery_kind != "generic-vm-low-level-cfg":
        return function
    if is_safe is None:
        return function
    from unidecompiler.core.recovery_refinement import refine_recovered_function

    return refine_recovered_function(function, is_safe=is_safe)


def structure_low_level_cfg(
    function: FunctionIR,
    *,
    is_safe: Callable[[FunctionIR], bool] | None = None,
) -> FunctionIR | None:
    """Run exact structurers after any lossless CFG normalization.

    Empty-jump threading and linear-block merging keep the low-level recovery
    kind, so they are preparatory rewrites rather than a completed structure.
    Restarting the registry after either one lets a subsequent exact matcher
    see its normalized topology. All actual structurers still return exactly
    one verified replacement, preserving the registry's deterministic policy.
    """

    if _has_exceptional_block_context(function):
        return None
    current = function
    normalized = False
    while True:
        for structurer in low_level_cfg_structurers():
            structured = structurer(current)
            if structured is None:
                continue
            if current.recovery_kind in {
                "generic-vm-low-level-cfg",
                "generic-vm-low-level-cfg-structured",
            }:
                proof = (
                    "lossless-normalization"
                    if structurer in _LOW_LEVEL_CFG_NORMALIZERS
                    else "exact-topology"
                )
                decision = validate_cfg_rewrite(
                    CFGRewriteCandidate(
                        original=current,
                        rewritten=structured,
                        rule=structured.metadata.get(
                            "low_level_cfg_structured", structurer.__name__
                        ),
                        proof=proof,
                    )
                )
                if not decision.accepted:
                    continue
                structured = decision.function
            else:
                # Keep the historical internal extension seam for callers
                # that pass a non-VM FunctionIR.  VM preservation views above
                # always cross the fail-closed rewrite gate, including later
                # iterations after the first structured result.
                structured = replace(
                    structured,
                    control_provenance=tuple(
                        dict.fromkeys(
                            (*current.control_provenance, *structured.control_provenance)
                        )
                    ),
                    bytecode_control_flow=tuple(
                        dict.fromkeys(
                            (*current.bytecode_control_flow, *structured.bytecode_control_flow)
                        )
                    ),
                )
            if is_safe is not None and not is_safe(structured):
                continue
            if structurer in _LOW_LEVEL_CFG_NORMALIZERS:
                current = structured
                normalized = True
                break
            return structured
        else:
            return current if normalized else None


def _has_exceptional_block_context(function: FunctionIR) -> bool:
    """Keep exceptional edges and active re-raise scopes on their exact CFG floor."""

    return any(
        block.exception_edge is not None or block.active_exception_handlers
        for block in function.blocks
    )


def _structure_exact_single_block_cfg(function: FunctionIR) -> FunctionIR | None:
    """Remove the CFG presentation floor when no control-flow graph remains."""

    if function.recovery_kind != "generic-vm-low-level-cfg" or len(function.blocks) != 1:
        return None
    cfg = build_cfg(function)
    if cfg.diagnostics or cfg.edges or _has_exceptional_block_context(function):
        return None
    return replace(
        function,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-single-block-cfg",
        },
    )


def _rewrite_while_structured_function(
    function: FunctionIR,
    statements: tuple[Stmt, ...],
    terminator: Terminator | None,
    rule_name: str,
) -> FunctionIR:
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=function.blocks[0].id, statements=statements, terminator=terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": rule_name,
        },
    )


def _structure_exact_empty_jump_chains(function: FunctionIR) -> FunctionIR | None:
    """Thread empty jump blocks without changing the CFG's observable behavior."""

    if not function.blocks:
        return None
    cfg = build_cfg(function)
    if cfg.diagnostics:
        return None
    block_map = {block.id: block for block in function.blocks}
    loop_blocks = frozenset(block_id for loop in find_natural_loops(cfg) for block_id in loop.blocks)
    # Phi incoming labels identify edges, not merely presentation names. An
    # empty predecessor can only be removed after its phi copies have been
    # materialized, so leave such blocks intact for a later exact structurer.
    phi_predecessors = {
        predecessor
        for block in function.blocks
        for statement in block.statements
        if isinstance(statement, Assign) and isinstance(statement.value, Phi)
        for predecessor, _value in statement.value.incoming
        if predecessor != "existing"
    }
    redirects: dict[str, str] = {}
    for index, block in enumerate(function.blocks):
        if index == 0:
            continue
        if (
            block.id in loop_blocks
            or block.id in phi_predecessors
            or block.statements
            or not isinstance(block.terminator, Jump)
        ):
            continue
        target = block.terminator.target
        seen = {block.id}
        while target in block_map:
            candidate = block_map[target]
            if candidate.id in loop_blocks or candidate.statements or not isinstance(candidate.terminator, Jump):
                break
            if target in seen:
                return None
            seen.add(target)
            target = candidate.terminator.target
        redirects[block.id] = target
    has_direct_degenerate_branch = any(
        isinstance(block.terminator, Branch)
        and block.terminator.true_target == block.terminator.false_target
        for block in function.blocks
    )
    if not redirects and not has_direct_degenerate_branch:
        return None

    def redirect(target: str) -> str:
        seen: set[str] = set()
        while target in redirects:
            if target in seen:
                return target
            seen.add(target)
            target = redirects[target]
        return target

    def rewrite_terminator(terminator: Terminator | None) -> Terminator | None:
        if isinstance(terminator, Jump):
            return Jump(source=terminator.source, target=redirect(terminator.target))
        if isinstance(terminator, Branch):
            return Branch(
                source=terminator.source,
                condition=terminator.condition,
                true_target=redirect(terminator.true_target),
                false_target=redirect(terminator.false_target),
            )
        if isinstance(terminator, MultiBranch):
            return MultiBranch(
                source=terminator.source,
                selector=terminator.selector,
                cases=tuple((value, redirect(target)) for value, target in terminator.cases),
                default_target=redirect(terminator.default_target),
            )
        return terminator

    def rewrite_block(block: BasicBlock) -> BasicBlock:
        terminator = rewrite_terminator(block.terminator)
        statements = block.statements
        if isinstance(terminator, Branch) and terminator.true_target == terminator.false_target:
            # A degenerate branch still evaluates its condition. Keep that
            # evaluation explicit before threading the common edge, including
            # any VM-visible exception or call effects in the expression.
            statements = (*statements, ExprStmt(source=terminator.condition.source, value=terminator.condition))
            terminator = Jump(source=terminator.source, target=terminator.true_target)
        return BasicBlock(id=block.id, statements=statements, terminator=terminator)

    kept = tuple(
        rewrite_block(block)
        for block in function.blocks
        if block.id not in redirects
    )
    if not kept:
        return None
    # The first block defines the FunctionIR entry.  An empty entry jump can
    # be removed only when its target is already the next retained block;
    # otherwise deleting it would silently change which block executes first
    # (notably for post-tested loops whose target is later in the tuple).
    if (
        kept[0].id == function.blocks[0].id
        and kept[0].statements == ()
        and isinstance(kept[0].terminator, Jump)
        and len(kept) > 1
        and kept[1].id == redirect(kept[0].terminator.target)
    ):
        kept = kept[1:]
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=kept,
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind=function.recovery_kind,
        metadata={
            **function.metadata,
            "low_level_cfg_structured": "exact-empty-jump-chains",
        },
    )


def _structure_exact_empty_jump_splices(function: FunctionIR) -> FunctionIR | None:
    """Remove one empty jump block while preserving every incoming edge.

    Empty blocks occur both as ordinary fall-through placeholders and inside
    natural loops.  The older jump-chain normalizer intentionally leaves loop
    members alone because a blind redirect can invalidate edge-local Phi
    values.  This rule handles the missing generic case: it expands an empty
    block's incoming edge labels at its successor, and declines the rewrite if
    any Phi or exceptional/irreducible edge cannot be expanded exactly.
    """

    if not function.blocks:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    block_map = cfg.blocks
    loop_headers = frozenset(loop.header for loop in find_natural_loops(cfg))

    for empty in function.blocks:
        if (
            empty.id == cfg.entry
            or empty.statements
            or not isinstance(empty.terminator, Jump)
            or empty.terminator.target == empty.id
            or empty.exception_edge is not None
            or empty.active_exception_handlers
        ):
            continue
        successor_id = empty.terminator.target
        # A block that is the last hop into a natural-loop header carries the
        # backedge topology used by later loop proofs.  Redirecting it can
        # merge distinct backedges (or turn an unmatched loop into a simple
        # one), changing which structured rule is applicable.  Keep that
        # boundary explicit; ordinary in-loop jump placeholders remain safe to
        # splice below.
        if successor_id in loop_headers:
            continue
        successor = block_map.get(successor_id)
        if successor is None:
            continue
        predecessors = cfg.predecessors(empty.id)
        if not predecessors:
            continue
        incoming_edges = tuple(
            edge
            for edge in region_graph.edges
            if edge.target == empty.id and edge.source in predecessors
        )
        if len(incoming_edges) != len(predecessors) or any(
            edge.kind == "fallthrough"
            or edge.roles & {"exceptional", "irreducible"}
            for edge in incoming_edges
        ):
            continue
        if any(
            block_map.get(predecessor) is None
            or block_map[predecessor].exception_edge is not None
            or block_map[predecessor].active_exception_handlers
            for predecessor in predecessors
        ):
            continue
        outgoing_edges = tuple(
            edge
            for edge in region_graph.edges
            if edge.source == empty.id and edge.target == successor_id
        )
        if any(edge.roles & {"exceptional", "irreducible"} for edge in outgoing_edges):
            continue

        # A malformed/stale Phi edge elsewhere cannot be repaired by removing
        # this block.  The successor is handled below, one replacement edge at
        # a time, with collision checks in _rename_phi_predecessor.
        if any(
            isinstance(statement, Assign)
            and isinstance(statement.value, Phi)
            and any(predecessor == empty.id for predecessor, _value in statement.value.incoming)
            for block in function.blocks
            if block.id != successor_id
            for statement in block.statements
        ):
            continue

        rewritten_successor = _expand_phi_predecessor(
            successor,
            previous=empty.id,
            replacements=predecessors,
        )
        if rewritten_successor is None:
            continue

        rewritten_blocks: list[BasicBlock] = []
        for block in function.blocks:
            if block.id == empty.id:
                continue
            if block.id == successor_id:
                rewritten_blocks.append(rewritten_successor)
                continue
            rewritten_blocks.append(
                replace(
                    block,
                    terminator=_retarget_terminator_target(
                        block.terminator,
                        previous=empty.id,
                        replacement=successor_id,
                    ),
                )
            )

        rewritten = replace(
            function,
            blocks=tuple(rewritten_blocks),
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-empty-jump-splice",
            },
        )
        if build_cfg(rewritten).diagnostics:
            continue
        return rewritten
    return None


def _structure_exact_loop_linear_block_merges(function: FunctionIR) -> FunctionIR | None:
    """Merge a unique-predecessor linear block inside a natural loop.

    Linear merging is already available for acyclic regions, but loop members
    were deliberately excluded because header Phi edges and backedges are
    observable CFG facts.  This companion rule handles only a successor that
    is not a loop header, has one explicit predecessor, and has a concrete
    terminator.  Any Phi at its outgoing edge is renamed to the surviving
    predecessor; a collision rejects the candidate instead of guessing.
    """

    if not function.blocks:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if not loops:
        return None
    loop_headers = frozenset(loop.header for loop in loops)
    block_loops: dict[str, frozenset[str]] = {
        block_id: frozenset(
            loop.header for loop in loops if block_id in loop.blocks
        )
        for block_id in cfg.blocks
    }
    block_map = cfg.blocks

    for source in function.blocks:
        if not isinstance(source.terminator, Jump):
            continue
        successor_id = source.terminator.target
        successor = block_map.get(successor_id)
        if successor is None or successor_id in loop_headers:
            continue
        if cfg.predecessors(successor_id) != (source.id,):
            continue
        if not block_loops.get(source.id) & block_loops.get(successor_id, frozenset()):
            continue
        if successor.terminator is None:
            # An implicit fallthrough depends on physical block order.  Keep
            # that edge explicit rather than moving it across the source block.
            continue
        if (
            source.exception_edge is not None
            or source.active_exception_handlers
            or successor.exception_edge is not None
            or successor.active_exception_handlers
            or _contains_terminal_statement(successor.statements)
            or _contains_unscoped_loop_control(successor.statements)
        ):
            continue
        successor_statements = _statements_without_trivial_phi_assignments(
            successor.statements,
            incoming_blocks=(source.id,),
        )
        if successor_statements is None:
            continue

        outgoing_targets = set(_terminator_targets(successor.terminator))
        if any(
            isinstance(statement, Assign)
            and isinstance(statement.value, Phi)
            and any(predecessor == successor.id for predecessor, _value in statement.value.incoming)
            for block in function.blocks
            if block.id not in outgoing_targets
            for statement in block.statements
        ):
            # A Phi mentioning the removed block must belong to one of its
            # actual outgoing successors.  Reject malformed/stale edge facts
            # instead of silently leaving an unresolvable predecessor label.
            continue
        rewritten_outgoing: dict[str, BasicBlock] = {}
        valid = True
        for target_id in outgoing_targets:
            target = block_map.get(target_id)
            if target is None:
                valid = False
                break
            renamed = _rename_phi_predecessor(
                target,
                previous=successor.id,
                replacement=source.id,
            )
            if renamed is None:
                valid = False
                break
            rewritten_outgoing[target_id] = renamed
        if not valid:
            continue

        rewritten_blocks: list[BasicBlock] = []
        for block in function.blocks:
            if block.id == successor.id:
                continue
            if block.id in rewritten_outgoing:
                rewritten_blocks.append(rewritten_outgoing[block.id])
                continue
            if block.id == source.id:
                rewritten_blocks.append(
                    replace(
                        block,
                        statements=(*block.statements, *successor_statements),
                        terminator=successor.terminator,
                    )
                )
                continue
            rewritten_blocks.append(block)

        rewritten = replace(
            function,
            blocks=tuple(rewritten_blocks),
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-loop-linear-block-merge",
            },
        )
        if build_cfg(rewritten).diagnostics:
            continue
        return rewritten
    return None


def _structure_exact_linear_block_merges(function: FunctionIR) -> FunctionIR | None:
    """Merge only a straight-line block pair with a unique CFG predecessor."""

    current = function
    changed = False
    while True:
        cfg = build_cfg(current)
        if cfg.diagnostics:
            return None if not changed else current
        loop_blocks = frozenset(block_id for loop in find_natural_loops(cfg) for block_id in loop.blocks)
        block_map = {block.id: block for block in current.blocks}
        merge_pair: tuple[str, str] | None = None
        for block in current.blocks:
            if block.id in loop_blocks or not isinstance(block.terminator, Jump):
                continue
            target = block.terminator.target
            successor = block_map.get(target)
            if successor is None or successor.id in loop_blocks:
                continue
            if cfg.predecessors(successor.id) != (block.id,):
                continue
            # The target's implicit fallthrough depends on its physical
            # position.  Removing it can redirect the surviving source to a
            # different tuple neighbour.  The sole safe exception is a final
            # block: it has no physical successor, so merging it cannot create
            # a new edge.
            if successor.terminator is None and successor.id != current.blocks[-1].id:
                continue
            incoming = (block.id,)
            merged_statements = _statements_without_trivial_phi_assignments(
                successor.statements,
                incoming_blocks=incoming,
            )
            if merged_statements is None:
                continue
            merge_pair = (block.id, successor.id)
            break
        if merge_pair is None:
            return None if not changed else current
        source_id, target_id = merge_pair
        source = block_map[source_id]
        target = block_map[target_id]
        merged_phi_free = _statements_without_trivial_phi_assignments(
            target.statements,
            incoming_blocks=(source.id,),
        )
        if merged_phi_free is None:
            return None if not changed else current

        # Removing ``target`` changes the predecessor identity seen by every
        # successor of that block.  Rewrite those edge-labelled Phi inputs
        # before committing the merge; otherwise a later analysis could read
        # a value from a block that no longer exists.  Reject collisions and
        # stale Phi labels conservatively instead of dropping data-flow facts.
        outgoing_targets = set(_terminator_targets(target.terminator))
        if any(
            isinstance(statement, Assign)
            and isinstance(statement.value, Phi)
            and any(predecessor == target.id for predecessor, _value in statement.value.incoming)
            for block in current.blocks
            if block.id not in outgoing_targets
            for statement in block.statements
        ):
            return None if not changed else current
        rewritten_outgoing: dict[str, BasicBlock] = {}
        for successor_id in outgoing_targets:
            successor = block_map.get(successor_id)
            if successor is None:
                return None if not changed else current
            rewritten_successor = _rename_phi_predecessor(
                successor,
                previous=target.id,
                replacement=source.id,
            )
            if rewritten_successor is None:
                return None if not changed else current
            rewritten_outgoing[successor_id] = rewritten_successor
        current = FunctionIR(
            name=current.name,
            params=current.params,
            blocks=tuple(
                BasicBlock(
                    id=source.id,
                    statements=(*source.statements, *merged_phi_free),
                    terminator=target.terminator,
                )
                if block.id == source.id
                else rewritten_outgoing[block.id]
                if block.id in rewritten_outgoing
                else block
                for block in current.blocks
                if block.id != target.id
            ),
            nested_functions=current.nested_functions,
            source=current.source,
            recovery_kind=current.recovery_kind,
            metadata={
                **current.metadata,
                "low_level_cfg_structured": "exact-linear-block-merges",
            },
        )
        changed = True


def _structure_exact_two_level_loop_with_guarded_continue(function: FunctionIR) -> FunctionIR | None:
    """Recover two nested natural loops with a guarded outer continue.

    The matcher is deliberately topology-first. It only removes trivial phi
    assignments and empty jump blocks after proving both loop exits and the
    continue paths. No source-language or frontend names are consulted.
    """

    if len(function.blocks) != 12:
        return None
    (
        setup,
        outer_header,
        outer_body,
        outer_continue,
        inner_setup,
        inner_header,
        inner_body,
        inner_push,
        inner_continue,
        inner_exit,
        outer_exit,
        final_return,
    ) = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != outer_header.id:
        return None
    if not isinstance(outer_header.terminator, Branch):
        return None
    if outer_header.terminator.true_target != outer_body.id or outer_header.terminator.false_target != outer_exit.id:
        return None
    if not isinstance(outer_body.terminator, Branch):
        return None
    if outer_body.terminator.true_target != outer_continue.id or outer_body.terminator.false_target != inner_setup.id:
        return None
    if outer_continue.statements or not isinstance(outer_continue.terminator, Jump):
        return None
    if outer_continue.terminator.target != outer_header.id:
        return None
    if not isinstance(inner_setup.terminator, Jump) or inner_setup.terminator.target != inner_header.id:
        return None
    if not isinstance(inner_header.terminator, Branch):
        return None
    if inner_header.terminator.true_target != inner_body.id or inner_header.terminator.false_target != inner_exit.id:
        return None
    if not isinstance(inner_body.terminator, Branch):
        return None
    if inner_body.terminator.true_target != inner_push.id or inner_body.terminator.false_target != inner_continue.id:
        return None
    if not isinstance(inner_push.terminator, Jump) or inner_push.terminator.target != inner_continue.id:
        return None
    if not isinstance(inner_continue.terminator, Jump) or inner_continue.terminator.target != inner_header.id:
        return None
    if inner_exit.statements or not isinstance(inner_exit.terminator, Jump):
        return None
    if inner_exit.terminator.target != outer_header.id:
        return None
    if outer_exit.statements or not isinstance(outer_exit.terminator, Jump):
        return None
    if outer_exit.terminator.target != final_return.id:
        return None
    if final_return.statements or not isinstance(final_return.terminator, Return):
        return None

    outer_body_statements = _statements_without_trivial_phi_assignments(
        outer_body.statements,
        incoming_blocks=(outer_header.id,),
    )
    inner_setup_statements = _statements_without_trivial_phi_assignments(
        inner_setup.statements,
        incoming_blocks=(outer_body.id,),
    )
    inner_body_statements = _statements_without_trivial_phi_assignments(
        inner_body.statements,
        incoming_blocks=(inner_header.id,),
    )
    if outer_body_statements is None or inner_setup_statements is None or inner_body_statements is None:
        return None

    inner_loop = While(
        condition=inner_header.terminator.condition,
        body=(
            *inner_body_statements,
            If(condition=inner_body.terminator.condition, then_body=inner_push.statements),
        ),
    )
    outer_loop = While(
        condition=outer_header.terminator.condition,
        body=(
            *outer_body_statements,
            If(
                condition=outer_body.terminator.condition,
                then_body=(Continue(),),
                else_body=(*inner_setup_statements, inner_loop),
            ),
        ),
    )
    return _rewrite_while_structured_function(
        function,
        (*setup.statements, outer_loop, *outer_exit.statements),
        final_return.terminator,
        "exact-two-level-loop-with-guarded-continue",
    )


def _structure_exact_branch_selected_dual_loops(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 12:
        return None
    (
        entry,
        base_setup,
        base_header,
        base_body,
        base_return,
        recursive_setup,
        recursive_header,
        recursive_body,
        candidate_better,
        recursive_join,
        recursive_latch,
        recursive_return,
    ) = function.blocks
    if not isinstance(entry.terminator, Branch):
        return None
    if (entry.terminator.true_target, entry.terminator.false_target) != (base_setup.id, recursive_setup.id):
        return None
    if not isinstance(base_setup.terminator, Jump) or base_setup.terminator.target != base_header.id:
        return None
    if not isinstance(base_header.terminator, Branch) or (
        base_header.terminator.true_target,
        base_header.terminator.false_target,
    ) != (base_body.id, base_return.id):
        return None
    if not isinstance(base_body.terminator, Jump) or base_body.terminator.target != base_header.id:
        return None
    if base_return.statements or not isinstance(base_return.terminator, Return):
        return None
    if not isinstance(recursive_setup.terminator, Jump) or recursive_setup.terminator.target != recursive_header.id:
        return None
    if not isinstance(recursive_header.terminator, Branch) or (
        recursive_header.terminator.true_target,
        recursive_header.terminator.false_target,
    ) != (recursive_body.id, recursive_return.id):
        return None
    if not isinstance(recursive_body.terminator, Branch) or (
        recursive_body.terminator.true_target,
        recursive_body.terminator.false_target,
    ) != (candidate_better.id, recursive_join.id):
        return None
    if not isinstance(candidate_better.terminator, Jump) or candidate_better.terminator.target != recursive_join.id:
        return None
    if not isinstance(recursive_join.terminator, Branch) or (
        recursive_join.terminator.true_target,
        recursive_join.terminator.false_target,
    ) != (recursive_return.id, recursive_latch.id):
        return None
    if not isinstance(recursive_latch.terminator, Jump) or recursive_latch.terminator.target != recursive_header.id:
        return None
    if recursive_return.statements or not isinstance(recursive_return.terminator, Return):
        return None

    base_header_body = _statements_without_trivial_phi_assignments(
        base_header.statements,
        incoming_blocks=(base_setup.id, base_body.id),
    )
    recursive_header_body = _statements_without_trivial_phi_assignments(
        recursive_header.statements,
        incoming_blocks=(recursive_setup.id, recursive_latch.id),
    )
    recursive_join_body = _statements_without_trivial_phi_assignments(
        recursive_join.statements,
        incoming_blocks=(recursive_body.id, candidate_better.id),
    )
    recursive_return_body = _statements_without_trivial_phi_assignments(
        recursive_return.statements,
        incoming_blocks=(recursive_body.id, recursive_header.id),
    )
    if any(body is None for body in (base_header_body, recursive_header_body, recursive_join_body, recursive_return_body)):
        return None
    if len(base_return.terminator.values) != 1 or len(recursive_return.terminator.values) != 1:
        return None

    return_name = _fresh_var_name(function, "structured_return_value")
    base_loop = While(
        condition=base_header.terminator.condition,
        body=(*base_header_body, *base_body.statements),
    )
    recursive_loop = While(
        condition=recursive_header.terminator.condition,
        body=(
            *recursive_header_body,
            *recursive_body.statements,
            If(condition=recursive_body.terminator.condition, then_body=candidate_better.statements),
            *recursive_join_body,
            If(condition=recursive_join.terminator.condition, then_body=(Break(),)),
            *recursive_latch.statements,
        ),
    )
    statements = (
        Assign(target=Var(name=return_name), value=Const(value=None)),
        If(
            condition=entry.terminator.condition,
            then_body=(*base_setup.statements, base_loop, Assign(target=Var(name=return_name), value=base_return.terminator.values[0])),
            else_body=(*recursive_setup.statements, recursive_loop, Assign(target=Var(name=return_name), value=recursive_return.terminator.values[0])),
        ),
    )
    return _rewrite_while_structured_function(
        function,
        statements,
        Return(source=recursive_return.terminator.source, values=(Var(name=return_name),)),
        "exact-branch-selected-dual-loops",
    )


def _structure_exact_three_phase_nested_loops(function: FunctionIR) -> FunctionIR | None:
    """Recover a selection, update, and aggregation nested-loop CFG."""

    if len(function.blocks) == 24:
        (
            setup, outer_header, select_setup, select_header, select_seen,
            select_existing, select_compare, select_take, select_join,
            selected_test, selected_finite, selected_unreachable, relax_setup,
            relax_header, relax_body, relax_compare, relax_take, relax_join,
            outer_latch, checksum_setup, checksum_header, checksum_body,
            checksum_exit, final_return,
        ) = function.blocks
    elif len(function.blocks) == 22:
        (
            setup, outer_header, select_setup, select_header, select_seen,
            select_existing, select_compare, select_take, select_join,
            selected_test, selected_finite, relax_setup, relax_header,
            relax_body, relax_compare, relax_take, relax_join, outer_latch,
            checksum_setup, checksum_header, checksum_body, final_return,
        ) = function.blocks
        selected_unreachable = None
        checksum_exit = None
    else:
        return None

    if not isinstance(setup.terminator, Jump) or setup.terminator.target != outer_header.id:
        return None
    if not isinstance(outer_header.terminator, Branch):
        return None
    if (outer_header.terminator.true_target, outer_header.terminator.false_target) != (select_setup.id, checksum_setup.id):
        return None
    if not isinstance(select_setup.terminator, Jump) or select_setup.terminator.target != select_header.id:
        return None
    if not isinstance(select_header.terminator, Branch) or (
        select_header.terminator.true_target,
        select_header.terminator.false_target,
    ) != (select_seen.id, selected_test.id):
        return None
    if not isinstance(select_seen.terminator, Branch) or (
        select_seen.terminator.true_target,
        select_seen.terminator.false_target,
    ) != (select_existing.id, select_join.id):
        return None
    if not isinstance(select_existing.terminator, Branch) or (
        select_existing.terminator.true_target,
        select_existing.terminator.false_target,
    ) != (select_compare.id, select_take.id):
        return None
    if not isinstance(select_compare.terminator, Branch) or (
        select_compare.terminator.true_target,
        select_compare.terminator.false_target,
    ) != (select_take.id, select_join.id):
        return None
    if not isinstance(select_take.terminator, Jump) or select_take.terminator.target != select_join.id:
        return None
    if not isinstance(select_join.terminator, Jump) or select_join.terminator.target != select_header.id:
        return None
    if not isinstance(selected_test.terminator, Branch) or (
        selected_test.terminator.true_target,
        selected_test.terminator.false_target,
    ) != (selected_finite.id, checksum_setup.id):
        return None
    if not isinstance(selected_finite.terminator, Branch) or (
        selected_finite.terminator.true_target,
        selected_finite.terminator.false_target,
    ) != (selected_unreachable.id if selected_unreachable is not None else checksum_setup.id, relax_setup.id):
        return None
    if selected_unreachable is not None and (
        selected_unreachable.statements
        or not isinstance(selected_unreachable.terminator, Jump)
        or selected_unreachable.terminator.target != checksum_setup.id
    ):
        return None
    if not isinstance(relax_setup.terminator, Jump) or relax_setup.terminator.target != relax_header.id:
        return None
    if not isinstance(relax_header.terminator, Branch) or (
        relax_header.terminator.true_target,
        relax_header.terminator.false_target,
    ) != (relax_body.id, outer_latch.id):
        return None
    if not isinstance(relax_body.terminator, Branch) or (
        relax_body.terminator.true_target,
        relax_body.terminator.false_target,
    ) != (relax_compare.id, relax_join.id):
        return None
    if not isinstance(relax_compare.terminator, Branch) or (
        relax_compare.terminator.true_target,
        relax_compare.terminator.false_target,
    ) != (relax_take.id, relax_join.id):
        return None
    if not isinstance(relax_take.terminator, Jump) or relax_take.terminator.target != relax_join.id:
        return None
    if not isinstance(relax_join.terminator, Jump) or relax_join.terminator.target != relax_header.id:
        return None
    if not isinstance(outer_latch.terminator, Jump) or outer_latch.terminator.target != outer_header.id:
        return None
    if not isinstance(checksum_setup.terminator, Jump) or checksum_setup.terminator.target != checksum_header.id:
        return None
    if not isinstance(checksum_header.terminator, Branch) or (
        checksum_header.terminator.true_target,
        checksum_header.terminator.false_target,
    ) != (checksum_body.id, checksum_exit.id if checksum_exit is not None else final_return.id):
        return None
    if not isinstance(checksum_body.terminator, Jump) or checksum_body.terminator.target != checksum_header.id:
        return None
    if checksum_exit is not None and (
        not isinstance(checksum_exit.terminator, Jump)
        or checksum_exit.terminator.target != final_return.id
    ):
        return None
    if final_return.statements or not isinstance(final_return.terminator, Return):
        return None

    def without_phi(block: BasicBlock, incoming: tuple[str, ...]) -> tuple[Stmt, ...] | None:
        return _statements_without_trivial_phi_assignments(block.statements, incoming_blocks=incoming)

    select_header_body = without_phi(select_header, (select_setup.id, select_join.id))
    select_join_body = without_phi(select_join, (select_seen.id, select_existing.id, select_compare.id, select_take.id))
    relax_header_body = without_phi(relax_header, (relax_setup.id, relax_join.id))
    relax_join_body = without_phi(relax_join, (relax_body.id, relax_compare.id, relax_take.id))
    checksum_header_body = without_phi(checksum_header, (checksum_setup.id, checksum_body.id))
    if any(body is None for body in (select_header_body, select_join_body, relax_header_body, relax_join_body, checksum_header_body)):
        return None

    select_loop = While(
        condition=select_header.terminator.condition,
        body=(
            *select_header_body,
            If(
                condition=select_seen.terminator.condition,
                then_body=(
                    If(
                        condition=select_existing.terminator.condition,
                        then_body=(If(condition=select_compare.terminator.condition, then_body=select_take.statements),),
                        else_body=select_take.statements,
                    ),
                ),
            ),
            *select_join_body,
        ),
    )
    relax_loop = While(
        condition=relax_header.terminator.condition,
        body=(
            *relax_header_body,
            *relax_body.statements,
            If(condition=relax_body.terminator.condition, then_body=(If(condition=relax_compare.terminator.condition, then_body=relax_take.statements),)),
            *relax_join_body,
        ),
    )
    outer_loop = While(
        condition=outer_header.terminator.condition,
        body=(
            *select_setup.statements,
            select_loop,
            If(
                condition=selected_test.terminator.condition,
                then_body=(
                    If(
                        condition=selected_finite.terminator.condition,
                        then_body=(Break(),),
                        else_body=(*relax_setup.statements, relax_loop),
                    ),
                ),
                else_body=(Break(),),
            ),
            *outer_latch.statements,
        ),
    )
    checksum_loop = While(
        condition=checksum_header.terminator.condition,
        body=(*checksum_header_body, *checksum_body.statements),
    )
    return _rewrite_while_structured_function(
        function,
        (
            *setup.statements,
            outer_loop,
            *checksum_setup.statements,
            checksum_loop,
            *(checksum_exit.statements if checksum_exit is not None else ()),
        ),
        final_return.terminator,
        "exact-three-phase-nested-loops",
    )


def _structure_exact_whole_function_while(function: FunctionIR) -> FunctionIR | None:
    if not _simple_while_header_has_no_statements(function):
        return None
    structured = structure_function(function)
    if len(structured.nodes) != 1:
        return None
    node = structured.nodes[0]
    if not isinstance(node, StructuredWhile):
        return None
    if _terminator_targets_any_original_block(node.exit_block.terminator, function):
        return None
    statements = (
        *node.setup.statements,
        While(condition=node.condition, body=node.body.statements),
        *node.exit_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=node.setup.id, statements=statements, terminator=node.exit_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-whole-function-while",
        },
    )


def _structure_exact_header_effect_while(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 4:
        return None
    setup, header, body, exit_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != header.id:
        return None
    if not header.statements:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target != body.id or header.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != header.id:
        return None
    if _terminator_targets_any_original_block(exit_block.terminator, function):
        return None
    header_statements = _statements_without_identity_phi_assignments(header.statements)
    if header_statements is None:
        return None

    loop_body = (
        *header_statements,
        If(
            condition=header.terminator.condition,
            then_body=body.statements,
            else_body=(Break(),),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=exit_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-header-effect-while",
        },
    )


def _structure_exact_header_effect_while_with_exit_jump(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 5:
        return None
    setup, body, header, exit_block, final_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != header.id:
        return None
    if not header.statements:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target != body.id or header.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != header.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None
    header_statements = _statements_without_identity_phi_assignments(header.statements)
    if header_statements is None:
        return None

    loop_body = (
        *header_statements,
        If(
            condition=header.terminator.condition,
            then_body=body.statements,
            else_body=(Break(),),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-header-effect-while-with-exit-jump",
        },
    )


def _structure_exact_branch_assign_return(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 5:
        return None
    entry, then_block, else_block, join_block, final_block = function.blocks
    if not isinstance(entry.terminator, Branch):
        return None
    if entry.terminator.true_target != then_block.id or entry.terminator.false_target != else_block.id:
        return None
    if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != join_block.id:
        return None
    if not isinstance(else_block.terminator, Jump) or else_block.terminator.target != join_block.id:
        return None
    if join_block.statements or not isinstance(join_block.terminator, Jump):
        return None
    if join_block.terminator.target != final_block.id:
        return None
    if final_block.statements or not isinstance(final_block.terminator, Return):
        return None

    statements = (
        *entry.statements,
        If(
            condition=entry.terminator.condition,
            then_body=then_block.statements,
            else_body=else_block.statements,
        ),
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=entry.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-branch-assign-return",
        },
    )


def _structure_exact_early_return_pretested_loop(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 7:
        return None
    entry, early_return, setup, body, latch, exit_block, final_block = function.blocks
    if entry.statements or not isinstance(entry.terminator, Branch):
        return None
    if entry.terminator.true_target != early_return.id or entry.terminator.false_target != setup.id:
        return None
    early_value = _single_return_value(early_return.terminator)
    if early_return.statements or early_value is None:
        return None
    if not isinstance(setup.terminator, Branch):
        return None
    if setup.terminator.true_target != body.id or setup.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Branch):
        return None
    if body.terminator.true_target != latch.id or body.terminator.false_target != exit_block.id:
        return None
    if not _expr_semantically_equal(setup.terminator.condition, body.terminator.condition):
        return None
    if latch.statements or not isinstance(latch.terminator, Jump) or latch.terminator.target != body.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    final_value = _single_return_value(final_block.terminator)
    if final_block.statements or final_value is None:
        return None

    body_statements = _statements_without_trivial_phi_assignments(
        body.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    exit_statements = _statements_without_trivial_phi_assignments(
        exit_block.statements,
        incoming_blocks=(setup.id, body.id),
    )
    if body_statements is None or exit_statements is None:
        return None

    return_name = _fresh_var_name(function, "structured_return_value")
    statements = (
        Assign(target=Var(name=return_name), value=Const(value=None)),
        If(
            condition=entry.terminator.condition,
            then_body=(
                Assign(target=Var(name=return_name), value=early_value),
            ),
            else_body=(
                *setup.statements,
                While(condition=setup.terminator.condition, body=body_statements),
                *exit_statements,
                Assign(target=Var(name=return_name), value=final_value),
            ),
        ),
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=entry.id,
                statements=statements,
                terminator=Return(source=final_block.terminator.source, values=(Var(name=return_name),)),
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-early-return-pretested-loop",
        },
    )


def _structure_exact_guard_cascade_pretested_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover guard exits followed by one single-entry pretested loop.

    This is a control-flow-only reduction: each guard's taken edge executes a
    single arm and joins the sole final return, while its other edge proceeds
    to the next guard or to the loop setup.  Restricting the matcher to the
    complete function and to trivial phi nodes makes moving the tail after the
    nested guards exact.
    """

    if len(function.blocks) < 7:
        return None
    block_map = {block.id: block for block in function.blocks}
    final_block = function.blocks[-1]
    if final_block.statements or not isinstance(final_block.terminator, Return):
        return None

    guard_conditions: list[tuple[BasicBlock, tuple[Stmt, ...], tuple[Stmt, ...]]] = []
    current = function.blocks[0]
    expected_index = 0
    previous_guard_id: str | None = None
    while isinstance(current.terminator, Branch):
        if function.blocks[expected_index].id != current.id:
            return None
        arm = block_map.get(current.terminator.true_target)
        continuation = block_map.get(current.terminator.false_target)
        if arm is None or continuation is None:
            return None
        if not isinstance(arm.terminator, Jump) or arm.terminator.target != final_block.id:
            return None
        if not arm.statements or arm.id == final_block.id:
            return None
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(current.id,),
        )
        condition_statements = _statements_without_trivial_phi_assignments(
            current.statements,
            incoming_blocks=() if previous_guard_id is None else (previous_guard_id,),
        )
        if arm_statements is None or condition_statements is None:
            return None
        guard_conditions.append((current, condition_statements, arm_statements))
        previous_guard_id = current.id
        expected_index += 2
        if expected_index >= len(function.blocks) or function.blocks[expected_index].id != continuation.id:
            return None
        current = continuation

    if not guard_conditions or expected_index + 5 != len(function.blocks):
        return None
    setup, header, body, tail, final = function.blocks[expected_index:]
    if final.id != final_block.id:
        return None
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != header.id:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target != body.id or header.terminator.false_target != tail.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != header.id:
        return None
    if not isinstance(tail.terminator, Jump) or tail.terminator.target != final.id:
        return None

    setup_statements = _statements_without_trivial_phi_assignments(
        setup.statements,
        incoming_blocks=(guard_conditions[-1][0].id,),
    )
    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=(setup.id, body.id),
    )
    body_statements = _statements_without_trivial_phi_assignments(
        body.statements,
        incoming_blocks=(header.id,),
    )
    tail_statements = _statements_without_trivial_phi_assignments(
        tail.statements,
        incoming_blocks=(header.id,),
    )
    if any(statements is None for statements in (setup_statements, header_statements, body_statements, tail_statements)):
        return None

    continuation_statements: tuple[Stmt, ...] = (
        *setup_statements,
        While(condition=header.terminator.condition, body=(*header_statements, *body_statements)),
        *tail_statements,
    )
    for guard, condition_statements, arm_statements in reversed(guard_conditions):
        continuation_statements = (
            *condition_statements,
            If(
                condition=guard.terminator.condition,
                then_body=arm_statements,
                else_body=continuation_statements,
            ),
        )
    return _rewrite_while_structured_function(
        function,
        continuation_statements,
        final.terminator,
        "exact-guard-cascade-pretested-loop",
    )


def _structure_exact_try_success_return(function: FunctionIR) -> FunctionIR | None:
    """Remove a try body's sole success jump to a terminal continuation."""

    if len(function.blocks) == 2:
        protected, final = function.blocks
        prelude = None
    elif len(function.blocks) == 3:
        prelude, protected, final = function.blocks
        if not isinstance(prelude.terminator, Jump) or prelude.terminator.target != protected.id:
            return None
    else:
        return None
    if protected.terminator is not None or len(protected.statements) != 1:
        return None
    try_statement = protected.statements[0]
    if not isinstance(try_statement, Try) or not try_statement.body:
        return None
    success_jump = try_statement.body[-1]
    if not isinstance(success_jump, Jump) or success_jump.target != final.id:
        return None
    # The terminal block can disappear only when no handler still transfers to
    # it.  A common-join try is handled by the dedicated normalizer below.
    if any(
        handler.body
        and isinstance(handler.body[-1], Jump)
        and handler.body[-1].target == final.id
        for handler in try_statement.handlers
    ):
        return None
    terminal_statements: tuple[Stmt, ...]
    terminal_terminator: Terminator | None
    if isinstance(final.terminator, Return):
        terminal_statements = final.statements
        terminal_terminator = final.terminator
    elif final.terminator is None and final.statements and isinstance(final.statements[-1], (Raise, Reraise)):
        terminal_statements = final.statements
        terminal_terminator = None
    else:
        return None
    rewritten_try = Try(
        source=try_statement.source,
        body=tuple(try_statement.body[:-1]),
        handlers=try_statement.handlers,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=prelude.id if prelude is not None else protected.id,
                statements=(
                    *prelude.statements,
                    rewritten_try,
                    *terminal_statements,
                )
                if prelude is not None
                else (rewritten_try, *terminal_statements),
                terminator=terminal_terminator,
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-try-success-return",
        },
    )


def _structure_exact_try_common_join_region(function: FunctionIR) -> FunctionIR | None:
    """Hoist a common post-try jump out of all protected paths.

    A recovered ``Try`` can retain low-level jumps in its normal body and in
    every handler solely to reconverge after the protected region.  When all
    of those jumps name the same target, replacing them with one terminator on
    the enclosing block preserves the selected handler, side-effect order,
    and continuation exactly.  This is intentionally independent of any VM's
    exception encoding.
    """

    block_map = {block.id: block for block in function.blocks}
    for protected in function.blocks:
        if protected.terminator is not None or len(protected.statements) != 1:
            continue
        try_statement = protected.statements[0]
        if not isinstance(try_statement, Try) or not try_statement.body or not try_statement.handlers:
            continue
        success_jump = try_statement.body[-1]
        if not isinstance(success_jump, Jump) or success_jump.target not in block_map:
            continue
        target = success_jump.target
        rewritten_handlers: list[ExceptHandler] = []
        for handler in try_statement.handlers:
            if not handler.body or not isinstance(handler.body[-1], Jump) or handler.body[-1].target != target:
                break
            rewritten_handlers.append(
                ExceptHandler(
                    exception_type=handler.exception_type,
                    binding=handler.binding,
                    body=tuple(handler.body[:-1]),
                )
            )
        else:
            rewritten_try = Try(
                source=try_statement.source,
                body=tuple(try_statement.body[:-1]),
                handlers=tuple(rewritten_handlers),
            )
            rewritten_protected = BasicBlock(
                id=protected.id,
                statements=(rewritten_try,),
                terminator=Jump(source=success_jump.source, target=target),
            )
            return FunctionIR(
                name=function.name,
                params=function.params,
                blocks=tuple(
                    rewritten_protected if block.id == protected.id else block
                    for block in function.blocks
                ),
                nested_functions=function.nested_functions,
                source=function.source,
                recovery_kind=function.recovery_kind,
                metadata={
                    **function.metadata,
                    "low_level_cfg_structured": "exact-try-common-join-region",
                },
            )
    return None


def _structure_exact_branch_terminal_arms(function: FunctionIR) -> FunctionIR | None:
    """Recover a complete two-arm branch whose arms both end the function."""

    if len(function.blocks) != 3:
        return None
    entry, first_arm, second_arm = function.blocks
    if not isinstance(entry.terminator, Branch):
        return None
    block_map = {block.id: block for block in function.blocks}
    true_arm = block_map.get(entry.terminator.true_target)
    false_arm = block_map.get(entry.terminator.false_target)
    if true_arm is None or false_arm is None or true_arm.id == false_arm.id:
        return None

    def terminal_body(block: BasicBlock, predecessor: str) -> tuple[Stmt | Return, ...] | None:
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor,),
        )
        if statements is None:
            return None
        if isinstance(block.terminator, Return):
            return (*statements, block.terminator)
        if block.terminator is None and statements and isinstance(statements[-1], (Raise, Reraise)):
            return statements
        return None

    true_body = terminal_body(true_arm, entry.id)
    false_body = terminal_body(false_arm, entry.id)
    if true_body is None or false_body is None:
        return None
    entry_statements = _statements_without_trivial_phi_assignments(entry.statements, incoming_blocks=())
    if entry_statements is None:
        return None
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=entry.id,
                statements=(
                    *entry_statements,
                    If(
                        condition=entry.terminator.condition,
                        then_body=true_body,
                        else_body=false_body,
                    ),
                ),
                terminator=Return(),
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-branch-terminal-arms",
        },
    )


def _structure_exact_branch_join_return(function: FunctionIR) -> FunctionIR | None:
    """Recover a two-arm diamond that reconverges at one return block."""

    if len(function.blocks) != 4:
        return None
    entry, _first_arm, _second_arm, final = function.blocks
    if not isinstance(entry.terminator, Branch) or not isinstance(final.terminator, Return):
        return None
    block_map = {block.id: block for block in function.blocks}
    true_arm = block_map.get(entry.terminator.true_target)
    false_arm = block_map.get(entry.terminator.false_target)
    if true_arm is None or false_arm is None or true_arm.id == false_arm.id:
        return None
    if not isinstance(true_arm.terminator, Jump) or true_arm.terminator.target != final.id:
        return None
    if not isinstance(false_arm.terminator, Jump) or false_arm.terminator.target != final.id:
        return None
    entry_statements = _statements_without_trivial_phi_assignments(entry.statements, incoming_blocks=())
    true_statements = _statements_without_trivial_phi_assignments(true_arm.statements, incoming_blocks=(entry.id,))
    false_statements = _statements_without_trivial_phi_assignments(false_arm.statements, incoming_blocks=(entry.id,))
    final_statements = _statements_without_trivial_phi_assignments(
        final.statements,
        incoming_blocks=(true_arm.id, false_arm.id),
    )
    if any(statements is None for statements in (entry_statements, true_statements, false_statements, final_statements)):
        return None
    return _rewrite_while_structured_function(
        function,
        (
            *entry_statements,
            If(condition=entry.terminator.condition, then_body=true_statements, else_body=false_statements),
            *final_statements,
        ),
        final.terminator,
        "exact-branch-join-return",
    )


def _structure_exact_optional_phi_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a diamond where one branch enters the join directly.

    The source shape is ``branch -> join`` on one edge and
    ``branch -> single-entry arm -> join`` on the other.  This is the
    VM-neutral optional-arm form of Ghidra's if collapse.  The join's Phi
    copies are placed on the corresponding mutually-exclusive edge, and every
    other incoming edge or non-linear continuation rejects the rewrite.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    loop_headers = frozenset(loop.header for loop in loops)
    for source in function.blocks:
        if not isinstance(source.terminator, Branch):
            continue
        true_block = block_map.get(source.terminator.true_target)
        false_block = block_map.get(source.terminator.false_target)
        if true_block is None or false_block is None or true_block.id == false_block.id:
            continue
        if source.id in loop_headers or any(source.id in loop.blocks for loop in loops):
            continue

        if true_block.id == false_block.id:
            continue
        direct_is_true: bool | None = None
        if true_block.terminator is not None and isinstance(true_block.terminator, Jump):
            if true_block.terminator.target == false_block.id and cfg.predecessors(true_block.id) == (source.id,):
                arm = true_block
                join = false_block
                direct_is_true = False
            else:
                arm = None
                join = None
        else:
            arm = None
            join = None
        if arm is None:
            if (
                isinstance(false_block.terminator, Jump)
                and false_block.terminator.target == true_block.id
                and cfg.predecessors(false_block.id) == (source.id,)
            ):
                arm = false_block
                join = true_block
                direct_is_true = True
            else:
                continue
        if arm is None or join is None or direct_is_true is None:
            continue
        if join.id in loop_headers:
            continue
        if loop_membership(source.id) != loop_membership(arm.id) or loop_membership(source.id) != loop_membership(join.id):
            continue
        if set(cfg.predecessors(join.id)) != {source.id, arm.id}:
            continue
        if (
            _contains_unscoped_loop_control(source.statements)
            or _contains_unscoped_loop_control(arm.statements)
        ):
            continue

        source_statements = _statements_without_trivial_phi_assignments(
            source.statements,
            incoming_blocks=cfg.predecessors(source.id),
        )
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(source.id,),
        )
        phi_copies = _direct_phi_join_copies(join, source.id, arm.id)
        if source_statements is None or arm_statements is None or phi_copies is None:
            continue
        direct_copies, arm_copies, join_statements = phi_copies

        if direct_is_true:
            then_body = direct_copies
            else_body = (*arm_statements, *arm_copies)
        else:
            then_body = (*arm_statements, *arm_copies)
            else_body = direct_copies

        rewritten_continuation: BasicBlock | None = None
        if isinstance(join.terminator, Jump):
            continuation_id = join.terminator.target
            if continuation_id in {source.id, arm.id, join.id}:
                continue
            continuation = block_map.get(continuation_id)
            if continuation is None:
                continue
            rewritten_continuation = _rename_phi_predecessor(
                continuation,
                previous=join.id,
                replacement=source.id,
            )
            if rewritten_continuation is None:
                continue
            rewritten_terminator: Terminator = Jump(
                source=join.terminator.source,
                target=continuation_id,
            )
        elif isinstance(join.terminator, Return):
            rewritten_terminator = join.terminator
        else:
            continue

        rewritten_source = BasicBlock(
            id=source.id,
            statements=(
                *source_statements,
                If(
                    source=source.terminator.source,
                    condition=source.terminator.condition,
                    then_body=then_body,
                    else_body=else_body,
                ),
                *join_statements,
            ),
            terminator=rewritten_terminator,
        )
        removed = {arm.id, join.id}
        return FunctionIR(
            name=function.name,
            params=function.params,
            blocks=tuple(
                rewritten_source
                if block.id == source.id
                else rewritten_continuation
                if rewritten_continuation is not None and block.id == rewritten_continuation.id
                else block
                for block in function.blocks
                if block.id not in removed
            ),
            nested_functions=function.nested_functions,
            source=function.source,
            recovery_kind=function.recovery_kind,
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-optional-phi-diamond-region",
            },
        )
    return None


def _structure_exact_optional_terminal_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse an optional arm whose common join terminates the function.

    The source shape is ``branch -> join`` on one edge and
    ``branch -> arm -> join`` on the other.  Unlike the Phi-diamond rule, the
    join here is a terminal block (including a block ending in a statement
    level raise).  The arm may already contain structured statements, but its
    CFG edge to the join must be unique so moving those statements under the
    branch cannot duplicate or skip an execution path.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    def is_terminal(block: BasicBlock) -> bool:
        return isinstance(block.terminator, (Return,)) or (
            block.terminator is None
            and bool(block.statements)
            and isinstance(block.statements[-1], (Raise, Reraise))
        )

    for source in function.blocks:
        if not isinstance(source.terminator, Branch):
            continue
        true_block = block_map.get(source.terminator.true_target)
        false_block = block_map.get(source.terminator.false_target)
        if true_block is None or false_block is None or true_block.id == false_block.id:
            continue

        arm: BasicBlock | None = None
        join: BasicBlock | None = None
        arm_is_true: bool | None = None
        if (
            isinstance(true_block.terminator, Jump)
            and true_block.terminator.target == false_block.id
            and cfg.predecessors(true_block.id) == (source.id,)
        ):
            arm, join, arm_is_true = true_block, false_block, True
        elif (
            isinstance(false_block.terminator, Jump)
            and false_block.terminator.target == true_block.id
            and cfg.predecessors(false_block.id) == (source.id,)
        ):
            arm, join, arm_is_true = false_block, true_block, False
        if arm is None or join is None or arm_is_true is None:
            continue
        if not is_terminal(join):
            continue
        if set(cfg.predecessors(join.id)) != {source.id, arm.id}:
            continue
        if any(loop_membership(block.id) for block in (source, arm, join)):
            continue
        if _contains_unscoped_loop_control(source.statements):
            continue

        source_statements = _statements_without_trivial_phi_assignments(
            source.statements,
            incoming_blocks=cfg.predecessors(source.id),
        )
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(source.id,),
        )
        phi_copies = _direct_phi_join_copies(join, source.id, arm.id)
        if source_statements is None or arm_statements is None or phi_copies is None:
            continue
        direct_copies, arm_copies, join_statements = phi_copies

        arm_body = (*arm_statements, *arm_copies)
        rewritten_source = BasicBlock(
            id=source.id,
            statements=(
                *source_statements,
                If(
                    source=source.terminator.source,
                    condition=source.terminator.condition,
                    then_body=arm_body if arm_is_true else direct_copies,
                    else_body=direct_copies if arm_is_true else arm_body,
                ),
                *join_statements,
            ),
            terminator=join.terminator,
        )
        removed = {arm.id, join.id}
        return FunctionIR(
            name=function.name,
            params=function.params,
            blocks=tuple(
                rewritten_source if block.id == source.id else block
                for block in function.blocks
                if block.id not in removed
            ),
            nested_functions=function.nested_functions,
            source=function.source,
            recovery_kind="generic-vm-low-level-cfg-structured",
            metadata={
                **function.metadata,
                "structured_lift": "generic-vm-low-level-cfg-structured",
                "low_level_cfg_structured": "exact-optional-terminal-diamond-region",
            },
        )
    return None


def _structure_exact_direct_phi_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse one two-arm phi diamond without requiring a whole function.

    The two arms must be the only predecessors of the phi join, and the join
    must have one ordinary jump continuation. Phi values are made explicit on
    their respective incoming edges before replacing the diamond with an
    ``If``. The successor's incoming label is rewritten only when it is an
    unambiguous edge rename, preserving SSA meaning for the surrounding CFG.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    loop_headers = frozenset(loop.header for loop in loops)
    for source in function.blocks:
        if not isinstance(source.terminator, Branch):
            continue
        true_block = block_map.get(source.terminator.true_target)
        false_block = block_map.get(source.terminator.false_target)
        if true_block is None or false_block is None or true_block.id == false_block.id:
            continue
        # An arm may only be inlined when this branch is its sole incoming
        # edge.  Otherwise removing the arm would leave another predecessor
        # targeting a deleted block (and, more importantly, would move code
        # whose execution is not controlled solely by this condition).
        if cfg.predecessors(true_block.id) != (source.id,):
            continue
        if cfg.predecessors(false_block.id) != (source.id,):
            continue
        if not isinstance(true_block.terminator, Jump) or not isinstance(false_block.terminator, Jump):
            continue
        if true_block.terminator.target != false_block.terminator.target:
            continue
        join = block_map.get(true_block.terminator.target)
        if join is None:
            continue
        # A direct diamond inside a loop is as safe to collapse as one outside
        # it when all four region blocks occupy the same nested-loop scope.
        # Reject headers and any boundary-crossing candidate: changing either
        # could alter which condition controls a backedge or an exit.
        region_ids = (source.id, true_block.id, false_block.id, join.id)
        if source.id in loop_headers or join.id in loop_headers:
            continue
        if len({loop_membership(block_id) for block_id in region_ids}) != 1:
            continue
        if set(cfg.predecessors(join.id)) != {true_block.id, false_block.id}:
            continue
        true_statements = _statements_without_trivial_phi_assignments(
            true_block.statements,
            incoming_blocks=(source.id,),
        )
        false_statements = _statements_without_trivial_phi_assignments(
            false_block.statements,
            incoming_blocks=(source.id,),
        )
        phi_copies = _direct_phi_join_copies(join, true_block.id, false_block.id)
        if true_statements is None or false_statements is None or phi_copies is None:
            continue
        true_copies, false_copies, join_statements = phi_copies
        rewritten_continuation: BasicBlock | None = None
        if isinstance(join.terminator, Jump):
            continuation_id = join.terminator.target
            if continuation_id in {source.id, true_block.id, false_block.id, join.id}:
                continue
            continuation = block_map.get(continuation_id)
            if continuation is None:
                continue
            rewritten_continuation = _rename_phi_predecessor(
                continuation,
                previous=join.id,
                replacement=source.id,
            )
            if rewritten_continuation is None:
                continue
            rewritten_terminator: Terminator = Jump(source=join.terminator.source, target=continuation_id)
        elif isinstance(join.terminator, Return):
            continuation_id = None
            rewritten_terminator = join.terminator
        else:
            continue
        rewritten_source = BasicBlock(
            id=source.id,
            statements=(
                *source.statements,
                If(
                    condition=source.terminator.condition,
                    then_body=(*true_statements, *true_copies),
                    else_body=(*false_statements, *false_copies),
                ),
                *join_statements,
            ),
            terminator=rewritten_terminator,
        )
        removed = {true_block.id, false_block.id, join.id}
        return FunctionIR(
            name=function.name,
            params=function.params,
            blocks=tuple(
                rewritten_source
                if block.id == source.id
                else rewritten_continuation
                if rewritten_continuation is not None and block.id == rewritten_continuation.id
                else block
                for block in function.blocks
                if block.id not in removed
            ),
            nested_functions=function.nested_functions,
            source=function.source,
            recovery_kind=function.recovery_kind,
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-direct-phi-diamond-region",
            },
        )
    return None


def _structure_exact_short_circuit_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a two-test short-circuit diamond with a shared arm.

    A common compiler lowering for ``a or b`` (and its inverted forms) sends
    one edge of the first test directly to an arm and its other edge to a
    second test.  The second test then selects that same arm or the alternate
    arm.  The shared arm has two predecessors, so a direct-diamond matcher
    must not delete it.  This matcher proves the complete five-block region
    and duplicates the shared *structured body* on the two mutually exclusive
    paths.  It never duplicates a CFG edge at runtime, and it preserves join
    phi copies on both occurrences of that arm.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    for source in function.blocks:
        source_terminator = source.terminator
        if not isinstance(source_terminator, Branch) or source.id in loop_headers:
            continue
        for first_id, shared_id in (
            (source_terminator.true_target, source_terminator.false_target),
            (source_terminator.false_target, source_terminator.true_target),
        ):
            first = block_map.get(first_id)
            shared = block_map.get(shared_id)
            if first is None or shared is None or first.id == shared.id:
                continue
            if not isinstance(first.terminator, Branch) or not isinstance(shared.terminator, Jump):
                continue
            first_targets = (first.terminator.true_target, first.terminator.false_target)
            if first_targets.count(shared.id) != 1:
                continue
            other_id = first.terminator.false_target if first.terminator.true_target == shared.id else first.terminator.true_target
            other = block_map.get(other_id)
            if other is None or other.id in {source.id, first.id, shared.id}:
                continue
            if not isinstance(other.terminator, Jump) or other.terminator.target != shared.terminator.target:
                continue
            join = block_map.get(shared.terminator.target)
            if join is None or join.id in loop_headers:
                continue
            region_ids = (source.id, first.id, shared.id, other.id, join.id)
            if len(set(region_ids)) != len(region_ids):
                continue
            if len({loop_membership(block_id) for block_id in region_ids}) != 1:
                continue
            if set(cfg.predecessors(first.id)) != {source.id}:
                continue
            if set(cfg.predecessors(shared.id)) != {source.id, first.id}:
                continue
            if set(cfg.predecessors(other.id)) != {first.id}:
                continue
            if set(cfg.predecessors(join.id)) != {shared.id, other.id}:
                continue
            first_statements = _statements_without_trivial_phi_assignments(
                first.statements,
                incoming_blocks=(source.id,),
            )
            shared_statements = _statements_without_trivial_phi_assignments(
                shared.statements,
                incoming_blocks=(source.id, first.id),
            )
            other_statements = _statements_without_trivial_phi_assignments(
                other.statements,
                incoming_blocks=(first.id,),
            )
            phi_copies = _direct_phi_join_copies(join, shared.id, other.id)
            if (
                first_statements is None
                or shared_statements is None
                or other_statements is None
                or phi_copies is None
                or _statements_contain_break(first_statements)
                or _statements_contain_break(shared_statements)
                or _statements_contain_break(other_statements)
            ):
                continue
            shared_copies, other_copies, join_statements = phi_copies
            shared_body = (*shared_statements, *shared_copies)
            other_body = (*other_statements, *other_copies)
            first_if = If(
                condition=first.terminator.condition,
                then_body=shared_body if first.terminator.true_target == shared.id else other_body,
                else_body=other_body if first.terminator.true_target == shared.id else shared_body,
            )
            nested_body = (*first_statements, first_if)
            outer_if = If(
                condition=source_terminator.condition,
                then_body=nested_body if source_terminator.true_target == first.id else shared_body,
                else_body=shared_body if source_terminator.true_target == first.id else nested_body,
            )
            rewritten_continuation: BasicBlock | None = None
            if isinstance(join.terminator, Jump):
                continuation_id = join.terminator.target
                if continuation_id in region_ids:
                    continue
                continuation = block_map.get(continuation_id)
                if continuation is None:
                    continue
                rewritten_continuation = _rename_phi_predecessor(
                    continuation,
                    previous=join.id,
                    replacement=source.id,
                )
                if rewritten_continuation is None:
                    continue
                rewritten_terminator: Terminator = Jump(source=join.terminator.source, target=continuation_id)
            elif isinstance(join.terminator, Return):
                rewritten_terminator = join.terminator
            else:
                continue
            rewritten_source = BasicBlock(
                id=source.id,
                statements=(*source.statements, outer_if, *join_statements),
                terminator=rewritten_terminator,
            )
            removed = {first.id, shared.id, other.id, join.id}
            return FunctionIR(
                name=function.name,
                params=function.params,
                blocks=tuple(
                    rewritten_source
                    if block.id == source.id
                    else rewritten_continuation
                    if rewritten_continuation is not None and block.id == rewritten_continuation.id
                    else block
                    for block in function.blocks
                    if block.id not in removed
                ),
                nested_functions=function.nested_functions,
                source=function.source,
                recovery_kind=function.recovery_kind,
                metadata={
                    **function.metadata,
                    "low_level_cfg_structured": "exact-short-circuit-diamond-region",
                },
            )
    return None


def _structure_exact_linear_arm_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a two-arm diamond whose arms are single-predecessor chains.

    The direct-diamond normalizer handles one block on each arm.  Compiler
    lowering commonly inserts bookkeeping blocks between a condition and its
    join; those blocks can be inlined just as safely when their predecessor
    relation proves they belong exclusively to that arm.  Join Phi copies and
    the successor edge rename use the same machinery as the direct rule.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    def linear_arm(start: str, predecessor_id: str) -> tuple[tuple[BasicBlock, ...], str] | None:
        blocks: list[BasicBlock] = []
        current = start
        predecessor = predecessor_id
        seen: set[str] = set()
        while current not in seen:
            block = block_map.get(current)
            if block is None or current in loop_headers or cfg.predecessors(current) != (predecessor,):
                return None
            if loop_membership(block.id) != loop_membership(predecessor_id):
                return None
            seen.add(current)
            blocks.append(block)
            if not isinstance(block.terminator, Jump):
                return None
            target = block.terminator.target
            if target not in block_map:
                return None
            # The last jump is the arm's join.  Do not absorb a target with a
            # second predecessor: it is precisely the candidate join.
            if cfg.predecessors(target) != (block.id,):
                return tuple(blocks), target
            predecessor = block.id
            current = target
        return None

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, Branch) or source.id in loop_headers:
            continue
        true_arm = linear_arm(terminator.true_target, source.id)
        false_arm = linear_arm(terminator.false_target, source.id)
        if true_arm is None or false_arm is None:
            continue
        true_blocks, true_join = true_arm
        false_blocks, false_join = false_arm
        if true_join != false_join:
            continue
        join = block_map.get(true_join)
        if join is None:
            continue
        arm_ids = {block.id for block in (*true_blocks, *false_blocks)}
        if len(arm_ids) != len(true_blocks) + len(false_blocks):
            continue
        if set(cfg.predecessors(join.id)) != {true_blocks[-1].id, false_blocks[-1].id}:
            continue
        region_ids = (source.id, *(block.id for block in true_blocks), *(block.id for block in false_blocks), join.id)
        if len({loop_membership(block_id) for block_id in region_ids}) != 1:
            continue

        def arm_statements(blocks: tuple[BasicBlock, ...], first_predecessor: str) -> tuple[Stmt, ...] | None:
            rendered: list[Stmt] = []
            predecessor = first_predecessor
            for block in blocks:
                statements = _statements_without_trivial_phi_assignments(
                    block.statements,
                    incoming_blocks=(predecessor,),
                )
                if statements is None or _statements_contain_break(statements):
                    return None
                rendered.extend(statements)
                predecessor = block.id
            return tuple(rendered)

        true_statements = arm_statements(true_blocks, source.id)
        false_statements = arm_statements(false_blocks, source.id)
        phi_copies = _direct_phi_join_copies(join, true_blocks[-1].id, false_blocks[-1].id)
        if true_statements is None or false_statements is None or phi_copies is None:
            continue
        true_copies, false_copies, join_statements = phi_copies
        rewritten_continuation: BasicBlock | None = None
        if isinstance(join.terminator, Jump):
            continuation_id = join.terminator.target
            if continuation_id in {*region_ids}:
                continue
            continuation = block_map.get(continuation_id)
            if continuation is None:
                continue
            rewritten_continuation = _rename_phi_predecessor(
                continuation,
                previous=join.id,
                replacement=source.id,
            )
            if rewritten_continuation is None:
                continue
            rewritten_terminator: Terminator = Jump(source=join.terminator.source, target=continuation_id)
        elif isinstance(join.terminator, Return):
            rewritten_terminator = join.terminator
        else:
            continue
        rewritten_source = BasicBlock(
            id=source.id,
            statements=(
                *source.statements,
                If(
                    source=terminator.source,
                    condition=terminator.condition,
                    then_body=(*true_statements, *true_copies),
                    else_body=(*false_statements, *false_copies),
                ),
                *join_statements,
            ),
            terminator=rewritten_terminator,
        )
        removed = arm_ids | {join.id}
        return FunctionIR(
            name=function.name,
            params=function.params,
            blocks=tuple(
                rewritten_source
                if block.id == source.id
                else rewritten_continuation
                if rewritten_continuation is not None and block.id == rewritten_continuation.id
                else block
                for block in function.blocks
                if block.id not in removed
            ),
            nested_functions=function.nested_functions,
            source=function.source,
            recovery_kind=function.recovery_kind,
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-linear-arm-diamond-region",
            },
        )
    return None


def _structure_exact_optional_linear_arm_diamond_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a branch with one linear arm and one direct join edge.

    This is the asymmetric counterpart to the two-arm diamond normalizer.
    One branch edge may already be the join, while the other passes through a
    chain that has the branch as its sole predecessor.  Phi copies on the
    join are materialized inside the selected branch (including the empty
    direct arm), so no value merge or side-effect ordering is guessed.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    def linear_arm(start: str, predecessor_id: str) -> tuple[tuple[BasicBlock, ...], str] | None:
        blocks: list[BasicBlock] = []
        current = start
        predecessor = predecessor_id
        while True:
            block = block_map.get(current)
            if block is None or current in loop_headers or cfg.predecessors(current) != (predecessor,):
                return None
            if loop_membership(block.id) != loop_membership(predecessor_id):
                return None
            blocks.append(block)
            if not isinstance(block.terminator, Jump) or block.terminator.target not in block_map:
                return None
            target = block.terminator.target
            if cfg.predecessors(target) != (block.id,):
                return tuple(blocks), target
            predecessor = block.id
            current = target

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, Branch) or source.id in loop_headers:
            continue
        for arm_start, join_id, arm_is_true in (
            (terminator.true_target, terminator.false_target, True),
            (terminator.false_target, terminator.true_target, False),
        ):
            arm = linear_arm(arm_start, source.id)
            join = block_map.get(join_id)
            if arm is None or join is None or arm[1] != join.id:
                continue
            arm_blocks, _ = arm
            if set(cfg.predecessors(join.id)) != {source.id, arm_blocks[-1].id}:
                continue
            region_ids = (source.id, *(block.id for block in arm_blocks), join.id)
            if len({loop_membership(block_id) for block_id in region_ids}) != 1:
                continue
            arm_statements: list[Stmt] = []
            predecessor = source.id
            for block in arm_blocks:
                statements = _statements_without_trivial_phi_assignments(
                    block.statements,
                    incoming_blocks=(predecessor,),
                )
                if statements is None or _statements_contain_break(statements):
                    break
                arm_statements.extend(statements)
                predecessor = block.id
            else:
                phi_copies = _direct_phi_join_copies(join, arm_blocks[-1].id, source.id)
                if phi_copies is None:
                    continue
                arm_copies, direct_copies, join_statements = phi_copies
                if isinstance(join.terminator, Jump):
                    continuation = block_map.get(join.terminator.target)
                    if continuation is None or continuation.id in region_ids:
                        continue
                    rewritten_continuation = _rename_phi_predecessor(
                        continuation,
                        previous=join.id,
                        replacement=source.id,
                    )
                    if rewritten_continuation is None:
                        continue
                    rewritten_terminator: Terminator = Jump(
                        source=join.terminator.source,
                        target=continuation.id,
                    )
                elif isinstance(join.terminator, Return):
                    rewritten_continuation = None
                    rewritten_terminator = join.terminator
                else:
                    successor_ids = _terminator_targets(join.terminator)
                    if not successor_ids or any(successor_id in region_ids for successor_id in successor_ids):
                        continue
                    rewritten_successors: dict[str, BasicBlock] = {}
                    for successor_id in successor_ids:
                        successor = block_map.get(successor_id)
                        if successor is None:
                            break
                        replacement = _rename_phi_predecessor(
                            successor,
                            previous=join.id,
                            replacement=source.id,
                        )
                        if replacement is None:
                            break
                        rewritten_successors[successor_id] = replacement
                    else:
                        rewritten_continuation = None
                        rewritten_terminator = join.terminator
                        # The mapping is consumed below for every successor;
                        # use a local marker so an empty mapping remains
                        # distinguishable from a failed rewrite.
                        successor_rewrites = rewritten_successors

                        then_body = (*arm_statements, *arm_copies) if arm_is_true else direct_copies
                        else_body = direct_copies if arm_is_true else (*arm_statements, *arm_copies)
                        rewritten_source = BasicBlock(
                            id=source.id,
                            statements=(
                                *source.statements,
                                If(source=terminator.source, condition=terminator.condition, then_body=then_body, else_body=else_body),
                                *join_statements,
                            ),
                            terminator=rewritten_terminator,
                        )
                        removed = {block.id for block in arm_blocks} | {join.id}
                        return FunctionIR(
                            name=function.name,
                            params=function.params,
                            blocks=tuple(
                                rewritten_source
                                if block.id == source.id
                                else successor_rewrites.get(block.id, block)
                                for block in function.blocks
                                if block.id not in removed
                            ),
                            nested_functions=function.nested_functions,
                            source=function.source,
                            recovery_kind=function.recovery_kind,
                            metadata={
                                **function.metadata,
                                "low_level_cfg_structured": "exact-optional-linear-arm-diamond-region",
                            },
                        )
                    continue
                then_body = (*arm_statements, *arm_copies) if arm_is_true else direct_copies
                else_body = direct_copies if arm_is_true else (*arm_statements, *arm_copies)
                rewritten_source = BasicBlock(
                    id=source.id,
                    statements=(
                        *source.statements,
                        If(source=terminator.source, condition=terminator.condition, then_body=then_body, else_body=else_body),
                        *join_statements,
                    ),
                    terminator=rewritten_terminator,
                )
                removed = {block.id for block in arm_blocks} | {join.id}
                return FunctionIR(
                    name=function.name,
                    params=function.params,
                    blocks=tuple(
                        rewritten_source
                        if block.id == source.id
                        else rewritten_continuation
                        if rewritten_continuation is not None and block.id == rewritten_continuation.id
                        else block
                        for block in function.blocks
                        if block.id not in removed
                    ),
                    nested_functions=function.nested_functions,
                    source=function.source,
                    recovery_kind=function.recovery_kind,
                    metadata={
                        **function.metadata,
                        "low_level_cfg_structured": "exact-optional-linear-arm-diamond-region",
                    },
                )
    return None


def _structure_exact_direct_phi_dispatch_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a multi-way dispatch whose direct arms share one join.

    This is the N-arm counterpart to the phi-diamond rule. It accepts only
    direct arm-to-join jumps and either a single jump continuation or a final
    return. Each join phi is materialized on exactly its incoming arm, so the
    rendered ``Switch`` preserves the original edge-specific value state.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    loop_headers = frozenset(loop.header for loop in loops)
    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, MultiBranch):
            continue
        arms = (*tuple(target for _value, target in terminator.cases), terminator.default_target)
        if not arms or any(target == source.id for target in arms):
            continue
        target_blocks = tuple(block_map.get(target) for target in arms)
        if any(block is None for block in target_blocks):
            continue
        typed_targets = tuple(block for block in target_blocks if block is not None)
        join_ids = {
            block.terminator.target if isinstance(block.terminator, Jump) else block.id
            for block in typed_targets
        }
        if len(join_ids) != 1:
            continue
        join = block_map.get(next(iter(join_ids)))
        if join is None or source.id in loop_headers or join.id in loop_headers:
            continue
        typed_arms = tuple(block for block in typed_targets if block.id != join.id)
        if any(not isinstance(block.terminator, Jump) or block.terminator.target != join.id for block in typed_arms):
            continue
        direct_join_targets = {target for target in arms if target == join.id}
        region_ids = (source.id, join.id, *(block.id for block in typed_arms))
        if len({loop_membership(block_id) for block_id in region_ids}) != 1:
            continue
        if any(set(cfg.predecessors(arm.id)) != {source.id} for arm in set(typed_arms)):
            continue
        join_predecessors = {block.id for block in typed_arms}
        if direct_join_targets:
            join_predecessors.add(source.id)
        if set(cfg.predecessors(join.id)) != join_predecessors:
            continue
        arm_statements: dict[str, tuple[Stmt, ...]] = {}
        for arm in typed_arms:
            statements = _statements_without_trivial_phi_assignments(
                arm.statements,
                incoming_blocks=(source.id,),
            )
            if statements is None:
                break
            if _statements_contain_break(statements):
                break
            arm_statements[arm.id] = statements
        else:
            phi_copies = _final_join_phi_copies(join, join_predecessors)
            if phi_copies is None:
                continue
            edge_copies, join_statements = phi_copies
            continuation: BasicBlock | None = None
            if isinstance(join.terminator, Jump):
                continuation_id = join.terminator.target
                if continuation_id in {source.id, join.id, *(block.id for block in typed_arms)}:
                    continue
                continuation_block = block_map.get(continuation_id)
                if continuation_block is None:
                    continue
                continuation = _rename_phi_predecessor(
                    continuation_block,
                    previous=join.id,
                    replacement=source.id,
                )
                if continuation is None:
                    continue
                rewritten_terminator: Terminator = Jump(
                    source=join.terminator.source,
                    target=continuation_id,
                )
            elif isinstance(join.terminator, Return):
                continuation_id = None
                rewritten_terminator = join.terminator
            else:
                successor_ids = _terminator_targets(join.terminator)
                if not successor_ids or any(successor_id in {source.id, join.id, *(block.id for block in typed_arms)} for successor_id in successor_ids):
                    continue
                rewritten_successors: dict[str, BasicBlock] = {}
                for successor_id in successor_ids:
                    successor = block_map.get(successor_id)
                    if successor is None:
                        break
                    replacement = _rename_phi_predecessor(
                        successor,
                        previous=join.id,
                        replacement=source.id,
                    )
                    if replacement is None:
                        break
                    rewritten_successors[successor_id] = replacement
                else:
                    case_bodies: list[tuple[Expr, tuple[Stmt, ...]]] = []
                    for value, target in terminator.cases:
                        if any(_values_semantically_equal(value, existing) for existing, _body in case_bodies):
                            break
                        body = edge_copies[source.id] if target == join.id else (*arm_statements[target], *edge_copies[target])
                        case_bodies.append((value, body))
                    else:
                        default_body = (
                            edge_copies[source.id]
                            if terminator.default_target == join.id
                            else (*arm_statements[terminator.default_target], *edge_copies[terminator.default_target])
                        )
                        rewritten_source = BasicBlock(
                            id=source.id,
                            statements=(
                                *source.statements,
                                Switch(
                                    source=terminator.source,
                                    selector=terminator.selector,
                                    cases=tuple(case_bodies),
                                    default_body=default_body,
                                ),
                                *join_statements,
                            ),
                            terminator=join.terminator,
                        )
                        removed = {join.id, *(block.id for block in typed_arms)}
                        return FunctionIR(
                            name=function.name,
                            params=function.params,
                            blocks=tuple(
                                rewritten_source
                                if block.id == source.id
                                else rewritten_successors.get(block.id, block)
                                for block in function.blocks
                                if block.id not in removed
                            ),
                            nested_functions=function.nested_functions,
                            source=function.source,
                            recovery_kind=function.recovery_kind,
                            metadata={
                                **function.metadata,
                                "low_level_cfg_structured": "exact-direct-phi-dispatch-region",
                            },
                        )
                continue

            case_bodies: list[tuple[Expr, tuple[Stmt, ...]]] = []
            for value, target in terminator.cases:
                if any(_values_semantically_equal(value, existing) for existing, _body in case_bodies):
                    break
                body = edge_copies[source.id] if target == join.id else (*arm_statements[target], *edge_copies[target])
                case_bodies.append((value, body))
            else:
                default_body = (
                    edge_copies[source.id]
                    if terminator.default_target == join.id
                    else (*arm_statements[terminator.default_target], *edge_copies[terminator.default_target])
                )
                rewritten_source = BasicBlock(
                    id=source.id,
                    statements=(
                        *source.statements,
                        Switch(
                            source=terminator.source,
                            selector=terminator.selector,
                            cases=tuple(case_bodies),
                            default_body=default_body,
                        ),
                        *join_statements,
                    ),
                    terminator=rewritten_terminator,
                )
                removed = {join.id, *(block.id for block in typed_arms)}
                return FunctionIR(
                    name=function.name,
                    params=function.params,
                    blocks=tuple(
                        rewritten_source
                        if block.id == source.id
                        else continuation
                        if continuation is not None and block.id == continuation.id
                        else block
                        for block in function.blocks
                        if block.id not in removed
                    ),
                    nested_functions=function.nested_functions,
                    source=function.source,
                    recovery_kind=function.recovery_kind,
                    metadata={
                        **function.metadata,
                        "low_level_cfg_structured": "exact-direct-phi-dispatch-region",
                    },
                )
    return None


def _structure_exact_partial_multiway_arm_splice(function: FunctionIR) -> FunctionIR | None:
    """Inline exclusive multiway arms while retaining a shared join block.

    Ghidra collapses switch cases locally even when the common continuation is
    also reached from outside the switch.  The full-region dispatch matcher
    intentionally requires an exact join predecessor set, so it cannot apply
    to that shape.  This rule removes only case blocks whose sole predecessor
    is the dispatch block and whose sole successor is the same join.  The join
    remains in the CFG, preserving every external entry and its terminator.

    A Phi at the retained join is rewritten only when all removed case edges
    carry the same value (and an existing dispatch edge, when present, carries
    that value too).  Distinct edge values cannot be represented by one new
    predecessor without adding a language-specific carrier, so the candidate
    is rejected and the original goto form is preserved.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, MultiBranch):
            continue
        targets = tuple(target for _value, target in terminator.cases) + (
            terminator.default_target,
        )
        if not targets or any(target == source.id for target in targets):
            continue
        if any(
            any(_values_semantically_equal(value, other) for other, _target in terminator.cases[:index])
            for index, (value, _target) in enumerate(terminator.cases)
        ):
            # Duplicate case values make the selected arm ambiguous in the
            # generic IR, even if a particular frontend normally forbids them.
            continue

        target_blocks = {target: block_map.get(target) for target in set(targets)}
        if any(block is None for block in target_blocks.values()):
            continue
        join_candidates: set[str] = set()
        arm_by_target: dict[str, BasicBlock] = {}
        valid = True
        for target in dict.fromkeys(targets):
            block = target_blocks[target]
            if block is None:
                valid = False
                break
            if target == source.id:
                valid = False
                break
            if (
                isinstance(block.terminator, Jump)
                and block.terminator.target != source.id
                # Several distinct case labels may intentionally enter one
                # physical arm.  The CFG records one predecessor occurrence
                # per label, so accept repeated occurrences only when every
                # one is still this dispatch source.
                and cfg.predecessors(block.id)
                and set(cfg.predecessors(block.id)) == {source.id}
            ):
                join_candidates.add(block.terminator.target)
                arm_by_target[target] = block
            else:
                # A direct target is the retained join candidate.  It may have
                # arbitrary external predecessors and need not be an arm.
                join_candidates.add(target)
        if not valid or len(join_candidates) != 1:
            continue
        join_id = next(iter(join_candidates))
        join = block_map.get(join_id)
        if join is None or join.id == source.id:
            continue
        # A partial splice that targets a natural-loop header can erase the
        # distinction between a loop backedge and an ordinary branch.  Leave
        # that topology for the loop-specific exact matchers below.
        if source.id in loop_headers or join.id in loop_headers:
            continue

        # Every non-join dispatch target must be an exclusive jump-only arm
        # into the selected join.  A target used by both a direct edge and an
        # arm is not removable without changing the edge identity.
        removable: dict[str, BasicBlock] = {}
        case_bodies: dict[str, tuple[Stmt, ...]] = {}
        valid = True
        for target in dict.fromkeys(targets):
            if target == join_id:
                case_bodies[target] = ()
                continue
            if target not in arm_by_target:
                valid = False
                break
            arm = arm_by_target[target]
            if arm.terminator is None or not isinstance(arm.terminator, Jump):
                valid = False
                break
            if (
                arm.terminator.target != join_id
                or not cfg.predecessors(arm.id)
                or set(cfg.predecessors(arm.id)) != {source.id}
            ):
                valid = False
                break
            if arm.exception_edge is not None or arm.active_exception_handlers:
                valid = False
                break
            statements = _statements_without_trivial_phi_assignments(
                arm.statements,
                incoming_blocks=(source.id,),
            )
            if (
                statements is None
                or _contains_terminal_statement(statements)
                or _contains_unscoped_loop_control(statements)
            ):
                valid = False
                break
            if loop_membership(arm.id) != loop_membership(source.id):
                valid = False
                break
            removable[target] = arm
            case_bodies[target] = statements
        if not valid or not removable:
            # No block would disappear, so this is not a splice.  Keeping this
            # guard also avoids replacing a dispatch with an equivalent empty
            # Switch/Jump pair indefinitely.
            continue
        if loop_membership(join.id) != loop_membership(source.id):
            continue

        old_join_predecessors = set(cfg.predecessors(join.id))
        if not old_join_predecessors:
            continue
        removed_ids = frozenset(removable)
        if not removed_ids <= old_join_predecessors:
            continue
        rewritten_join = _merge_phi_predecessors(
            join,
            previous=removed_ids,
            replacement=source.id,
            expected_predecessors=old_join_predecessors,
        )
        if rewritten_join is None:
            continue

        cases: list[tuple[Expr, tuple[Stmt, ...]]] = []
        rejected = False
        for value, target in terminator.cases:
            body = case_bodies.get(target)
            if body is None:
                rejected = True
                break
            cases.append((value, body))
        if rejected:
            continue
        default_body = case_bodies.get(terminator.default_target)
        if default_body is None:
            continue
        rewritten_source = replace(
            source,
            statements=(
                *source.statements,
                Switch(
                    source=terminator.source,
                    selector=terminator.selector,
                    cases=tuple(cases),
                    default_body=default_body,
                ),
            ),
            terminator=Jump(source=terminator.source, target=join_id),
        )
        rewritten_blocks = tuple(
            rewritten_source
            if block.id == source.id
            else rewritten_join
            if block.id == join.id
            else block
            for block in function.blocks
            if block.id not in removed_ids
        )
        rewritten = replace(
            function,
            blocks=rewritten_blocks,
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-partial-multiway-arm-splice",
            },
        )
        if build_cfg(rewritten).diagnostics:
            continue
        return rewritten
    return None


def _structure_exact_partial_branch_arm_splice(function: FunctionIR) -> FunctionIR | None:
    """Inline exclusive binary arms while retaining a shared join block.

    This is the binary counterpart of
    :func:`_structure_exact_partial_multiway_arm_splice` and mirrors
    Ghidra's local proper-if/if-else collapse.  Each non-direct branch target
    must be a single-entry block that jumps to the same retained join.  The
    join may have additional predecessors; only equivalent Phi values from
    removed arms may be merged into the source predecessor.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    loop_headers = frozenset(loop.header for loop in loops)

    def loop_membership(block_id: str) -> frozenset[str]:
        return frozenset(loop.header for loop in loops if block_id in loop.blocks)

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, Branch):
            continue
        targets = (terminator.true_target, terminator.false_target)
        if targets[0] == targets[1] or any(target == source.id for target in targets):
            continue

        join_candidates: set[str] = set()
        arm_by_target: dict[str, BasicBlock] = {}
        for target in targets:
            block = block_map.get(target)
            if block is None:
                join_candidates.clear()
                break
            if (
                isinstance(block.terminator, Jump)
                and block.terminator.target != source.id
                and cfg.predecessors(block.id) == (source.id,)
            ):
                join_candidates.add(block.terminator.target)
                arm_by_target[target] = block
            else:
                join_candidates.add(target)
        if len(join_candidates) != 1:
            continue
        join_id = next(iter(join_candidates))
        join = block_map.get(join_id)
        if join is None or join.id == source.id:
            continue
        if source.id in loop_headers or join.id in loop_headers:
            continue

        removable: dict[str, BasicBlock] = {}
        bodies: dict[str, tuple[Stmt, ...]] = {}
        valid = True
        for target in targets:
            if target == join_id:
                bodies[target] = ()
                continue
            arm = arm_by_target.get(target)
            if arm is None or arm.terminator is None or not isinstance(arm.terminator, Jump):
                valid = False
                break
            if arm.terminator.target != join_id or cfg.predecessors(arm.id) != (source.id,):
                valid = False
                break
            if arm.exception_edge is not None or arm.active_exception_handlers:
                valid = False
                break
            statements = _statements_without_trivial_phi_assignments(
                arm.statements,
                incoming_blocks=(source.id,),
            )
            if (
                statements is None
                or _contains_terminal_statement(statements)
                or _contains_unscoped_loop_control(statements)
            ):
                valid = False
                break
            if loop_membership(arm.id) != loop_membership(source.id):
                valid = False
                break
            removable[target] = arm
            bodies[target] = statements
        if not valid or not removable or loop_membership(join.id) != loop_membership(source.id):
            continue

        old_join_predecessors = set(cfg.predecessors(join.id))
        if not old_join_predecessors or not set(removable) <= old_join_predecessors:
            continue
        rewritten_join = _merge_phi_predecessors(
            join,
            previous=frozenset(removable),
            replacement=source.id,
            expected_predecessors=old_join_predecessors,
        )
        if rewritten_join is None:
            continue

        rewritten_source = replace(
            source,
            statements=(
                *source.statements,
                If(
                    source=terminator.source,
                    condition=terminator.condition,
                    then_body=bodies[terminator.true_target],
                    else_body=bodies[terminator.false_target],
                ),
            ),
            terminator=Jump(source=terminator.source, target=join_id),
        )
        removed_ids = frozenset(removable)
        rewritten = replace(
            function,
            blocks=tuple(
                rewritten_source
                if block.id == source.id
                else rewritten_join
                if block.id == join.id
                else block
                for block in function.blocks
                if block.id not in removed_ids
            ),
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-partial-branch-arm-splice",
            },
        )
        if build_cfg(rewritten).diagnostics:
            continue
        return rewritten
    return None


def _structure_exact_acyclic_tree_join_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse one proven acyclic branch tree with a single postdominating join.

    This is a VM-neutral SESE reduction.  It accepts a branch or dispatch whose
    arms form a tree (not a shared DAG) and reconverge at its immediate
    postdominator.  Each tree node has one incoming edge, all leaves are the
    join's complete predecessor set, and every join phi is materialized on the
    original leaf edge before the tree is nested into generic ``If``/``Switch``
    nodes.  Any loop, shared node, alternate entry, non-local exit, or complex
    phi declines the rewrite and leaves the exact low-level CFG unchanged.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or find_natural_loops(cfg):
        return None
    immediate_postdominators = compute_immediate_postdominators(cfg)
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)

    def successor_tree(
        target: str,
        predecessor: str,
        join_id: str,
        edge_copies: dict[str, tuple[Stmt, ...]],
        path: frozenset[str],
    ) -> tuple[tuple[Stmt, ...], frozenset[str], frozenset[str]] | None:
        if target == join_id:
            copies = edge_copies.get(predecessor)
            if copies is None:
                return None
            return copies, frozenset(), frozenset({predecessor})
        if target in path:
            return None
        block = block_map.get(target)
        if block is None or set(cfg.predecessors(target)) != {predecessor}:
            return None
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor,),
        )
        if statements is None or _statements_contain_break(statements):
            return None
        next_path = path | {target}
        terminator = block.terminator
        if isinstance(terminator, Jump):
            child = successor_tree(
                terminator.target,
                block.id,
                join_id,
                edge_copies,
                next_path,
            )
            if child is None:
                return None
            child_statements, child_blocks, leaves = child
            return (*statements, *child_statements), child_blocks | {block.id}, leaves
        if terminator is None:
            successors = cfg.successors(block.id)
            if len(successors) != 1:
                return None
            child = successor_tree(
                successors[0],
                block.id,
                join_id,
                edge_copies,
                next_path,
            )
            if child is None:
                return None
            child_statements, child_blocks, leaves = child
            return (*statements, *child_statements), child_blocks | {block.id}, leaves
        if isinstance(terminator, Branch):
            if terminator.true_target == terminator.false_target:
                return None
            then_result = successor_tree(
                terminator.true_target,
                block.id,
                join_id,
                edge_copies,
                next_path,
            )
            else_result = successor_tree(
                terminator.false_target,
                block.id,
                join_id,
                edge_copies,
                next_path,
            )
            if then_result is None or else_result is None:
                return None
            then_body, then_blocks, then_leaves = then_result
            else_body, else_blocks, else_leaves = else_result
            if then_blocks & else_blocks or then_leaves & else_leaves:
                return None
            if not statements and not then_body and not else_body:
                return None
            return (
                (
                    *statements,
                    If(
                        source=terminator.source,
                        condition=terminator.condition,
                        then_body=then_body,
                        else_body=else_body,
                    ),
                ),
                then_blocks | else_blocks | {block.id},
                then_leaves | else_leaves,
            )
        if not isinstance(terminator, MultiBranch):
            return None
        targets = (*tuple(target for _value, target in terminator.cases), terminator.default_target)
        if len(set(targets)) != len(targets):
            return None
        cases: list[tuple[Expr, tuple[Stmt, ...]]] = []
        removed: frozenset[str] = frozenset({block.id})
        leaves: frozenset[str] = frozenset()
        for value, case_target in terminator.cases:
            if any(_values_semantically_equal(value, existing) for existing, _body in cases):
                return None
            result = successor_tree(case_target, block.id, join_id, edge_copies, next_path)
            if result is None:
                return None
            body, case_blocks, case_leaves = result
            if removed & case_blocks or leaves & case_leaves:
                return None
            cases.append((value, body))
            removed |= case_blocks
            leaves |= case_leaves
        default_result = successor_tree(
            terminator.default_target,
            block.id,
            join_id,
            edge_copies,
            next_path,
        )
        if default_result is None:
            return None
        default_body, default_blocks, default_leaves = default_result
        if removed & default_blocks or leaves & default_leaves:
            return None
        if not statements and not any(body for _value, body in cases) and not default_body:
            return None
        return (
            (*statements, Switch(source=terminator.source, selector=terminator.selector, cases=tuple(cases), default_body=default_body)),
            removed | default_blocks,
            leaves | default_leaves,
        )

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, (Branch, MultiBranch)):
            continue
        if isinstance(terminator, Branch):
            if terminator.true_target == terminator.false_target:
                continue
        else:
            targets = (*tuple(target for _value, target in terminator.cases), terminator.default_target)
            if not targets or len(set(targets)) != len(targets):
                continue
        join_id = immediate_postdominators.get(source.id)
        join = block_map.get(join_id) if join_id is not None else None
        if join is None or join.id == source.id:
            continue
        join_predecessors = set(cfg.predecessors(join.id))
        if not join_predecessors:
            continue
        phi_copies = _final_join_phi_copies(join, join_predecessors)
        if phi_copies is None:
            continue
        edge_copies, join_statements = phi_copies

        if isinstance(terminator, Branch):
            then_result = successor_tree(
                terminator.true_target,
                source.id,
                join.id,
                edge_copies,
                frozenset(),
            )
            else_result = successor_tree(
                terminator.false_target,
                source.id,
                join.id,
                edge_copies,
                frozenset(),
            )
            if then_result is None or else_result is None:
                continue
            then_body, then_blocks, then_leaves = then_result
            else_body, else_blocks, else_leaves = else_result
            if then_blocks & else_blocks or then_leaves & else_leaves:
                continue
            if not then_body and not else_body:
                continue
            structured_statement: Stmt = If(
                source=terminator.source,
                condition=terminator.condition,
                then_body=then_body,
                else_body=else_body,
            )
            removed = then_blocks | else_blocks
            leaves = then_leaves | else_leaves
        else:
            cases: list[tuple[Expr, tuple[Stmt, ...]]] = []
            removed = frozenset()
            leaves = frozenset()
            rejected = False
            for value, target in terminator.cases:
                if any(_values_semantically_equal(value, existing) for existing, _body in cases):
                    rejected = True
                    break
                result = successor_tree(target, source.id, join.id, edge_copies, frozenset())
                if result is None:
                    rejected = True
                    break
                body, case_blocks, case_leaves = result
                if removed & case_blocks or leaves & case_leaves:
                    rejected = True
                    break
                cases.append((value, body))
                removed |= case_blocks
                leaves |= case_leaves
            if rejected:
                continue
            default_result = successor_tree(
                terminator.default_target,
                source.id,
                join.id,
                edge_copies,
                frozenset(),
            )
            if default_result is None:
                continue
            default_body, default_blocks, default_leaves = default_result
            if removed & default_blocks or leaves & default_leaves:
                continue
            if not any(body for _value, body in cases) and not default_body:
                continue
            structured_statement = Switch(
                source=terminator.source,
                selector=terminator.selector,
                cases=tuple(cases),
                default_body=default_body,
            )
            removed |= default_blocks
            leaves |= default_leaves

        if leaves != join_predecessors or source.id in removed or join.id in removed:
            continue
        region_members = removed | {source.id}
        if not region_graph.is_sese_body(
            region_members,
            entry=source.id,
            exit=join.id,
        ):
            continue
        collapsed_region_id = f"region:{source.id}:{join.id}"
        collapsed_region = region_graph.collapse(
            region_id=collapsed_region_id,
            members=region_members,
            kind="acyclic-tree",
        )
        if collapsed_region is None:
            continue
        collapsed_node = collapsed_region.node(collapsed_region_id)
        if collapsed_node is None or collapsed_node.members != region_members:
            continue
        continuation_rewrites: dict[str, BasicBlock] = {}
        if isinstance(join.terminator, Jump):
            successor_ids = (join.terminator.target,)
        elif isinstance(join.terminator, Return):
            successor_ids = ()
        elif isinstance(join.terminator, (Branch, MultiBranch)):
            successor_ids = _terminator_targets(join.terminator)
        else:
            continue
        for successor_id in dict.fromkeys(successor_ids):
            if successor_id in removed | {source.id, join.id}:
                break
            successor = block_map.get(successor_id)
            if successor is None:
                break
            rewritten_successor = _rename_phi_predecessor(
                successor,
                previous=join.id,
                replacement=source.id,
            )
            if rewritten_successor is None:
                break
            continuation_rewrites[successor_id] = rewritten_successor
        else:
            rewritten_source = BasicBlock(
                id=source.id,
                statements=(*source.statements, structured_statement, *join_statements),
                terminator=join.terminator,
            )
            return FunctionIR(
                name=function.name,
                params=function.params,
                blocks=tuple(
                    rewritten_source
                    if block.id == source.id
                    else continuation_rewrites.get(block.id, block)
                    for block in function.blocks
                    if block.id not in removed | {join.id}
                ),
                nested_functions=function.nested_functions,
                source=function.source,
                recovery_kind=function.recovery_kind,
                metadata={
                    **function.metadata,
                    "low_level_cfg_structured": "exact-acyclic-tree-join-region",
                },
            )
    return None


def _rename_phi_predecessor(
    block: BasicBlock,
    *,
    previous: str,
    replacement: str,
) -> BasicBlock | None:
    """Rename one removed CFG edge in leading or ordinary phi assignments."""

    statements: list[Stmt] = []
    for statement in block.statements:
        if not isinstance(statement, Assign) or not isinstance(statement.value, Phi):
            statements.append(statement)
            continue
        incoming = list(statement.value.incoming)
        if len({block_id for block_id, _value in incoming}) != len(incoming):
            return None
        if not any(block_id == previous for block_id, _value in incoming):
            statements.append(statement)
            continue
        if any(block_id == replacement for block_id, _value in incoming):
            return None
        renamed = tuple(
            (replacement if block_id == previous else block_id, value)
            for block_id, value in incoming
        )
        statements.append(
            Assign(
                source=statement.source,
                target=statement.target,
                value=Phi(source=statement.value.source, type=statement.value.type, incoming=renamed),
            )
        )
    return BasicBlock(id=block.id, statements=tuple(statements), terminator=block.terminator)


def _merge_phi_predecessors(
    block: BasicBlock,
    *,
    previous: frozenset[str],
    replacement: str,
    expected_predecessors: set[str],
) -> BasicBlock | None:
    """Merge equivalent Phi edges into one retained CFG predecessor.

    The caller removes several blocks that all enter ``block`` from the same
    dispatch source.  A single predecessor cannot carry case-dependent Phi
    values, so every removed edge must agree exactly with one another and with
    an existing replacement edge, if present.  Requiring a complete old
    predecessor set also rejects malformed/stale Phi metadata instead of
    silently dropping a value.
    """

    if not previous or replacement in previous:
        return None
    statements: list[Stmt] = []
    for statement in block.statements:
        if not isinstance(statement, Assign) or not isinstance(statement.value, Phi):
            statements.append(statement)
            continue
        incoming = tuple(statement.value.incoming)
        incoming_ids = {predecessor for predecessor, _value in incoming}
        if len(incoming_ids) != len(incoming):
            return None
        if not expected_predecessors <= incoming_ids:
            return None
        if incoming_ids - expected_predecessors - {"existing"}:
            return None
        removed_values = tuple(
            value for predecessor, value in incoming if predecessor in previous
        )
        if not removed_values:
            statements.append(statement)
            continue
        if any(
            not _values_semantically_equal(removed_values[0], value)
            for value in removed_values[1:]
        ):
            return None
        replacement_values = tuple(
            value for predecessor, value in incoming if predecessor == replacement
        )
        if replacement_values and not _values_semantically_equal(
            replacement_values[0], removed_values[0]
        ):
            return None
        if len(replacement_values) > 1:
            return None

        merged: list[tuple[str, Expr]] = []
        inserted = False
        for predecessor, value in incoming:
            if predecessor in previous:
                if not inserted and not replacement_values:
                    merged.append((replacement, removed_values[0]))
                    inserted = True
                continue
            merged.append((predecessor, value))
        if replacement_values:
            inserted = True
        if not inserted:
            return None
        statements.append(replace(statement, value=replace(statement.value, incoming=tuple(merged))))
    return replace(block, statements=tuple(statements))


def _expand_phi_predecessor(
    block: BasicBlock,
    *,
    previous: str,
    replacements: tuple[str, ...],
) -> BasicBlock | None:
    """Expand one removed edge label into several equivalent predecessor edges."""

    if not replacements or len(replacements) != len(set(replacements)):
        return None
    statements: list[Stmt] = []
    for statement in block.statements:
        if not isinstance(statement, Assign) or not isinstance(statement.value, Phi):
            statements.append(statement)
            continue
        if len({predecessor for predecessor, _value in statement.value.incoming}) != len(statement.value.incoming):
            return None
        previous_values = tuple(
            value
            for predecessor, value in statement.value.incoming
            if predecessor == previous
        )
        if not previous_values:
            statements.append(statement)
            continue
        if len(previous_values) != 1:
            return None
        previous_value = previous_values[0]
        incoming_by_predecessor = dict(statement.value.incoming)
        for replacement in replacements:
            existing = incoming_by_predecessor.get(replacement)
            if existing is not None and not _values_semantically_equal(
                existing,
                previous_value,
            ):
                return None

        expanded: list[tuple[str, Expr]] = []
        for predecessor, value in statement.value.incoming:
            if predecessor != previous:
                expanded.append((predecessor, value))
                continue
            expanded.extend(
                (replacement, previous_value)
                for replacement in replacements
                if replacement not in incoming_by_predecessor
            )
        statements.append(
            replace(
                statement,
                value=replace(statement.value, incoming=tuple(expanded)),
            )
        )
    return replace(block, statements=tuple(statements))


def _structure_exact_acyclic_shared_linear_block(function: FunctionIR) -> FunctionIR | None:
    """Split a shared linear block so an acyclic branch DAG becomes a tree.

    Ghidra's region collapse can duplicate a side-effecting leaf when several
    mutually exclusive paths converge on that leaf before a common
    continuation.  This is exact for an acyclic CFG: each execution reaches at
    most one clone, so the statements and their order are unchanged.  Phi
    incoming values at the continuation are copied for every new predecessor;
    any Phi ambiguity, exceptional edge, loop, or duplicate case declines the
    rewrite and leaves the original CFG intact.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or find_natural_loops(cfg):
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    block_map = cfg.blocks
    existing_ids = set(block_map)
    for shared in function.blocks:
        if shared.id == cfg.entry or not isinstance(shared.terminator, Jump):
            continue
        predecessors = cfg.predecessors(shared.id)
        if len(predecessors) < 2:
            continue
        if _region_loop_edges_are_reducible(region_graph, frozenset({shared.id})) is False:
            continue
        if any(
            isinstance(statement, Assign)
            and isinstance(statement.target, Var)
            and isinstance(statement.value, Phi)
            for statement in shared.statements
        ):
            continue
        if not shared.statements or _contains_terminal_statement(shared.statements):
            continue
        successor = block_map.get(shared.terminator.target)
        if successor is None or successor.id == shared.id:
            continue
        predecessor_blocks = [block_map.get(predecessor) for predecessor in predecessors]
        if any(block is None or block.exception_edge is not None for block in predecessor_blocks):
            continue
        if any(
            edge.source in predecessors
            and edge.target == shared.id
            and edge.roles & {"exceptional", "irreducible"}
            for edge in region_graph.edges
        ):
            continue

        # Keep the original block for the first incoming edge and create one
        # clone for each remaining edge.  The order follows CFG predecessor
        # order, which is deterministic for build_cfg.
        clone_by_predecessor: dict[str, str] = {}
        for index, predecessor in enumerate(predecessors[1:], start=1):
            clone_id = f"{shared.id}__shared_{index}"
            if clone_id in existing_ids:
                clone_by_predecessor = {}
                break
            clone_by_predecessor[predecessor] = clone_id
        if not clone_by_predecessor:
            continue

        rewritten_blocks: list[BasicBlock] = []
        for block in function.blocks:
            target_id = clone_by_predecessor.get(block.id)
            if target_id is not None:
                rewritten_terminator = _retarget_terminator_target(
                    block.terminator,
                    previous=shared.id,
                    replacement=target_id,
                )
                if rewritten_terminator is None:
                    rewritten_blocks = []
                    break
                rewritten_blocks.append(replace(block, terminator=rewritten_terminator))
            else:
                rewritten_blocks.append(block)
        if not rewritten_blocks:
            continue

        rewritten_successor = _add_shared_block_phi_incomings(
            successor,
            shared_id=shared.id,
            clone_ids=tuple(clone_by_predecessor.values()),
        )
        if rewritten_successor is None:
            continue
        rewritten_blocks = [
            rewritten_successor if block.id == successor.id else block
            for block in rewritten_blocks
        ]
        rewritten_blocks.extend(
            replace(shared, id=clone_id)
            for clone_id in clone_by_predecessor.values()
        )
        split = replace(function, blocks=tuple(rewritten_blocks))
        structured = _structure_exact_acyclic_tree_join_region(split)
        if structured is None:
            continue
        return replace(
            structured,
            metadata={
                **structured.metadata,
                "low_level_cfg_structured": "exact-acyclic-shared-linear-block",
            },
        )
    return None


def _retarget_terminator_target(
    terminator: Terminator | None,
    *,
    previous: str,
    replacement: str,
) -> Terminator | None:
    if isinstance(terminator, Jump):
        return replace(terminator, target=replacement if terminator.target == previous else terminator.target)
    if isinstance(terminator, Branch):
        return replace(
            terminator,
            true_target=replacement if terminator.true_target == previous else terminator.true_target,
            false_target=replacement if terminator.false_target == previous else terminator.false_target,
        )
    if isinstance(terminator, MultiBranch):
        return replace(
            terminator,
            cases=tuple(
                (value, replacement if target == previous else target)
                for value, target in terminator.cases
            ),
            default_target=replacement if terminator.default_target == previous else terminator.default_target,
        )
    return terminator


def _add_shared_block_phi_incomings(
    block: BasicBlock,
    *,
    shared_id: str,
    clone_ids: tuple[str, ...],
) -> BasicBlock | None:
    statements: list[Stmt] = []
    for statement in block.statements:
        if not (isinstance(statement, Assign) and isinstance(statement.value, Phi)):
            statements.append(statement)
            continue
        incoming = dict(statement.value.incoming)
        if shared_id not in incoming:
            return None
        if any(clone_id in incoming for clone_id in clone_ids):
            return None
        rewritten_incoming = [*statement.value.incoming]
        rewritten_incoming.extend((clone_id, incoming[shared_id]) for clone_id in clone_ids)
        statements.append(
            replace(
                statement,
                value=replace(statement.value, incoming=tuple(rewritten_incoming)),
            )
        )
    return replace(block, statements=tuple(statements))


def _structure_exact_acyclic_decision_tree_return(function: FunctionIR) -> FunctionIR | None:
    """Recover a complete acyclic decision tree with one return join.

    This is deliberately a CFG rule, rather than a source-language ``match``
    reconstruction.  Every reachable non-final block must have exactly one
    predecessor (apart from the entry), so the graph is a tree.  Its only
    leaves are edges to one final return block.  Phi nodes at that join are
    made explicit on their incoming edges, but only for independent constant
    or variable copies.  Those constraints make moving the final statements
    after a nested ``If``/``Switch`` tree semantics-preserving.
    """

    if len(function.blocks) < 3:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    final = function.blocks[-1]
    if not isinstance(final.terminator, Return):
        return None
    block_map = cfg.blocks
    if cfg.entry != function.blocks[0].id or final.id == cfg.entry:
        return None
    # A top-level switch whose arms already terminate is an intentional
    # low-level dispatch representation.  Keep it as a Switch with explicit
    # terminal edges; this rule is for decision trees that compute a value and
    # reconverge, not for replacing terminal switch arms with fallthrough.
    entry_block = block_map.get(cfg.entry)
    if entry_block is None or isinstance(entry_block.terminator, MultiBranch):
        return None
    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != set(block_map) or final.id not in reachable:
        return None

    final_predecessors = set(cfg.predecessors(final.id))
    if not final_predecessors:
        return None
    for block_id, block in block_map.items():
        if block_id == final.id:
            continue
        predecessors = cfg.predecessors(block_id)
        if block_id == cfg.entry:
            if predecessors:
                return None
        elif len(predecessors) != 1:
            return None
        if not isinstance(block.terminator, (Jump, Branch, MultiBranch)):
            return None
        if isinstance(block.terminator, Jump):
            if block.terminator.target not in block_map:
                return None
        elif any(target not in block_map for target in _terminator_targets(block.terminator)):
            return None

    # A tree with a final sink cannot contain a cycle.  The recursive renderer
    # below also rejects re-entry, which keeps this proof local and explicit.
    final_copies = _final_join_phi_copies(final, final_predecessors)
    if final_copies is None:
        return None
    edge_copies, final_statements = final_copies

    def block_statements(block: BasicBlock) -> tuple[Stmt, ...] | None:
        predecessors = () if block.id == cfg.entry else cfg.predecessors(block.id)
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=predecessors,
        )
        if statements is None or _statements_contain_break(statements):
            return None
        return statements

    def render_edge(source_id: str, target: str, path: frozenset[str]) -> tuple[Stmt, ...] | None:
        if target == final.id:
            return edge_copies.get(source_id)
        return render_block(target, path)

    def render_block(block_id: str, path: frozenset[str]) -> tuple[Stmt, ...] | None:
        if block_id == final.id or block_id in path:
            return None
        block = block_map.get(block_id)
        if block is None:
            return None
        statements = block_statements(block)
        if statements is None:
            return None
        terminator = block.terminator
        next_path = path | {block_id}
        if isinstance(terminator, Jump):
            child = render_edge(block.id, terminator.target, next_path)
            return None if child is None else (*statements, *child)
        if isinstance(terminator, Branch):
            then_body = render_edge(block.id, terminator.true_target, next_path)
            else_body = render_edge(block.id, terminator.false_target, next_path)
            if then_body is None or else_body is None:
                return None
            return (*statements, If(source=terminator.source, condition=terminator.condition, then_body=then_body, else_body=else_body))
        if isinstance(terminator, MultiBranch):
            cases: list[tuple[Expr, tuple[Stmt, ...]]] = []
            for value, target in terminator.cases:
                if any(_values_semantically_equal(value, existing) for existing, _body in cases):
                    return None
                body = render_edge(block.id, target, next_path)
                if body is None:
                    return None
                cases.append((value, body))
            default_body = render_edge(block.id, terminator.default_target, next_path)
            if default_body is None:
                return None
            return (*statements, Switch(source=terminator.source, selector=terminator.selector, cases=tuple(cases), default_body=default_body))
        return None

    tree = render_block(cfg.entry, frozenset())
    if tree is None:
        return None
    return _rewrite_while_structured_function(
        function,
        (*tree, *final_statements),
        final.terminator,
        "exact-acyclic-decision-tree-return",
    )


def _structure_exact_acyclic_terminal_tree(function: FunctionIR) -> FunctionIR | None:
    """Recover an acyclic single-entry decision tree with terminal leaves.

    Exception checks routinely compile to a branch tree whose failing leaves
    raise while the successful leaf returns.  They have no common return join,
    so the join-based tree rule deliberately leaves them as CFG.  The proof
    here is equally strict: every non-entry block has exactly one predecessor,
    all reachable blocks are in the tree, and leaves can only return or raise.
    Consequently recursively replacing edges with structured bodies neither
    duplicates a side effect nor changes which terminal outcome is selected.
    """

    if len(function.blocks) < 3:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or find_natural_loops(cfg):
        return None
    block_map = cfg.blocks
    if _reachable_block_ids(cfg, cfg.entry) != set(block_map):
        return None
    for block_id, block in block_map.items():
        predecessors = cfg.predecessors(block_id)
        if block_id == cfg.entry:
            if predecessors:
                return None
        elif len(predecessors) != 1:
            return None
        if not isinstance(block.terminator, (Jump, Branch, MultiBranch, Return, type(None))):
            return None

    def render(block_id: str, path: frozenset[str]) -> tuple[Stmt | Terminator, ...] | None:
        if block_id in path:
            return None
        block = block_map.get(block_id)
        if block is None:
            return None
        predecessors = () if block.id == cfg.entry else cfg.predecessors(block.id)
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=predecessors,
        )
        if statements is None or _statements_contain_break(statements):
            return None
        terminator = block.terminator
        next_path = path | {block_id}
        if isinstance(terminator, Return):
            return (*statements, terminator)
        if terminator is None:
            if statements and isinstance(statements[-1], (Raise, Reraise)):
                return statements
            return None
        if isinstance(terminator, Jump):
            child = render(terminator.target, next_path)
            return None if child is None else (*statements, *child)
        if isinstance(terminator, Branch):
            then_body = render(terminator.true_target, next_path)
            else_body = render(terminator.false_target, next_path)
            if then_body is None or else_body is None:
                return None
            return (
                *statements,
                If(
                    source=terminator.source,
                    condition=terminator.condition,
                    then_body=then_body,
                    else_body=else_body,
                ),
            )
        cases: list[tuple[Expr, tuple[Stmt | Terminator, ...]]] = []
        for value, target in terminator.cases:
            if any(_values_semantically_equal(value, existing) for existing, _body in cases):
                return None
            body = render(target, next_path)
            if body is None:
                return None
            cases.append((value, body))
        default_body = render(terminator.default_target, next_path)
        if default_body is None:
            return None
        return (*statements, Switch(source=terminator.source, selector=terminator.selector, cases=tuple(cases), default_body=default_body))

    tree = render(cfg.entry, frozenset())
    if tree is None:
        return None
    return _rewrite_while_structured_function(
        function,
        tree,
        Return(),
        "exact-acyclic-terminal-tree",
    )


def _structure_exact_terminal_branch_region(function: FunctionIR) -> FunctionIR | None:
    """Inline one proven terminal branch subtree and retain its continuation.

    This is the local counterpart to :func:`_structure_exact_acyclic_terminal_tree`.
    It handles guard checks at the edge of a larger CFG (often before a loop):
    one branch may only return or raise, while the other continues through the
    original graph.  Every subtree node must have exactly one incoming CFG
    edge, be acyclic, and have no edge leaving the subtree, so moving it into
    an ``If`` cannot duplicate effects or change a later join's value state.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    natural_loops = find_natural_loops(cfg)
    loop_blocks = frozenset(block_id for loop in natural_loops for block_id in loop.blocks)

    def has_distinct_terminal_returns() -> bool:
        returns = [
            block.terminator
            for block in function.blocks
            if isinstance(block.terminator, Return)
        ]
        if len(returns) < 2:
            return False
        first = returns[0]
        return any(not _returns_semantically_equal(first, candidate) for candidate in returns[1:])

    def render_terminal(
        block_id: str,
        predecessor_id: str,
        path: frozenset[str],
    ) -> tuple[tuple[Stmt | Terminator, ...], frozenset[str]] | None:
        if block_id in path or block_id in loop_blocks:
            return None
        block = block_map.get(block_id)
        if block is None or cfg.predecessors(block_id) != (predecessor_id,):
            return None
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor_id,),
        )
        if statements is None or _statements_contain_break(statements):
            return None
        terminator = block.terminator
        visited = path | {block_id}
        if isinstance(terminator, Return):
            return (*statements, terminator), visited
        if terminator is None:
            if statements and isinstance(statements[-1], (Raise, Reraise)):
                return statements, visited
            return None
        if isinstance(terminator, Jump):
            child = render_terminal(terminator.target, block.id, visited)
            if child is None:
                return None
            body, child_blocks = child
            return (*statements, *body), child_blocks
        if isinstance(terminator, Branch):
            if terminator.true_target == terminator.false_target:
                return None
            then_result = render_terminal(terminator.true_target, block.id, visited)
            else_result = render_terminal(terminator.false_target, block.id, visited)
            if then_result is None or else_result is None:
                return None
            then_body, then_blocks = then_result
            else_body, else_blocks = else_result
            if then_blocks & else_blocks:
                return None
            return (
                (*statements, If(source=terminator.source, condition=terminator.condition, then_body=then_body, else_body=else_body)),
                then_blocks | else_blocks | {block_id},
            )
        targets = (*tuple(target for _value, target in terminator.cases), terminator.default_target)
        if len(set(targets)) != len(targets):
            return None
        cases: list[tuple[Expr, tuple[Stmt | Terminator, ...]]] = []
        case_blocks: set[str] = set()
        for value, target in terminator.cases:
            result = render_terminal(target, block.id, visited)
            if result is None or result[1] & case_blocks:
                return None
            body, rendered_blocks = result
            cases.append((value, body))
            case_blocks.update(rendered_blocks)
        default_result = render_terminal(terminator.default_target, block.id, visited)
        if default_result is None or default_result[1] & case_blocks:
            return None
        default_body, default_blocks = default_result
        return (
            (*statements, Switch(source=terminator.source, selector=terminator.selector, cases=tuple(cases), default_body=default_body)),
            frozenset(case_blocks) | default_blocks | {block_id},
        )

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, Branch) or terminator.true_target == terminator.false_target:
            continue
        for terminal_target, continuation_target, terminal_is_true in (
            (terminator.true_target, terminator.false_target, True),
            (terminator.false_target, terminator.true_target, False),
        ):
            rendered = render_terminal(terminal_target, source.id, frozenset())
            if rendered is None:
                continue
            terminal_body, removed = rendered
            if continuation_target in removed:
                continue
            # Keep a guard before a loop on the CFG floor when the function
            # has distinct terminal values.  Inlining that guard would erase
            # the exit partition needed by a global loop proof.
            if natural_loops and has_distinct_terminal_returns():
                continue
            conditional = If(
                source=terminator.source,
                condition=terminator.condition,
                then_body=terminal_body if terminal_is_true else (),
                else_body=() if terminal_is_true else terminal_body,
            )
            rewritten_source = BasicBlock(
                id=source.id,
                statements=(*source.statements, conditional),
                terminator=Jump(source=terminator.source, target=continuation_target),
            )
            return FunctionIR(
                name=function.name,
                params=function.params,
                blocks=tuple(
                    rewritten_source if block.id == source.id else block
                    for block in function.blocks
                    if block.id not in removed
                ),
                nested_functions=function.nested_functions,
                source=function.source,
                recovery_kind=function.recovery_kind,
                metadata={
                    **function.metadata,
                    "low_level_cfg_structured": "exact-terminal-branch-region",
                },
            )
    return None


def _structure_exact_terminal_guarded_loop_region(function: FunctionIR) -> FunctionIR | None:
    """Nest a proven terminal guard around one complete natural loop.

    The generic shape is ``entry -> terminal`` or ``entry -> setup -> loop``.
    The setup path is a single-predecessor chain, and the loop is the exact
    terminal-body form handled by :func:`_structure_exact_loop_with_terminal_body_branch`.
    This is the composition of two ordinary region collapses: no block is
    duplicated, and the outer terminal tree and inner loop must cover every
    reachable block exactly once.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    if cfg.entry != function.blocks[0].id:
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    loop_blocks = frozenset(loop.blocks)
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, loop_blocks):
        return None

    block_map = cfg.blocks
    header = block_map.get(loop.header)
    source = block_map.get(cfg.entry)
    if (
        header is None
        or source is None
        or not isinstance(source.terminator, Branch)
        or not isinstance(header.terminator, Branch)
        or source.terminator.true_target == source.terminator.false_target
    ):
        return None

    loop_preheaders = tuple(
        predecessor
        for predecessor in cfg.predecessors(header.id)
        if predecessor not in loop_blocks
    )
    if len(loop_preheaders) != 1:
        return None
    loop_preheader_id = loop_preheaders[0]
    loop_preheader = block_map.get(loop_preheader_id)
    if loop_preheader is None:
        return None

    true_in_loop = header.terminator.true_target in loop_blocks
    false_in_loop = header.terminator.false_target in loop_blocks
    if true_in_loop == false_in_loop:
        return None

    def render_terminal(
        start: str,
        predecessor_id: str,
        path: frozenset[str],
    ) -> tuple[tuple[Stmt | Terminator, ...], frozenset[str]] | None:
        """Render one acyclic terminal tree with exact edge ownership."""

        if start in path or start in loop_blocks:
            return None
        block = block_map.get(start)
        if block is None or cfg.predecessors(start) != (predecessor_id,):
            return None
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor_id,),
        )
        if (
            statements is None
            or _contains_terminal_statement(statements)
            or _contains_unscoped_loop_control(statements)
        ):
            return None
        visited = path | {start}
        terminator = block.terminator
        if isinstance(terminator, (Return, Raise, Reraise)):
            return (*statements, terminator), frozenset({start})
        if terminator is None:
            if statements and isinstance(statements[-1], (Raise, Reraise)):
                return statements, frozenset({start})
            return None
        if isinstance(terminator, Jump):
            child = render_terminal(terminator.target, start, visited)
            if child is None:
                return None
            child_body, child_nodes = child
            return (*statements, *child_body), frozenset({start}) | child_nodes
        if isinstance(terminator, Branch):
            if terminator.true_target == terminator.false_target:
                return None
            then_result = render_terminal(terminator.true_target, start, visited)
            else_result = render_terminal(terminator.false_target, start, visited)
            if then_result is None or else_result is None:
                return None
            then_body, then_nodes = then_result
            else_body, else_nodes = else_result
            if then_nodes & else_nodes:
                return None
            return (
                *statements,
                If(
                    source=terminator.source,
                    condition=terminator.condition,
                    then_body=then_body,
                    else_body=else_body,
                ),
            ), frozenset({start}) | then_nodes | else_nodes
        if not isinstance(terminator, MultiBranch):
            return None
        targets = tuple(target for _value, target in terminator.cases) + (
            terminator.default_target,
        )
        if not targets or len(set(targets)) != len(targets):
            return None
        cases: list[tuple[Expr, tuple[Stmt | Terminator, ...]]] = []
        used_nodes: set[str] = set()
        for value, target in terminator.cases:
            if any(_values_semantically_equal(value, existing) for existing, _body in cases):
                return None
            result = render_terminal(target, start, visited)
            if result is None:
                return None
            body, nodes = result
            if used_nodes & set(nodes):
                return None
            used_nodes.update(nodes)
            cases.append((value, body))
        default_result = render_terminal(terminator.default_target, start, visited)
        if default_result is None:
            return None
        default_body, default_nodes = default_result
        if used_nodes & set(default_nodes):
            return None
        return (
            *statements,
            Switch(
                source=terminator.source,
                selector=terminator.selector,
                cases=tuple(cases),
                default_body=default_body,
            ),
        ), frozenset({start}) | frozenset(used_nodes) | default_nodes

    def render_setup(
        start: str,
    ) -> tuple[tuple[Stmt, ...], frozenset[str]] | None:
        """Render the single-entry setup chain ending at the loop preheader."""

        statements: list[Stmt] = []
        nodes: set[str] = set()
        current = start
        predecessor = source.id
        while True:
            if current in loop_blocks or current in nodes:
                return None
            block = block_map.get(current)
            if block is None or cfg.predecessors(current) != (predecessor,):
                return None
            block_statements = _statements_without_trivial_phi_assignments(
                block.statements,
                incoming_blocks=(predecessor,),
            )
            if (
                block_statements is None
                or _contains_terminal_statement(block_statements)
                or _contains_unscoped_loop_control(block_statements)
            ):
                return None
            statements.extend(block_statements)
            nodes.add(current)
            if current == loop_preheader_id:
                if not isinstance(block.terminator, Jump) or block.terminator.target != header.id:
                    return None
                return tuple(statements), frozenset(nodes)
            if not isinstance(block.terminator, Jump):
                return None
            predecessor, current = current, block.terminator.target

    source_statements = _statements_without_trivial_phi_assignments(
        source.statements,
        incoming_blocks=(),
    )
    if (
        source_statements is None
        or _contains_terminal_statement(source_statements)
        or _contains_unscoped_loop_control(source_statements)
    ):
        return None

    for terminal_is_true in (True, False):
        terminal_target = (
            source.terminator.true_target
            if terminal_is_true
            else source.terminator.false_target
        )
        setup_target = (
            source.terminator.false_target
            if terminal_is_true
            else source.terminator.true_target
        )
        terminal_result = render_terminal(
            terminal_target,
            source.id,
            frozenset({source.id}),
        )
        setup_result = render_setup(setup_target)
        if terminal_result is None or setup_result is None:
            continue
        terminal_body, terminal_nodes = terminal_result
        setup_statements, setup_nodes = setup_result
        if terminal_nodes & setup_nodes:
            continue

        inner_reachable = _reachable_block_ids(cfg, loop_preheader_id)
        if (
            source.id in inner_reachable
            or terminal_nodes & inner_reachable
            or not inner_reachable <= set(block_map)
        ):
            continue
        reachable = _reachable_block_ids(cfg, cfg.entry)
        if reachable != set(block_map):
            continue
        if reachable != {source.id} | set(terminal_nodes) | set(inner_reachable):
            continue

        inner_preheader = replace(loop_preheader, statements=setup_statements)
        inner_blocks = (inner_preheader,) + tuple(
            block
            for block in function.blocks
            if block.id in inner_reachable and block.id != loop_preheader_id
        )
        if {block.id for block in inner_blocks} != set(inner_reachable):
            continue
        inner_function = replace(function, blocks=inner_blocks)
        structured_inner = _structure_exact_loop_with_terminal_body_branch(inner_function)
        if structured_inner is None or len(structured_inner.blocks) != 1:
            continue
        inner_block = structured_inner.blocks[0]
        if not isinstance(inner_block.terminator, (Return, Raise, Reraise)):
            continue
        inner_body = (*inner_block.statements, inner_block.terminator)
        conditional = If(
            source=source.terminator.source,
            condition=source.terminator.condition,
            then_body=terminal_body if terminal_is_true else inner_body,
            else_body=inner_body if terminal_is_true else terminal_body,
        )
        return _rewrite_while_structured_function(
            function,
            (*source_statements, conditional),
            None,
            "exact-terminal-guarded-loop-region",
        )
    return None


def _final_join_phi_copies(
    final: BasicBlock,
    predecessors: set[str],
) -> tuple[dict[str, tuple[Stmt, ...]], tuple[Stmt, ...]] | None:
    """Materialize independent leading phi copies at one common join."""

    copies: dict[str, list[Stmt]] = {predecessor: [] for predecessor in predecessors}
    targets: set[str] = set()
    remaining: list[Stmt] = []
    saw_non_phi = False
    for statement in final.statements:
        if isinstance(statement, Assign) and isinstance(statement.target, Var) and isinstance(statement.value, Phi):
            if saw_non_phi:
                return None
            incoming = dict(statement.value.incoming)
            if not predecessors <= set(incoming) or set(incoming) - predecessors - {"existing"}:
                return None
            values = {predecessor: incoming[predecessor] for predecessor in predecessors}
            if not all(isinstance(value, (Const, Var)) for value in values.values()):
                return None
            existing = incoming.get("existing")
            if existing is not None and not any(
                _values_semantically_equal(existing, value) for value in values.values()
            ):
                return None
            targets.add(statement.target.name)
            for predecessor, value in values.items():
                if not (isinstance(value, Var) and value.name == statement.target.name):
                    copies[predecessor].append(
                        Assign(source=statement.source, target=statement.target, value=value)
                    )
            continue
        saw_non_phi = True
        remaining.append(statement)
    # A prior lossless normalizer may already have removed all join phi nodes
    # as identity copies.  The tree proof still holds in that case: there is
    # simply no edge-local assignment to materialize.
    if not targets:
        return {predecessor: () for predecessor in predecessors}, tuple(remaining)
    copy_inputs = {
        value.name
        for edge_copies in copies.values()
        for copy in edge_copies
        if isinstance(copy, Assign) and isinstance(copy.value, Var)
        for value in (copy.value,)
    }
    if targets & copy_inputs:
        return None
    return {predecessor: tuple(edge_copies) for predecessor, edge_copies in copies.items()}, tuple(remaining)


def _structure_exact_guard_return_cascade(function: FunctionIR) -> FunctionIR | None:
    """Recover a complete linear cascade of branch-local returns.

    Return is valid in a structured statement body and preserves both the
    selected arm's effects and its value.  The rule accepts only a complete
    function whose false edges advance through the physical block sequence,
    excluding joins, loops, and frontend-specific dispatch conventions.
    """

    if len(function.blocks) < 3:
        return None
    final_block = function.blocks[-1]
    if not isinstance(final_block.terminator, Return):
        return None

    entries: list[tuple[BasicBlock, tuple[Stmt, ...], tuple[Stmt, ...], Return]] = []
    index = 0
    previous_id: str | None = None
    while index + 2 <= len(function.blocks) - 1:
        condition_block = function.blocks[index]
        arm = function.blocks[index + 1]
        if not isinstance(condition_block.terminator, Branch):
            return None
        if (
            condition_block.terminator.true_target != arm.id
            or condition_block.terminator.false_target != function.blocks[index + 2].id
        ):
            return None
        if not isinstance(arm.terminator, Return):
            return None
        condition_statements = _statements_without_trivial_phi_assignments(
            condition_block.statements,
            incoming_blocks=() if previous_id is None else (previous_id,),
        )
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(condition_block.id,),
        )
        if condition_statements is None or arm_statements is None:
            return None
        entries.append((condition_block, condition_statements, arm_statements, arm.terminator))
        previous_id = condition_block.id
        index += 2

    if not entries or function.blocks[index].id != final_block.id:
        return None
    final_statements = _statements_without_trivial_phi_assignments(
        final_block.statements,
        incoming_blocks=(previous_id,) if previous_id is not None else (),
    )
    if final_statements is None:
        return None

    continuation: tuple[Stmt, ...] = final_statements
    for condition_block, condition_statements, arm_statements, arm_return in reversed(entries):
        continuation = (
            *condition_statements,
            If(
                condition=condition_block.terminator.condition,
                then_body=(*arm_statements, arm_return),
                else_body=continuation,
            ),
        )
    return _rewrite_while_structured_function(
        function,
        continuation,
        final_block.terminator,
        "exact-guard-return-cascade",
    )


def _structure_exact_pretested_loop_with_posttested_inner_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover a pretested loop containing a single posttested inner loop."""

    if len(function.blocks) != 6:
        return None
    setup, outer_header, outer_body, inner_header, outer_latch, exit_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != outer_header.id:
        return None
    if not isinstance(outer_header.terminator, Branch) or (
        outer_header.terminator.true_target,
        outer_header.terminator.false_target,
    ) != (outer_body.id, exit_block.id):
        return None
    if not isinstance(outer_body.terminator, Jump) or outer_body.terminator.target != inner_header.id:
        return None
    if not isinstance(inner_header.terminator, Branch) or (
        inner_header.terminator.true_target,
        inner_header.terminator.false_target,
    ) != (outer_latch.id, inner_header.id):
        return None
    if not isinstance(outer_latch.terminator, Jump) or outer_latch.terminator.target != outer_header.id:
        return None
    if exit_block.statements or not isinstance(exit_block.terminator, Return):
        return None

    outer_header_statements = _statements_without_trivial_phi_assignments(
        outer_header.statements,
        incoming_blocks=(setup.id, outer_latch.id),
    )
    outer_body_statements = _statements_without_trivial_phi_assignments(
        outer_body.statements,
        incoming_blocks=(outer_header.id,),
    )
    inner_header_statements = _statements_without_trivial_phi_assignments(
        inner_header.statements,
        incoming_blocks=(outer_body.id, inner_header.id),
    )
    outer_latch_statements = _statements_without_trivial_phi_assignments(
        outer_latch.statements,
        incoming_blocks=(inner_header.id,),
    )
    if any(
        statements is None
        for statements in (outer_header_statements, outer_body_statements, inner_header_statements, outer_latch_statements)
    ):
        return None

    inner_loop = While(
        condition=Const(value=True),
        body=(
            *inner_header_statements,
            If(condition=inner_header.terminator.condition, then_body=(Break(),)),
        ),
    )
    outer_loop = While(
        condition=outer_header.terminator.condition,
        body=(
            *outer_header_statements,
            *outer_body_statements,
            inner_loop,
            *outer_latch_statements,
        ),
    )
    return _rewrite_while_structured_function(
        function,
        (*setup.statements, outer_loop),
        exit_block.terminator,
        "exact-pretested-loop-with-posttested-inner-loop",
    )


def _structure_exact_optional_pretested_body_if_loop(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 13:
        return None
    (
        entry,
        setup,
        loop_header,
        then_block,
        loop_join,
        latch,
        loop_exit,
        first_return_jump,
        first_return,
        second_return_jump,
        second_return,
        else_return_jump,
        else_return,
    ) = function.blocks
    if not isinstance(entry.terminator, Branch):
        return None
    if entry.terminator.true_target != setup.id or entry.terminator.false_target != else_return_jump.id:
        return None
    if not isinstance(setup.terminator, Branch):
        return None
    if setup.terminator.true_target != loop_header.id or setup.terminator.false_target != loop_exit.id:
        return None
    if not isinstance(loop_header.terminator, Branch):
        return None
    if loop_header.terminator.true_target != then_block.id or loop_header.terminator.false_target != loop_join.id:
        return None
    if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != loop_join.id:
        return None
    if not isinstance(loop_join.terminator, Branch):
        return None
    if loop_join.terminator.true_target != latch.id or loop_join.terminator.false_target != loop_exit.id:
        return None
    if not _expr_semantically_equal(setup.terminator.condition, loop_join.terminator.condition):
        return None
    if latch.statements or not isinstance(latch.terminator, Jump) or latch.terminator.target != loop_header.id:
        return None
    if not isinstance(loop_exit.terminator, Branch):
        return None
    if loop_exit.terminator.true_target != first_return_jump.id:
        return None
    if loop_exit.terminator.false_target != second_return_jump.id:
        return None

    first_value = _jump_to_single_return_value(first_return_jump, first_return)
    second_value = _jump_to_single_return_value(second_return_jump, second_return)
    else_value = _jump_to_single_return_value(else_return_jump, else_return)
    if first_value is None or second_value is None or else_value is None:
        return None

    header_statements = _statements_without_trivial_phi_assignments(
        loop_header.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    join_statements = _statements_without_trivial_phi_assignments(
        loop_join.statements,
        incoming_blocks=(loop_header.id, then_block.id),
    )
    exit_statements = _statements_without_trivial_phi_assignments(
        loop_exit.statements,
        incoming_blocks=(setup.id, loop_join.id),
    )
    if header_statements is None or join_statements is None or exit_statements is None:
        return None

    return_name = _fresh_var_name(function, "structured_return_value")
    loop_body = (
        *header_statements,
        If(condition=loop_header.terminator.condition, then_body=then_block.statements),
        *join_statements,
    )
    statements = (
        *entry.statements,
        Assign(target=Var(name=return_name), value=Const(value=None)),
        If(
            condition=entry.terminator.condition,
            then_body=(
                *setup.statements,
                While(condition=setup.terminator.condition, body=loop_body),
                *exit_statements,
                If(
                    condition=loop_exit.terminator.condition,
                    then_body=(Assign(target=Var(name=return_name), value=first_value),),
                    else_body=(Assign(target=Var(name=return_name), value=second_value),),
                ),
            ),
            else_body=(Assign(target=Var(name=return_name), value=else_value),),
        ),
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=entry.id,
                statements=statements,
                terminator=Return(source=second_return.terminator.source, values=(Var(name=return_name),)),
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-optional-pretested-body-if-loop",
        },
    )


def _structure_exact_pretested_loop_with_latch_and_raise_tail(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 8:
        return None
    setup, body, latch, exit_block, raise_value, raise_block, return_value, final_block = function.blocks
    if not isinstance(setup.terminator, Branch):
        return None
    if setup.terminator.true_target != body.id or setup.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Branch):
        return None
    if body.terminator.true_target != latch.id or body.terminator.false_target != exit_block.id:
        return None
    if not _expr_semantically_equal(setup.terminator.condition, body.terminator.condition):
        return None
    if latch.statements or not isinstance(latch.terminator, Jump) or latch.terminator.target != body.id:
        return None
    if not isinstance(exit_block.terminator, Branch):
        return None
    if exit_block.terminator.true_target != raise_value.id or exit_block.terminator.false_target != return_value.id:
        return None
    if not isinstance(raise_value.terminator, Jump) or raise_value.terminator.target != raise_block.id:
        return None
    if raise_block.terminator is not None or len(raise_block.statements) != 1:
        return None
    if not isinstance(raise_block.statements[0], Raise):
        return None
    if not isinstance(return_value.terminator, Jump) or return_value.terminator.target != final_block.id:
        return None
    if not isinstance(final_block.terminator, Return) or final_block.statements:
        return None

    statements = (
        *setup.statements,
        While(condition=setup.terminator.condition, body=body.statements),
        If(
            condition=exit_block.terminator.condition,
            then_body=(*raise_value.statements, *raise_block.statements),
        ),
        *return_value.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-pretested-loop-with-latch-and-raise-tail",
        },
    )


def _structure_exact_or_guard_loop_with_two_latches(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 8:
        return None
    setup, alternate_test, body, body_latch, after_body_test, after_body_latch, exit_block, final_block = function.blocks
    if not isinstance(setup.terminator, Branch):
        return None
    if setup.terminator.true_target != alternate_test.id or setup.terminator.false_target != body.id:
        return None
    if alternate_test.statements or not isinstance(alternate_test.terminator, Branch):
        return None
    if alternate_test.terminator.true_target != body.id or alternate_test.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Branch):
        return None
    if body.terminator.true_target != body_latch.id or body.terminator.false_target != after_body_test.id:
        return None
    if body_latch.statements or not isinstance(body_latch.terminator, Jump) or body_latch.terminator.target != body.id:
        return None
    if after_body_test.statements or not isinstance(after_body_test.terminator, Branch):
        return None
    if after_body_test.terminator.true_target != after_body_latch.id:
        return None
    if after_body_test.terminator.false_target != exit_block.id:
        return None
    if after_body_latch.statements:
        return None
    if not isinstance(after_body_latch.terminator, Jump) or after_body_latch.terminator.target != body.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if not isinstance(final_block.terminator, Return) or final_block.statements:
        return None
    if not _condition_is_false_comparison_of(setup.terminator.condition, body.terminator.condition):
        return None
    if not _expr_semantically_equal(alternate_test.terminator.condition, after_body_test.terminator.condition):
        return None

    loop_body = (
        If(
            condition=setup.terminator.condition,
            then_body=(
                If(
                    condition=UnaryOp(op="not ", value=alternate_test.terminator.condition),
                    then_body=(Break(),),
                ),
            ),
        ),
        *body.statements,
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-or-guard-loop-with-two-latches",
        },
    )


def _structure_exact_and_guard_loop_with_duplicate_return_exits(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 9:
        return None
    (
        first_test,
        second_test,
        body,
        after_body_second_test,
        latch,
        exit_after_second,
        exit_after_first,
        exit_second,
        exit_first,
    ) = function.blocks
    if first_test.statements or not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != second_test.id or first_test.terminator.false_target != exit_first.id:
        return None
    if second_test.statements or not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != body.id or second_test.terminator.false_target != exit_second.id:
        return None
    if not isinstance(body.terminator, Branch):
        return None
    if body.terminator.true_target != after_body_second_test.id:
        return None
    if body.terminator.false_target != exit_after_first.id:
        return None
    if after_body_second_test.statements or not isinstance(after_body_second_test.terminator, Branch):
        return None
    if after_body_second_test.terminator.true_target != latch.id:
        return None
    if after_body_second_test.terminator.false_target != exit_after_second.id:
        return None
    if latch.statements or not isinstance(latch.terminator, Jump) or latch.terminator.target != body.id:
        return None
    if not _expr_semantically_equal(first_test.terminator.condition, body.terminator.condition):
        return None
    if not _expr_semantically_equal(second_test.terminator.condition, after_body_second_test.terminator.condition):
        return None
    exit_blocks = (exit_after_second, exit_after_first, exit_second, exit_first)
    if any(exit_block.statements for exit_block in exit_blocks):
        return None
    if not all(isinstance(exit_block.terminator, Return) for exit_block in exit_blocks):
        return None
    final_return = exit_first.terminator
    if not all(_returns_semantically_equal(final_return, exit_block.terminator) for exit_block in exit_blocks):
        return None

    loop_body = (
        If(
            condition=UnaryOp(op="not ", value=first_test.terminator.condition),
            then_body=(Break(),),
        ),
        If(
            condition=UnaryOp(op="not ", value=second_test.terminator.condition),
            then_body=(Break(),),
        ),
        *body.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=first_test.id,
                statements=(While(condition=Const(value=True), body=loop_body),),
                terminator=final_return,
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-and-guard-loop-with-duplicate-return-exits",
        },
    )


def _structure_exact_short_circuit_phi_loop(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 7:
        return None
    setup, body, first_test, second_true, second_false, join_block, exit_block = function.blocks
    if setup.statements or not isinstance(setup.terminator, Jump) or setup.terminator.target != first_test.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != first_test.id:
        return None
    if first_test.statements or not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != second_true.id or first_test.terminator.false_target != second_false.id:
        return None
    if len(second_true.statements) != 1:
        return None
    second_assign = second_true.statements[0]
    if not isinstance(second_assign, Assign) or not isinstance(second_assign.target, Var):
        return None
    if not isinstance(second_true.terminator, Jump) or second_true.terminator.target != join_block.id:
        return None
    if second_false.statements or not isinstance(second_false.terminator, Jump):
        return None
    if second_false.terminator.target != join_block.id:
        return None
    if len(join_block.statements) != 1:
        return None
    join_assign = join_block.statements[0]
    if not _is_short_circuit_bool_phi(join_assign, second_true.id, second_assign.target.name, second_false.id):
        return None
    if not isinstance(join_block.terminator, Branch):
        return None
    if join_block.terminator.true_target != exit_block.id or join_block.terminator.false_target != body.id:
        return None
    second_break_condition = _condition_with_temp_value(
        join_block.terminator.condition,
        join_assign.target.name,
        Var(name=second_assign.target.name),
    )
    if second_break_condition is None:
        return None
    if exit_block.statements or not isinstance(exit_block.terminator, Return):
        return None

    loop_body = (
        If(
            condition=UnaryOp(op="not ", value=first_test.terminator.condition),
            then_body=(Break(),),
        ),
        *second_true.statements,
        If(condition=second_break_condition, then_body=(Break(),)),
        *body.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=setup.id,
                statements=(While(condition=Const(value=True), body=loop_body),),
                terminator=exit_block.terminator,
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-short-circuit-phi-loop",
        },
    )


def _structure_exact_dual_condition_loop_with_exit_jump(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 7:
        return None
    setup, first_test, first_body, second_test, second_body, exit_block, final_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != first_test.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != exit_block.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != first_test.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None

    loop_body = (
        *first_test.statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(Break(),),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-dual-condition-loop-with-exit-jump",
        },
    )


def _structure_exact_dual_condition_loop_with_latch(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 8:
        return None
    setup, first_test, first_body, second_test, second_body, exit_block, final_block, latch = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != latch.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != exit_block.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != latch.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if not isinstance(latch.terminator, Jump) or latch.terminator.target != first_test.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None
    first_test_statements = _statements_without_trivial_phi_assignments(
        first_test.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    if first_test_statements is None or not _block_has_only_trivial_phi_assignments(
        latch,
        incoming_blocks=(first_body.id, second_body.id),
    ):
        return None

    loop_body = (
        *first_test_statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(Break(),),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-dual-condition-loop-with-latch",
        },
    )


def _structure_exact_dual_condition_loop_with_preheader_latch(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 10:
        return None
    (
        setup,
        first_test,
        first_body,
        second_test,
        second_body,
        exit_assign,
        latch,
        preheader,
        exit_jump,
        final_block,
    ) = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != preheader.id:
        return None
    if not isinstance(preheader.terminator, Jump) or preheader.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != latch.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != exit_assign.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != latch.id:
        return None
    if not isinstance(latch.terminator, Jump) or latch.terminator.target != preheader.id:
        return None
    if not isinstance(exit_assign.terminator, Jump) or exit_assign.terminator.target != exit_jump.id:
        return None
    if exit_jump.statements or not isinstance(exit_jump.terminator, Jump) or exit_jump.terminator.target != final_block.id:
        return None
    if final_block.statements or not isinstance(final_block.terminator, Return):
        return None
    preheader_statements = _statements_without_trivial_phi_assignments(
        preheader.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    latch_statements = _statements_without_trivial_phi_assignments(
        latch.statements,
        incoming_blocks=(first_body.id, second_body.id),
    )
    if preheader_statements is None or latch_statements != ():
        return None

    loop_body = (
        *preheader_statements,
        *first_test.statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(Break(),),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_assign.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-dual-condition-loop-with-preheader-latch",
        },
    )


def _structure_exact_triple_condition_loop_with_exit_jump(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 9:
        return None
    (
        setup,
        first_test,
        first_body,
        second_test,
        second_body,
        third_test,
        third_body,
        exit_block,
        final_block,
    ) = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != first_test.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != third_test.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != first_test.id:
        return None
    if not isinstance(third_test.terminator, Branch):
        return None
    if third_test.terminator.true_target != third_body.id or third_test.terminator.false_target != exit_block.id:
        return None
    if not isinstance(third_body.terminator, Jump) or third_body.terminator.target != first_test.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None

    loop_body = (
        *first_test.statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(
                        *third_test.statements,
                        If(
                            condition=third_test.terminator.condition,
                            then_body=third_body.statements,
                            else_body=(Break(),),
                        ),
                    ),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-triple-condition-loop-with-exit-jump",
        },
    )


def _structure_exact_triple_condition_loop_with_latch(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 10:
        return None
    (
        setup,
        first_test,
        first_body,
        second_test,
        second_body,
        third_test,
        third_body,
        exit_block,
        final_block,
        latch,
    ) = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != latch.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != third_test.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != latch.id:
        return None
    if not isinstance(third_test.terminator, Branch):
        return None
    if third_test.terminator.true_target != third_body.id or third_test.terminator.false_target != exit_block.id:
        return None
    if not isinstance(third_body.terminator, Jump) or third_body.terminator.target != latch.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if not isinstance(latch.terminator, Jump) or latch.terminator.target != first_test.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None
    first_test_statements = _statements_without_trivial_phi_assignments(
        first_test.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    if first_test_statements is None or not _block_has_only_trivial_phi_assignments(
        latch,
        incoming_blocks=(first_body.id, second_body.id, third_body.id),
    ):
        return None

    loop_body = (
        *first_test_statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(
                        *third_test.statements,
                        If(
                            condition=third_test.terminator.condition,
                            then_body=third_body.statements,
                            else_body=(Break(),),
                        ),
                    ),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-triple-condition-loop-with-latch",
        },
    )


def _structure_exact_triple_condition_loop_with_preheader_latch(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 12:
        return None
    (
        setup,
        first_test,
        first_body,
        second_test,
        second_body,
        third_test,
        third_body,
        exit_assign,
        latch,
        preheader,
        exit_jump,
        final_block,
    ) = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != preheader.id:
        return None
    if not isinstance(preheader.terminator, Jump) or preheader.terminator.target != first_test.id:
        return None
    if not isinstance(first_test.terminator, Branch):
        return None
    if first_test.terminator.true_target != first_body.id or first_test.terminator.false_target != second_test.id:
        return None
    if not isinstance(first_body.terminator, Jump) or first_body.terminator.target != latch.id:
        return None
    if not isinstance(second_test.terminator, Branch):
        return None
    if second_test.terminator.true_target != second_body.id or second_test.terminator.false_target != third_test.id:
        return None
    if not isinstance(second_body.terminator, Jump) or second_body.terminator.target != latch.id:
        return None
    if not isinstance(third_test.terminator, Branch):
        return None
    if third_test.terminator.true_target != third_body.id or third_test.terminator.false_target != exit_assign.id:
        return None
    if not isinstance(third_body.terminator, Jump) or third_body.terminator.target != latch.id:
        return None
    if not isinstance(latch.terminator, Jump) or latch.terminator.target != preheader.id:
        return None
    if not isinstance(exit_assign.terminator, Jump) or exit_assign.terminator.target != exit_jump.id:
        return None
    if exit_jump.statements or not isinstance(exit_jump.terminator, Jump) or exit_jump.terminator.target != final_block.id:
        return None
    if final_block.statements or not isinstance(final_block.terminator, Return):
        return None
    preheader_statements = _statements_without_trivial_phi_assignments(
        preheader.statements,
        incoming_blocks=(setup.id, latch.id),
    )
    latch_statements = _statements_without_trivial_phi_assignments(
        latch.statements,
        incoming_blocks=(first_body.id, second_body.id, third_body.id),
    )
    if preheader_statements is None or latch_statements != ():
        return None

    loop_body = (
        *preheader_statements,
        *first_test.statements,
        If(
            condition=first_test.terminator.condition,
            then_body=first_body.statements,
            else_body=(
                *second_test.statements,
                If(
                    condition=second_test.terminator.condition,
                    then_body=second_body.statements,
                    else_body=(
                        *third_test.statements,
                        If(
                            condition=third_test.terminator.condition,
                            then_body=third_body.statements,
                            else_body=(Break(),),
                        ),
                    ),
                ),
            ),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_assign.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-triple-condition-loop-with-preheader-latch",
        },
    )


def _structure_exact_header_body_if_join_loop(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 7:
        return None
    setup, header, body_if, then_block, join_block, exit_block, final_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != header.id:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target != body_if.id or header.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body_if.terminator, Branch):
        return None
    if body_if.terminator.true_target != then_block.id or body_if.terminator.false_target != join_block.id:
        return None
    if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != join_block.id:
        return None
    if not isinstance(join_block.terminator, Jump) or join_block.terminator.target != header.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None

    loop_body = (
        *header.statements,
        If(
            condition=header.terminator.condition,
            then_body=(
                *body_if.statements,
                If(
                    condition=body_if.terminator.condition,
                    then_body=then_block.statements,
                ),
                *join_block.statements,
            ),
            else_body=(Break(),),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
        *final_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-header-body-if-join-loop",
        },
    )


def _structure_exact_header_effect_body_if(function: FunctionIR) -> FunctionIR | None:
    if len(function.blocks) != 7:
        return None
    setup, body_if, else_block, then_block, header, exit_block, final_block = function.blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != header.id:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target != body_if.id or header.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body_if.terminator, Branch):
        return None
    if body_if.terminator.true_target != then_block.id or body_if.terminator.false_target != else_block.id:
        return None
    if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != header.id:
        return None
    if not isinstance(else_block.terminator, Jump) or else_block.terminator.target != header.id:
        return None
    if not isinstance(exit_block.terminator, Jump) or exit_block.terminator.target != final_block.id:
        return None
    if _terminator_targets_any_original_block(final_block.terminator, function):
        return None

    inner_condition = _extract_condition_from_body_if(
        body_if,
        later_paths=(
            ((*then_block.statements, *header.statements), header.terminator),
            ((*else_block.statements, *header.statements), header.terminator),
            (exit_block.statements, final_block.terminator),
        ),
    )
    if inner_condition is None:
        return None
    condition_prefix, condition = inner_condition

    loop_body = (
        *header.statements,
        If(
            condition=header.terminator.condition,
            then_body=(
                *condition_prefix,
                If(
                    condition=condition,
                    then_body=then_block.statements,
                    else_body=else_block.statements,
                ),
            ),
            else_body=(Break(),),
        ),
    )
    statements = (
        *setup.statements,
        While(condition=Const(value=True), body=loop_body),
        *exit_block.statements,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(BasicBlock(id=setup.id, statements=statements, terminator=final_block.terminator),),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-header-effect-body-if",
        },
    )


def _structure_exact_single_natural_loop(function: FunctionIR) -> FunctionIR | None:
    """Structure one non-nested natural loop with an acyclic body.

    This is intentionally language-neutral. The proof uses only CFG topology:
    one preheader, one loop header, one outside exit, no nested backedge, and
    body paths that terminate only at the header or the exit. A branch whose
    two arms are empty is rejected because preserving its condition evaluation
    would not improve the low-level representation and is easy to misread.
    """

    if len(function.blocks) < 4:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    loops = find_natural_loops(cfg)
    candidates = [
        loop
        for loop in loops
        if not any(
            other.header != loop.header
            and other.blocks < loop.blocks
            for other in loops
        )
    ]
    for loop in sorted(candidates, key=lambda item: len(item.blocks)):
        structured = _try_structure_single_natural_loop(function, cfg, loop)
        if structured is not None:
            return structured
    return None


def _structure_exact_terminal_natural_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover a loop with a direct terminal return on its unique backedge path.

    A number of VM lowerings place the loop-exit return in a shared join block
    and represent the non-return path as an unconditional jump back to the
    header.  The CFG has no ordinary exit edge in that form because the return
    is already a statement-level terminal.  This reducer accepts only the
    fully linear, single-entry shape and materializes the branch-arm phi copies
    before the shared return test.  Any additional entry, branch, or terminal
    shape remains on the explicit CFG floor.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if loop.exits:
            continue
        header = block_map.get(loop.header)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        predecessors = cfg.predecessors(header.id)
        preheaders = tuple(pred for pred in predecessors if pred not in loop.blocks)
        if len(preheaders) != 1:
            continue
        preheader = block_map.get(preheaders[0])
        if (
            preheader is None
            or not isinstance(preheader.terminator, Jump)
            or preheader.terminator.target != header.id
            or _contains_unscoped_loop_control(preheader.statements)
        ):
            continue
        # This reducer consumes the complete function region.  A surrounding
        # continuation would require a distinct exit proof and is rejected.
        if {block.id for block in function.blocks} != set(loop.blocks) | {preheader.id}:
            continue
        if not _region_loop_edges_are_reducible(region_graph, frozenset(loop.blocks)):
            continue
        true_target = header.terminator.true_target
        false_target = header.terminator.false_target
        if true_target == false_target or true_target not in loop.blocks or false_target not in loop.blocks:
            continue
        true_block = block_map.get(true_target)
        false_block = block_map.get(false_target)
        if true_block is None or false_block is None:
            continue
        if not isinstance(true_block.terminator, Jump) or not isinstance(false_block.terminator, Jump):
            continue
        if true_block.terminator.target != false_block.terminator.target:
            continue
        join_id = true_block.terminator.target
        join = block_map.get(join_id)
        if join is None or join_id not in loop.blocks:
            continue
        if set(cfg.predecessors(join_id)) != {true_block.id, false_block.id}:
            continue
        true_statements = _statements_without_trivial_phi_assignments(
            true_block.statements,
            incoming_blocks=(header.id,),
        )
        false_statements = _statements_without_trivial_phi_assignments(
            false_block.statements,
            incoming_blocks=(header.id,),
        )
        phi_copies = _direct_phi_join_copies(join, true_block.id, false_block.id)
        if true_statements is None or false_statements is None or phi_copies is None:
            continue
        true_copies, false_copies, join_statements = phi_copies
        if not join_statements or not isinstance(join.terminator, Jump):
            continue
        terminal = join_statements[-1]
        if not isinstance(terminal, If):
            continue
        if not _is_direct_terminal_return_if(terminal):
            continue
        # Keep the existing conservative truthiness contract for short
        # circuit phi loops: a non-return arm is only structurally safe to
        # project when one incoming copy is an explicit falsey sentinel.  A
        # truthy sentinel can carry VM-specific truthiness semantics that the
        # generic reducer must leave on the CFG preservation floor.
        has_falsey_sentinel = any(
            isinstance(copy.value, Const)
            and isinstance(copy.value.value, (bool, int, float))
            and (copy.value.value is False or copy.value.value == 0)
            for copy in (*true_copies, *false_copies)
            if isinstance(copy, Assign)
        )
        if not has_falsey_sentinel:
            if not _is_explicit_scalar_comparison(terminal.condition):
                continue
            if not _phi_copies_are_statically_boolean(
                (*true_copies, *false_copies),
                (true_statements, false_statements),
            ):
                continue

        tail_statements: list[Stmt] = []
        current_id = join.terminator.target
        visited = {header.id, true_block.id, false_block.id, join.id}
        previous_id = join.id
        while current_id != header.id:
            if current_id in visited:
                break
            visited.add(current_id)
            tail = block_map.get(current_id)
            if tail is None or not isinstance(tail.terminator, Jump):
                break
            if set(cfg.predecessors(current_id)) != {previous_id}:
                break
            tail_without_phi = _statements_without_trivial_phi_assignments(
                tail.statements,
                incoming_blocks=tuple(cfg.predecessors(current_id)),
            )
            if tail_without_phi is None:
                break
            tail_statements.extend(tail_without_phi)
            previous_id = current_id
            current_id = tail.terminator.target
        else:
            # Every loop block must be represented exactly once by the two
            # branch arms, the join, or the unique linear tail.
            if visited != set(loop.blocks):
                continue
            branch = If(
                source=header.terminator.source,
                condition=header.terminator.condition,
                then_body=(*true_statements, *true_copies),
                else_body=(*false_statements, *false_copies),
            )
            loop_statement = While(
                source=header.terminator.source,
                condition=Const(source=header.terminator.source, value=True),
                body=(branch, *join_statements, *tail_statements),
            )
            return _rewrite_while_structured_function(
                function,
                (*preheader.statements, loop_statement),
                None,
                "exact-terminal-natural-loop",
            )
    return None


def _structure_exact_direct_join_terminal_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a loop whose branch has one direct edge to its terminal latch.

    A common lowering shares the latch between the two outcomes of a loop
    header: one outcome jumps directly to the latch, while the other executes
    a single-entry arm and then jumps to that same latch.  The latch contains
    the loop-carried Phi copies, a statement-level terminal guard, and the
    backedge.  This is the direct-join counterpart of
    ``_structure_exact_terminal_join_loop``.

    The matcher is deliberately complete-graph only.  It accepts exactly one
    preheader and three loop blocks, requires the direct latch and arm
    predecessor sets to be exact, and leaves any extra entry, exit, nested
    loop, exceptional edge, or ambiguous Phi on the low-level preservation
    floor.  Moving the header branch and latch statements into one ``while
    (true)`` body preserves their original evaluation order; no condition is
    synthesized or combined.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    loop_blocks = frozenset(loop.blocks)
    if loop.exits or len(loop_blocks) != 3:
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, loop_blocks):
        return None

    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None
    if header.terminator.true_target == header.terminator.false_target:
        return None

    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(pred for pred in predecessors if pred not in loop_blocks)
    if len(preheaders) != 1 or preheaders[0] != cfg.entry:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or cfg.predecessors(preheader.id)
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_terminal_statement(preheader.statements)
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    if set(block_map) != loop_blocks | {preheader.id}:
        return None

    # Try both branch orientations.  ``direct`` is the latch reached directly
    # from the header; ``arm`` is the only intermediate block on the other
    # edge.  Exact predecessor sets exclude hidden side entries.
    for direct_id, arm_id in (
        (header.terminator.true_target, header.terminator.false_target),
        (header.terminator.false_target, header.terminator.true_target),
    ):
        direct = block_map.get(direct_id)
        arm = block_map.get(arm_id)
        if direct is None or arm is None or direct.id == arm.id:
            continue
        if direct.id not in loop_blocks or arm.id not in loop_blocks:
            continue
        if not isinstance(direct.terminator, Jump) or direct.terminator.target != header.id:
            continue
        if not isinstance(arm.terminator, Jump) or arm.terminator.target != direct.id:
            continue
        if set(cfg.predecessors(arm.id)) != {header.id}:
            continue
        if set(cfg.predecessors(direct.id)) != {header.id, arm.id}:
            continue
        if set(loop_blocks) != {header.id, direct.id, arm.id}:
            continue

        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=predecessors,
        )
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(header.id,),
        )
        phi_copies = _direct_phi_join_copies(
            direct,
            arm.id,
            header.id,
        )
        if header_statements is None or arm_statements is None or phi_copies is None:
            continue
        if (
            _contains_terminal_statement(header_statements)
            or _contains_terminal_statement(arm_statements)
            or _contains_unscoped_loop_control(header_statements)
            or _contains_unscoped_loop_control(arm_statements)
        ):
            continue

        arm_copies, direct_copies, latch_statements = phi_copies
        if (
            _contains_terminal_statement(arm_copies)
            or _contains_terminal_statement(direct_copies)
            or _contains_unscoped_loop_control(arm_copies)
            or _contains_unscoped_loop_control(direct_copies)
            or not _contains_terminal_statement(latch_statements)
            or _contains_unscoped_loop_control(latch_statements)
        ):
            continue

        # A loop-carried value used by the terminal guard must have a
        # language-neutral truthiness proof.  An explicit falsey incoming
        # constant is sufficient.  Otherwise accept only a scalar comparison
        # whose non-constant Phi inputs were produced by static comparisons;
        # an arbitrary call result (or a truthy sentinel) stays on the CFG
        # floor, matching the existing short-circuit loop contract.
        has_falsey_sentinel = any(
            isinstance(copy, Assign)
            and isinstance(copy.value, Const)
            and isinstance(copy.value.value, (bool, int, float))
            and (copy.value.value is False or copy.value.value == 0)
            for copy in (*arm_copies, *direct_copies)
        )
        if not has_falsey_sentinel:
            terminal_conditions = tuple(
                statement.condition
                for statement in latch_statements
                if isinstance(statement, If)
                and _contains_terminal_statement(
                    (*statement.then_body, *statement.else_body)
                )
            )
            if (
                len(terminal_conditions) != 1
                or not _is_explicit_scalar_comparison(terminal_conditions[0])
                or not _phi_copies_are_statically_boolean(
                    (*arm_copies, *direct_copies),
                    (arm_statements, ()),
                )
            ):
                continue

        arm_body = (*arm_statements, *arm_copies)
        direct_body = direct_copies
        branch = If(
            source=header.terminator.source,
            condition=header.terminator.condition,
            then_body=arm_body if direct_id == header.terminator.false_target else direct_body,
            else_body=direct_body if direct_id == header.terminator.false_target else arm_body,
        )
        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, branch, *latch_statements),
        )
        return _rewrite_while_structured_function(
            function,
            (*preheader.statements, loop_statement),
            None,
            "exact-direct-join-terminal-loop",
        )
    return None


def _structure_exact_pretested_loop_with_terminal_body_return(function: FunctionIR) -> FunctionIR | None:
    """Collapse a pretested loop with one terminal body arm.

    The complete graph is ``preheader -> header``; the header branches to a
    three-block loop body or to a terminal exit; and the body branches either
    through one latch back to the header or to a second terminal block.  This
    is the smallest generic form of a loop with an early return.  Requiring
    the complete reachable graph and unique predecessors prevents a shared
    continuation, hidden entry, or duplicated side effect from being folded.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    if len(loop.blocks) != 3 or len(loop.exits) != 2:
        return None
    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None

    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(pred for pred in predecessors if pred not in loop.blocks)
    if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None

    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != set(block_map):
        return None
    if not _region_loop_edges_are_reducible(RegionGraph.from_cfg(cfg), frozenset(loop.blocks)):
        return None

    # Header statements run before every condition evaluation.  Keeping only
    # identity Phi copies proves there is no effect that would move across the
    # pretested while condition.
    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=predecessors,
    )
    if header_statements is None or header_statements:
        return None

    body_targets = [target for target in _terminator_targets(header.terminator) if target in loop.blocks]
    exit_targets = [target for target in _terminator_targets(header.terminator) if target not in loop.blocks]
    if len(body_targets) != 1 or len(exit_targets) != 1:
        return None
    body_id = body_targets[0]
    header_exit_id = exit_targets[0]
    body = block_map.get(body_id)
    header_exit = block_map.get(header_exit_id)
    if body is None or header_exit is None or not isinstance(body.terminator, Branch):
        return None
    if cfg.predecessors(body.id) != (header.id,):
        return None

    latch_targets = [target for target in _terminator_targets(body.terminator) if target in loop.blocks]
    body_exit_targets = [target for target in _terminator_targets(body.terminator) if target not in loop.blocks]
    if len(latch_targets) != 1 or len(body_exit_targets) != 1:
        return None
    latch = block_map.get(latch_targets[0])
    body_exit = block_map.get(body_exit_targets[0])
    if latch is None or body_exit is None or latch.id == header.id or latch.id == body.id:
        return None
    if not isinstance(latch.terminator, Jump) or latch.terminator.target != header.id:
        return None
    if cfg.predecessors(latch.id) != (body.id,):
        return None
    if cfg.predecessors(header_exit.id) != (header.id,) or cfg.predecessors(body_exit.id) != (body.id,):
        return None
    if {header_exit.id, body_exit.id} & set(loop.blocks):
        return None
    if set(block_map) != set(loop.blocks) | {preheader.id, header_exit.id, body_exit.id}:
        return None

    body_statements = _statements_without_trivial_phi_assignments(
        body.statements,
        incoming_blocks=(header.id,),
    )
    latch_statements = _statements_without_trivial_phi_assignments(
        latch.statements,
        incoming_blocks=(body.id,),
    )
    if body_statements is None or latch_statements is None:
        return None
    if _contains_unscoped_loop_control(body_statements) or _contains_unscoped_loop_control(latch_statements):
        return None

    def terminal_body(block: BasicBlock, predecessor: str) -> tuple[Stmt, ...] | None:
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor,),
        )
        if statements is None or _contains_unscoped_loop_control(statements):
            return None
        if isinstance(block.terminator, Return):
            return (*statements, block.terminator)
        if block.terminator is None and statements and isinstance(statements[-1], (Raise, Reraise)):
            return statements
        return None

    header_exit_body = terminal_body(header_exit, header.id)
    body_exit_body = terminal_body(body_exit, body.id)
    if header_exit_body is None or body_exit_body is None:
        return None

    backedge_body = (*latch_statements, Continue())
    body_branch = body.terminator
    if body_branch.true_target == latch.id:
        then_body, else_body = backedge_body, body_exit_body
    elif body_branch.false_target == latch.id:
        then_body, else_body = body_exit_body, backedge_body
    else:
        return None
    loop_body = (
        *body_statements,
        If(
            source=body_branch.source,
            condition=body_branch.condition,
            then_body=then_body,
            else_body=else_body,
        ),
    )
    header_branch = header.terminator
    if header_branch.true_target == body.id:
        loop_condition = header_branch.condition
    elif header_branch.false_target == body.id:
        loop_condition = UnaryOp(
            source=header_branch.condition.source,
            type=header_branch.condition.type,
            op="not ",
            value=header_branch.condition,
        )
    else:
        return None
    header_exit_terminator = header_exit.terminator
    if isinstance(header_exit_terminator, Return):
        terminal_statements = (*header_exit_body[:-1],)
        terminal_terminator: Terminator | None = header_exit_terminator
    else:
        terminal_statements = header_exit_body
        terminal_terminator = None
    return _rewrite_while_structured_function(
        function,
        (
            *preheader.statements,
            While(condition=loop_condition, body=loop_body),
            *terminal_statements,
        ),
        terminal_terminator,
        "exact-pretested-loop-with-terminal-body-return",
    )


def _is_direct_terminal_return_if(statement: If) -> bool:
    """Accept one linear terminal-return arm and one empty arm.

    A compiler may materialize the return value immediately before the
    terminator.  That prefix is safe to retain in the same branch, provided it
    contains no nested terminal or loop-control statement that would make the
    surrounding CFG edge inaccurate.
    """

    def terminal_arm(body: tuple[Stmt, ...]) -> bool:
        return bool(body) and isinstance(body[-1], Return) and not _contains_terminal_statement(body[:-1]) and not _contains_unscoped_loop_control(body[:-1])

    return (terminal_arm(statement.then_body) and not statement.else_body) or (
        terminal_arm(statement.else_body) and not statement.then_body
    )


def _is_explicit_scalar_comparison(condition: Expr) -> bool:
    """Recognize a comparison whose result is tested explicitly.

    This is distinct from a bare truthiness test: the loop reducer can retain
    the exact comparison without relying on VM-specific truthiness of a Phi
    value.  Requiring a constant operand keeps the proof scalar and neutral.
    """

    return (
        isinstance(condition, BinaryOp)
        and condition.op in {"==", "!=", "<", "<=", ">", ">="}
        and (isinstance(condition.left, Const) or isinstance(condition.right, Const))
    )


def _phi_copies_are_statically_boolean(
    copies: tuple[Stmt, ...],
    arm_statements: tuple[tuple[Stmt, ...], tuple[Stmt, ...]],
) -> bool:
    """Prove non-constant Phi inputs came from explicit static comparisons."""

    definitions = {
        statement.target.name: statement.value
        for statements in arm_statements
        for statement in statements
        if isinstance(statement, Assign)
        and isinstance(statement.target, Var)
    }
    variable_copies = [
        copy.value
        for copy in copies
        if isinstance(copy, Assign)
        and isinstance(copy.value, Var)
    ]
    if not variable_copies:
        return False
    return all(
        isinstance(definitions.get(value.name), BinaryOp)
        and definitions[value.name].op in {"==", "!=", "<", "<=", ">", ">="}
        and definitions[value.name].semantics == "static"
        for value in variable_copies
    )


def _region_loop_edges_are_reducible(region_graph: RegionGraph, members: frozenset[str]) -> bool:
    """Reject exceptional or irreducible edges touching a candidate loop."""

    return not any(
        edge.roles & {"exceptional", "irreducible"}
        for edge in region_graph.edges
        if edge.source in members or edge.target in members
    )


def _structure_exact_two_edge_header_phi_while(function: FunctionIR) -> FunctionIR | None:
    """Recover a simple pretested loop with explicit two-edge header phis."""

    if len(function.blocks) != 4:
        return None
    preheader, header, body, exit_block = function.blocks
    if (
        not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if {header.terminator.true_target, header.terminator.false_target} != {body.id, exit_block.id}:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != header.id:
        return None
    if not isinstance(exit_block.terminator, Return):
        return None
    copies = _two_edge_header_phi_copies(header, preheader.id, body.id)
    if copies is None:
        return None
    entry_copies, backedge_copies, header_statements = copies
    body_statements = _statements_without_trivial_phi_assignments(
        body.statements,
        incoming_blocks=(header.id,),
    )
    exit_statements = _statements_without_trivial_phi_assignments(
        exit_block.statements,
        incoming_blocks=(header.id,),
    )
    if body_statements is None or exit_statements is None:
        return None
    condition = header.terminator.condition
    if header.terminator.false_target == body.id:
        condition = UnaryOp(source=condition.source, type=condition.type, op="not ", value=condition)
    return _rewrite_while_structured_function(
        function,
        (
            *preheader.statements,
            *entry_copies,
            While(condition=condition, body=(*header_statements, *body_statements, *backedge_copies)),
            *exit_statements,
        ),
        exit_block.terminator,
        "exact-two-edge-header-phi-while",
    )


def _two_edge_header_phi_copies(
    header: BasicBlock,
    preheader_id: str,
    backedge_id: str,
) -> tuple[tuple[Stmt, ...], tuple[Stmt, ...], tuple[Stmt, ...]] | None:
    """Destroy independent leading header phis on the two known incoming edges."""

    entry: list[Stmt] = []
    backedge: list[Stmt] = []
    targets: set[str] = set()
    remaining: list[Stmt] = []
    saw_non_phi = False
    for statement in header.statements:
        if isinstance(statement, Assign) and isinstance(statement.target, Var) and isinstance(statement.value, Phi):
            if saw_non_phi:
                return None
            incoming = dict(statement.value.incoming)
            if set(incoming) != {preheader_id, backedge_id}:
                return None
            initial = incoming[preheader_id]
            repeated = incoming[backedge_id]
            if not isinstance(initial, (Const, Var)) or not isinstance(repeated, (Const, Var)):
                return None
            targets.add(statement.target.name)
            if not (isinstance(initial, Var) and initial.name == statement.target.name):
                entry.append(Assign(source=statement.source, target=statement.target, value=initial))
            if not (isinstance(repeated, Var) and repeated.name == statement.target.name):
                backedge.append(Assign(source=statement.source, target=statement.target, value=repeated))
            continue
        saw_non_phi = True
        remaining.append(statement)
    if not targets:
        return None
    inputs = {
        copy.value.name
        for copy in (*entry, *backedge)
        if isinstance(copy, Assign) and isinstance(copy.value, Var)
    }
    if targets & inputs:
        return None
    return tuple(entry), tuple(backedge), tuple(remaining)


def _structure_exact_header_terminal_guard_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover a linear loop whose header guard returns on its false arm.

    A lowering may keep a loop test as a statement-level ``If`` in the loop
    header: the true arm falls through to a linear body and the false arm
    returns the accumulated value.  The body then reaches an empty latch that
    jumps back to the header.  This is equivalent to a pre-tested ``While``
    followed by the header's terminal return, but only when the complete
    natural-loop boundary is proven.  Matching is intentionally independent of
    the condition's expression or callee so this remains VM-neutral.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != set(block_map):
        return None

    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if loop.exits or len(loop.blocks) != 3:
            continue
        header = block_map.get(loop.header)
        if header is None:
            continue
        if not _region_loop_edges_are_reducible(region_graph, frozenset(loop.blocks)):
            continue
        predecessors = cfg.predecessors(header.id)
        preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop.blocks)
        if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
            continue
        preheader = block_map.get(preheaders[0])
        if preheader is None or not isinstance(preheader.terminator, Jump) or preheader.terminator.target != header.id:
            continue
        if (
            _contains_unscoped_loop_control(preheader.statements)
            or _contains_terminal_statement(preheader.statements)
        ):
            continue
        if not isinstance(header.terminator, Jump):
            continue
        body_id = header.terminator.target
        body = block_map.get(body_id)
        if body is None or body_id not in loop.blocks or body_id == header.id:
            continue
        if set(cfg.predecessors(body_id)) != {header.id} or not isinstance(body.terminator, Jump):
            continue
        latch_id = body.terminator.target
        latch = block_map.get(latch_id)
        if latch is None or latch_id not in loop.blocks or latch_id in {header.id, body_id}:
            continue
        if set(cfg.predecessors(latch_id)) != {body.id} or not isinstance(latch.terminator, Jump):
            continue
        if latch.terminator.target != header.id or latch.statements:
            continue
        if set(loop.blocks) != {header.id, body.id, latch.id} or loop.backedge_source != latch.id:
            continue
        if len(header.statements) != 1 or not isinstance(header.statements[0], If):
            continue
        guard = header.statements[0]
        if guard.then_body or len(guard.else_body) != 1 or not isinstance(guard.else_body[0], Return):
            continue
        body_statements = _statements_without_trivial_phi_assignments(
            body.statements,
            incoming_blocks=(header.id,),
        )
        if body_statements is None or _contains_unscoped_loop_control(body_statements):
            continue
        terminal_return = guard.else_body[0]
        return _rewrite_while_structured_function(
            function,
            (
                *preheader.statements,
                While(
                    source=guard.source,
                    condition=guard.condition,
                    body=body_statements,
                ),
            ),
            terminal_return,
            "exact-header-terminal-guard-loop",
        )
    return None


def _structure_exact_multi_backedge_natural_loop_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a single-entry loop whose header has multiple backedges.

    ``find_natural_loops`` reports one loop per backedge. A common VM lowering
    splits a loop body into several conditional latches, so each report has a
    different exit edge even though their union is one exact single-exit
    region. Unioning only loops with the same header is VM-neutral; the proof
    still requires one external preheader, one continuation target, and no
    nested or crossing loop inside the union.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    loops = find_natural_loops(cfg)
    groups: dict[str, list[object]] = {}
    for loop in loops:
        groups.setdefault(loop.header, []).append(loop)
    for header_id, group in sorted(groups.items(), key=lambda item: (len(item[1]), item[0])):
        if len(group) < 2:
            continue
        union_blocks = frozenset().union(*(loop.blocks for loop in group))
        if any(
            other.header != header_id
            and other.blocks <= union_blocks
            for other in loops
        ):
            continue
        if any(
            other.header != header_id
            and other.blocks & union_blocks
            and not (other.blocks <= union_blocks or union_blocks <= other.blocks)
            for other in loops
        ):
            continue
        exits = tuple(
            edge
            for source in union_blocks
            for edge in cfg.edges
            if edge.source == source
            if edge.target not in union_blocks
        )
        if len({edge.target for edge in exits}) != 1:
            continue
        synthetic_loop = SimpleNamespace(
            header=header_id,
            blocks=union_blocks,
            exits=exits,
            backedge_source=sorted(union_blocks)[-1],
        )
        structured = _collapse_exact_natural_loop_region(function, cfg, synthetic_loop)
        if structured is not None and _low_level_edge_count(structured) < _low_level_edge_count(function):
            return structured
    return None


def _structure_exact_multi_backedge_terminal_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a tree-shaped loop with several backedges and inline exits.

    A VM can lower ``while true`` with an ``if``/``elseif`` cascade so every
    successful arm jumps back to the same header while the final arm returns
    directly from a statement-level branch.  Natural-loop discovery reports a
    separate loop for each backedge, but the union is one executable region.
    This reducer accepts only the strict tree form: one external preheader,
    one header, no ordinary edge leaving the union, one predecessor for every
    non-header member, and complete reachability from the header arms.  Those
    conditions make rendering each arm independently equivalent to traversing
    the original CFG; any shared node, phi ambiguity, exception edge, or
    additional exit remains on the low-level preservation floor.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    groups: dict[str, list[object]] = {}
    for loop in loops:
        groups.setdefault(loop.header, []).append(loop)

    for header_id, group in sorted(groups.items(), key=lambda item: (item[0], len(item[1]))):
        if len(group) < 2:
            continue
        union = frozenset().union(*(loop.blocks for loop in group))
        header = block_map.get(header_id)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        if header.terminator.true_target == header.terminator.false_target:
            continue
        if not {header.terminator.true_target, header.terminator.false_target} <= union:
            continue

        predecessors = cfg.predecessors(header_id)
        preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in union)
        if len(preheaders) != 1 or function.blocks[0].id != preheaders[0]:
            continue
        preheader = block_map.get(preheaders[0])
        if (
            preheader is None
            or not isinstance(preheader.terminator, Jump)
            or preheader.terminator.target != header_id
            or _contains_unscoped_loop_control(preheader.statements)
        ):
            continue
        if set(block_map) != set(union) | {preheader.id}:
            continue
        if not _region_loop_edges_are_reducible(region_graph, union):
            continue

        # A tree-shaped arm has exactly one incoming edge.  This prevents a
        # shared block from being rendered twice with different predecessor
        # state and also makes every edge-local phi unambiguous.
        if any(
            block_id != header_id
            and (
                len(cfg.predecessors(block_id)) != 1
                or cfg.predecessors(block_id)[0] not in union
            )
            for block_id in union
        ):
            continue
        if any(
            edge.source in union and edge.target not in union
            for edge in cfg.edges
        ):
            continue
        reachable = _reachable_block_ids(cfg, header_id) & set(union)
        if reachable != set(union):
            continue
        if any(
            not isinstance(block_map[block_id].terminator, (Jump, Branch, MultiBranch))
            for block_id in union
        ):
            continue

        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=predecessors,
        )
        if header_statements is None:
            continue
        true_target = header.terminator.true_target
        false_target = header.terminator.false_target
        true_result = _render_loop_body(
            block_map,
            true_target,
            header_id=header_id,
            exit_id="__no_loop_exit__",
            loop_blocks=union,
            path=frozenset({header_id}),
        )
        false_result = _render_loop_body(
            block_map,
            false_target,
            header_id=header_id,
            exit_id="__no_loop_exit__",
            loop_blocks=union,
            path=frozenset({header_id}),
        )
        if true_result is None or false_result is None:
            continue
        true_body, true_effect = true_result
        false_body, false_effect = false_result
        body = (
            If(
                source=header.terminator.source,
                condition=header.terminator.condition,
                then_body=true_body,
                else_body=false_body,
            ),
        )
        if not true_effect or not false_effect:
            continue
        if _statements_contain_break(body) or not _contains_terminal_statement(body):
            continue
        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, *body),
        )
        return _rewrite_while_structured_function(
            function,
            (*preheader.statements, loop_statement),
            None,
            "exact-multi-backedge-terminal-loop",
        )
    return None


def _structure_exact_multiway_terminal_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a dispatch-headed loop with only terminal or header arms.

    This is the multi-way counterpart of the strict terminal-arm loop rule.
    It models the common CFG shape ``header -> switch -> ... -> header`` where
    each dispatch arm is an independent tree and every non-backedge leaf
    returns or raises.  The proof deliberately rejects shared nodes, nontrivial
    Phi values, exceptional edges, nested/crossing loops, and nonterminal
    exits.  A failed proof leaves the original CFG untouched.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    groups: dict[str, list[object]] = {}
    for loop in loops:
        groups.setdefault(loop.header, []).append(loop)

    for header_id, group in sorted(groups.items(), key=lambda item: (item[0], len(item[1]))):
        header = block_map.get(header_id)
        if header is None or not isinstance(header.terminator, MultiBranch):
            continue
        loop_blocks = frozenset().union(*(loop.blocks for loop in group))
        if not loop_blocks or header_id not in loop_blocks:
            continue
        if any(
            other.header != header_id
            and other.blocks & loop_blocks
            and not (other.blocks <= loop_blocks or loop_blocks <= other.blocks)
            for other in loops
        ):
            continue
        if not _region_loop_edges_are_reducible(region_graph, loop_blocks):
            continue

        predecessors = cfg.predecessors(header_id)
        preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop_blocks)
        if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
            continue
        preheader = block_map.get(preheaders[0])
        if (
            preheader is None
            or not isinstance(preheader.terminator, Jump)
            or preheader.terminator.target != header_id
            or cfg.predecessors(preheader.id)
            or _contains_terminal_statement(preheader.statements)
            or _contains_unscoped_loop_control(preheader.statements)
        ):
            continue

        # Non-header loop members must be entered only from the loop itself.
        # A single predecessor alone is insufficient: an external block could
        # otherwise be folded into an arm and silently change loop-entry
        # semantics.  The header is the only member with an external
        # preheader entry.
        if any(
            member != header_id
            and (
                len(cfg.predecessors(member)) != 1
                or cfg.predecessors(member)[0] not in loop_blocks
            )
            for member in loop_blocks
        ):
            continue
        if any(
            edge.source in loop_blocks and edge.target in loop_blocks
            and edge.target == header_id and edge.roles & {"exceptional", "irreducible"}
            for edge in region_graph.edges
        ):
            continue

        targets = tuple(target for _value, target in header.terminator.cases) + (
            header.terminator.default_target,
        )
        if not targets or len(set(targets)) != len(targets):
            continue

        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=predecessors,
        )
        if header_statements is None:
            continue

        rendered_nodes: set[str] = {header_id}
        terminal_nodes: set[str] = set()
        rendered_arms: dict[str, tuple[tuple[Stmt, ...], bool]] = {}
        valid = True
        for target in targets:
            result = _render_terminal_dispatch_path(
                block_map,
                cfg,
                target,
                header_id=header_id,
                loop_blocks=loop_blocks,
                path=frozenset({header_id}),
                allow_header=True,
            )
            if result is None:
                valid = False
                break
            body, effect, nodes = result
            node_set = set(nodes)
            # Every node in an arm must be reached from the arm itself or the
            # dispatch header.  Without this boundary check, an unrelated
            # predecessor could enter a rendered terminal tree (or a loop
            # member) and make the arm's execution context incomplete.
            if any(
                set(cfg.predecessors(node_id)) - node_set - {header_id}
                for node_id in node_set
            ):
                valid = False
                break
            if rendered_nodes & nodes or terminal_nodes & nodes:
                valid = False
                break
            rendered_arms[target] = (body, effect)
            rendered_nodes.update(nodes & set(loop_blocks))
            terminal_nodes.update(nodes - set(loop_blocks))
        if not valid:
            continue

        reachable = _reachable_block_ids(cfg, cfg.entry)
        if reachable != {preheader.id} | rendered_nodes | terminal_nodes:
            continue
        if rendered_nodes != set(loop_blocks) or not terminal_nodes:
            continue
        if any(
            block_id not in loop_blocks | {preheader.id}
            and any(
                edge.source == block_id
                and edge.target in loop_blocks | terminal_nodes
                and edge.roles & {"exceptional", "irreducible"}
                for edge in region_graph.edges
            )
            for block_id in reachable
        ):
            continue

        cases = tuple(
            (value, rendered_arms[target][0])
            for value, target in header.terminator.cases
        )
        default_body = rendered_arms[header.terminator.default_target][0]
        if not any(effect for _body, effect in rendered_arms.values()):
            continue
        switch = Switch(
            source=header.terminator.source,
            selector=header.terminator.selector,
            cases=cases,
            default_body=default_body,
        )
        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, switch),
        )
        return _rewrite_while_structured_function(
            function,
            (*preheader.statements, loop_statement),
            None,
            "exact-multiway-terminal-loop",
        )
    return None


def _render_terminal_dispatch_path(
    block_map: dict[str, BasicBlock],
    cfg,
    start: str,
    *,
    header_id: str,
    loop_blocks: frozenset[str],
    path: frozenset[str],
    allow_header: bool,
) -> tuple[tuple[Stmt, ...], bool, frozenset[str]] | None:
    """Render one disjoint dispatch arm, including terminal leaves."""

    if start == header_id:
        if not allow_header:
            return None
        # The header is owned by the enclosing loop and is already accounted
        # for by the caller.  A backedge contributes only the structured
        # control outcome, not another rendered node.
        return ((Continue(),), False, frozenset())
    if start in path:
        return None
    block = block_map.get(start)
    if block is None:
        return None
    predecessors = cfg.predecessors(start)
    if start in loop_blocks:
        if len(predecessors) != 1 or predecessors[0] not in loop_blocks:
            return None
    statements = _statements_without_trivial_phi_assignments(
        block.statements,
        incoming_blocks=predecessors,
    )
    if statements is None or _contains_unscoped_loop_control(statements):
        return None
    next_path = path | {start}
    nodes = frozenset({start})
    terminator = block.terminator
    if isinstance(terminator, Return):
        return (*statements, terminator), True, nodes
    if terminator is None:
        if statements and isinstance(statements[-1], (Raise, Reraise)):
            return statements, True, nodes
        return None
    if isinstance(terminator, Jump):
        child = _render_terminal_dispatch_path(
            block_map,
            cfg,
            terminator.target,
            header_id=header_id,
            loop_blocks=loop_blocks,
            path=next_path,
            allow_header=start in loop_blocks,
        )
        if child is None:
            return None
        child_body, child_effect, child_nodes = child
        return (*statements, *child_body), bool(statements) or child_effect, nodes | child_nodes
    if isinstance(terminator, Branch):
        if terminator.true_target == terminator.false_target:
            return None
        then_result = _render_terminal_dispatch_path(
            block_map,
            cfg,
            terminator.true_target,
            header_id=header_id,
            loop_blocks=loop_blocks,
            path=next_path,
            allow_header=start in loop_blocks,
        )
        else_result = _render_terminal_dispatch_path(
            block_map,
            cfg,
            terminator.false_target,
            header_id=header_id,
            loop_blocks=loop_blocks,
            path=next_path,
            allow_header=start in loop_blocks,
        )
        if then_result is None or else_result is None:
            return None
        then_body, then_effect, then_nodes = then_result
        else_body, else_effect, else_nodes = else_result
        if then_nodes & else_nodes:
            return None
        return (
            *statements,
            If(
                source=terminator.source,
                condition=terminator.condition,
                then_body=then_body,
                else_body=else_body,
            ),
        ), bool(statements) or then_effect or else_effect, nodes | then_nodes | else_nodes
    if not isinstance(terminator, MultiBranch):
        return None
    targets = tuple(target for _value, target in terminator.cases) + (terminator.default_target,)
    if not targets or len(set(targets)) != len(targets):
        return None
    case_results: list[tuple[Expr, tuple[Stmt, ...], bool, frozenset[str]]] = []
    used_nodes = set(nodes)
    for value, target in terminator.cases:
        result = _render_terminal_dispatch_path(
            block_map,
            cfg,
            target,
            header_id=header_id,
            loop_blocks=loop_blocks,
            path=next_path,
            allow_header=start in loop_blocks,
        )
        if result is None:
            return None
        body, effect, child_nodes = result
        if used_nodes & set(child_nodes):
            return None
        used_nodes.update(child_nodes)
        case_results.append((value, body, effect, child_nodes))
    default_result = _render_terminal_dispatch_path(
        block_map,
        cfg,
        terminator.default_target,
        header_id=header_id,
        loop_blocks=loop_blocks,
        path=next_path,
        allow_header=start in loop_blocks,
    )
    if default_result is None:
        return None
    default_body, default_effect, default_nodes = default_result
    if used_nodes & set(default_nodes):
        return None
    return (
        *statements,
        Switch(
            source=terminator.source,
            selector=terminator.selector,
            cases=tuple((value, body) for value, body, _effect, _nodes in case_results),
            default_body=default_body,
        ),
    ), bool(statements) or default_effect or any(effect for _value, _body, effect, _nodes in case_results), nodes | frozenset(used_nodes) | default_nodes


def _structure_exact_multiway_shared_linear_join_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a dispatch loop with a single linear shared continuation.

    This is the deliberately small switch-loop subset of Ghidra's region
    collapse: every case arm is a disjoint jump-only chain into one join, and
    the join's suffix is another jump-only chain ending at the header or a
    terminal block.  No arm branch, shared interior node, nontrivial Phi,
    exceptional edge, or alternate exit is guessed here.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    groups: dict[str, list[object]] = {}
    for loop in loops:
        groups.setdefault(loop.header, []).append(loop)

    for header_id, group in sorted(groups.items(), key=lambda item: (item[0], len(item[1]))):
        header = block_map.get(header_id)
        if header is None or not isinstance(header.terminator, MultiBranch):
            continue
        loop_blocks = frozenset().union(*(loop.blocks for loop in group))
        if header_id not in loop_blocks or not _region_loop_edges_are_reducible(region_graph, loop_blocks):
            continue
        if any(
            other.header != header_id
            and other.blocks & loop_blocks
            and not (other.blocks <= loop_blocks or loop_blocks <= other.blocks)
            for other in loops
        ):
            continue

        predecessors = cfg.predecessors(header_id)
        preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop_blocks)
        if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
            continue
        preheader = block_map.get(preheaders[0])
        if (
            preheader is None
            or cfg.predecessors(preheader.id)
            or not isinstance(preheader.terminator, Jump)
            or preheader.terminator.target != header_id
            or _contains_terminal_statement(preheader.statements)
            or _contains_unscoped_loop_control(preheader.statements)
        ):
            continue

        targets = tuple(target for _value, target in header.terminator.cases) + (
            header.terminator.default_target,
        )
        if not targets or len(set(targets)) != len(targets):
            continue
        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=predecessors,
        )
        if header_statements is None:
            continue

        joins = sorted(
            (
                member
                for member in loop_blocks
                if member != header_id and len(set(cfg.predecessors(member))) > 1
            ),
            key=lambda member: (-len(set(cfg.predecessors(member))), member),
        )
        for join_id in joins:
            join_predecessors = tuple(dict.fromkeys(cfg.predecessors(join_id)))
            if len(join_predecessors) != len(cfg.predecessors(join_id)):
                continue
            if not set(join_predecessors) <= set(loop_blocks):
                continue

            arm_nodes: set[str] = set()
            arm_bodies: dict[str, tuple[Stmt, ...]] = {}
            arm_sources: set[str] = set()
            valid = True
            for target in targets:
                result = _render_linear_dispatch_arm(
                    block_map,
                    cfg,
                    target,
                    join_id=join_id,
                    header_id=header_id,
                    loop_blocks=loop_blocks,
                    path=frozenset({header_id}),
                    predecessor_id=header_id,
                )
                if result is None:
                    valid = False
                    break
                body, nodes, sources = result
                if arm_nodes & set(nodes):
                    valid = False
                    break
                arm_nodes.update(nodes)
                arm_sources.update(sources)
                arm_bodies[target] = body
            if not valid or arm_sources != set(join_predecessors):
                continue

            tail = _render_linear_join_tail(
                block_map,
                cfg,
                join_id,
                header_id=header_id,
                loop_blocks=loop_blocks,
                path=frozenset(),
                allow_header=True,
                predecessor_id=None,
            )
            if tail is None:
                continue
            tail_body, tail_nodes, tail_effect = tail
            if arm_nodes & set(tail_nodes):
                continue
            rendered_loop_nodes = arm_nodes | (set(tail_nodes) & set(loop_blocks)) | {header_id}
            if rendered_loop_nodes != set(loop_blocks):
                continue
            reachable = _reachable_block_ids(cfg, cfg.entry)
            external_nodes = set(tail_nodes) - set(loop_blocks)
            if reachable != {preheader.id} | rendered_loop_nodes | external_nodes:
                continue
            if not arm_nodes and not tail_effect:
                continue

            switch = Switch(
                source=header.terminator.source,
                selector=header.terminator.selector,
                cases=tuple(
                    (value, arm_bodies[target])
                    for value, target in header.terminator.cases
                ),
                default_body=arm_bodies[header.terminator.default_target],
            )
            loop_statement = While(
                source=header.terminator.source,
                condition=Const(source=header.terminator.source, value=True),
                body=(*header_statements, switch, *tail_body),
            )
            return _rewrite_while_structured_function(
                function,
                (*preheader.statements, loop_statement),
                None,
                "exact-multiway-shared-linear-join-loop",
            )
    return None


def _render_linear_dispatch_arm(
    block_map: dict[str, BasicBlock],
    cfg,
    start: str,
    *,
    join_id: str,
    header_id: str,
    loop_blocks: frozenset[str],
    path: frozenset[str],
    predecessor_id: str,
) -> tuple[tuple[Stmt, ...], frozenset[str], frozenset[str]] | None:
    """Render one jump-only case arm up to the shared join."""

    if start == join_id:
        return (), frozenset(), frozenset({predecessor_id})
    if start == header_id or start in path or start not in loop_blocks:
        return None
    if cfg.predecessors(start) != (predecessor_id,):
        return None
    block = block_map.get(start)
    if block is None or not isinstance(block.terminator, Jump):
        return None
    statements = _statements_without_trivial_phi_assignments(
        block.statements,
        incoming_blocks=(predecessor_id,),
    )
    if (
        statements is None
        or _contains_terminal_statement(statements)
        or _contains_unscoped_loop_control(statements)
    ):
        return None
    child = _render_linear_dispatch_arm(
        block_map,
        cfg,
        block.terminator.target,
        join_id=join_id,
        header_id=header_id,
        loop_blocks=loop_blocks,
        path=path | {start},
        predecessor_id=start,
    )
    if child is None:
        return None
    child_body, child_nodes, child_sources = child
    return (*statements, *child_body), frozenset({start}) | child_nodes, child_sources


def _render_linear_join_tail(
    block_map: dict[str, BasicBlock],
    cfg,
    start: str,
    *,
    header_id: str,
    loop_blocks: frozenset[str],
    path: frozenset[str],
    allow_header: bool,
    predecessor_id: str | None,
) -> tuple[tuple[Stmt, ...], frozenset[str], bool] | None:
    """Render a jump-only shared suffix to the header or a terminal block."""

    if start == header_id:
        if not allow_header:
            return None
        return (Continue(),), frozenset(), False
    if start in path:
        return None
    block = block_map.get(start)
    if block is None:
        return None
    # The join is entered by all dispatch arms and is checked by the caller.
    # Every subsequent tail node must have exactly the edge from the node that
    # rendered it; otherwise a hidden side entry could change the structured
    # execution order or data-flow state.
    if predecessor_id is None:
        if path:
            return None
    elif cfg.predecessors(start) != (predecessor_id,):
        return None
    statements = _statements_without_trivial_phi_assignments(
        block.statements,
        incoming_blocks=cfg.predecessors(start),
    )
    if (
        statements is None
        or _contains_terminal_statement(statements)
        or _contains_unscoped_loop_control(statements)
    ):
        return None
    nodes = frozenset({start})
    terminator = block.terminator
    if isinstance(terminator, Return):
        return (*statements, terminator), nodes, True
    if terminator is None:
        if statements and isinstance(statements[-1], (Raise, Reraise)):
            return statements, nodes, True
        return None
    if not isinstance(terminator, Jump):
        return None
    child = _render_linear_join_tail(
        block_map,
        cfg,
        terminator.target,
        header_id=header_id,
        loop_blocks=loop_blocks,
        path=path | {start},
        allow_header=start in loop_blocks,
        predecessor_id=start,
    )
    if child is None:
        return None
    child_body, child_nodes, child_effect = child
    return (*statements, *child_body), nodes | child_nodes, bool(statements) or child_effect


def _structure_exact_terminal_join_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a single loop with a shared join before its backedge.

    The loop body may begin with a binary diamond (for example, an error path
    and a normal path) whose arms converge at one join.  After that join the
    CFG must be a single linear chain back to the header.  This is the shape
    Ghidra handles by collapsing the diamond first and then the loop; keeping
    the two proofs separate lets the generic join renderer materialize any
    safe edge-local Phi values without making the loop matcher language aware.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    if loop.exits:
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, frozenset(loop.blocks)):
        return None
    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None
    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop.blocks)
    if len(preheaders) != 1 or function.blocks[0].id != preheaders[0]:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    if set(block_map) != set(loop.blocks) | {preheader.id}:
        return None
    true_block = block_map.get(header.terminator.true_target)
    false_block = block_map.get(header.terminator.false_target)
    if (
        true_block is None
        or false_block is None
        or true_block.id == false_block.id
        or true_block.id not in loop.blocks
        or false_block.id not in loop.blocks
        or not isinstance(true_block.terminator, Jump)
        or not isinstance(false_block.terminator, Jump)
        or true_block.terminator.target != false_block.terminator.target
    ):
        return None
    join_id = true_block.terminator.target
    join = block_map.get(join_id)
    if join is None or join_id not in loop.blocks:
        return None
    if set(cfg.predecessors(join_id)) != {true_block.id, false_block.id}:
        return None

    # Prove that the post-join portion is one linear chain ending at the
    # header.  Any second branch, side entry, or alternate cycle would need a
    # different structuring proof and is deliberately left as CFG/goto.
    chain = {header.id, true_block.id, false_block.id}
    current_id = join_id
    while current_id != header.id:
        if current_id in chain:
            return None
        current = block_map.get(current_id)
        if current is None:
            return None
        if current_id == join_id:
            if set(cfg.predecessors(current_id)) != {true_block.id, false_block.id}:
                return None
        elif len(cfg.predecessors(current_id)) != 1:
            return None
        if not isinstance(current.terminator, Jump):
            return None
        chain.add(current_id)
        current_id = current.terminator.target
    if chain != set(loop.blocks):
        return None

    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=predecessors,
    )
    if header_statements is None:
        return None
    join_phi = _direct_phi_join_copies(join, true_block.id, false_block.id)
    if join_phi is None:
        # _direct_phi_join_copies accepts only constant or variable incoming
        # values, rejects cyclic phi dependencies, and emits each non-identity
        # copy at the end of its mutually-exclusive arm.  That is an exact
        # representation of the original join assignment, including a
        # loop-carried ``existing`` value when it is semantically identical to
        # one arm.  Any richer value or ambiguous incoming set remains on the
        # preservation CFG floor.
        return None
    if any(
        isinstance(statement, Assign)
        and isinstance(statement.value, Phi)
        and not any(
            isinstance(incoming, Const)
            and isinstance(incoming.value, (bool, int, float))
            and (incoming.value is False or incoming.value == 0)
            for predecessor in (true_block.id, false_block.id)
            for source_id, incoming in statement.value.incoming
            if source_id == predecessor
        )
        for statement in join.statements
    ):
        # A truthy sentinel does not define a language-neutral short-circuit
        # value.  Keep the join explicit until a VM-neutral truthiness fact is
        # available instead of guessing that it behaves like false.
        return None
    rendered = _render_direct_phi_join(
        block_map,
        header,
        header_id=header.id,
        exit_id="__no_loop_exit__",
        loop_blocks=frozenset(loop.blocks),
        path=frozenset({header.id}),
    )
    if rendered is None:
        return None
    body, has_effect = rendered
    if (
        not has_effect
        or _statements_contain_break(body)
        or not _contains_terminal_statement(body)
    ):
        return None
    loop_statement = While(
        source=header.terminator.source,
        condition=Const(source=header.terminator.source, value=True),
        body=(*header_statements, *body),
    )
    return _rewrite_while_structured_function(
        function,
        (*preheader.statements, loop_statement),
        None,
        "exact-terminal-join-loop",
    )


def _structure_exact_loop_with_terminal_body_branch(function: FunctionIR) -> FunctionIR | None:
    """Collapse a loop whose body branch either terminates or continues.

    The generic CFG shape is ``header -> body``/``exit`` followed by a body
    branch with one linear terminal arm and one linear backedge arm.  Every
    path is checked independently so distinct terminal values are preserved.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    loop_blocks = frozenset(loop.blocks)
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, loop_blocks):
        return None
    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None

    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop_blocks)
    if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_terminal_statement(preheader.statements)
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None

    true_in_loop = header.terminator.true_target in loop_blocks
    false_in_loop = header.terminator.false_target in loop_blocks
    if true_in_loop == false_in_loop and not (true_in_loop and not loop.exits):
        return None
    body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
    header_exit = None if true_in_loop and false_in_loop else (
        header.terminator.false_target if true_in_loop else header.terminator.true_target
    )
    if body_start == header.id or (header_exit is not None and header_exit in loop_blocks):
        return None

    if any(
        member != header.id
        and any(predecessor not in loop_blocks for predecessor in cfg.predecessors(member))
        for member in loop_blocks
    ):
        return None

    def terminal_path(
        start: str,
        predecessor_id: str,
    ) -> tuple[tuple[Stmt, ...], Terminator, frozenset[str]] | None:
        statements: list[Stmt] = []
        nodes: set[str] = set()
        current = start
        previous = predecessor_id
        while True:
            if current in loop_blocks or current in nodes:
                return None
            block = block_map.get(current)
            if block is None or cfg.predecessors(current) != (previous,):
                return None
            block_statements = _statements_without_trivial_phi_assignments(
                block.statements,
                incoming_blocks=(previous,),
            )
            if (
                block_statements is None
                or _contains_terminal_statement(block_statements)
                or _contains_unscoped_loop_control(block_statements)
            ):
                return None
            statements.extend(block_statements)
            nodes.add(current)
            terminator = block.terminator
            if isinstance(terminator, (Return, Raise, Reraise)):
                return tuple(statements), terminator, frozenset(nodes)
            if not isinstance(terminator, Jump):
                return None
            previous, current = current, terminator.target

    def continue_path(
        start: str,
        predecessor_id: str,
    ) -> tuple[tuple[Stmt, ...], frozenset[str]] | None:
        statements: list[Stmt] = []
        nodes: set[str] = set()
        current = start
        previous = predecessor_id
        while current != header.id:
            if current not in loop_blocks or current in nodes:
                return None
            block = block_map.get(current)
            if block is None or cfg.predecessors(current) != (previous,):
                return None
            block_statements = _statements_without_trivial_phi_assignments(
                block.statements,
                incoming_blocks=(previous,),
            )
            if (
                block_statements is None
                or _contains_terminal_statement(block_statements)
                or _contains_unscoped_loop_control(block_statements)
            ):
                return None
            statements.extend(block_statements)
            nodes.add(current)
            terminator = block.terminator
            if not isinstance(terminator, Jump):
                return None
            previous, current = current, terminator.target
        return (*statements, Continue()), frozenset(nodes)

    def terminal_continue_branch(block: BasicBlock) -> tuple[If, frozenset[str]] | None:
        terminator = block.terminator
        if not isinstance(terminator, Branch) or terminator.true_target == terminator.false_target:
            return None
        terminal_result = terminal_path(terminator.true_target, block.id)
        continue_result = continue_path(terminator.false_target, block.id)
        terminal_is_true = True
        if terminal_result is None or continue_result is None:
            terminal_result = terminal_path(terminator.false_target, block.id)
            continue_result = continue_path(terminator.true_target, block.id)
            terminal_is_true = False
        if terminal_result is None or continue_result is None:
            return None
        terminal_statements, terminal_terminator, terminal_nodes = terminal_result
        continue_statements, continue_nodes = continue_result
        if terminal_nodes & continue_nodes:
            return None
        branch = If(
            source=terminator.source,
            condition=terminator.condition,
            then_body=(
                (*terminal_statements, terminal_terminator)
                if terminal_is_true
                else continue_statements
            ),
            else_body=(
                continue_statements
                if terminal_is_true
                else (*terminal_statements, terminal_terminator)
            ),
        )
        return branch, frozenset({block.id}) | terminal_nodes | continue_nodes

    def statement_guard_continue(
        block: BasicBlock,
        statements: tuple[Stmt, ...],
    ) -> tuple[If, frozenset[str]] | None:
        """Render a statement-level terminal guard followed by a backedge."""

        if not isinstance(block.terminator, Jump) or len(statements) != 1:
            return None
        guard = statements[0]
        if not isinstance(guard, If):
            return None
        if len(guard.then_body) == 0 and guard.else_body:
            terminal_body = guard.else_body
            terminal_is_true = False
        elif len(guard.else_body) == 0 and guard.then_body:
            terminal_body = guard.then_body
            terminal_is_true = True
        else:
            return None
        if not isinstance(terminal_body[-1], (Return, Raise, Reraise)):
            return None
        if _contains_unscoped_loop_control(terminal_body):
            return None
        continue_result = continue_path(block.terminator.target, block.id)
        if continue_result is None:
            return None
        continue_statements, continue_nodes = continue_result
        branch = If(
            source=guard.source,
            condition=guard.condition,
            then_body=terminal_body if terminal_is_true else continue_statements,
            else_body=continue_statements if terminal_is_true else terminal_body,
        )
        return branch, frozenset({block.id}) | continue_nodes

    def optional_body_entry(
        block: BasicBlock,
    ) -> tuple[tuple[Stmt, ...], frozenset[str]] | None:
        """Render an in-loop optional-arm diamond ending in a terminal guard."""

        terminator = block.terminator
        if not isinstance(terminator, Branch) or terminator.true_target == terminator.false_target:
            return None
        true_block = block_map.get(terminator.true_target)
        false_block = block_map.get(terminator.false_target)
        if (
            true_block is None
            or false_block is None
            or true_block.id == false_block.id
            or true_block.id not in loop_blocks
            or false_block.id not in loop_blocks
            or not isinstance(true_block.terminator, Jump)
            or not isinstance(false_block.terminator, Jump)
            or true_block.terminator.target != false_block.terminator.target
            or cfg.predecessors(true_block.id) != (block.id,)
            or cfg.predecessors(false_block.id) != (block.id,)
        ):
            return None
        join = block_map.get(true_block.terminator.target)
        if (
            join is None
            or join.id not in loop_blocks
            or set(cfg.predecessors(join.id)) != {true_block.id, false_block.id}
        ):
            return None
        true_statements = _statements_without_trivial_phi_assignments(
            true_block.statements,
            incoming_blocks=(block.id,),
        )
        false_statements = _statements_without_trivial_phi_assignments(
            false_block.statements,
            incoming_blocks=(block.id,),
        )
        if true_statements is None or false_statements is None:
            return None
        phi_copies = _direct_phi_join_copies(join, true_block.id, false_block.id)
        if phi_copies is None:
            return None
        if not any(
            isinstance(copy.value, Const)
            and isinstance(copy.value.value, (bool, int, float))
            and (copy.value.value is False or copy.value.value == 0)
            for copy in (*phi_copies[0], *phi_copies[1])
            if isinstance(copy, Assign)
        ):
            # Without an explicit falsey incoming value, projecting a Phi
            # into a short-circuit guard depends on VM-specific truthiness.
            return None
        join_statements = phi_copies[2]
        join_branch = terminal_continue_branch(join)
        if join_branch is None:
            join_branch = statement_guard_continue(join, join_statements)
            if join_branch is not None:
                join_statements = ()
        if join_branch is None:
            return None
        rendered_join, join_nodes = join_branch
        true_copies, false_copies, _ = phi_copies
        arm_if = If(
            source=terminator.source,
            condition=terminator.condition,
            then_body=(*true_statements, *true_copies),
            else_body=(*false_statements, *false_copies),
        )
        return (
            (arm_if, *join_statements, rendered_join),
            frozenset({block.id, true_block.id, false_block.id, join.id}) | join_nodes,
        )

    def body_path(
        start: str,
        predecessor_id: str,
    ) -> tuple[tuple[Stmt, ...], frozenset[str]] | None:
        statements: list[Stmt] = []
        nodes: set[str] = set()
        current = start
        previous = predecessor_id
        while True:
            if current not in loop_blocks or current == header.id or current in nodes:
                return None
            block = block_map.get(current)
            if block is None or cfg.predecessors(current) != (previous,):
                return None
            block_statements = _statements_without_trivial_phi_assignments(
                block.statements,
                incoming_blocks=(previous,),
            )
            if (
                block_statements is None
                or _contains_terminal_statement(block_statements)
                or _contains_unscoped_loop_control(block_statements)
            ):
                return None
            statements.extend(block_statements)
            nodes.add(current)
            terminator = block.terminator
            if isinstance(terminator, Jump):
                if terminator.target == header.id:
                    return (*statements, Continue()), frozenset(nodes)
                previous, current = current, terminator.target
                continue
            if not isinstance(terminator, Branch) or terminator.true_target == terminator.false_target:
                return None
            direct = terminal_continue_branch(block)
            if direct is not None:
                branch, branch_nodes = direct
                return (*statements, branch), frozenset(nodes | branch_nodes)

            # The body may begin with a small optional-arm diamond.  Both
            # arms must enter one join, and only the join may decide between
            # the terminal path and the backedge.  Materialize the join Phi
            # copies on the mutually exclusive arms before rendering it.
            true_block = block_map.get(terminator.true_target)
            false_block = block_map.get(terminator.false_target)
            if (
                true_block is None
                or false_block is None
                or true_block.id == false_block.id
                or true_block.id not in loop_blocks
                or false_block.id not in loop_blocks
                or not isinstance(true_block.terminator, Jump)
                or not isinstance(false_block.terminator, Jump)
                or true_block.terminator.target != false_block.terminator.target
                or cfg.predecessors(true_block.id) != (block.id,)
                or cfg.predecessors(false_block.id) != (block.id,)
            ):
                return None
            join = block_map.get(true_block.terminator.target)
            if (
                join is None
                or join.id not in loop_blocks
                or set(cfg.predecessors(join.id)) != {true_block.id, false_block.id}
            ):
                return None
            true_statements = _statements_without_trivial_phi_assignments(
                true_block.statements,
                incoming_blocks=(block.id,),
            )
            false_statements = _statements_without_trivial_phi_assignments(
                false_block.statements,
                incoming_blocks=(block.id,),
            )
            phi_copies = _direct_phi_join_copies(join, true_block.id, false_block.id)
            if (
                true_statements is None
                or false_statements is None
                or phi_copies is None
            ):
                return None
            join_statements = phi_copies[2]
            join_branch = terminal_continue_branch(join)
            if join_branch is None:
                join_branch = statement_guard_continue(join, join_statements)
                if join_branch is not None:
                    join_statements = ()
            if join_branch is None:
                return None
            rendered_join, join_nodes = join_branch
            true_copies, false_copies, _ = phi_copies
            arm_if = If(
                source=terminator.source,
                condition=terminator.condition,
                then_body=(*true_statements, *true_copies),
                else_body=(*false_statements, *false_copies),
            )
            return (
                (*statements, arm_if, *join_statements, rendered_join),
                frozenset(nodes | {true_block.id, false_block.id, join.id} | join_nodes),
            )

    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=predecessors,
    )
    if header_statements is None:
        return None
    if true_in_loop and false_in_loop:
        if loop.exits:
            return None
        if body_start != header.terminator.true_target:
            return None
        entry_body = optional_body_entry(header)
        if entry_body is None:
            return None
        body_statements, body_nodes = entry_body
        if (body_nodes & loop_blocks) != loop_blocks:
            return None
        body_terminal_nodes = body_nodes - loop_blocks
        reachable = _reachable_block_ids(cfg, cfg.entry)
        if reachable != {preheader.id, *loop_blocks, *body_terminal_nodes}:
            return None
        return _rewrite_while_structured_function(
            function,
            (
                *preheader.statements,
                While(
                    source=header.terminator.source,
                    condition=Const(source=header.terminator.source, value=True),
                    body=(*header_statements, *body_statements),
                ),
            ),
            None,
            "exact-loop-with-terminal-body-branch",
        )
    body_result = body_path(body_start, header.id)
    if body_result is None:
        return None
    body_statements, body_nodes = body_result
    if (body_nodes & loop_blocks) != loop_blocks - {header.id}:
        return None
    body_terminal_nodes = body_nodes - loop_blocks
    if not body_terminal_nodes:
        return None
    header_exit_result = terminal_path(header_exit, header.id)
    if header_exit_result is None:
        return None
    header_exit_statements, header_exit_terminator, header_exit_nodes = header_exit_result
    if body_terminal_nodes & set(header_exit_nodes):
        return None
    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != {preheader.id, *loop_blocks, *body_terminal_nodes, *header_exit_nodes}:
        return None

    body_if = If(
        source=header.terminator.source,
        condition=header.terminator.condition,
        then_body=body_statements if true_in_loop else (Break(),),
        else_body=(Break(),) if true_in_loop else body_statements,
    )
    loop_statement = While(
        source=header.terminator.source,
        condition=Const(source=header.terminator.source, value=True),
        body=(*header_statements, body_if),
    )
    return _rewrite_while_structured_function(
        function,
        (*preheader.statements, loop_statement, *header_exit_statements),
        header_exit_terminator,
        "exact-loop-with-terminal-body-branch",
    )


def _structure_exact_tree_loop_with_terminal_exits(function: FunctionIR) -> FunctionIR | None:
    """Collapse a loop whose body is a disjoint decision tree.

    Each tree leaf is either a terminal instruction outside the loop or a
    linear path back to the header.  The proof rejects joins and Phi values;
    consequently every execution visits each rendered node at most once and
    branch-local effects remain in their original order.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    loop_blocks = frozenset(loop.blocks)
    if not loop.exits:
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, loop_blocks):
        return None
    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None
    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop_blocks)
    if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_terminal_statement(preheader.statements)
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    true_in_loop = header.terminator.true_target in loop_blocks
    false_in_loop = header.terminator.false_target in loop_blocks
    if true_in_loop == false_in_loop:
        return None
    body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
    header_exit = header.terminator.false_target if true_in_loop else header.terminator.true_target
    if body_start == header.id or header_exit in loop_blocks:
        return None
    if any(
        member != header.id
        and (
            len(cfg.predecessors(member)) != 1
            or cfg.predecessors(member)[0] not in loop_blocks
        )
        for member in loop_blocks
    ):
        return None

    def terminal_path(
        start: str,
        predecessor_id: str,
    ) -> tuple[tuple[Stmt, ...], Terminator, frozenset[str]] | None:
        statements: list[Stmt] = []
        nodes: set[str] = set()
        current = start
        previous = predecessor_id
        while True:
            if current in loop_blocks or current in nodes:
                return None
            block = block_map.get(current)
            if block is None or cfg.predecessors(current) != (previous,):
                return None
            block_statements = _statements_without_trivial_phi_assignments(
                block.statements,
                incoming_blocks=(previous,),
            )
            if (
                block_statements is None
                or _contains_terminal_statement(block_statements)
                or _contains_unscoped_loop_control(block_statements)
            ):
                return None
            statements.extend(block_statements)
            nodes.add(current)
            terminator = block.terminator
            if isinstance(terminator, (Return, Raise, Reraise)):
                return tuple(statements), terminator, frozenset(nodes)
            if not isinstance(terminator, Jump):
                return None
            previous, current = current, terminator.target

    def render_tree(
        start: str,
        predecessor_id: str,
        path: frozenset[str],
    ) -> tuple[tuple[Stmt, ...], frozenset[str], frozenset[str]] | None:
        if start == header.id:
            return (Continue(),), frozenset(), frozenset({"continue"})
        if start not in loop_blocks or start in path:
            return None
        block = block_map.get(start)
        if block is None or cfg.predecessors(start) != (predecessor_id,):
            return None
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(predecessor_id,),
        )
        if (
            statements is None
            or _contains_terminal_statement(statements)
            or _contains_unscoped_loop_control(statements)
        ):
            return None
        next_path = path | {start}
        terminator = block.terminator
        if isinstance(terminator, Jump):
            if terminator.target == header.id:
                return (*statements, Continue()), frozenset({start}), frozenset({"continue"})
            child = render_tree(terminator.target, start, next_path)
            if child is None:
                terminal = terminal_path(terminator.target, start)
                if terminal is None:
                    return None
                terminal_statements, terminal_terminator, terminal_nodes = terminal
                return (
                    (*statements, *terminal_statements, terminal_terminator),
                    frozenset({start}) | terminal_nodes,
                    frozenset({"terminal"}),
                )
            child_statements, child_nodes, child_outcomes = child
            return (
                (*statements, *child_statements),
                frozenset({start}) | child_nodes,
                child_outcomes,
            )
        if not isinstance(terminator, Branch) or terminator.true_target == terminator.false_target:
            return None

        def render_target(target: str) -> tuple[tuple[Stmt, ...], frozenset[str], frozenset[str]] | None:
            if target in loop_blocks:
                return render_tree(target, start, next_path)
            terminal = terminal_path(target, start)
            if terminal is None:
                return None
            terminal_statements, terminal_terminator, terminal_nodes = terminal
            return (
                (*terminal_statements, terminal_terminator),
                terminal_nodes,
                frozenset({"terminal"}),
            )

        then_result = render_target(terminator.true_target)
        else_result = render_target(terminator.false_target)
        if then_result is None or else_result is None:
            return None
        then_body, then_nodes, then_outcomes = then_result
        else_body, else_nodes, else_outcomes = else_result
        if then_nodes & else_nodes or then_nodes & set(path) or else_nodes & set(path):
            return None
        branch = If(
            source=terminator.source,
            condition=terminator.condition,
            then_body=then_body,
            else_body=else_body,
        )
        return (
            (*statements, branch),
            frozenset({start}) | then_nodes | else_nodes,
            then_outcomes | else_outcomes,
        )

    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=predecessors,
    )
    if header_statements is None:
        return None
    body_result = render_tree(body_start, header.id, frozenset({header.id}))
    if body_result is None:
        return None
    body_statements, body_nodes, outcomes = body_result
    if not {"terminal", "continue"} <= outcomes:
        return None
    if (body_nodes & loop_blocks) != loop_blocks - {header.id}:
        return None
    body_terminal_nodes = body_nodes - loop_blocks
    header_exit_result = terminal_path(header_exit, header.id)
    if header_exit_result is None:
        return None
    header_exit_statements, header_exit_terminator, header_exit_nodes = header_exit_result
    if body_terminal_nodes & set(header_exit_nodes):
        return None
    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != {preheader.id, *loop_blocks, *body_terminal_nodes, *header_exit_nodes}:
        return None
    body_if = If(
        source=header.terminator.source,
        condition=header.terminator.condition,
        then_body=body_statements if true_in_loop else (Break(),),
        else_body=(Break(),) if true_in_loop else body_statements,
    )
    loop_statement = While(
        source=header.terminator.source,
        condition=Const(source=header.terminator.source, value=True),
        body=(*header_statements, body_if),
    )
    return _rewrite_while_structured_function(
        function,
        (*preheader.statements, loop_statement, *header_exit_statements),
        header_exit_terminator,
        "exact-tree-loop-with-terminal-exits",
    )


def _structure_exact_statement_guard_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a linear loop whose pretest is already a statement-level guard.

    Some VM lifters recover a conditional terminator inside a basic block before
    the low-level CFG pass runs.  The resulting shape is still generic:
    ``preheader -> header(if guard; jump body) -> linear body -> header``.
    This reducer moves only the proven terminal arm after a ``while`` and keeps
    every body statement in its original order.  It deliberately rejects
    multiple backedges, exits, exceptional edges, side entries, and any guard
    arm that is not exactly a terminal statement.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    loops = find_natural_loops(cfg)
    if len(loops) != 1:
        return None
    loop = loops[0]
    if loop.exits:
        return None
    region_graph = RegionGraph.from_cfg(cfg)
    if not _region_loop_edges_are_reducible(region_graph, frozenset(loop.blocks)):
        return None

    block_map = cfg.blocks
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Jump):
        return None
    if not header.statements or not isinstance(header.statements[-1], If):
        return None
    guard = header.statements[-1]
    header_prefix = _statements_without_trivial_phi_assignments(
        header.statements[:-1],
        incoming_blocks=cfg.predecessors(header.id),
    )
    if (
        header_prefix is None
        or _contains_terminal_statement(header_prefix)
        or _contains_unscoped_loop_control(header_prefix)
    ):
        return None
    if len(guard.then_body) == 0 and len(guard.else_body) == 1:
        body_condition = guard.condition
        terminal_body = guard.else_body
    elif len(guard.else_body) == 0 and len(guard.then_body) == 1:
        body_condition = UnaryOp(
            source=guard.condition.source,
            type=guard.condition.type,
            op="not ",
            value=guard.condition,
        )
        terminal_body = guard.then_body
    else:
        return None
    terminal = terminal_body[0]
    if not isinstance(terminal, (Return, Raise, Reraise)):
        return None

    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop.blocks)
    if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    if set(block_map) != set(loop.blocks) | {preheader.id}:
        return None

    # The loop body is one exact predecessor chain.  This proves that moving
    # the guard to a while condition neither duplicates a shared block nor
    # drops a hidden side entry.
    body_statements: list[Stmt] = []
    visited: set[str] = {header.id}
    previous_id = header.id
    current_id = header.terminator.target
    while current_id != header.id:
        if current_id in visited or current_id not in loop.blocks:
            return None
        block = block_map.get(current_id)
        if block is None or cfg.predecessors(current_id) != (previous_id,):
            return None
        statements = _statements_without_trivial_phi_assignments(
            block.statements,
            incoming_blocks=(previous_id,),
        )
        if (
            statements is None
            or _contains_unscoped_loop_control(statements)
            or not isinstance(block.terminator, Jump)
        ):
            return None
        body_statements.extend(statements)
        visited.add(current_id)
        previous_id = current_id
        current_id = block.terminator.target
    if visited != set(loop.blocks) or not body_statements:
        return None

    if header_prefix:
        # Keep per-iteration preparation before the guard.  Hoisting it into
        # the while condition would change evaluation order for non-pure
        # generic expressions.
        if body_condition is guard.condition:
            guard_then = tuple(body_statements)
            guard_else = (terminal,)
        else:
            guard_then = (terminal,)
            guard_else = tuple(body_statements)
        loop_statement = While(
            source=guard.source,
            condition=Const(source=guard.source, value=True),
            body=(
                *header_prefix,
                If(
                    source=guard.source,
                    condition=guard.condition,
                    then_body=guard_then,
                    else_body=guard_else,
                ),
            ),
        )
        terminal = None
    else:
        loop_statement = While(
            source=guard.source,
            condition=body_condition,
            body=tuple(body_statements),
        )
    return _rewrite_while_structured_function(
        function,
        (*preheader.statements, loop_statement),
        terminal,
        "exact-statement-guard-loop",
    )


def _structure_exact_nested_loop_flattening(function: FunctionIR) -> FunctionIR | None:
    """Flatten a guarded child loop whose backedge is equivalent to outer continue.

    Ghidra can collapse a child loop before deciding whether its exits belong to
    the containing loop.  The generic renderer cannot represent labelled
    ``continue`` directly, so this pass handles the narrower equivalent shape
    proven by :func:`_safe_nested_loop_continue_aliases`.  It is a normalizer:
    the resulting function remains on the low-level CFG floor so a subsequent
    pass can structure the containing loop.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        header = block_map.get(loop.header)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        true_in_loop = header.terminator.true_target in loop.blocks
        false_in_loop = header.terminator.false_target in loop.blocks
        if true_in_loop == false_in_loop:
            continue
        body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
        aliases = _safe_nested_loop_continue_aliases(
            cfg,
            loop,
            header=header,
            body_start=body_start,
            condition=header.terminator.condition,
            body_on_true=true_in_loop,
        )
        if not aliases:
            continue
        structured = _collapse_exact_natural_loop_region(function, cfg, loop)
        if structured is not None and _low_level_edge_count(structured) < _low_level_edge_count(function):
            return structured
    return None


def _structure_exact_innermost_natural_loop_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse one proven single-entry/single-exit innermost loop in place.

    Unlike the whole-function matcher, this leaves the surrounding CFG in
    place. Reapplying the normalizer therefore exposes enclosing loops without
    relying on VM, frontend, source-language, or corpus-specific information.
    Only phi nodes that are identity copies on every incoming edge are removed;
    every other value merge rejects the candidate.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if any(
            other is not loop and other.blocks < loop.blocks
            for other in loops
        ):
            continue
        if any(
            other is not loop
            and other.blocks & loop.blocks
            and not (other.blocks <= loop.blocks or loop.blocks <= other.blocks)
            for other in loops
        ):
            continue
        structured = _collapse_exact_natural_loop_region(function, cfg, loop)
        if structured is not None:
            return structured
    return None


def _structure_exact_entry_natural_loop_region(function: FunctionIR) -> FunctionIR | None:
    """Collapse a proven loop whose header is the function entry.

    Some VMs enter a loop header directly instead of emitting a separate
    preheader jump.  This is a CFG fact, not a language convention.  The
    usual natural-loop reducer intentionally requires a preheader so it can
    append the recovered ``While`` there; this companion rule supplies the
    equivalent proof for an entry header, where no external edge can exist.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if loop.header != cfg.entry:
            continue
        if any(other is not loop and other.blocks < loop.blocks for other in loops):
            continue
        if any(
            other is not loop
            and other.blocks & loop.blocks
            and not (other.blocks <= loop.blocks or loop.blocks <= other.blocks)
            for other in loops
        ):
            continue
        header = block_map.get(loop.header)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        if any(
            block_id != header.id
            and any(predecessor not in loop.blocks for predecessor in cfg.predecessors(block_id))
            for block_id in loop.blocks
        ):
            continue
        exits = tuple(edge for edge in loop.exits if edge.target not in loop.blocks)
        exit_targets = {edge.target for edge in exits}
        if len(exit_targets) != 1:
            continue
        exit_id = next(iter(exit_targets))
        exit_block = block_map.get(exit_id)
        if exit_block is None or any(predecessor not in loop.blocks for predecessor in cfg.predecessors(exit_id)):
            continue
        if not region_graph.is_single_entry_loop(
            frozenset(loop.blocks),
            header=header.id,
            preheader=None,
            exit=exit_id,
        ):
            continue
        true_in_loop = header.terminator.true_target in loop.blocks
        false_in_loop = header.terminator.false_target in loop.blocks
        if true_in_loop == false_in_loop:
            continue
        body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
        if body_start == header.id or body_start == exit_id:
            continue
        body_result = _render_loop_body(
            block_map,
            body_start,
            header_id=header.id,
            exit_id=exit_id,
            loop_blocks=loop.blocks,
            path=frozenset(),
        )
        if body_result is None or not body_result[1]:
            continue
        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=cfg.predecessors(header.id),
        )
        exit_statements = _statements_without_trivial_phi_assignments(
            exit_block.statements,
            incoming_blocks=cfg.predecessors(exit_id),
        )
        if header_statements is None or exit_statements is None:
            continue
        condition = header.terminator.condition
        if not true_in_loop:
            condition = UnaryOp(source=condition.source, type=condition.type, op="not ", value=condition)
        body, _has_effect = body_result
        loop_statement = (
            While(
                condition=Const(value=True),
                body=(*header_statements, If(condition=condition, then_body=body, else_body=(Break(),))),
            )
            if header_statements
            else While(condition=condition, body=body)
        )
        collapsed_header = BasicBlock(
            id=header.id,
            statements=(loop_statement, *exit_statements),
            terminator=exit_block.terminator,
        )
        return FunctionIR(
            name=function.name,
            params=function.params,
            blocks=tuple(
                collapsed_header
                if block.id == header.id
                else block
                for block in function.blocks
                if (block.id not in loop.blocks or block.id == header.id) and block.id != exit_id
            ),
            nested_functions=function.nested_functions,
            source=function.source,
            recovery_kind=function.recovery_kind,
            metadata={
                **function.metadata,
                "low_level_cfg_structured": "exact-entry-natural-loop-region",
            },
        )
    return None


def _structure_exact_entry_posttested_self_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover a two-block entry loop whose test follows every body execution.

    The header must be the entry and its only in-function successor apart from
    the terminal continuation must be itself.  Therefore its statements run
    before every test, including the first one; representing that as
    ``while true`` with a conditional ``break`` is exact, unlike projecting it
    to a pre-tested ``while condition``.  This is a pure CFG property shared
    by VMs that lower post-tested loops this way.
    """

    if len(function.blocks) == 2:
        prelude = None
        header, exit_block = function.blocks
    elif len(function.blocks) == 3:
        prelude, header, exit_block = function.blocks
        if not isinstance(prelude.terminator, Jump) or prelude.terminator.target != header.id:
            return None
    else:
        return None
    if not isinstance(header.terminator, Branch):
        return None
    if not isinstance(exit_block.terminator, Return):
        return None
    cfg = build_cfg(function)
    if cfg.entry != (prelude.id if prelude is not None else header.id) or cfg.diagnostics:
        return None
    expected_header_predecessors = (
        (header.id,) if prelude is None else (prelude.id, header.id)
    )
    if cfg.predecessors(header.id) != expected_header_predecessors:
        return None
    if cfg.predecessors(exit_block.id) != (header.id,):
        return None
    terminator = header.terminator
    if {terminator.true_target, terminator.false_target} != {header.id, exit_block.id}:
        return None
    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=expected_header_predecessors,
    )
    exit_statements = _statements_without_trivial_phi_assignments(
        exit_block.statements,
        incoming_blocks=(header.id,),
    )
    if header_statements is None or exit_statements is None:
        return None
    break_condition: Expr = terminator.condition
    if terminator.true_target == header.id:
        break_condition = UnaryOp(
            source=break_condition.source,
            type=break_condition.type,
            op="not ",
            value=break_condition,
        )
    return _rewrite_while_structured_function(
        function,
        (
            *(prelude.statements if prelude is not None else ()),
            While(
                condition=Const(value=True),
                body=(*header_statements, If(condition=break_condition, then_body=(Break(),))),
            ),
            *exit_statements,
        ),
        exit_block.terminator,
        "exact-entry-posttested-self-loop",
    )


def _structure_exact_infinite_loop_with_optional_arm(function: FunctionIR) -> FunctionIR | None:
    """Collapse a single-entry infinite loop with one optional linear arm.

    This is the smallest non-trivial form of Ghidra's infinite-loop collapse:
    the loop header evaluates a branch, one edge executes an exclusive arm,
    both edges meet at a latch, and the latch jumps back to the header.  The
    loop has no exits, so the exact structured form is ``while (true)`` with
    the header condition represented as an inner ``if``.  Keeping the inner
    test (rather than projecting it into the while condition) preserves the
    header's per-iteration statements and evaluation order.

    The matcher deliberately accepts only a complete, three-node loop body
    with one optional preheader.  Any extra entry, nested loop, terminal
    statement, exceptional edge, or non-identity Phi keeps the low-level CFG
    intact for a later, more capable proof.
    """

    if not function.blocks:
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if loop.exits or len(loop.blocks) != 3:
            continue
        if any(other is not loop and other.blocks < loop.blocks for other in loops):
            continue
        if any(
            other is not loop
            and other.blocks & loop.blocks
            and not (other.blocks <= loop.blocks or loop.blocks <= other.blocks)
            for other in loops
        ):
            continue

        header = block_map.get(loop.header)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        outside = tuple(predecessor for predecessor in cfg.predecessors(header.id) if predecessor not in loop.blocks)
        if len(outside) > 1:
            continue
        preheader = block_map.get(outside[0]) if outside else None
        if preheader is None and cfg.entry != header.id:
            continue
        if preheader is not None:
            if cfg.entry != preheader.id or not isinstance(preheader.terminator, Jump):
                continue
            if preheader.terminator.target != header.id or set(cfg.predecessors(preheader.id)):
                continue

        expected_header_predecessors = (
            {loop.backedge_source}
            if preheader is None
            else {preheader.id, loop.backedge_source}
        )
        if set(cfg.predecessors(header.id)) != expected_header_predecessors:
            continue
        targets = (header.terminator.true_target, header.terminator.false_target)
        if targets[0] == targets[1] or any(target not in loop.blocks for target in targets):
            continue
        first = block_map.get(targets[0])
        second = block_map.get(targets[1])
        if first is None or second is None:
            continue

        arm: BasicBlock | None = None
        latch: BasicBlock | None = None
        arm_is_true = False
        for candidate_arm, candidate_latch, is_true in (
            (first, second, True),
            (second, first, False),
        ):
            if (
                isinstance(candidate_arm.terminator, Jump)
                and candidate_arm.terminator.target == candidate_latch.id
                and candidate_latch.terminator is not None
                and isinstance(candidate_latch.terminator, Jump)
                and candidate_latch.terminator.target == header.id
                and set(cfg.predecessors(candidate_arm.id)) == {header.id}
                and set(cfg.predecessors(candidate_latch.id)) == {header.id, candidate_arm.id}
            ):
                arm, latch, arm_is_true = candidate_arm, candidate_latch, is_true
                break
        if arm is None or latch is None:
            continue

        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=cfg.predecessors(header.id),
        )
        arm_statements = _statements_without_trivial_phi_assignments(
            arm.statements,
            incoming_blocks=(header.id,),
        )
        latch_statements = _statements_without_trivial_phi_assignments(
            latch.statements,
            incoming_blocks=(header.id, arm.id),
        )
        preheader_statements = (
            _statements_without_trivial_phi_assignments(
                preheader.statements,
                incoming_blocks=cfg.predecessors(preheader.id),
            )
            if preheader is not None
            else ()
        )
        if any(statements is None for statements in (header_statements, arm_statements, latch_statements, preheader_statements)):
            continue
        if not arm_statements:
            # With an empty optional arm the branch still evaluates its
            # condition, but replacing it with an empty structured ``If``
            # adds no recovered structure and is easy for a renderer to erase
            # or misread. Keep that shape on the explicit CFG floor.
            continue
        if any(
            isinstance(statement, (Return, Raise, Reraise))
            for statements in (header_statements, arm_statements, latch_statements)
            for statement in statements
        ):
            continue
        if any(
            _contains_unscoped_loop_control(statements)
            for statements in (header_statements, arm_statements, latch_statements, preheader_statements)
        ):
            continue

        branch = If(
            source=header.terminator.source,
            condition=header.terminator.condition,
            then_body=arm_statements if arm_is_true else (),
            else_body=() if arm_is_true else arm_statements,
        )
        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, branch, *latch_statements),
        )
        return _rewrite_while_structured_function(
            function,
            (*preheader_statements, loop_statement),
            None,
            "exact-infinite-loop-with-optional-arm",
        )
    return None


def _structure_exact_posttested_natural_loop(function: FunctionIR) -> FunctionIR | None:
    """Recover a single-entry post-tested loop with an unconditional header.

    Some bytecode layouts execute a loop header's assignments and terminal
    checks before an unconditional jump into the body.  The body has one
    backedge to that header, so the header is executed once per iteration but
    has no branch terminator of its own.  This is a neutral CFG property: the
    rewrite is accepted only for a complete, innermost natural loop with one
    preheader, one backedge, no external exits, and simple two-edge phi
    values.  The backedge phi copies are materialized immediately before each
    structured ``continue`` so the next header execution observes the exact
    edge value.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    loops = find_natural_loops(cfg)
    for loop in sorted(loops, key=lambda item: (len(item.blocks), item.header, item.backedge_source)):
        if any(other is not loop and other.blocks < loop.blocks for other in loops):
            continue
        if any(
            other is not loop
            and other.blocks & loop.blocks
            and not (other.blocks <= loop.blocks or loop.blocks <= other.blocks)
            for other in loops
        ):
            continue
        if loop.exits:
            continue
        header = block_map.get(loop.header)
        if header is None or not isinstance(header.terminator, Jump):
            continue
        predecessors = cfg.predecessors(header.id)
        preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop.blocks)
        if len(preheaders) != 1:
            continue
        preheader = block_map.get(preheaders[0])
        if preheader is None or not isinstance(preheader.terminator, Jump) or preheader.terminator.target != header.id:
            continue
        # A terminal guard in the preheader may select a result that differs
        # from the loop's terminal arms.  Keeping that surrounding CFG avoids
        # conflating distinct return paths when the loop itself has no normal
        # exit edge.
        if _contains_terminal_statement(preheader.statements) or _contains_unscoped_loop_control(preheader.statements):
            continue
        backedges = tuple(predecessor for predecessor in predecessors if predecessor in loop.blocks)
        if len(backedges) != 1 or backedges[0] != loop.backedge_source:
            continue
        if not _region_loop_edges_are_reducible(region_graph, frozenset(loop.blocks)):
            continue
        body_start = header.terminator.target
        if body_start not in loop.blocks or body_start == header.id:
            continue
        # This rule deliberately consumes a complete reachable function.  A
        # normal continuation would need a separate exit proof; without one,
        # projecting the loop to a carrier Return could invent termination.
        reachable = _reachable_block_ids(cfg, cfg.entry)
        if reachable != set(loop.blocks) | {preheader.id} or reachable != set(block_map):
            continue

        phi_copies = _direct_phi_join_copies(header, preheader.id, backedges[0])
        if phi_copies is None:
            continue
        preheader_copies, backedge_copies, header_statements = phi_copies
        body_result = _render_loop_body(
            block_map,
            body_start,
            header_id=header.id,
            exit_id=header.id,
            loop_blocks=loop.blocks,
            path=frozenset(),
        )
        if body_result is None:
            continue
        body, body_effect = body_result
        if backedge_copies and _contains_continue_in_exception_scope(body):
            # A Continue reached from an exception handler does not identify
            # the same normal CFG edge as a loop backedge.  Do not inject the
            # normal-edge Phi copies into that handler or guess its state.
            continue
        body = _prepend_before_continues(body, backedge_copies)
        if not header_statements and not body_effect and not _contains_terminal_statement(body):
            continue
        if not _contains_terminal_statement((*header_statements, *body)):
            continue

        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, *body),
        )
        return _rewrite_while_structured_function(
            function,
            (*preheader.statements, *preheader_copies, loop_statement),
            Return(source=header.terminator.source),
            "exact-posttested-natural-loop",
        )
    return None


def _prepend_before_continues(
    statements: tuple[Stmt, ...],
    prefix: tuple[Stmt, ...],
) -> tuple[Stmt, ...]:
    """Place edge copies immediately before loop-local Continue statements."""

    if not prefix:
        return statements
    rewritten: list[Stmt] = []
    for statement in statements:
        if isinstance(statement, Continue):
            rewritten.extend((*prefix, statement))
        elif isinstance(statement, If):
            rewritten.append(
                replace(
                    statement,
                    then_body=_prepend_before_continues(statement.then_body, prefix),
                    else_body=_prepend_before_continues(statement.else_body, prefix),
                )
            )
        elif isinstance(statement, Switch):
            rewritten.append(
                replace(
                    statement,
                    cases=tuple(
                        (value, _prepend_before_continues(body, prefix))
                        for value, body in statement.cases
                    ),
                    default_body=_prepend_before_continues(statement.default_body, prefix),
                )
            )
        elif isinstance(statement, Try):
            # Exception handlers have distinct control-flow and Phi incoming
            # edges.  They are rejected by the caller when a normal backedge
            # prefix is required, so leave the subtree opaque here.
            rewritten.append(statement)
        else:
            # A nested loop owns its Continue targets.  Do not inject outer
            # edge copies into that loop's body.
            rewritten.append(statement)
    return tuple(rewritten)


def _contains_continue_in_exception_scope(statements: tuple[Stmt, ...]) -> bool:
    """Return whether a Continue occurs inside a Try body or handler.

    Nested loop bodies own their Continue statements and therefore stop the
    search; ordinary If/Switch nesting does not introduce a new loop scope.
    """

    for statement in statements:
        if isinstance(statement, Try):
            if _contains_unscoped_continue(statement.body) or any(
                _contains_unscoped_continue(handler.body)
                for handler in statement.handlers
            ):
                return True
            if _contains_continue_in_exception_scope(statement.body) or any(
                _contains_continue_in_exception_scope(handler.body)
                for handler in statement.handlers
            ):
                return True
        elif isinstance(statement, If):
            if _contains_continue_in_exception_scope(statement.then_body) or _contains_continue_in_exception_scope(
                statement.else_body
            ):
                return True
        elif isinstance(statement, Switch):
            if any(
                _contains_continue_in_exception_scope(body)
                for _value, body in statement.cases
            ) or _contains_continue_in_exception_scope(statement.default_body):
                return True
        # While/For bodies own their Continue statements.
    return False


def _contains_unscoped_continue(statements: tuple[Stmt, ...]) -> bool:
    """Find Continue statements without descending into nested loop scopes."""

    for statement in statements:
        if isinstance(statement, Continue):
            return True
        if isinstance(statement, If) and (
            _contains_unscoped_continue(statement.then_body)
            or _contains_unscoped_continue(statement.else_body)
        ):
            return True
        if isinstance(statement, Switch) and (
            any(_contains_unscoped_continue(body) for _value, body in statement.cases)
            or _contains_unscoped_continue(statement.default_body)
        ):
            return True
        if isinstance(statement, Try) and (
            _contains_unscoped_continue(statement.body)
            or any(_contains_unscoped_continue(handler.body) for handler in statement.handlers)
        ):
            return True
    return False


def _contains_terminal_statement(statements: tuple[Stmt, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, (Return, Raise, Reraise)):
            return True
        if isinstance(statement, If) and (
            _contains_terminal_statement(statement.then_body)
            or _contains_terminal_statement(statement.else_body)
        ):
            return True
        if isinstance(statement, Switch) and (
            any(_contains_terminal_statement(body) for _value, body in statement.cases)
            or _contains_terminal_statement(statement.default_body)
        ):
            return True
        if isinstance(statement, Try) and (
            _contains_terminal_statement(statement.body)
            or any(_contains_terminal_statement(handler.body) for handler in statement.handlers)
        ):
            return True
    return False


def _contains_unscoped_loop_control(statements: tuple[Stmt, ...]) -> bool:
    """Find break/continue that would be outside a loop after a rewrite.

    ``If``, ``Switch`` and ``Try`` do not introduce loop scope, while a nested
    loop owns its own control statements.  The helper therefore descends only
    through the former constructs.  It is used as a fail-closed preheader
    guard before statements are moved outside the newly materialized loop.
    """

    for statement in statements:
        if isinstance(statement, (Break, Continue)):
            return True
        if isinstance(statement, If) and (
            _contains_unscoped_loop_control(statement.then_body)
            or _contains_unscoped_loop_control(statement.else_body)
        ):
            return True
        if isinstance(statement, Switch) and (
            any(_contains_unscoped_loop_control(body) for _value, body in statement.cases)
            or _contains_unscoped_loop_control(statement.default_body)
        ):
            return True
        if isinstance(statement, Try) and (
            _contains_unscoped_loop_control(statement.body)
            or any(_contains_unscoped_loop_control(handler.body) for handler in statement.handlers)
        ):
            return True
    return False


def _structure_exact_natural_loop_with_shared_exit(function: FunctionIR) -> FunctionIR | None:
    """Recover a natural loop whose distinct exits share one linear tail.

    Ghidra first collapses loop bodies and then folds exit paths that
    reconverge at one block.  This matcher implements the conservative subset
    that can be proven from generic IR alone: every non-loop exit follows a
    disjoint, jump-only path to one terminal join, and leading join phis can be
    copied on the corresponding path.  The loop condition is kept in a
    ``while (true)`` carrier so statements on a header-exit path execute at
    the exact point where the low-level branch leaves the loop.
    """

    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics or _has_exceptional_block_context(function):
        return None
    block_map = cfg.blocks
    loops = find_natural_loops(cfg)
    groups: dict[str, list[object]] = {}
    for loop in loops:
        groups.setdefault(loop.header, []).append(loop)

    candidates: list[tuple[str, frozenset[str], tuple[object, ...]]] = []
    for header_id, group in groups.items():
        unions = [frozenset(loop.blocks) for loop in group]
        if len(group) > 1:
            unions.append(frozenset().union(*unions))
        for members in dict.fromkeys(unions):
            if not members:
                continue
            exits = tuple(
                edge
                for edge in cfg.edges
                if edge.source in members and edge.target not in members
            )
            if exits:
                candidates.append((header_id, members, exits))

    for header_id, loop_blocks, exits in sorted(
        candidates,
        key=lambda item: (len(item[1]), item[0], tuple((edge.source, edge.target) for edge in item[2])),
    ):
        header = block_map.get(header_id)
        if header is None or not isinstance(header.terminator, Branch):
            continue
        if any(
            other.header != header_id
            and other.blocks & loop_blocks
            and not (other.blocks <= loop_blocks or loop_blocks <= other.blocks)
            for other in loops
        ):
            continue
        predecessors = cfg.predecessors(header_id)
        preheaders = tuple(pred for pred in predecessors if pred not in loop_blocks)
        if len(preheaders) != 1:
            continue
        preheader = block_map.get(preheaders[0])
        if preheader is None or not isinstance(preheader.terminator, Jump):
            continue
        if (
            preheader.terminator.target != header_id
            or _contains_terminal_statement(preheader.statements)
            or _contains_unscoped_loop_control(preheader.statements)
        ):
            continue
        if any(
            block_id != header_id
            and any(pred not in loop_blocks for pred in cfg.predecessors(block_id))
            for block_id in loop_blocks
        ):
            continue
        if len({edge.target for edge in exits}) < 2:
            continue

        # Trace each exit through a linear, disjoint tail.  The first common
        # block among all paths is the only permitted continuation join.
        paths: list[tuple[object, tuple[str, ...]]] = []
        for edge in exits:
            current = edge.target
            path: list[str] = []
            seen: set[str] = set()
            while current not in seen and current not in loop_blocks:
                seen.add(current)
                block = block_map.get(current)
                if block is None:
                    break
                path.append(current)
                if not isinstance(block.terminator, Jump):
                    break
                current = block.terminator.target
            else:
                continue
            if not path:
                continue
            paths.append((edge, tuple(path)))
        if len(paths) != len(exits):
            continue
        common = set(paths[0][1])
        for _edge, path in paths[1:]:
            common &= set(path)
        if not common:
            continue
        join_id = min(common, key=lambda block_id: (max(path.index(block_id) for _edge, path in paths), block_id))
        join = block_map.get(join_id)
        if join is None or not isinstance(join.terminator, (Return, Raise, Reraise)):
            continue

        join_predecessors = set(cfg.predecessors(join_id))
        if not join_predecessors:
            continue
        phi_result = _final_join_phi_copies(join, join_predecessors)
        if phi_result is None:
            continue
        edge_copies, join_statements = phi_result

        exit_paths: dict[str, tuple[Stmt, ...]] = {}
        path_blocks: set[str] = set()
        path_predecessors: set[str] = set()
        valid_paths = True
        for edge, path in paths:
            try:
                join_index = path.index(join_id)
            except ValueError:
                valid_paths = False
                break
            prefix_ids = path[:join_index]
            if path_blocks & set(prefix_ids):
                valid_paths = False
                break
            if prefix_ids and cfg.predecessors(prefix_ids[0]) != (edge.source,):
                valid_paths = False
                break
            statements: list[Stmt] = []
            previous = edge.source
            for block_id in prefix_ids:
                block = block_map.get(block_id)
                if block is None or not isinstance(block.terminator, Jump):
                    valid_paths = False
                    break
                if cfg.predecessors(block_id) != (previous,):
                    valid_paths = False
                    break
                block_statements = _statements_without_trivial_phi_assignments(
                    block.statements,
                    incoming_blocks=(previous,),
                )
                if block_statements is None or _statements_contain_break(block_statements):
                    valid_paths = False
                    break
                statements.extend(block_statements)
                previous = block_id
            if not valid_paths:
                break
            predecessor_to_join = previous
            if predecessor_to_join not in join_predecessors:
                valid_paths = False
                break
            statements.extend(edge_copies.get(predecessor_to_join, ()))
            if edge.target in exit_paths:
                valid_paths = False
                break
            exit_paths[edge.target] = tuple(statements)
            path_blocks.update(prefix_ids)
            path_predecessors.add(predecessor_to_join)
        if not valid_paths or path_predecessors != join_predecessors:
            continue

        true_target = header.terminator.true_target
        false_target = header.terminator.false_target
        true_in_loop = true_target in loop_blocks
        false_in_loop = false_target in loop_blocks
        if true_in_loop == false_in_loop:
            continue
        body_start = true_target if true_in_loop else false_target
        header_exit_target = false_target if true_in_loop else true_target
        if header_exit_target not in exit_paths:
            continue
        condition = header.terminator.condition
        if not true_in_loop:
            condition = UnaryOp(source=condition.source, type=condition.type, op="not ", value=condition)
        body_result = _render_loop_body(
            block_map,
            body_start,
            header_id=header_id,
            exit_id="",
            loop_blocks=loop_blocks,
            path=frozenset(),
            exit_paths=exit_paths,
        )
        if body_result is None:
            continue
        body, body_effect = body_result
        header_statements = _statements_without_trivial_phi_assignments(
            header.statements,
            incoming_blocks=predecessors,
        )
        if header_statements is None:
            continue
        if not body_effect and not header_statements:
            continue
        guarded_body = If(
            source=header.terminator.source,
            condition=condition,
            then_body=body,
            else_body=(*exit_paths[header_exit_target], Break()),
        )
        loop_statement = While(
            source=header.terminator.source,
            condition=Const(source=header.terminator.source, value=True),
            body=(*header_statements, guarded_body),
        )
        reachable = _reachable_block_ids(cfg, cfg.entry)
        expected = set(loop_blocks) | {preheader.id, join_id} | path_blocks
        if reachable != expected or reachable != set(block_map):
            continue
        return _rewrite_while_structured_function(
            function,
            (*preheader.statements, loop_statement, *join_statements),
            join.terminator,
            "exact-natural-loop-with-shared-exit",
        )
    return None


def _prepare_collapsed_exit_statements(
    exit_block: BasicBlock,
    cfg,
    *,
    loop_exit_predecessors: frozenset[str],
    replacement_predecessor: str,
    outside_predecessors: tuple[str, ...],
) -> tuple[Stmt, ...] | None:
    """Rewrite an exit block's leading phis after collapsing a loop.

    Collapsing a loop changes several outgoing CFG edges into one edge from the
    preheader.  This is exact only when every edge leaving the loop contributes
    the same phi value.  Values are restricted to constants or variables so
    the rewrite never moves an expression with evaluation effects across the
    loop boundary.  Outside predecessors retain their original incoming
    values, while all loop predecessors are replaced by the new preheader edge.
    """

    original_predecessors = set(cfg.predecessors(exit_block.id))
    if not loop_exit_predecessors <= original_predecessors:
        return None
    if replacement_predecessor in original_predecessors:
        return None
    expected_after = tuple((*outside_predecessors, replacement_predecessor))
    if set(expected_after) != (original_predecessors - set(loop_exit_predecessors)) | {replacement_predecessor}:
        return None

    rewritten: list[Stmt] = []
    saw_non_phi = False
    for statement in exit_block.statements:
        if not (
            isinstance(statement, Assign)
            and isinstance(statement.target, Var)
            and isinstance(statement.value, Phi)
        ):
            saw_non_phi = True
            rewritten.append(statement)
            continue
        if saw_non_phi:
            return None
        incoming = dict(statement.value.incoming)
        if not original_predecessors <= set(incoming):
            return None
        if set(incoming) - original_predecessors - {"existing"}:
            return None
        loop_values = [incoming[predecessor] for predecessor in loop_exit_predecessors]
        if not loop_values or not all(isinstance(value, (Const, Var)) for value in loop_values):
            return None
        representative = loop_values[0]
        if not all(_values_semantically_equal(representative, value) for value in loop_values[1:]):
            return None
        existing = incoming.get("existing")
        if existing is not None and not _values_semantically_equal(existing, representative):
            return None

        new_incoming: list[tuple[str, Expr]] = []
        inserted_replacement = False
        for predecessor, value in statement.value.incoming:
            if predecessor in loop_exit_predecessors:
                if not inserted_replacement:
                    new_incoming.append((replacement_predecessor, representative))
                    inserted_replacement = True
                continue
            if predecessor == "existing":
                continue
            new_incoming.append((predecessor, value))
        if not inserted_replacement:
            new_incoming.append((replacement_predecessor, representative))
        if set(predecessor for predecessor, _value in new_incoming) != set(expected_after):
            return None
        rewritten.append(
            Assign(
                source=statement.source,
                target=statement.target,
                value=Phi(
                    source=statement.value.source,
                    type=statement.value.type,
                    incoming=tuple(new_incoming),
                ),
            )
        )

    return _statements_without_trivial_phi_assignments(
        tuple(rewritten),
        incoming_blocks=expected_after,
    )


def _safe_nested_loop_continue_aliases(
    cfg,
    outer_loop,
    *,
    header: BasicBlock,
    body_start: str,
    condition: Expr,
    body_on_true: bool,
) -> frozenset[str]:
    """Find a child-loop header whose guarded backedges can join the outer loop.

    This is the conservative flattening used when a nested loop's only
    non-header entry is a backedge guarded by the same pure condition as the
    enclosing loop.  Re-routing that edge through the enclosing header adds no
    observable work: the guard has just established the condition and all
    intervening blocks are empty jumps.  Any phi, side effect, extra entry, or
    polarity mismatch rejects the alias.
    """

    if header.statements or not _is_pure_cfg_condition(condition):
        return frozenset()
    block_map = cfg.blocks
    aliases: set[str] = set()
    for child in find_natural_loops(cfg):
        if child.header != body_start or not child.blocks < outer_loop.blocks:
            continue
        child_header = block_map.get(child.header)
        if child_header is None:
            continue
        child_predecessors = tuple(cfg.predecessors(child.header))
        outside_entries = tuple(
            predecessor for predecessor in child_predecessors if predecessor not in child.blocks
        )
        if outside_entries != (header.id,):
            continue
        child_header_statements = _statements_without_trivial_phi_assignments(
            child_header.statements,
            incoming_blocks=child_predecessors,
        )
        if child_header_statements is None or any(
            isinstance(statement, Assign) and isinstance(statement.value, Phi)
            for statement in child_header_statements
        ):
            continue
        if any(
            block_id != child.header
            and any(predecessor not in child.blocks for predecessor in cfg.predecessors(block_id))
            for block_id in child.blocks
        ):
            continue
        backedge_sources = tuple(
            predecessor for predecessor in child_predecessors if predecessor in child.blocks
        )
        if not backedge_sources:
            continue
        # A child loop that exits into another block of the containing loop is
        # interleaved with the outer iteration (for example, one arm skips to
        # the outer header while another arm leaves the outer region).  The
        # alias would then collapse two distinct loop scopes and can move the
        # outer guard across observable child-loop work.  Only child loops
        # whose exits leave the entire outer region are eligible.
        if any(
            edge.target in outer_loop.blocks
            for edge in child.exits
            if edge.target not in child.blocks
        ):
            continue
        if not all(
            _backedge_rechecks_condition(
                cfg,
                source,
                child.header,
                condition=condition,
                expected_true=body_on_true,
            )
            for source in backedge_sources
        ):
            continue
        aliases.add(child.header)
    return frozenset(aliases)


def _is_pure_cfg_condition(condition: Expr) -> bool:
    """Accept only conditions whose repeated evaluation is unconditionally safe.

    A variable read may still be dynamic in a VM (globals, captured storage,
    and metamethod-backed values can observe state or raise).  Constants are
    the only expression kind whose value and evaluation effects are guaranteed
    by the generic IR, so all other conditions stay on the CFG floor.
    """

    return isinstance(condition, Const)


def _backedge_rechecks_condition(
    cfg,
    source: str,
    target: str,
    *,
    condition: Expr,
    expected_true: bool,
) -> bool:
    """Prove a child-loop backedge is immediately preceded by the outer guard."""

    block_map = cfg.blocks
    current = source
    visited: set[str] = set()
    while current not in visited:
        visited.add(current)
        predecessors = cfg.predecessors(current)
        if len(predecessors) != 1:
            return False
        predecessor = block_map.get(predecessors[0])
        if predecessor is None:
            return False
        terminator = predecessor.terminator
        if isinstance(terminator, Branch):
            selected_target = terminator.true_target if expected_true else terminator.false_target
            if selected_target != current or not _values_semantically_equal(terminator.condition, condition):
                return False
            return _statements_without_trivial_phi_assignments(
                block_map[current].statements,
                incoming_blocks=(predecessor.id,),
            ) == ()
        if not isinstance(terminator, Jump):
            return False
        if _statements_without_trivial_phi_assignments(
            block_map[current].statements,
            incoming_blocks=(predecessor.id,),
        ) != ():
            return False
        current = predecessor.id
    return False


def _collapse_exact_natural_loop_region(function: FunctionIR, cfg, loop) -> FunctionIR | None:
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None
    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(predecessor for predecessor in predecessors if predecessor not in loop.blocks)
    if len(preheaders) != 1:
        return None
    preheader = block_map.get(preheaders[0])
    if (
        preheader is None
        or not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    if any(
        block_id != header.id
        and any(predecessor not in loop.blocks for predecessor in cfg.predecessors(block_id))
        for block_id in loop.blocks
    ):
        return None

    exits = tuple(edge for edge in loop.exits if edge.target not in loop.blocks)
    continuation_targets = {
        edge.target
        for edge in exits
    }
    if len(continuation_targets) != 1:
        return None
    exit_id = next(iter(continuation_targets))
    exit_block = block_map.get(exit_id)
    if exit_block is None:
        return None
    loop_exit_predecessors = frozenset(edge.source for edge in exits)
    if not loop_exit_predecessors:
        return None
    if {
        predecessor
        for predecessor in cfg.predecessors(exit_id)
        if predecessor in loop.blocks
    } != set(loop_exit_predecessors):
        return None
    outside_exit_predecessors = tuple(
        predecessor
        for predecessor in cfg.predecessors(exit_id)
        if predecessor not in loop.blocks
    )
    if not region_graph.is_single_entry_loop(
        frozenset(loop.blocks),
        header=header.id,
        preheader=preheader.id,
        exit=exit_id,
    ):
        return None

    true_in_loop = header.terminator.true_target in loop.blocks
    false_in_loop = header.terminator.false_target in loop.blocks
    if true_in_loop == false_in_loop:
        return None
    body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
    if body_start == header.id or body_start == exit_id:
        return None
    condition = header.terminator.condition
    if not true_in_loop:
        condition = UnaryOp(source=condition.source, type=condition.type, op="not ", value=condition)

    body_result = _render_loop_body(
        block_map,
        body_start,
        header_id=header.id,
        exit_id=exit_id,
        loop_blocks=loop.blocks,
        path=frozenset(),
    )
    if body_result is None:
        continue_targets = _safe_nested_loop_continue_aliases(
            cfg,
            loop,
            header=header,
            body_start=body_start,
            condition=header.terminator.condition,
            body_on_true=true_in_loop,
        )
        if continue_targets:
            body_result = _render_loop_body(
                block_map,
                body_start,
                header_id=header.id,
                exit_id=exit_id,
                loop_blocks=loop.blocks,
                path=frozenset(),
                continue_targets=continue_targets,
                allow_continue_target_start=True,
            )
    if body_result is None:
        return None
    body, has_effect = body_result
    if not has_effect:
        return None
    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=predecessors,
    )
    exit_statements = _prepare_collapsed_exit_statements(
        exit_block,
        cfg,
        loop_exit_predecessors=loop_exit_predecessors,
        replacement_predecessor=preheader.id,
        outside_predecessors=outside_exit_predecessors,
    )
    if header_statements is None or exit_statements is None:
        return None

    loop_statement: While
    if header_statements:
        loop_statement = While(
            condition=Const(value=True),
            body=(
                *header_statements,
                If(condition=condition, then_body=body, else_body=(Break(),)),
            ),
        )
    else:
        loop_statement = While(condition=condition, body=body)

    collapsed_preheader = BasicBlock(
        id=preheader.id,
        statements=(*preheader.statements, loop_statement),
        terminator=Jump(source=preheader.terminator.source, target=exit_id),
    )
    collapsed_exit = BasicBlock(
        id=exit_block.id,
        statements=exit_statements,
        terminator=exit_block.terminator,
    )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=tuple(
            collapsed_preheader
            if block.id == preheader.id
            else collapsed_exit
            if block.id == exit_id
            else block
            for block in function.blocks
            if block.id not in loop.blocks or block.id == preheader.id
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind=function.recovery_kind,
        metadata={
            **function.metadata,
            "low_level_cfg_structured": "exact-innermost-natural-loop-region",
        },
    )


def _try_structure_single_natural_loop(function: FunctionIR, cfg, loop) -> FunctionIR | None:
    block_map = cfg.blocks
    region_graph = RegionGraph.from_cfg(cfg)
    header = block_map.get(loop.header)
    if header is None or not isinstance(header.terminator, Branch):
        return None
    predecessors = cfg.predecessors(header.id)
    preheaders = tuple(pred for pred in predecessors if pred not in loop.blocks)
    if len(preheaders) != 1 or preheaders[0] != function.blocks[0].id:
        return None
    preheader = block_map[preheaders[0]]
    if (
        not isinstance(preheader.terminator, Jump)
        or preheader.terminator.target != header.id
        or _contains_unscoped_loop_control(preheader.statements)
    ):
        return None
    exits = tuple(edge for edge in loop.exits if edge.target not in loop.blocks)
    exit_targets = {edge.target for edge in exits}
    if len(exit_targets) != 1:
        return None
    exit_id = next(iter(exit_targets))
    if exit_id not in block_map:
        return None
    if not region_graph.is_single_entry_loop(
        frozenset(loop.blocks),
        header=loop.header,
        preheader=preheader.id,
        exit=exit_id,
    ):
        return None

    true_in_loop = header.terminator.true_target in loop.blocks
    false_in_loop = header.terminator.false_target in loop.blocks
    if true_in_loop == false_in_loop:
        return None
    body_start = header.terminator.true_target if true_in_loop else header.terminator.false_target
    condition = header.terminator.condition
    if not true_in_loop:
        condition = UnaryOp(source=condition.source, type=condition.type, op="not ", value=condition)
    if body_start == header.id or body_start == exit_id:
        return None

    # The candidate must be a whole-function loop with a linear tail.
    tail_statements, tail_terminator, tail_blocks = _collect_linear_tail(
        block_map,
        exit_id,
        excluded=loop.blocks,
    )
    if tail_terminator is None or not isinstance(tail_terminator, Return):
        return None
    reachable = _reachable_block_ids(cfg, cfg.entry)
    if reachable != set(loop.blocks) | set(preheaders) | tail_blocks:
        return None

    body_result = _render_loop_body(
        block_map,
        body_start,
        header_id=header.id,
        exit_id=exit_id,
        loop_blocks=loop.blocks,
        path=frozenset(),
    )
    if body_result is None or not body_result[0]:
        return None
    body, has_effect = body_result
    if not has_effect:
        return None

    header_statements = _statements_without_trivial_phi_assignments(
        header.statements,
        incoming_blocks=tuple(predecessors),
    )
    if header_statements is None:
        return None
    if header_statements:
        loop_statement = While(
            condition=Const(value=True),
            body=(
                *header_statements,
                If(condition=condition, then_body=body, else_body=(Break(),)),
            ),
        )
    else:
        loop_statement = While(condition=condition, body=body)
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=(
            BasicBlock(
                id=preheader.id,
                statements=(
                    *preheader.statements,
                    loop_statement,
                    *tail_statements,
                ),
                terminator=tail_terminator,
            ),
        ),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": "exact-single-natural-loop",
        },
    )


def _collect_linear_tail(
    block_map: dict[str, BasicBlock],
    start: str,
    *,
    excluded: frozenset[str],
) -> tuple[tuple[Stmt, ...], Terminator | None, set[str]]:
    statements: list[Stmt] = []
    visited: set[str] = set()
    current = start
    while current not in visited and current not in excluded:
        block = block_map.get(current)
        if block is None:
            return (), None, visited
        visited.add(current)
        statements.extend(block.statements)
        terminator = block.terminator
        if isinstance(terminator, Return):
            return tuple(statements), terminator, visited
        if not isinstance(terminator, Jump):
            return (), None, visited
        current = terminator.target
    return (), None, visited


def _render_loop_body(
    block_map: dict[str, BasicBlock],
    start: str,
    *,
    header_id: str,
    exit_id: str,
    loop_blocks: frozenset[str],
    path: frozenset[str],
    exit_paths: dict[str, tuple[Stmt, ...]] | None = None,
    continue_targets: frozenset[str] = frozenset(),
    allow_continue_target_start: bool = False,
) -> tuple[tuple[Stmt, ...], bool] | None:
    if start == header_id:
        return (Continue(),), False
    if start == exit_id:
        return (Break(),), False
    if start in continue_targets and not allow_continue_target_start:
        return (Continue(),), False
    if exit_paths is not None and start in exit_paths:
        return (*exit_paths[start], Break()), bool(exit_paths[start])
    if start not in loop_blocks or start in path:
        return None
    block = block_map.get(start)
    if block is None:
        return None
    incoming = tuple(
        predecessor_id
        for predecessor_id, predecessor in block_map.items()
        if isinstance(predecessor.terminator, (Jump, Branch, MultiBranch))
        and start in _terminator_targets(predecessor.terminator)
    )
    statements = _statements_without_trivial_phi_assignments(
        block.statements,
        incoming_blocks=incoming,
    )
    if statements is None:
        return None
    next_path = path | {start}
    terminator = block.terminator
    if isinstance(terminator, Jump):
        child = _render_loop_body(
            block_map,
            terminator.target,
            header_id=header_id,
            exit_id=exit_id,
            loop_blocks=loop_blocks,
            path=next_path,
            exit_paths=exit_paths,
            continue_targets=continue_targets,
        )
        if child is None:
            return None
        child_statements, child_effect = child
        return (*statements, *child_statements), bool(statements) or child_effect
    if isinstance(terminator, MultiBranch):
        case_results: list[tuple[Expr, tuple[Stmt, ...], bool]] = []
        for value, target in terminator.cases:
            if any(_values_semantically_equal(value, existing) for existing, _body, _effect in case_results):
                return None
            result = _render_loop_body(
                block_map,
                target,
                header_id=header_id,
                exit_id=exit_id,
                loop_blocks=loop_blocks,
                path=next_path,
                exit_paths=exit_paths,
                continue_targets=continue_targets,
            )
            if result is None:
                return None
            case_results.append((value, *result))
        default_result = _render_loop_body(
            block_map,
            terminator.default_target,
            header_id=header_id,
            exit_id=exit_id,
            loop_blocks=loop_blocks,
            path=next_path,
            exit_paths=exit_paths,
            continue_targets=continue_targets,
        )
        if default_result is None:
            return None
        default_body, default_effect = default_result
        if _statements_contain_break(default_body) or any(
            _statements_contain_break(body) for _value, body, _effect in case_results
        ):
            return None
        if not default_effect and not any(effect for _value, _body, effect in case_results):
            return None
        switch = Switch(
            source=terminator.source,
            selector=terminator.selector,
            cases=tuple((value, body) for value, body, _effect in case_results),
            default_body=default_body,
        )
        return (*statements, switch), bool(statements) or default_effect or any(
            effect for _value, _body, effect in case_results
        )
    if not isinstance(terminator, Branch):
        return None
    phi_join = _render_direct_phi_join(
        block_map,
        block,
        header_id=header_id,
        exit_id=exit_id,
        loop_blocks=loop_blocks,
        path=next_path,
        exit_paths=exit_paths,
        continue_targets=continue_targets,
    )
    if phi_join is not None:
        join_statement, join_effect = phi_join
        return (*statements, *join_statement), bool(statements) or join_effect
    then_result = _render_loop_body(
        block_map,
        terminator.true_target,
        header_id=header_id,
        exit_id=exit_id,
        loop_blocks=loop_blocks,
        path=next_path,
        exit_paths=exit_paths,
        continue_targets=continue_targets,
    )
    else_result = _render_loop_body(
        block_map,
        terminator.false_target,
        header_id=header_id,
        exit_id=exit_id,
        loop_blocks=loop_blocks,
        path=next_path,
        exit_paths=exit_paths,
        continue_targets=continue_targets,
    )
    if then_result is None or else_result is None:
        return None
    then_body, then_effect = then_result
    else_body, else_effect = else_result
    # A branch whose children only choose ``Break``/``Continue`` is still a
    # meaningful loop body when the *body entry* executed statements before
    # making that choice.  Do not propagate this exception through an empty
    # jump chain: doing so could move a condition ahead of an intervening
    # effect and turn a post-tested loop into a pre-tested one.
    if (
        not then_effect
        and not else_effect
        and not (statements and not path)
        and not {"break", "continue"} <= _loop_control_kinds((*then_body, *else_body))
    ):
        return None
    return (
        *statements,
        If(source=terminator.source, condition=terminator.condition, then_body=then_body, else_body=else_body),
    ), bool(statements) or then_effect or else_effect


def _statements_contain_break(statements: tuple[Stmt, ...]) -> bool:
    for statement in statements:
        if isinstance(statement, Break):
            return True
        if isinstance(statement, If) and (
            _statements_contain_break(statement.then_body)
            or _statements_contain_break(statement.else_body)
        ):
            return True
        if isinstance(statement, Switch) and (
            any(_statements_contain_break(body) for _value, body in statement.cases)
            or _statements_contain_break(statement.default_body)
        ):
            return True
    return False


def _loop_control_kinds(statements: tuple[Stmt, ...]) -> frozenset[str]:
    """Collect direct loop-control outcomes from a rendered branch."""

    kinds: set[str] = set()
    for statement in statements:
        if isinstance(statement, Break):
            kinds.add("break")
        elif isinstance(statement, Continue):
            kinds.add("continue")
        elif isinstance(statement, If):
            kinds.update(_loop_control_kinds(statement.then_body))
            kinds.update(_loop_control_kinds(statement.else_body))
        elif isinstance(statement, Switch):
            for _value, body in statement.cases:
                kinds.update(_loop_control_kinds(body))
            kinds.update(_loop_control_kinds(statement.default_body))
        elif isinstance(statement, Try):
            kinds.update(_loop_control_kinds(statement.body))
            for handler in statement.handlers:
                kinds.update(_loop_control_kinds(handler.body))
    return frozenset(kinds)


def _render_direct_phi_join(
    block_map: dict[str, BasicBlock],
    block: BasicBlock,
    *,
    header_id: str,
    exit_id: str,
    loop_blocks: frozenset[str],
    path: frozenset[str],
    exit_paths: dict[str, tuple[Stmt, ...]] | None = None,
    continue_targets: frozenset[str] = frozenset(),
) -> tuple[tuple[Stmt, ...], bool] | None:
    """Render a two-arm diamond whose join has safe, explicit phi copies."""

    terminator = block.terminator
    if not isinstance(terminator, Branch):
        return None
    true_block = block_map.get(terminator.true_target)
    false_block = block_map.get(terminator.false_target)
    if true_block is None or false_block is None or true_block.id == false_block.id:
        return None
    if not isinstance(true_block.terminator, Jump) or not isinstance(false_block.terminator, Jump):
        return None
    if true_block.terminator.target != false_block.terminator.target:
        return None
    join_id = true_block.terminator.target
    join = block_map.get(join_id)
    if join is None or join_id in path or join_id not in loop_blocks:
        return None
    if {
        predecessor_id
        for predecessor_id, predecessor in block_map.items()
        if predecessor.terminator is not None
        if join_id in _terminator_targets(predecessor.terminator)
    } != {true_block.id, false_block.id}:
        return None
    true_statements = _statements_without_trivial_phi_assignments(
        true_block.statements,
        incoming_blocks=(block.id,),
    )
    false_statements = _statements_without_trivial_phi_assignments(
        false_block.statements,
        incoming_blocks=(block.id,),
    )
    phi_copies = _direct_phi_join_copies(join, true_block.id, false_block.id)
    if true_statements is None or false_statements is None or phi_copies is None:
        return None
    true_copies, false_copies, join_statements = phi_copies
    rewritten_blocks = {
        **block_map,
        join_id: BasicBlock(id=join.id, statements=join_statements, terminator=join.terminator),
    }
    continuation = _render_loop_body(
        rewritten_blocks,
        join_id,
        header_id=header_id,
        exit_id=exit_id,
        loop_blocks=loop_blocks,
        path=path | {true_block.id, false_block.id},
        exit_paths=exit_paths,
        continue_targets=continue_targets,
    )
    if continuation is None:
        return None
    continuation_statements, continuation_effect = continuation
    return (
        (
            If(
                source=terminator.source,
                condition=terminator.condition,
                then_body=(*true_statements, *true_copies),
                else_body=(*false_statements, *false_copies),
            ),
            *continuation_statements,
        ),
        bool(true_statements)
        or bool(false_statements)
        or bool(true_copies)
        or bool(false_copies)
        or continuation_effect,
    )


def _direct_phi_join_copies(
    join: BasicBlock,
    true_id: str,
    false_id: str,
) -> tuple[tuple[Stmt, ...], tuple[Stmt, ...], tuple[Stmt, ...]] | None:
    true_copies: list[Stmt] = []
    false_copies: list[Stmt] = []
    targets: set[str] = set()
    remaining: list[Stmt] = []
    saw_non_phi = False
    for statement in join.statements:
        if isinstance(statement, Assign) and isinstance(statement.target, Var) and isinstance(statement.value, Phi):
            if saw_non_phi:
                return None
            if len({block_id for block_id, _value in statement.value.incoming}) != len(statement.value.incoming):
                return None
            incoming = dict(statement.value.incoming)
            if not {true_id, false_id} <= set(incoming) or set(incoming) - {true_id, false_id, "existing"}:
                return None
            true_value = incoming[true_id]
            false_value = incoming[false_id]
            if not isinstance(true_value, (Const, Var)) or not isinstance(false_value, (Const, Var)):
                return None
            existing_value = incoming.get("existing")
            if existing_value is not None and not (
                _values_semantically_equal(existing_value, true_value)
                or _values_semantically_equal(existing_value, false_value)
            ):
                return None
            targets.add(statement.target.name)
            if not (isinstance(true_value, Var) and true_value.name == statement.target.name):
                true_copies.append(Assign(source=statement.source, target=statement.target, value=true_value))
            if not (isinstance(false_value, Var) and false_value.name == statement.target.name):
                false_copies.append(Assign(source=statement.source, target=statement.target, value=false_value))
            continue
        saw_non_phi = True
        remaining.append(statement)
    # Low-level VM lifting deliberately precedes SSA insertion.  A direct
    # diamond can therefore be safe even when its join has no explicit Phi;
    # moving the common continuation after the ``If`` keeps the selected arm,
    # and the normal SSA pass can introduce a later value merge if necessary.
    # The caller has already proved that these are the join's only two edges.
    if not targets:
        return (), (), tuple(remaining)
    copy_inputs = {
        value.name
        for copy in (*true_copies, *false_copies)
        if isinstance(copy, Assign) and isinstance(copy.value, Var)
        for value in (copy.value,)
    }
    if targets & copy_inputs:
        return None
    return tuple(true_copies), tuple(false_copies), tuple(remaining)


def _reachable_block_ids(cfg, entry: str) -> set[str]:
    reachable: set[str] = set()
    pending = [entry]
    while pending:
        current = pending.pop()
        if current in reachable:
            continue
        reachable.add(current)
        pending.extend(cfg.successors(current))
    return reachable


def _low_level_edge_count(function: FunctionIR) -> int:
    """Count rendered low-level jump targets for a monotonic rewrite gate."""

    count = 0
    for block in function.blocks:
        terminator = block.terminator
        if isinstance(terminator, Jump):
            count += 1
        elif isinstance(terminator, Branch):
            count += 2
        elif isinstance(terminator, MultiBranch):
            count += len(terminator.cases) + 1
    return count


def _extract_condition_from_body_if(
    block: BasicBlock,
    *,
    later_paths: tuple[tuple[tuple[Stmt, ...], Terminator | None], ...],
) -> tuple[tuple[Stmt, ...], Expr] | None:
    if len(block.statements) == 1:
        statement = block.statements[0]
        if not isinstance(statement, Assign) or not isinstance(statement.target, Var):
            return None
        folded = _condition_with_temp_value(block.terminator.condition, statement.target.name, statement.value)
        if folded is None:
            return None
        if _var_is_read_after_condition(statement.target.name, later_paths):
            return (block.statements, block.terminator.condition)
        return ((), folded)
    if len(block.statements) != 2:
        return None
    first, second = block.statements
    if not isinstance(first, Assign) or not isinstance(first.target, Var):
        return None
    if not isinstance(first.value, (CapturedVar, Global, Var)):
        return None
    if not isinstance(second, Assign) or not isinstance(second.target, Var):
        return None
    if second.target.name != first.target.name:
        return None
    if not isinstance(second.value, GetItem):
        return None
    if not isinstance(second.value.obj, Var) or second.value.obj.name != first.target.name:
        return None
    value = GetItem(source=second.value.source, type=second.value.type, obj=first.value, key=second.value.key)
    folded = _condition_with_temp_value(block.terminator.condition, second.target.name, value)
    if folded is None:
        return None
    if _var_is_read_after_condition(second.target.name, later_paths):
        return (block.statements, block.terminator.condition)
    return ((), folded)


def _condition_with_temp_value(condition: Expr, temp_name: str, value: Expr) -> Expr | None:
    if isinstance(condition, Var) and condition.name == temp_name:
        return value
    if isinstance(condition, UnaryOp) and condition.op == "not ":
        operand = condition.value
        if isinstance(operand, Var) and operand.name == temp_name:
            return UnaryOp(op="not ", value=value)
    if isinstance(condition, BinaryOp):
        replaced_left = value if isinstance(condition.left, Var) and condition.left.name == temp_name else condition.left
        replaced_right = value if isinstance(condition.right, Var) and condition.right.name == temp_name else condition.right
        if replaced_left is not condition.left or replaced_right is not condition.right:
            return BinaryOp(
                source=condition.source,
                type=condition.type,
                op=condition.op,
                left=replaced_left,
                right=replaced_right,
                semantics=condition.semantics,
            )
    return None


def _var_is_read_after_condition(
    name: str,
    later_paths: tuple[tuple[tuple[Stmt, ...], Terminator | None], ...],
) -> bool:
    return any(_path_reads_existing_var(statements, terminator, name) for statements, terminator in later_paths)


def _path_reads_existing_var(
    statements: tuple[Stmt, ...],
    terminator: Terminator | None,
    name: str,
) -> bool:
    for statement in statements:
        if _stmt_reads_var(statement, name):
            return True
        if _stmt_writes_var(statement, name):
            return False
    return terminator is not None and _terminator_reads_var(terminator, name)


def _stmt_reads_var(statement: Stmt, name: str) -> bool:
    if isinstance(statement, Assign):
        target_reads = not isinstance(statement.target, Var) and _expr_reads_var(statement.target, name)
        return target_reads or _expr_reads_var(statement.value, name)
    if isinstance(statement, AssignMany):
        return any(_expr_reads_var(value, name) for value in statement.values)
    if isinstance(statement, StoreAttr):
        return (
            _expr_reads_var(statement.obj, name)
            or _expr_reads_var(statement.value, name)
        )
    if isinstance(statement, StoreItem):
        return (
            _expr_reads_var(statement.obj, name)
            or _expr_reads_var(statement.key, name)
            or _expr_reads_var(statement.value, name)
        )
    if isinstance(statement, ExprStmt):
        return _expr_reads_var(statement.value, name)
    if isinstance(statement, Raise):
        return _expr_reads_var(statement.value, name)
    if isinstance(statement, Yield):
        return _expr_reads_var(statement.value, name)
    if isinstance(statement, If):
        return (
            _expr_reads_var(statement.condition, name)
            or any(_stmt_reads_var(child, name) for child in statement.then_body)
            or any(_stmt_reads_var(child, name) for child in statement.else_body)
        )
    if isinstance(statement, While):
        return _expr_reads_var(statement.condition, name) or any(
            _stmt_reads_var(child, name) for child in statement.body
        )
    if isinstance(statement, ForEach):
        return _expr_reads_var(statement.iterable, name) or any(
            _stmt_reads_var(child, name) for child in statement.body
        )
    if isinstance(statement, ForRange):
        return (
            _expr_reads_var(statement.start, name)
            or _expr_reads_var(statement.stop, name)
            or _expr_reads_var(statement.step, name)
            or any(_stmt_reads_var(child, name) for child in statement.body)
        )
    if isinstance(statement, Try):
        return any(_stmt_reads_var(child, name) for child in statement.body) or any(
            _expr_reads_var(handler.exception_type, name)
            or any(_stmt_reads_var(child, name) for child in handler.body)
            for handler in statement.handlers
        )
    return False


def _stmt_writes_var(statement: Stmt, name: str) -> bool:
    if isinstance(statement, Assign):
        return isinstance(statement.target, Var) and statement.target.name == name
    if isinstance(statement, AssignMany):
        return any(target.name == name for target in statement.targets)
    return False


def _terminator_reads_var(terminator: Terminator, name: str) -> bool:
    if isinstance(terminator, Return):
        return any(_expr_reads_var(value, name) for value in terminator.values)
    if isinstance(terminator, Branch):
        return _expr_reads_var(terminator.condition, name)
    if isinstance(terminator, MultiBranch):
        return _expr_reads_var(terminator.selector, name) or any(
            _expr_reads_var(case_value, name) for case_value, _target in terminator.cases
        )
    return False


def _expr_reads_var(expression: Expr, name: str) -> bool:
    if isinstance(expression, Var):
        return expression.name == name
    if not is_dataclass(expression):
        return False
    for field in fields(expression):
        if field.name in {"source", "type"}:
            continue
        if _value_reads_var(getattr(expression, field.name), name):
            return True
    return False


def _value_reads_var(value: object, name: str) -> bool:
    if isinstance(value, Expr):
        return _expr_reads_var(value, name)
    if isinstance(value, tuple):
        return any(_value_reads_var(item, name) for item in value)
    return False


def _expr_semantically_equal(left: Expr, right: Expr) -> bool:
    return _values_semantically_equal(left, right)


def _condition_is_false_comparison_of(condition: Expr, positive_condition: Expr) -> bool:
    if isinstance(condition, BinaryOp) and condition.op == "==":
        if isinstance(condition.right, Const) and condition.right.value is False:
            return _expr_semantically_equal(condition.left, positive_condition)
        if isinstance(condition.left, Const) and condition.left.value is False:
            return _expr_semantically_equal(condition.right, positive_condition)
    return False


def _returns_semantically_equal(left: Return, right: Terminator | None) -> bool:
    if not isinstance(right, Return):
        return False
    return _values_semantically_equal(left.values, right.values)


def _single_return_value(terminator: Terminator | None) -> Expr | None:
    if not isinstance(terminator, Return):
        return None
    if len(terminator.values) != 1:
        return None
    return terminator.values[0]


def _jump_to_single_return_value(jump_block: BasicBlock, return_block: BasicBlock) -> Expr | None:
    if jump_block.statements:
        return None
    if not isinstance(jump_block.terminator, Jump) or jump_block.terminator.target != return_block.id:
        return None
    if return_block.statements:
        return None
    return _single_return_value(return_block.terminator)


def _fresh_var_name(function: FunctionIR, prefix: str) -> str:
    used = set(function.params)
    for block in function.blocks:
        for statement in block.statements:
            used.update(_value_var_names(statement))
        if block.terminator is not None:
            used.update(_value_var_names(block.terminator))
    if prefix not in used:
        return prefix
    index = 1
    while f"{prefix}_{index}" in used:
        index += 1
    return f"{prefix}_{index}"


def _value_var_names(value: object) -> set[str]:
    if isinstance(value, (Var, CapturedVar, Global)):
        return {value.name}
    names: set[str] = set()
    if isinstance(value, tuple):
        for item in value:
            names.update(_value_var_names(item))
        return names
    if not is_dataclass(value):
        return names
    for field in fields(value):
        if field.name in {"source", "type"}:
            continue
        names.update(_value_var_names(getattr(value, field.name)))
    return names


def _values_semantically_equal(left: object, right: object) -> bool:
    if isinstance(left, Expr) or isinstance(right, Expr):
        if type(left) is not type(right):
            return False
        if not is_dataclass(left) or not is_dataclass(right):
            return left == right
        for field in fields(left):
            if field.name in {"source", "type"}:
                continue
            if not _values_semantically_equal(getattr(left, field.name), getattr(right, field.name)):
                return False
        return True
    if isinstance(left, tuple) or isinstance(right, tuple):
        if not isinstance(left, tuple) or not isinstance(right, tuple):
            return False
        if len(left) != len(right):
            return False
        return all(_values_semantically_equal(left_item, right_item) for left_item, right_item in zip(left, right))
    if is_dataclass(left) or is_dataclass(right):
        if type(left) is not type(right) or not is_dataclass(left) or not is_dataclass(right):
            return False
        for field in fields(left):
            if not _values_semantically_equal(getattr(left, field.name), getattr(right, field.name)):
                return False
        return True
    return left == right


def _block_has_only_trivial_phi_assignments(
    block: BasicBlock,
    *,
    incoming_blocks: tuple[str, ...],
) -> bool:
    return _statements_without_trivial_phi_assignments(
        block.statements,
        incoming_blocks=incoming_blocks,
    ) == ()


def _statements_without_trivial_phi_assignments(
    statements: tuple[Stmt, ...],
    *,
    incoming_blocks: tuple[str, ...],
) -> tuple[Stmt, ...] | None:
    kept: list[Stmt] = []
    for statement in statements:
        if _is_trivial_phi_assignment(statement, incoming_blocks=incoming_blocks):
            continue
        if isinstance(statement, Assign) and isinstance(statement.value, Phi):
            return None
        kept.append(statement)
    return tuple(kept)


def _statements_without_identity_phi_assignments(statements: tuple[Stmt, ...]) -> tuple[Stmt, ...] | None:
    """Drop only Phi assignments that cannot change their target value."""

    kept: list[Stmt] = []
    for statement in statements:
        if isinstance(statement, Assign) and isinstance(statement.value, Phi):
            if not isinstance(statement.target, Var) or not statement.value.incoming:
                return None
            if not all(
                isinstance(value, Var) and value.name == statement.target.name
                for _block_id, value in statement.value.incoming
            ):
                return None
            continue
        kept.append(statement)
    return tuple(kept)


def _is_trivial_phi_assignment(
    statement: Stmt,
    *,
    incoming_blocks: tuple[str, ...],
) -> bool:
    if not isinstance(statement, Assign):
        return False
    if not isinstance(statement.target, Var) or not isinstance(statement.value, Phi):
        return False
    if not statement.value.incoming:
        return False
    if {block_id for block_id, _incoming_value in statement.value.incoming} != set(incoming_blocks):
        return False
    return all(
        isinstance(incoming_value, Var) and incoming_value.name == statement.target.name
        for _block_id, incoming_value in statement.value.incoming
    )


def _is_short_circuit_bool_phi(
    statement: Stmt,
    true_block: str,
    true_name: str,
    false_block: str,
) -> bool:
    if not isinstance(statement, Assign):
        return False
    if not isinstance(statement.target, Var) or not isinstance(statement.value, Phi):
        return False
    incoming = dict(statement.value.incoming)
    true_value = incoming.get(true_block)
    false_value = incoming.get(false_block)
    if not isinstance(true_value, Var) or true_value.name != true_name:
        return False
    if not isinstance(false_value, Const) or false_value.value not in {0, False}:
        return False
    existing_value = incoming.get("existing")
    return existing_value is None or (isinstance(existing_value, Var) and existing_value.name == true_name)


def _terminator_targets_any_original_block(terminator: Terminator | None, function: FunctionIR) -> bool:
    if terminator is None:
        return False
    block_ids = {block.id for block in function.blocks}
    return any(target in block_ids for target in _terminator_targets(terminator))


def _terminator_targets(terminator: Terminator) -> tuple[str, ...]:
    if isinstance(terminator, Jump):
        return (terminator.target,)
    if isinstance(terminator, Branch):
        return (terminator.true_target, terminator.false_target)
    if isinstance(terminator, MultiBranch):
        return (*tuple(target for _case, target in terminator.cases), terminator.default_target)
    return ()


def _simple_while_header_has_no_statements(function: FunctionIR) -> bool:
    if len(function.blocks) != 4:
        return False
    setup, condition, _body, _exit_block = function.blocks
    if not isinstance(setup.terminator, Jump):
        return False
    if setup.terminator.target != condition.id:
        return False
    return not condition.statements


_LOW_LEVEL_CFG_NORMALIZERS = (
    _structure_exact_try_common_join_region,
    _structure_exact_terminal_branch_region,
    _structure_exact_acyclic_tree_join_region,
    _structure_exact_acyclic_shared_linear_block,
    _structure_exact_partial_branch_arm_splice,
    _structure_exact_nested_loop_flattening,
    _structure_exact_innermost_natural_loop_region,
    _structure_exact_entry_natural_loop_region,
    _structure_exact_empty_jump_chains,
    _structure_exact_linear_block_merges,
    _structure_exact_direct_phi_diamond_region,
    _structure_exact_short_circuit_diamond_region,
    _structure_exact_linear_arm_diamond_region,
    _structure_exact_optional_linear_arm_diamond_region,
    _structure_exact_direct_phi_dispatch_region,
    _structure_exact_partial_multiway_arm_splice,
    _structure_exact_empty_jump_splices,
    _structure_exact_loop_linear_block_merges,
)

register_low_level_cfg_structurer(_structure_exact_single_block_cfg)
register_low_level_cfg_structurer(_structure_exact_branch_selected_dual_loops)
register_low_level_cfg_structurer(_structure_exact_three_phase_nested_loops)
register_low_level_cfg_structurer(_structure_exact_two_level_loop_with_guarded_continue)
register_low_level_cfg_structurer(_structure_exact_whole_function_while)
register_low_level_cfg_structurer(_structure_exact_header_effect_while)
register_low_level_cfg_structurer(_structure_exact_header_effect_while_with_exit_jump)
register_low_level_cfg_structurer(_structure_exact_branch_assign_return)
register_low_level_cfg_structurer(_structure_exact_early_return_pretested_loop)
register_low_level_cfg_structurer(_structure_exact_guard_cascade_pretested_loop)
register_low_level_cfg_structurer(_structure_exact_try_success_return)
register_low_level_cfg_structurer(_structure_exact_branch_terminal_arms)
register_low_level_cfg_structurer(_structure_exact_branch_join_return)
register_low_level_cfg_structurer(_structure_exact_optional_phi_diamond_region)
register_low_level_cfg_structurer(_structure_exact_optional_terminal_diamond_region)
register_low_level_cfg_structurer(_structure_exact_acyclic_tree_join_region)
register_low_level_cfg_structurer(_structure_exact_acyclic_shared_linear_block)
register_low_level_cfg_structurer(_structure_exact_acyclic_decision_tree_return)
register_low_level_cfg_structurer(_structure_exact_guard_return_cascade)
register_low_level_cfg_structurer(_structure_exact_acyclic_terminal_tree)
register_low_level_cfg_structurer(_structure_exact_pretested_loop_with_posttested_inner_loop)
register_low_level_cfg_structurer(_structure_exact_optional_pretested_body_if_loop)
register_low_level_cfg_structurer(_structure_exact_pretested_loop_with_latch_and_raise_tail)
register_low_level_cfg_structurer(_structure_exact_or_guard_loop_with_two_latches)
register_low_level_cfg_structurer(_structure_exact_and_guard_loop_with_duplicate_return_exits)
register_low_level_cfg_structurer(_structure_exact_short_circuit_phi_loop)
register_low_level_cfg_structurer(_structure_exact_dual_condition_loop_with_exit_jump)
register_low_level_cfg_structurer(_structure_exact_dual_condition_loop_with_latch)
register_low_level_cfg_structurer(_structure_exact_dual_condition_loop_with_preheader_latch)
register_low_level_cfg_structurer(_structure_exact_triple_condition_loop_with_exit_jump)
register_low_level_cfg_structurer(_structure_exact_triple_condition_loop_with_latch)
register_low_level_cfg_structurer(_structure_exact_triple_condition_loop_with_preheader_latch)
register_low_level_cfg_structurer(_structure_exact_header_body_if_join_loop)
register_low_level_cfg_structurer(_structure_exact_header_effect_body_if)
register_low_level_cfg_structurer(_structure_exact_two_edge_header_phi_while)
register_low_level_cfg_structurer(_structure_exact_header_terminal_guard_loop)
register_low_level_cfg_structurer(_structure_exact_terminal_natural_loop)
register_low_level_cfg_structurer(_structure_exact_direct_join_terminal_loop)
register_low_level_cfg_structurer(_structure_exact_pretested_loop_with_terminal_body_return)
register_low_level_cfg_structurer(_structure_exact_natural_loop_with_shared_exit)
register_low_level_cfg_structurer(_structure_exact_posttested_natural_loop)
register_low_level_cfg_structurer(_structure_exact_single_natural_loop)
register_low_level_cfg_structurer(_structure_exact_multi_backedge_terminal_loop)
register_low_level_cfg_structurer(_structure_exact_multiway_terminal_loop)
register_low_level_cfg_structurer(_structure_exact_multiway_shared_linear_join_loop)
register_low_level_cfg_structurer(_structure_exact_terminal_join_loop)
register_low_level_cfg_structurer(_structure_exact_loop_with_terminal_body_branch)
register_low_level_cfg_structurer(_structure_exact_terminal_guarded_loop_region)
register_low_level_cfg_structurer(_structure_exact_tree_loop_with_terminal_exits)
register_low_level_cfg_structurer(_structure_exact_statement_guard_loop)
register_low_level_cfg_structurer(_structure_exact_nested_loop_flattening)
register_low_level_cfg_structurer(_structure_exact_innermost_natural_loop_region)
register_low_level_cfg_structurer(_structure_exact_entry_natural_loop_region)
register_low_level_cfg_structurer(_structure_exact_entry_posttested_self_loop)
register_low_level_cfg_structurer(_structure_exact_infinite_loop_with_optional_arm)
register_low_level_cfg_structurer(_structure_exact_try_common_join_region)
register_low_level_cfg_structurer(_structure_exact_empty_jump_chains)
register_low_level_cfg_structurer(_structure_exact_empty_jump_splices)
register_low_level_cfg_structurer(_structure_exact_loop_linear_block_merges)
register_low_level_cfg_structurer(_structure_exact_linear_block_merges)
register_low_level_cfg_structurer(_structure_exact_multi_backedge_natural_loop_region)
register_low_level_cfg_structurer(_structure_exact_direct_phi_diamond_region)
register_low_level_cfg_structurer(_structure_exact_short_circuit_diamond_region)
register_low_level_cfg_structurer(_structure_exact_linear_arm_diamond_region)
register_low_level_cfg_structurer(_structure_exact_optional_linear_arm_diamond_region)
register_low_level_cfg_structurer(_structure_exact_direct_phi_dispatch_region)
register_low_level_cfg_structurer(_structure_exact_terminal_branch_region)
register_low_level_cfg_structurer(_structure_exact_partial_branch_arm_splice)
register_low_level_cfg_structurer(_structure_exact_partial_multiway_arm_splice)
