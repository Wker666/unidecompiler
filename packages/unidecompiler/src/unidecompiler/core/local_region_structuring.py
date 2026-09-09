"""Conservative, VM-neutral local region structuring rules.

This module deliberately operates on generic ``FunctionIR`` only.  A rule
collapses one proven single-entry diamond at a time; it never guesses stack
values, crosses exception boundaries, or rewrites a loop/irreducible edge.
The preservation CFG remains the source of truth and the common reducer gate
performs the final rewrite validation.
"""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import (
    Assign,
    BasicBlock,
    BinaryOp,
    Branch,
    Break,
    Const,
    DoWhile,
    Expr,
    Fallthrough,
    FunctionIR,
    If,
    Continue,
    Jump,
    MultiBranch,
    Phi,
    Return,
    Raise,
    Reraise,
    Stmt,
    Switch,
    Terminator,
    Try,
    UnaryOp,
    Var,
    While,
    exceptional_transfers,
)
from unidecompiler.core.region import RegionGraph
from unidecompiler.core.structuring_utils import contains_unscoped_loop_control


def collapse_local_linear_chain(function: FunctionIR) -> FunctionIR | None:
    """Collapse one ordinary, Phi-free ``jump -> block`` chain.

    This is the stack-VM counterpart of Ghidra's ``ruleBlockCat``.  It does
    not invent a fallthrough, rewrite a Phi predecessor, or cross a loop or
    exception boundary.  The target's successor may be a branch or terminal;
    the target itself simply ceases to be a separately addressable CFG node.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        if not _ordinary_block(source) or not isinstance(source.terminator, Jump):
            continue
        # This rule concatenates *basic* blocks only.  A block that already
        # owns a recovered region has an explicit region exit; consuming its
        # successor would widen that region without a proof for the exit
        # boundary.  Keep the successor addressable for both subsequent
        # structuring passes and exact CFG preservation.
        if _contains_structured_control(source.statements):
            continue
        target = blocks.get(source.terminator.target)
        if target is None or target.id == source.id or not _ordinary_block(target):
            continue
        incoming = cfg.incoming_edges(target.id)
        outgoing = cfg.outgoing_edges(source.id)
        # Use concrete edge identity, never a set of predecessor block IDs:
        # parallel edges may have equal endpoints but distinct stack Phi
        # inputs.
        if len(incoming) != 1 or incoming[0].source != source.id:
            continue
        if len(outgoing) != 1 or outgoing[0].target != target.id:
            continue
        if _contains_phi((source, target)):
            continue
        if any(
            _contains_phi((blocks[edge.target],))
            for edge in cfg.outgoing_edges(target.id)
            if edge.target in blocks
        ):
            # Removing ``target`` would otherwise need a Phi label/edge-ID
            # remap at its successor.  That belongs to a separate proof.
            continue
        members = frozenset({source.id, target.id})
        if not _same_region_scope(region, members):
            continue
        if not _ordinary_component_isolated(cfg, members):
            continue

        rewritten_source = replace(
            source,
            statements=(*source.statements, *target.statements),
            terminator=target.terminator,
        )
        return _with_replaced_block(
            function,
            source_id=source.id,
            replacement=rewritten_source,
            removed_ids=frozenset({target.id}),
            rule="local-linear-chain",
        )
    return None


def collapse_local_short_circuit(function: FunctionIR) -> FunctionIR | None:
    """Fold one exact two-clause boolean branch into short-circuit ``and/or``.

    This is the stack-VM equivalent of Ghidra's ``ruleBlockOr``.  It only
    consumes a statement-free second condition block, so moving the second
    condition into ``BinaryOp`` preserves both the single evaluation and the
    conditional evaluation order.  It deliberately does not fold a value Phi
    or a condition block that materializes stack state.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        first = source.terminator
        if not isinstance(first, Branch) or not _ordinary_block(source):
            continue
        for operator, next_id, pass_target, stop_target in (
            ("and", first.true_target, None, first.false_target),
            ("or", first.false_target, first.true_target, None),
        ):
            next_block = blocks.get(next_id)
            if (
                next_block is None
                or not _ordinary_block(next_block)
                or next_block.statements
                or not isinstance(next_block.terminator, Branch)
                or next_block.id == source.id
            ):
                continue
            second = next_block.terminator
            if operator == "and":
                if second.false_target != stop_target:
                    continue
                pass_target = second.true_target
            else:
                if second.true_target != pass_target:
                    continue
                stop_target = second.false_target
            if pass_target is None or stop_target is None or pass_target == stop_target:
                continue
            if pass_target in {source.id, next_block.id} or stop_target in {source.id, next_block.id}:
                continue
            next_incoming = cfg.incoming_edges(next_block.id)
            source_outgoing = cfg.outgoing_edges(source.id)
            if (
                len(source_outgoing) != 2
                or len(next_incoming) != 1
                or next_incoming[0].source != source.id
            ):
                continue
            pass_block = blocks.get(pass_target)
            stop_block = blocks.get(stop_target)
            if pass_block is None or stop_block is None:
                continue
            # The rewritten branch replaces the next block's concrete edge.
            # A join Phi would need an edge-label remap, which is outside this
            # deliberately small condition proof.
            if _contains_phi((source, next_block, pass_block, stop_block)):
                continue
            members = frozenset({source.id, next_block.id})
            if not _same_region_scope(region, members):
                continue
            if not _ordinary_component_isolated(cfg, members):
                continue
            replacement = replace(
                source,
                terminator=Branch(
                    source=first.source,
                    condition=BinaryOp(
                        source=first.condition.source,
                        type=first.condition.type,
                        op=operator,
                        left=first.condition,
                        right=second.condition,
                    ),
                    true_target=pass_target,
                    false_target=stop_target,
                ),
            )
            return _with_replaced_block(
                function,
                source_id=source.id,
                replacement=replacement,
                removed_ids=frozenset({next_block.id}),
                rule=f"local-short-circuit-{operator}",
            )
    return None


