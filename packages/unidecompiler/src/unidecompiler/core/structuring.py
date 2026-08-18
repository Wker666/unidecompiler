from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import (
    Assign,
    BasicBlock,
    BinaryOp,
    Branch,
    Const,
    Expr,
    FunctionIR,
    Jump,
    Return,
    Var,
)


@dataclass(frozen=True)
class StructuredBlock:
    block: BasicBlock


@dataclass(frozen=True)
class StructuredIfElse:
    condition: Expr
    prelude: BasicBlock
    then_block: BasicBlock
    else_block: BasicBlock
    continuation_block: BasicBlock | None = None


@dataclass(frozen=True)
class StructuredWhile:
    setup: BasicBlock
    header_id: str
    condition: Expr
    body: BasicBlock
    exit_block: BasicBlock


@dataclass(frozen=True)
class StructuredForRange:
    setup: BasicBlock
    header_id: str
    target: Var
    start: Expr
    stop: Expr
    step: Expr
    body: BasicBlock
    exit_block: BasicBlock


@dataclass(frozen=True)
class StructuredFunction:
    nodes: tuple[StructuredBlock | StructuredIfElse | StructuredWhile | StructuredForRange, ...]
    diagnostics: tuple[str, ...] = ()


def structure_function(function: FunctionIR) -> StructuredFunction:
    _ = build_cfg(function)
    if if_else := _match_simple_if_else(function.blocks):
        return StructuredFunction(nodes=(if_else,))
    if if_join := _match_if_with_join(function.blocks):
        return StructuredFunction(nodes=(if_join,))
    if for_range := _match_simple_for_range(function.blocks):
        return StructuredFunction(nodes=(for_range,))
    if while_loop := _match_simple_while(function.blocks):
        return StructuredFunction(nodes=(while_loop,))
    return StructuredFunction(
        nodes=tuple(StructuredBlock(block=block) for block in function.blocks)
    )


def _match_simple_if_else(
    blocks: tuple[BasicBlock, ...],
) -> StructuredIfElse | None:
    if len(blocks) != 3:
        return None
    entry, then_block, else_block = blocks
    if not isinstance(entry.terminator, Branch):
        return None
    if entry.terminator.true_target != then_block.id:
        return None
    if entry.terminator.false_target != else_block.id:
        return None
    if not isinstance(then_block.terminator, Return):
        return None
    if not isinstance(else_block.terminator, Return):
        return None
    return StructuredIfElse(
        condition=entry.terminator.condition,
        prelude=BasicBlock(id=entry.id, statements=entry.statements),
        then_block=then_block,
        else_block=else_block,
    )


def _match_if_with_join(
    blocks: tuple[BasicBlock, ...],
) -> StructuredIfElse | None:
    if len(blocks) == 3:
        entry, then_block, join_block = blocks
        if not isinstance(entry.terminator, Branch):
            return None
        if entry.terminator.true_target != then_block.id:
            return None
        if entry.terminator.false_target != join_block.id:
            return None
        if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != join_block.id:
            return None
        return StructuredIfElse(
            condition=entry.terminator.condition,
            prelude=BasicBlock(id=entry.id, statements=entry.statements),
            then_block=then_block,
            else_block=BasicBlock(id="empty_else"),
            continuation_block=join_block,
        )
    if len(blocks) != 4:
        return None
    entry, then_block, else_block, join_block = blocks
    if not isinstance(entry.terminator, Branch):
        return None
    if entry.terminator.true_target != then_block.id:
        return None
    if entry.terminator.false_target not in {else_block.id, join_block.id}:
        return None
    if not isinstance(then_block.terminator, Jump) or then_block.terminator.target != join_block.id:
        return None
    if entry.terminator.false_target == join_block.id:
        return StructuredIfElse(
            condition=entry.terminator.condition,
            prelude=BasicBlock(id=entry.id, statements=entry.statements),
            then_block=then_block,
            else_block=BasicBlock(id="empty_else"),
            continuation_block=join_block,
        )
    if not isinstance(else_block.terminator, Jump) or else_block.terminator.target != join_block.id:
        return None
    return StructuredIfElse(
        condition=entry.terminator.condition,
        prelude=BasicBlock(id=entry.id, statements=entry.statements),
        then_block=then_block,
        else_block=else_block,
        continuation_block=join_block,
    )


def _match_simple_while(
    blocks: tuple[BasicBlock, ...],
) -> StructuredWhile | None:
    if len(blocks) != 4:
        return None
    setup, condition, body, exit_block = blocks
    if not isinstance(setup.terminator, Jump):
        return None
    if setup.terminator.target != condition.id:
        return None
    if not isinstance(condition.terminator, Branch):
        return None
    if condition.terminator.true_target != body.id:
        return None
    if condition.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != condition.id:
        return None
    if not isinstance(exit_block.terminator, Return):
        return None
    return StructuredWhile(
        setup=setup,
        header_id=condition.id,
        condition=condition.terminator.condition,
        body=body,
        exit_block=exit_block,
    )


def _match_simple_for_range(
    blocks: tuple[BasicBlock, ...],
) -> StructuredForRange | None:
    if len(blocks) != 4:
        return None
    setup, condition, body, exit_block = blocks
    if not isinstance(setup.terminator, Jump) or setup.terminator.target != condition.id:
        return None
    if not isinstance(condition.terminator, Branch):
        return None
    if condition.terminator.true_target != body.id:
        return None
    if condition.terminator.false_target != exit_block.id:
        return None
    if not isinstance(body.terminator, Jump) or body.terminator.target != condition.id:
        return None
    if not isinstance(exit_block.terminator, Return):
        return None
    if len(setup.statements) != 1:
        return None

    initializer = setup.statements[0]
    if not isinstance(initializer, Assign) or not isinstance(initializer.target, Var):
        return None

    condition_expr = condition.terminator.condition
    if not isinstance(condition_expr, BinaryOp):
        return None
    if not _same_var(condition_expr.left, initializer.target):
        return None

    step = _infer_loop_step(body, initializer.target)
    if step is None:
        return None

    return StructuredForRange(
        setup=setup,
        header_id=condition.id,
        target=initializer.target,
        start=initializer.value,
        stop=condition_expr.right,
        step=step,
        body=body,
        exit_block=exit_block,
    )


def _infer_loop_step(body: BasicBlock, target: Var) -> Expr | None:
    for statement in reversed(body.statements):
        if not isinstance(statement, Assign):
            continue
        if statement.target.name != target.name:
            continue
        value = statement.value
        if not isinstance(value, BinaryOp):
            continue
        if not _same_var(value.left, target):
            continue
        if isinstance(value.right, Const):
            return value.right
    return None


def _same_var(left: Expr, right: Var) -> bool:
    return isinstance(left, Var) and left.name == right.name
