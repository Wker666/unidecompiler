from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


ValueId = str
BlockId = str


@dataclass(frozen=True)
class SourceRef:
    """Best-effort location in a frontend-specific input artifact."""

    frontend: str
    offset: int | None = None
    line: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class TypeRef:
    """A deliberately loose type reference.

    Frontends may provide precise types, but dynamic VMs can leave this as
    ``unknown`` without lying to later passes.
    """

    name: str = "unknown"


@dataclass(frozen=True)
class Expr:
    source: SourceRef | None = None
    type: TypeRef = field(default_factory=TypeRef)


@dataclass(frozen=True)
class Var(Expr):
    name: ValueId = ""


@dataclass(frozen=True)
class Const(Expr):
    value: Any = None


@dataclass(frozen=True)
class UnaryOp(Expr):
    op: str = ""
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class BinaryOp(Expr):
    op: str = ""
    left: Expr = field(default_factory=Expr)
    right: Expr = field(default_factory=Expr)
    semantics: Literal["static", "dynamic"] = "dynamic"


@dataclass(frozen=True)
class Call(Expr):
    callee: Expr = field(default_factory=Expr)
    args: tuple[Expr, ...] = ()
    keywords: tuple[TableField, ...] = ()
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class MultiReturn(Expr):
    """Represents a dynamic-language expression that may yield many values."""

    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Phi(Expr):
    """SSA phi node with incoming values keyed by predecessor block id."""

    incoming: tuple[tuple[str, Expr], ...] = ()


@dataclass(frozen=True)
class Global(Expr):
    name: str = ""


@dataclass(frozen=True)
class CapturedVar(Expr):
    """A value supplied by an enclosing function or VM activation environment."""

    name: str = ""


@dataclass(frozen=True)
class Placeholder(Expr):
    """Temporary value identity used while generic effects recover VM state."""

    token: str = ""
    label: str = "placeholder"


@dataclass(frozen=True)
class TableField:
    key: Expr
    value: Expr


@dataclass(frozen=True)
class TableLiteral(Expr):
    array_items: tuple[Expr, ...] = ()
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class ArrayLiteral(Expr):
    items: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class SetLiteral(Expr):
    items: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class CollectionProjection(Expr):
    kind: Literal["list", "set"] = "list"
    target: Var = field(default_factory=Var)
    iterable: Expr = field(default_factory=Expr)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ObjectLiteral(Expr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class MapLiteral(Expr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class NewObject(Expr):
    type_name: str = "unknown"
    args: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class GetAttr(Expr):
    obj: Expr = field(default_factory=Expr)
    attr: str = ""


@dataclass(frozen=True)
class GetItem(Expr):
    obj: Expr = field(default_factory=Expr)
    key: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class IndirectCall(Expr):
    """A VM-neutral dynamic dispatch with an explicit selector operand."""

    selector: Expr = field(default_factory=Expr)
    signature: str = "unknown"


@dataclass(frozen=True)
class IndirectRef(Expr):
    """A VM-neutral writable reference to a value location."""

    target: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Stmt:
    source: SourceRef | None = None


@dataclass(frozen=True)
class Assign(Stmt):
    target: Expr = field(default_factory=Var)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class AssignMany(Stmt):
    targets: tuple[Var, ...] = ()
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class StoreAttr(Stmt):
    obj: Expr = field(default_factory=Expr)
    attr: str = ""
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class StoreItem(Stmt):
    obj: Expr = field(default_factory=Expr)
    key: Expr = field(default_factory=Expr)
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ExprStmt(Stmt):
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class Unsupported(Stmt):
    message: str = "unsupported"
    detail: str | None = None
    raw: tuple[str, ...] = ()


@dataclass(frozen=True)
class Raise(Stmt):
    value: Expr = field(default_factory=Expr)
    cause: Expr | None = None


@dataclass(frozen=True)
class Reraise(Stmt):
    pass


@dataclass(frozen=True)
class Yield(Stmt):
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class ExceptHandler:
    """A recovered handler attached to a generic protected region."""

    exception_type: Expr = field(default_factory=Expr)
    binding: Var | None = None
    body: tuple[Stmt | Terminator, ...] = ()


@dataclass(frozen=True)
class Try(Stmt):
    """A structured protected region recovered by the VM-neutral core."""

    body: tuple[Stmt | Terminator, ...] = ()
    handlers: tuple[ExceptHandler, ...] = ()


@dataclass(frozen=True)
class If(Stmt):
    condition: Expr = field(default_factory=Expr)
    then_body: tuple[Stmt, ...] = ()
    else_body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Switch(Stmt):
    """A structured VM-neutral multi-way dispatch."""

    selector: Expr = field(default_factory=Expr)
    cases: tuple[tuple[Expr, tuple[Stmt, ...]], ...] = ()
    default_body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class While(Stmt):
    condition: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class ForEach(Stmt):
    target: Var = field(default_factory=Var)
    iterable: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Break(Stmt):
    pass


@dataclass(frozen=True)
class Continue(Stmt):
    pass


@dataclass(frozen=True)
class ForRange(Stmt):
    target: Var = field(default_factory=Var)
    start: Expr = field(default_factory=Expr)
    stop: Expr = field(default_factory=Expr)
    step: Expr = field(default_factory=Expr)
    body: tuple[Stmt, ...] = ()


@dataclass(frozen=True)
class Terminator:
    source: SourceRef | None = None


@dataclass(frozen=True)
class Return(Terminator):
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class Jump(Terminator):
    target: BlockId = ""


@dataclass(frozen=True)
class Branch(Terminator):
    condition: Expr = field(default_factory=Expr)
    true_target: BlockId = ""
    false_target: BlockId = ""


@dataclass(frozen=True)
class MultiBranch(Terminator):
    selector: Expr = field(default_factory=Expr)
    cases: tuple[tuple[Expr, BlockId], ...] = ()
    default_target: BlockId = ""


@dataclass(frozen=True)
class BasicBlock:
    id: BlockId
    statements: tuple[Stmt, ...] = ()
    terminator: Terminator | None = None


@dataclass(frozen=True)
class FunctionIR:
    name: str
    params: tuple[str, ...] = ()
    blocks: tuple[BasicBlock, ...] = ()
    nested_functions: tuple["FunctionIR", ...] = ()
    source: SourceRef | None = None
    recovery_kind: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleIR:
    name: str
    functions: tuple[FunctionIR, ...] = ()
    source_language: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