def collapse_local_proper_if(function: FunctionIR) -> FunctionIR | None:
    """Collapse one proven proper-if region without an else clause.

    This follows the topology of Ghidra's ``ruleBlockProperIf``:
    ``branch -> clause -> continuation`` with the other branch directly
    reaching that continuation.  For a stack VM, the rule additionally
    rejects every Phi and all non-ordinary edges it touches, because deleting
    the clause changes the concrete predecessor identity at the continuation.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        branch = source.terminator
        if not isinstance(branch, Branch) or not _ordinary_block(source):
            continue
        targets = (branch.true_target, branch.false_target)
        if targets[0] == targets[1] or source.id in targets:
            continue
        for clause_index, clause_id in enumerate(targets):
            continuation_id = targets[1 - clause_index]
            clause = blocks.get(clause_id)
            continuation = blocks.get(continuation_id)
            if (
                clause is None
                or continuation is None
                or continuation.id in {source.id, clause.id}
                or not _ordinary_block(clause)
                or not _ordinary_block(continuation)
                or not isinstance(clause.terminator, Jump)
                or clause.terminator.target != continuation.id
            ):
                continue
            source_outgoing = cfg.outgoing_edges(source.id)
            clause_incoming = cfg.incoming_edges(clause.id)
            clause_outgoing = cfg.outgoing_edges(clause.id)
            continuation_incoming = cfg.incoming_edges(continuation.id)
            if len(source_outgoing) != 2 or len(clause_incoming) != 1:
                continue
            if clause_incoming[0].source != source.id or len(clause_outgoing) != 1:
                continue
            if clause_outgoing[0].target != continuation.id:
                continue
            # The continuation has one direct edge from the branch and one
            # through the clause.  Do not collapse a parallel/third incoming
            # edge into an implicit source predecessor.
            if len(continuation_incoming) != 2:
                continue
            if tuple(edge.source for edge in continuation_incoming).count(source.id) != 1:
                continue
            if tuple(edge.source for edge in continuation_incoming).count(clause.id) != 1:
                continue
            if _contains_phi((source, clause, continuation)):
                continue

            members = frozenset({source.id, clause.id})
            if not _same_region_scope(region, members):
                continue
            if not _ordinary_component_isolated(cfg, members):
                continue

            condition = branch.condition
            if clause_index == 1:
                # A false-edge clause is represented as a positive proper-if
                # while retaining the source/type provenance of the original
                # condition.  It is evaluated once by the If node.
                condition = UnaryOp(
                    source=condition.source,
                    type=condition.type,
                    op="not ",
                    value=condition,
                )
            rewritten_source = replace(
                source,
                statements=(
                    *source.statements,
                    If(
                        source=branch.source,
                        condition=condition,
                        then_body=clause.statements,
                    ),
                ),
                terminator=Jump(source=clause.terminator.source, target=continuation.id),
            )
            return _with_replaced_block(
                function,
                source_id=source.id,
                replacement=rewritten_source,
                removed_ids=frozenset({clause.id}),
                rule="local-proper-if",
            )
    return None


def collapse_local_if_diamond(function: FunctionIR) -> FunctionIR | None:
    """Collapse one ordinary two-arm diamond anywhere in a function.

    The rule accepts ``branch -> arm1/arm2 -> join`` where both arms have one
    concrete predecessor and an unconditional jump to the same join.  The
    join either returns or jumps to one continuation.  Phi-bearing regions,
    exceptional/irreducible/loop edges, and ambiguous continuation values are
    rejected because moving them would require a separate value proof.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    if analysis.irreducible_edges or analysis.exception_edges:
        # A candidate may still exist elsewhere, so inspect individual
        # regions below rather than rejecting the whole function here.
        pass
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        terminator = source.terminator
        if not isinstance(terminator, Branch):
            continue
        true_arm = blocks.get(terminator.true_target)
        false_arm = blocks.get(terminator.false_target)
        if true_arm is None or false_arm is None or true_arm.id == false_arm.id:
            continue
        if not _ordinary_block(source) or not _ordinary_block(true_arm) or not _ordinary_block(false_arm):
            continue
        true_incoming = cfg.incoming_edges(true_arm.id)
        false_incoming = cfg.incoming_edges(false_arm.id)
        if (
            len(true_incoming) != 1
            or true_incoming[0].source != source.id
            or len(false_incoming) != 1
            or false_incoming[0].source != source.id
        ):
            continue
        if not isinstance(true_arm.terminator, Jump) or not isinstance(false_arm.terminator, Jump):
            continue
        if true_arm.terminator.target != false_arm.terminator.target:
            continue
        join = blocks.get(true_arm.terminator.target)
        if join is None or join.id in {source.id, true_arm.id, false_arm.id}:
            continue
        join_incoming = cfg.incoming_edges(join.id)
        if len(join_incoming) != 2:
            continue
        if tuple(edge.source for edge in join_incoming).count(true_arm.id) != 1:
            continue
        if tuple(edge.source for edge in join_incoming).count(false_arm.id) != 1:
            continue
        if not _ordinary_block(join):
            continue

        members = frozenset({source.id, true_arm.id, false_arm.id, join.id})
        if not _same_region_scope(region, members):
            continue
        if not _ordinary_component_isolated(cfg, members):
            continue
        if _contains_phi((source, true_arm, false_arm, join)):
            continue

        continuation: BasicBlock | None = None
        rewritten_terminator: Terminator | None
        if isinstance(join.terminator, Return):
            rewritten_terminator = join.terminator
        elif isinstance(join.terminator, Jump):
            if join.terminator.target in members:
                continue
            continuation = blocks.get(join.terminator.target)
            if continuation is None or _contains_phi((continuation,)):
                continue
            rewritten_terminator = join.terminator
        else:
            continue

        rewritten_source = replace(
            source,
            statements=(
                *source.statements,
                If(
                    source=terminator.source,
                    condition=terminator.condition,
                    then_body=true_arm.statements,
                    else_body=false_arm.statements,
                ),
                *join.statements,
            ),
            terminator=rewritten_terminator,
        )

        return _with_replaced_block(
            function,
            source_id=source.id,
            replacement=rewritten_source,
            removed_ids=frozenset({true_arm.id, false_arm.id, join.id}),
            rule="local-sese-if-diamond",
        )
    return None


