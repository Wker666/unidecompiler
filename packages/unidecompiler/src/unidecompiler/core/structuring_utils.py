"""Small VM-neutral helpers shared by core structuring passes."""

from __future__ import annotations

from unidecompiler.core.ir import Break, Continue, If, Stmt, Switch, Try


def contains_unscoped_loop_control(statements: tuple[Stmt, ...]) -> bool:
    """Return whether ``statements`` contain an outer-loop control.

    ``If``, ``Switch`` and ``Try`` do not introduce loop scope, while a
    nested ``While``/``DoWhile`` owns its own controls.  The traversal is
    therefore deliberately limited to the non-loop structured statements.
    Unknown statement dataclasses are not traversed: a new construct must
    opt into this proof explicitly rather than silently changing a control's
    target.
    """

    for statement in statements:
        if isinstance(statement, (Break, Continue)):
            return True
        if isinstance(statement, If) and (
            contains_unscoped_loop_control(statement.then_body)
            or contains_unscoped_loop_control(statement.else_body)
        ):
            return True
        if isinstance(statement, Switch) and (
            any(
                contains_unscoped_loop_control(body)
                for _value, body in statement.cases
            )
            or contains_unscoped_loop_control(statement.default_body)
        ):
            return True
        if isinstance(statement, Try) and (
            contains_unscoped_loop_control(statement.body)
            or any(
                contains_unscoped_loop_control(handler.body)
                for handler in statement.handlers
            )
        ):
            return True
    return False
