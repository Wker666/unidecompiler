from __future__ import annotations

from unidecompiler.core.ir import (
    Branch,
    Break,
    Continue,
    Expr,
    ForEach,
    ForRange,
    Return,
    SourceRef,
    Stmt,
    Terminator,
    Try,
    Unsupported,
    Var,
    While,
    If,
)


VM_STRUCTURE_NODE_TYPES = (If, While, ForEach, ForRange)


def vm_return(source: SourceRef | None = None, values: tuple[Expr, ...] = ()) -> Return:
    return Return(source=source, values=values)


def vm_branch(
    *,
    source: SourceRef | None = None,
    condition: Expr,
    true_target: str,
    false_target: str,
) -> Branch:
    return Branch(source=source, condition=condition, true_target=true_target, false_target=false_target)


def vm_if(
    *,
    source: SourceRef | None = None,
    condition: Expr,
    then_body: tuple[Stmt, ...] = (),
    else_body: tuple[Stmt, ...] = (),
) -> If:
    return If(source=source, condition=condition, then_body=then_body, else_body=else_body)


def vm_while(
    *,
    source: SourceRef | None = None,
    condition: Expr,
    body: tuple[Stmt, ...] = (),
) -> While:
    return While(source=source, condition=condition, body=body)


def vm_foreach(
    *,
    source: SourceRef | None = None,
    target: Var,
    iterable: Expr,
    body: tuple[Stmt, ...] = (),
) -> ForEach:
    return ForEach(source=source, target=target, iterable=iterable, body=body)


def vm_for_range(
    *,
    source: SourceRef | None = None,
    target: Var,
    start: Expr,
    stop: Expr,
    step: Expr,
    body: tuple[Stmt, ...] = (),
) -> ForRange:
    return ForRange(source=source, target=target, start=start, stop=stop, step=step, body=body)


def vm_unsupported(
    *,
    source: SourceRef | None = None,
    message: str = "unsupported region",
    detail: str | None = None,
    raw: tuple[str, ...] = (),
) -> Unsupported:
    return Unsupported(source=source, message=message, detail=detail, raw=raw)


def vm_break(source: SourceRef | None = None) -> Break:
    return Break(source=source)


def vm_continue(source: SourceRef | None = None) -> Continue:
    return Continue(source=source)


def vm_no_terminator() -> Terminator | None:
    return None


def is_vm_return(value: object) -> bool:
    return isinstance(value, Return)


def is_vm_unsupported(value: object) -> bool:
    return isinstance(value, Unsupported)


def contains_vm_unsupported(value: object) -> bool:
    if isinstance(value, Unsupported):
        return True
    if isinstance(value, If):
        return any(contains_vm_unsupported(inner) for inner in (*value.then_body, *value.else_body))
    if isinstance(value, (While, ForEach, ForRange)):
        return any(contains_vm_unsupported(inner) for inner in value.body)
    if isinstance(value, Try):
        return any(contains_vm_unsupported(inner) for inner in value.body) or any(
            contains_vm_unsupported(inner)
            for handler in value.handlers
            for inner in handler.body
        )
    return False