def collapse_local_switch(function: FunctionIR) -> FunctionIR | None:
    """Collapse one single-entry, single-join ordinary multi-way region.

    This is the deliberately narrow stack-VM form of Ghidra's
    ``ruleBlockSwitch``.  Each non-empty arm must be entered only from the
    dispatch and leave through one exact jump to the same join.  Direct case
    edges to that join become empty arms.  The rule does not retarget Phi
    labels, infer source-language case semantics, or cross a protected
    exception boundary.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        dispatch = source.terminator
        if not isinstance(dispatch, MultiBranch) or not _ordinary_block(source):
            continue
        targets = tuple(target for _value, target in dispatch.cases) + (dispatch.default_target,)
        if source.id in targets:
            continue
        # A source dispatch may send one or more labels straight to the join.
        # Derive the join only from concrete arm jumps; never treat the join
        # as a removable arm block.
        join_candidates = tuple(
            arm.terminator.target
            for target in targets
            if (arm := blocks.get(target)) is not None
            and _ordinary_block(arm)
            and isinstance(arm.terminator, Jump)
        )
        for join_id in dict.fromkeys(join_candidates):
            if join_id == source.id:
                continue
            join = blocks.get(join_id)
            if join is None or not _ordinary_block(join):
                continue
            arms: dict[str, BasicBlock] = {}
            valid = True
            for target in targets:
                if target == join_id:
                    continue
                arm = blocks.get(target)
                if (
                    arm is None
                    or not _ordinary_block(arm)
                    or not isinstance(arm.terminator, Jump)
                    or arm.terminator.target != join_id
                ):
                    valid = False
                    break
                arms[target] = arm
            if not valid or not arms:
                continue
            if len(arms) != len(tuple(target for target in targets if target != join_id)):
                # Multiple labels may safely share the empty join arm, but a
                # non-empty shared target needs a separate fallthrough/shared
                # case proof so its distinct incoming edges are not merged.
                continue
            # Every non-empty arm is single-entry from the dispatch.  This
            # deliberately excludes fallthrough; it has a separate rule.
            if any(
                len(cfg.incoming_edges(arm.id)) != 1
                or cfg.incoming_edges(arm.id)[0].source != source.id
                or len(cfg.outgoing_edges(arm.id)) != 1
                or cfg.outgoing_edges(arm.id)[0].target != join_id
                for arm in arms.values()
            ):
                continue
            source_outgoing = cfg.outgoing_edges(source.id)
            if len(source_outgoing) != len(targets):
                continue
            # The join will acquire a new concrete predecessor edge from
            # source. Phi remapping belongs to the stack-value proof layer.
            if _contains_phi((source, *arms.values(), join)):
                continue
            members = frozenset({source.id, *arms})
            if not _same_region_scope(region, members):
                continue
            if not _ordinary_component_isolated(cfg, members):
                continue

            def arm_body(target: str) -> tuple[Stmt, ...]:
                return () if target == join_id else arms[target].statements

            cases = tuple((value, arm_body(target)) for value, target in dispatch.cases)
            replacement = replace(
                source,
                statements=(
                    *source.statements,
                    Switch(
                        source=dispatch.source,
                        selector=dispatch.selector,
                        cases=cases,
                        default_body=arm_body(dispatch.default_target),
                    ),
                ),
                terminator=Jump(source=dispatch.source, target=join_id),
            )
            return _with_replaced_block(
                function,
                source_id=source.id,
                replacement=replacement,
                removed_ids=frozenset(arms),
                rule="local-switch-single-join",
            )
    return None


def collapse_local_switch_fallthrough(function: FunctionIR) -> FunctionIR | None:
    """Collapse one two-case, explicit fallthrough switch region.

    The only accepted form is ``case A -> arm A -> arm B -> join`` and
    ``case B -> arm B -> join`` with a direct empty default arm.  This makes
    the execution order explicit in generic IR and avoids guessing whether a
    VM's multi-way dispatch uses table order, source order, or an implicit
    default transfer.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    region = RegionGraph.from_cfg(cfg)
    blocks = cfg.blocks

    for source in function.blocks:
        dispatch = source.terminator
        if (
            not isinstance(dispatch, MultiBranch)
            or not _ordinary_block(source)
            or len(dispatch.cases) != 2
        ):
            continue
        (first_value, first_id), (second_value, second_id) = dispatch.cases
        if first_id == second_id or dispatch.default_target in {source.id, first_id, second_id}:
            continue
        first = blocks.get(first_id)
        second = blocks.get(second_id)
        if (
            first is None
            or second is None
            or not _ordinary_block(first)
            or not _ordinary_block(second)
            or not isinstance(first.terminator, Jump)
            or first.terminator.target != second_id
            or not isinstance(second.terminator, Jump)
        ):
            continue
        join_id = second.terminator.target
        if join_id in {source.id, first_id, second_id}:
            continue
        join = blocks.get(join_id)
        default = blocks.get(dispatch.default_target)
        if (
            join is None
            or default is None
            or dispatch.default_target != join_id
            or not _ordinary_block(join)
        ):
            # A direct default-to-join edge is the only empty default form.
            continue
        first_incoming = cfg.incoming_edges(first_id)
        second_incoming = cfg.incoming_edges(second_id)
        if (
            len(first_incoming) != 1
            or first_incoming[0].source != source.id
            or len(second_incoming) != 2
            or tuple(edge.source for edge in second_incoming).count(source.id) != 1
            or tuple(edge.source for edge in second_incoming).count(first_id) != 1
            or len(cfg.outgoing_edges(first_id)) != 1
            or len(cfg.outgoing_edges(second_id)) != 1
            or len(cfg.outgoing_edges(source.id)) != 3
        ):
            continue
        if _contains_phi((source, first, second, join)):
            continue
        members = frozenset({source.id, first_id, second_id})
        if not _same_region_scope(region, members):
            continue
        if not _ordinary_component_isolated(cfg, members):
            continue
        replacement = replace(
            source,
            statements=(
                *source.statements,
                Switch(
                    source=dispatch.source,
                    selector=dispatch.selector,
                    cases=(
                        (first_value, (*first.statements, Fallthrough(source=first.terminator.source))),
                        (second_value, second.statements),
                    ),
                    default_body=(),
                ),
            ),
            terminator=Jump(source=dispatch.source, target=join_id),
        )
        return _with_replaced_block(
            function,
            source_id=source.id,
            replacement=replacement,
            removed_ids=frozenset({first_id, second_id}),
            rule="local-switch-explicit-fallthrough",
        )
    return None


def collapse_local_pretested_while(function: FunctionIR) -> FunctionIR | None:
    """Collapse ``header -> body -> header`` into one proven ``While``.

    The rule uses the shared natural-loop facts but accepts only a single
    concrete backedge and a single loop exit.  It intentionally leaves break,
    continue, and multi-backedge loops for rules with their own scope proof.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    blocks = cfg.blocks

    for header in function.blocks:
        branch = header.terminator
        if (
            not isinstance(branch, Branch)
            or not _ordinary_block(header)
            # Header statements execute before *each* condition evaluation.
            # This small rule has no stack-value proof for moving them into
            # the loop condition, so it must not hoist them outside ``While``.
            or header.statements
        ):
            continue
        for body_id, exit_id, condition in (
            (branch.true_target, branch.false_target, branch.condition),
            (
                branch.false_target,
                branch.true_target,
                UnaryOp(
                    source=branch.condition.source,
                    type=branch.condition.type,
                    op="not ",
                    value=branch.condition,
                ),
            ),
        ):
            body = blocks.get(body_id)
            exit_block = blocks.get(exit_id)
            if (
                body is None
                or exit_block is None
                or body_id == header.id
                or exit_id in {header.id, body_id}
                or not _ordinary_block(body)
                or not _ordinary_block(exit_block)
                or not isinstance(body.terminator, Jump)
                or body.terminator.target != header.id
            ):
                continue
            body_incoming = cfg.incoming_edges(body_id)
            header_outgoing = cfg.outgoing_edges(header.id)
            if (
                len(header_outgoing) != 2
                or len(body_incoming) != 1
                or body_incoming[0].source != header.id
                or len(cfg.outgoing_edges(body_id)) != 1
            ):
                continue
            loop = _single_exact_loop(analysis.loop_infos, header.id, body_id, {header.id, body_id})
            if loop is None or tuple(edge.target for edge in loop.exits) != (exit_id,):
                continue
            if _contains_phi((header, body, exit_block)):
                continue
            members = frozenset({header.id, body_id})
            if not _ordinary_loop_scope_isolated(
                cfg, members=members, header_id=header.id, exit_id=exit_id
            ):
                continue
            replacement = replace(
                header,
                statements=(
                    *header.statements,
                    While(source=branch.source, condition=condition, body=body.statements),
                ),
                terminator=Jump(source=branch.source, target=exit_id),
            )
            return _with_replaced_block(
                function,
                source_id=header.id,
                replacement=replacement,
                removed_ids=frozenset({body_id}),
                rule="local-pretested-while",
            )
    return None


def collapse_local_posttested_do_while(function: FunctionIR) -> FunctionIR | None:
    """Collapse ``body -> condition -> body/exit`` into ``DoWhile``.

    A stack VM may execute the body before materialising the branch condition,
    so this rule has a distinct generic node instead of rewriting through a
    synthetic first condition test.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    blocks = cfg.blocks

    for body in function.blocks:
        if not _ordinary_block(body) or not isinstance(body.terminator, Jump):
            continue
        condition_block = blocks.get(body.terminator.target)
        if (
            condition_block is None
            or not _ordinary_block(condition_block)
            or condition_block.statements
            or not isinstance(condition_block.terminator, Branch)
            or condition_block.id == body.id
        ):
            continue
        branch = condition_block.terminator
        for repeat_id, exit_id, condition in (
            (branch.true_target, branch.false_target, branch.condition),
            (
                branch.false_target,
                branch.true_target,
                UnaryOp(
                    source=branch.condition.source,
                    type=branch.condition.type,
                    op="not ",
                    value=branch.condition,
                ),
            ),
        ):
            exit_block = blocks.get(exit_id)
            if (
                repeat_id != body.id
                or exit_block is None
                or exit_id in {body.id, condition_block.id}
                or not _ordinary_block(exit_block)
                or len(cfg.incoming_edges(condition_block.id)) != 1
                or cfg.incoming_edges(condition_block.id)[0].source != body.id
                or len(cfg.outgoing_edges(body.id)) != 1
                or len(cfg.outgoing_edges(condition_block.id)) != 2
            ):
                continue
            loop = _single_exact_loop(
                analysis.loop_infos,
                body.id,
                condition_block.id,
                {body.id, condition_block.id},
            )
            if loop is None or tuple(edge.target for edge in loop.exits) != (exit_id,):
                continue
            if _contains_phi((body, condition_block, exit_block)):
                continue
            members = frozenset({body.id, condition_block.id})
            if not _ordinary_loop_scope_isolated(
                cfg, members=members, header_id=body.id, exit_id=exit_id
            ):
                continue
            replacement = replace(
                body,
                statements=(
                    DoWhile(source=branch.source, body=body.statements, condition=condition),
                ),
                terminator=Jump(source=branch.source, target=exit_id),
            )
            return _with_replaced_block(
                function,
                source_id=body.id,
                replacement=replacement,
                removed_ids=frozenset({condition_block.id}),
                rule="local-posttested-do-while",
            )
    return None


def collapse_local_posttested_self_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a single block whose body and post-test share one block.

    Stack VMs commonly emit the body effects and the terminating condition in
    one basic block, with the non-exit edge returning directly to that block.
    This is a post-tested loop even though there is no separate condition
    block.  Keep the condition as an inner ``break`` so the body executes
    before the first test and on every subsequent iteration.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    blocks = cfg.blocks

    for header in function.blocks:
        branch = header.terminator
        if (
            not isinstance(branch, Branch)
            or not _ordinary_block(header)
            or branch.true_target == branch.false_target
        ):
            continue
        if branch.true_target == header.id:
            exit_id = branch.false_target
            break_condition: Expr = UnaryOp(
                source=branch.condition.source,
                type=branch.condition.type,
                op="not ",
                value=branch.condition,
            )
        elif branch.false_target == header.id:
            exit_id = branch.true_target
            break_condition = branch.condition
        else:
            continue

        exit_block = blocks.get(exit_id)
        if (
            exit_block is None
            or exit_id == header.id
            or not _ordinary_block(exit_block)
            or len(cfg.outgoing_edges(header.id)) != 2
            or len(cfg.incoming_edges(header.id)) < 1
            or not any(
                edge.source == header.id
                for edge in cfg.incoming_edges(exit_id)
            )
        ):
            continue

        loop = _single_exact_loop(
            analysis.loop_infos,
            header.id,
            header.id,
            {header.id},
        )
        if loop is None or tuple(edge.target for edge in loop.exits) != (exit_id,):
            continue
        if sum(edge.target == exit_id for edge in cfg.outgoing_edges(header.id)) != 1:
            continue
        if not _ordinary_loop_scope_isolated(
            cfg,
            members=frozenset({header.id}),
            header_id=header.id,
            exit_id=exit_id,
        ):
            continue

        if _contains_phi((exit_block,)):
            continue

        preheader_edges = tuple(
            edge
            for edge in cfg.incoming_edges(header.id)
            if edge.source != header.id
        )
        if len(preheader_edges) != 1:
            continue
        preheader = blocks.get(preheader_edges[0].source)
        if preheader is None or not isinstance(preheader.terminator, Jump):
            continue
        if preheader.terminator.target != header.id:
            continue

        carrier_rewrite = _rewrite_self_loop_carrier_phis(
            header,
            preheader,
            preheader_edge=preheader_edges[0],
            backedge=next(
                edge
                for edge in cfg.outgoing_edges(header.id)
                if edge.target == header.id
            ),
        )
        if carrier_rewrite is None:
            if _contains_phi((header,)):
                continue
            rewritten_preheader = preheader
            header_statements = header.statements
        else:
            rewritten_preheader, header_statements = carrier_rewrite
        if any(
            isinstance(statement, (Return, Raise, Reraise))
            for statement in header_statements
        ):
            continue
        if contains_unscoped_loop_control(header_statements):
            # A pre-existing break/continue targets an enclosing loop.  Once
            # these statements are wrapped in the synthesized loop, their
            # nearest loop would change, so retain the preservation CFG.
            continue

        replacement = replace(
            header,
            statements=(
                While(
                    source=branch.source,
                    condition=Const(source=branch.source, value=True),
                    body=(
                        *header_statements,
                        If(
                            source=branch.source,
                            condition=break_condition,
                            then_body=(Break(source=branch.source),),
                        ),
                    ),
                ),
            ),
            terminator=Jump(source=branch.source, target=exit_id),
        )
        # Keep the exit block addressable: surrounding CFG edges and any
        # following blocks remain untouched by this local rewrite.
        rewritten_blocks = tuple(
            replacement
            if block.id == header.id
            else rewritten_preheader
            if block.id == preheader.id
            else block
            for block in function.blocks
        )
        return replace(
            function,
            blocks=rewritten_blocks,
            recovery_kind="generic-vm-low-level-cfg-structured",
            metadata={
                **function.metadata,
                "structured_lift": "generic-vm-low-level-cfg-structured",
                "low_level_cfg_structured": "local-posttested-self-loop",
            },
        )
    return None


def _rewrite_self_loop_carrier_phis(
    header: BasicBlock,
    preheader: BasicBlock,
    *,
    preheader_edge,
    backedge,
) -> tuple[BasicBlock, tuple[Stmt, ...]] | None:
    """Materialize a narrowly provable self-loop carrier Phi set.

    A carrier is accepted only when its initial value is a pure expression and
    its backedge value is produced by one self-update in the same block.  The
    initial assignments are emitted in the preheader, so the loop body sees
    exactly the value selected by the original Phi on its first iteration.
    Cross-carrier dependencies are rejected to preserve parallel-copy order.
    """

    phi_statements: list[Assign] = []
    index = 0
    while index < len(header.statements):
        statement = header.statements[index]
        if not isinstance(statement, Assign) or not isinstance(statement.value, Phi):
            break
        if not isinstance(statement.target, Var):
            return None
        phi_statements.append(statement)
        index += 1
    if not phi_statements:
        return None

    preheader_label = preheader_edge.edge_id
    backedge_label = backedge.edge_id
    carrier_names = {statement.target.name for statement in phi_statements}
    updates = header.statements[index:]

    def incoming_for(phi: Phi, edge_id: str, block_id: str):
        for position, (label, value) in enumerate(phi.incoming):
            concrete = phi.edge_ids[position] if position < len(phi.edge_ids) else label
            if concrete == edge_id or label == block_id:
                return value
        return None

    def contains_name(value: object, name: str) -> bool:
        if isinstance(value, Var):
            return value.name == name
        if isinstance(value, tuple):
            return any(contains_name(item, name) for item in value)
        if is_dataclass(value):
            return any(
                contains_name(getattr(value, field.name), name)
                for field in fields(value)
                if field.name not in {"source", "type"}
            )
        return False

    rewritten_updates = list(updates)
    initial_assignments: list[Stmt] = []
    back_value_names: set[str] = set()
    for statement in phi_statements:
        phi = statement.value
        if len(phi.incoming) != 2:
            return None
        if phi.edge_ids:
            if (
                len(phi.edge_ids) != 2
                or len(set(phi.edge_ids)) != 2
                or set(phi.edge_ids) != {preheader_label, backedge_label}
            ):
                return None
        elif {label for label, _value in phi.incoming} != {
            preheader.id,
            header.id,
        }:
            return None
        initial = incoming_for(phi, preheader_label, preheader.id)
        back_value = incoming_for(phi, backedge_label, header.id)
        if not isinstance(initial, (Var, Const)) or not isinstance(back_value, (Var, Const)):
            return None
        if not isinstance(back_value, Var):
            return None
        target_name = statement.target.name
        # Only accept a true self-carrier.  Renaming a distinct backedge
        # variable would require proving and rewriting every use across the
        # body, condition, and exit region; retaining the CFG is safer than
        # risking an undefined or stale value.
        if back_value.name != target_name:
            return None
        if back_value.name in back_value_names:
            return None
        back_value_names.add(back_value.name)
        assignments_to_back_value = [
            position
            for position, candidate in enumerate(rewritten_updates)
            if isinstance(candidate, Assign)
            and isinstance(candidate.target, Var)
            and candidate.target.name == back_value.name
        ]
        update_positions = [
            position
            for position, candidate in enumerate(rewritten_updates)
            if isinstance(candidate, Assign)
            and isinstance(candidate.target, Var)
            and candidate.target.name == back_value.name
            and contains_name(candidate.value, target_name)
        ]

        # A carrier with no proven self-update cannot be materialized safely:
        # its value may still be observed by an exit block or another region.
        if not update_positions:
            return None
        if len(update_positions) != 1:
            return None
        if assignments_to_back_value != update_positions:
            return None
        update_position = update_positions[0]
        update = rewritten_updates[update_position]
        assert isinstance(update, Assign)
        # A self-referential incoming value already denotes the value in the
        # preheader's variable.  Emitting ``x = x`` would add no semantics and
        # would manufacture the exact redundant assignment this pass is
        # intended to avoid.  Other pure initial values still need an
        # explicit preheader materialization.
        if not (isinstance(initial, Var) and initial.name == target_name):
            initial_assignments.append(
                Assign(
                    source=statement.source,
                    target=Var(name=back_value.name, source=statement.target.source),
                    value=initial,
                )
            )

    if any(
        isinstance(statement, Assign)
        and isinstance(statement.value, Phi)
        for statement in rewritten_updates
    ):
        return None
    if any(
        contains_name(initial, carrier)
        for initial in (assignment.value for assignment in initial_assignments)
        for carrier in carrier_names
    ):
        return None
    return (
        replace(
            preheader,
            statements=(*preheader.statements, *initial_assignments),
        ),
        tuple(rewritten_updates),
    )


def collapse_local_while_break_continue(function: FunctionIR) -> FunctionIR | None:
    """Recover one exact loop whose body branch is ``continue`` or ``break``.

    This is intentionally a small scope proof, not a generic conversion of
    every loop exit.  The header owns the repeated condition; its one body
    block owns a second condition which can reach only that header or the one
    loop exit.  Consequently both emitted controls have an unambiguous
    nearest loop scope and no target label is lost.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    blocks = cfg.blocks

    for header in function.blocks:
        header_branch = header.terminator
        if (
            not isinstance(header_branch, Branch)
            or not _ordinary_block(header)
            # See collapse_local_pretested_while: hoisting a header effect
            # would change a per-iteration operation into a one-shot one.
            or header.statements
        ):
            continue
        for body_id, exit_id, loop_condition in (
            (header_branch.true_target, header_branch.false_target, header_branch.condition),
            (
                header_branch.false_target,
                header_branch.true_target,
                UnaryOp(
                    source=header_branch.condition.source,
                    type=header_branch.condition.type,
                    op="not ",
                    value=header_branch.condition,
                ),
            ),
        ):
            body = blocks.get(body_id)
            exit_block = blocks.get(exit_id)
            if (
                body is None
                or exit_block is None
                or body_id == header.id
                or exit_id in {header.id, body_id}
                or not _ordinary_block(body)
                or not _ordinary_block(exit_block)
                or not isinstance(body.terminator, Branch)
                or len(cfg.outgoing_edges(header.id)) != 2
                or len(cfg.incoming_edges(body_id)) != 1
                or cfg.incoming_edges(body_id)[0].source != header.id
                or len(cfg.outgoing_edges(body_id)) != 2
            ):
                continue
            tail = body.terminator
            if {tail.true_target, tail.false_target} != {header.id, exit_id}:
                continue
            loop = _single_exact_loop(analysis.loop_infos, header.id, body_id, {header.id, body_id})
            if loop is None or {edge.target for edge in loop.exits} != {exit_id}:
                continue
            if _contains_phi((header, body, exit_block)):
                continue
            members = frozenset({header.id, body_id})
            if not _ordinary_loop_scope_isolated(
                cfg, members=members, header_id=header.id, exit_id=exit_id
            ):
                continue
            body_condition = tail.condition
            then_body: tuple[Stmt, ...]
            else_body: tuple[Stmt, ...]
            if tail.true_target == header.id:
                then_body, else_body = (Continue(source=tail.source),), (Break(source=tail.source),)
            else:
                then_body, else_body = (Break(source=tail.source),), (Continue(source=tail.source),)
            replacement = replace(
                header,
                statements=(
                    *header.statements,
                    While(
                        source=header_branch.source,
                        condition=loop_condition,
                        body=(
                            *body.statements,
                            If(
                                source=tail.source,
                                condition=body_condition,
                                then_body=then_body,
                                else_body=else_body,
                            ),
                        ),
                    ),
                ),
                terminator=Jump(source=header_branch.source, target=exit_id),
            )
            return _with_replaced_block(
                function,
                source_id=header.id,
                replacement=replacement,
                removed_ids=frozenset({body_id}),
                rule="local-while-break-continue",
            )
    return None


def collapse_local_multi_backedge_while(function: FunctionIR) -> FunctionIR | None:
    """Collapse one exact two-backedge natural loop into a ``While``.

    This is the smallest safe extension of Ghidra's ``ruleBlockWhileDo`` for
    a stack VM: the loop header owns the entry condition, the first body block
    can either continue the next iteration directly or enter one tail block,
    and that tail also continues.  Every edge has a unique structured meaning:

    ``header -> body -> header`` becomes ``continue`` and
    ``header -> body -> tail -> header`` becomes the normal end of the body.

    More general multiple-backedge graphs may need nested loop scopes,
    parallel Phi copies, or value materialisation.  Those remain on the exact
    CFG preservation path until separately proved.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    analysis = cfg.analyze()
    blocks = cfg.blocks

    for header in function.blocks:
        header_branch = header.terminator
        if (
            not isinstance(header_branch, Branch)
            or not _ordinary_block(header)
            # Multi-backedge recovery uses the same header condition proof.
            # A nonempty header needs a separate materialisation proof.
            or header.statements
        ):
            continue
        for body_id, exit_id, loop_condition in (
            (header_branch.true_target, header_branch.false_target, header_branch.condition),
            (
                header_branch.false_target,
                header_branch.true_target,
                UnaryOp(
                    source=header_branch.condition.source,
                    type=header_branch.condition.type,
                    op="not ",
                    value=header_branch.condition,
                ),
            ),
        ):
            body = blocks.get(body_id)
            exit_block = blocks.get(exit_id)
            if (
                body is None
                or exit_block is None
                or body_id == header.id
                or exit_id in {header.id, body_id}
                or not _ordinary_block(body)
                or not _ordinary_block(exit_block)
                or not isinstance(body.terminator, Branch)
                or len(cfg.outgoing_edges(header.id)) != 2
                or len(cfg.incoming_edges(body.id)) != 1
                or cfg.incoming_edges(body.id)[0].source != header.id
                or len(cfg.outgoing_edges(body.id)) != 2
            ):
                continue

            body_branch = body.terminator
            direct_backedge = next(
                (
                    target
                    for target in (body_branch.true_target, body_branch.false_target)
                    if target == header.id
                ),
                None,
            )
            if direct_backedge is None:
                continue
            tail_id = (
                body_branch.false_target
                if body_branch.true_target == header.id
                else body_branch.true_target
            )
            tail = blocks.get(tail_id)
            if (
                tail is None
                or tail_id in {header.id, body.id, exit_id}
                or not _ordinary_block(tail)
                or not isinstance(tail.terminator, Jump)
                or tail.terminator.target != header.id
                or len(cfg.incoming_edges(tail.id)) != 1
                or cfg.incoming_edges(tail.id)[0].source != body.id
                or len(cfg.outgoing_edges(tail.id)) != 1
            ):
                continue

            loop = _multi_exact_loop(
                analysis.loop_infos,
                header.id,
                backedge_sources=(body.id, tail.id),
                members={header.id, body.id, tail.id},
            )
            if loop is None or tuple(edge.target for edge in loop.exits) != (exit_id,):
                continue
            if _contains_phi((header, body, tail, exit_block)):
                continue
            members = frozenset({header.id, body.id, tail.id})
            if not _ordinary_loop_scope_isolated(
                cfg, members=members, header_id=header.id, exit_id=exit_id
            ):
                continue

            # A direct backedge is an early completion of this iteration.  Do
            # not erase it into an implicit fallthrough: keeping Continue
            # makes the loop scope explicit and lets the shared gate validate
            # the transfer against the original concrete edge.
            if body_branch.true_target == header.id:
                body_tail = (
                    If(
                        source=body_branch.source,
                        condition=body_branch.condition,
                        then_body=(Continue(source=body_branch.source),),
                    ),
                    *tail.statements,
                )
            else:
                body_tail = (
                    If(
                        source=body_branch.source,
                        condition=body_branch.condition,
                        then_body=tail.statements,
                        else_body=(Continue(source=body_branch.source),),
                    ),
                )
            replacement = replace(
                header,
                statements=(
                    *header.statements,
                    While(
                        source=header_branch.source,
                        condition=loop_condition,
                        body=(*body.statements, *body_tail),
                    ),
                ),
                terminator=Jump(source=header_branch.source, target=exit_id),
            )
            return _with_replaced_block(
                function,
                source_id=header.id,
                replacement=replacement,
                removed_ids=frozenset({body.id, tail.id}),
                rule="local-multi-backedge-while",
            )
    return None


def collapse_local_infinite_loop(function: FunctionIR) -> FunctionIR | None:
    """Collapse a side-effectful ordinary self-loop into ``while true``.

    Empty self loops remain in the preservation CFG because the generic
    structured representation rejects empty loop bodies and must not invent a
    source-language no-op merely to remove a label.
    """

    if not _is_low_level_cfg(function):
        return None
    cfg = build_cfg(function)
    if cfg.entry is None or cfg.diagnostics:
        return None
    for block in function.blocks:
        if (
            not _ordinary_block(block)
            or not block.statements
            or not isinstance(block.terminator, Jump)
            or block.terminator.target != block.id
            or _contains_phi((block,))
            or not _ordinary_component_isolated(cfg, frozenset({block.id}))
        ):
            continue
        if len(cfg.incoming_edges(block.id)) != 1 or len(cfg.outgoing_edges(block.id)) != 1:
            continue
        replacement = replace(
            block,
            statements=(
                While(
                    source=block.terminator.source,
                    condition=Const(source=block.terminator.source, value=True),
                    body=block.statements,
                ),
            ),
            terminator=None,
        )
        return _with_replaced_block(
            function,
            source_id=block.id,
            replacement=replacement,
            removed_ids=frozenset(),
            rule="local-infinite-self-loop",
        )
    return None


def _ordinary_block(block: BasicBlock) -> bool:
    return not exceptional_transfers(block) and not block.active_exception_handlers


def _single_exact_loop(loop_infos, header_id: str, backedge_source: str, members: set[str]):
    """Return one natural loop only when its concrete topology is exact."""

    matching = tuple(
        loop
        for loop in loop_infos
        if loop.header == header_id
        and loop.backedge_sources == (backedge_source,)
        and loop.blocks == frozenset(members)
    )
    return matching[0] if len(matching) == 1 else None


def _multi_exact_loop(
    loop_infos,
    header_id: str,
    *,
    backedge_sources: tuple[str, ...],
    members: set[str],
):
    """Return one grouped natural loop only for exact concrete backedges."""

    matching = tuple(
        loop
        for loop in loop_infos
        if loop.header == header_id
        and loop.backedge_sources == tuple(sorted(backedge_sources))
        and loop.blocks == frozenset(members)
    )
    return matching[0] if len(matching) == 1 else None


def _ordinary_component_isolated(cfg, members: frozenset[str]) -> bool:
    """Prove that a local rewrite does not touch an exception boundary.

    Ghidra isolates exceptional control before ordinary block collapse.  The
    generic stack-VM graph keeps exceptional edges explicit, so the protected
    set also includes handler targets.  Ordinary regions elsewhere in the
    same function remain eligible, but an edge between a protected block and
    a candidate is a hard boundary even when that particular edge is normal.
    """

    protected = {
        block.id
        for block in cfg.blocks.values()
        if exceptional_transfers(block) or block.active_exception_handlers
    }
    protected.update(
        edge.target for edge in cfg.edges if edge.kind == "exception"
    )
    if members & protected:
        return False
    return not any(
        (edge.source in members and edge.target in protected)
        or (edge.target in members and edge.source in protected)
        for edge in cfg.edges
    )


def _ordinary_loop_scope_isolated(
    cfg,
    *,
    members: frozenset[str],
    header_id: str,
    exit_id: str,
) -> bool:
    """Prove the loop boundary is a reducible ordinary single-entry scope.

    Ghidra runs loop/irreducibility analysis before its while rules.  Reuse
    the equivalent immutable ``RegionGraph`` proof here so a natural backedge
    nested in a multi-entry SCC cannot be mistaken for a standalone stack-VM
    loop.  A loop entered directly at the function entry has no preheader;
    otherwise exactly one external predecessor must enter the header.
    """

    if not _ordinary_component_isolated(cfg, members):
        return False
    incoming = tuple(
        edge
        for edge in cfg.edges
        if edge.target in members and edge.source not in members
    )
    if any(edge.target != header_id for edge in incoming):
        return False
    preheaders = {edge.source for edge in incoming}
    if len(preheaders) > 1:
        return False
    preheader = next(iter(preheaders), None)
    return RegionGraph.from_cfg(cfg).is_single_entry_loop(
        members,
        header=header_id,
        preheader=preheader,
        exit=exit_id,
    )


def _contains_phi(blocks: tuple[BasicBlock, ...]) -> bool:
    def visit(value: object) -> bool:
        if isinstance(value, Phi):
            return True
        if isinstance(value, tuple):
            return any(visit(item) for item in value)
        if is_dataclass(value):
            return any(
                visit(getattr(value, field.name))
                for field in fields(value)
                if field.name not in {"source", "type"}
            )
        return False

    return any(visit(block.statements) or visit(block.terminator) for block in blocks)


def _contains_structured_control(statements: tuple[Stmt, ...]) -> bool:
    """Return whether a block already owns a recovered control region."""

    structured_types = (If, Switch, Try, While, DoWhile)

    def visit(value: object) -> bool:
        if isinstance(value, structured_types):
            return True
        if isinstance(value, tuple):
            return any(visit(item) for item in value)
        if is_dataclass(value):
            return any(
                visit(getattr(value, field.name))
                for field in fields(value)
                if field.name not in {"source", "type"}
            )
        return False

    return visit(statements)


def _is_low_level_cfg(function: FunctionIR) -> bool:
    return function.recovery_kind in {
        "generic-vm-low-level-cfg",
        "generic-vm-low-level-cfg-structured",
    }


def _with_replaced_block(
    function: FunctionIR,
    *,
    source_id: str,
    replacement: BasicBlock,
    removed_ids: frozenset[str],
    rule: str,
) -> FunctionIR:
    """Build a local collapsed view without changing unrelated block order."""

    blocks = tuple(
        replacement if block.id == source_id else block
        for block in function.blocks
        if block.id not in removed_ids
    )
    return replace(
        function,
        blocks=blocks,
        recovery_kind="generic-vm-low-level-cfg-structured",
        metadata={
            **function.metadata,
            "structured_lift": "generic-vm-low-level-cfg-structured",
            "low_level_cfg_structured": rule,
        },
    )


def _same_region_scope(region: RegionGraph, members: frozenset[str]) -> bool:
    """Require a plain acyclic ordinary scope for this first local rule."""

    for edge in region.edges:
        if edge.source in members or edge.target in members:
            if edge.roles & {"back", "loop-exit", "exceptional", "irreducible"}:
                return False
    return True
