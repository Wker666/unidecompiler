from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from unidecompiler.core.ir import SourceRef, TypeRef


@dataclass(frozen=True)
class AstExpr:
    source: SourceRef | None = None
    type: TypeRef = field(default_factory=TypeRef)


@dataclass(frozen=True)
class VarRef(AstExpr):
    name: str = ""


@dataclass(frozen=True)
class ConstExpr(AstExpr):
    value: Any = None


@dataclass(frozen=True)
class UnaryExpr(AstExpr):
    op: str = ""
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class BinaryExpr(AstExpr):
    op: str = ""
    left: AstExpr = field(default_factory=AstExpr)
    right: AstExpr = field(default_factory=AstExpr)
    semantics: Literal["static", "dynamic"] = "dynamic"


@dataclass(frozen=True)
class CallExpr(AstExpr):
    callee: AstExpr = field(default_factory=AstExpr)
    args: tuple[AstExpr, ...] = ()
    keywords: tuple[tuple[str, AstExpr], ...] = ()
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class MultiReturnExpr(AstExpr):
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class PhiExpr(AstExpr):
    incoming: tuple[tuple[str, AstExpr], ...] = ()


@dataclass(frozen=True)
class GlobalRef(AstExpr):
    name: str = ""


@dataclass(frozen=True)
class CapturedVarRef(AstExpr):
    name: str = ""


@dataclass(frozen=True)
class IndirectRefExpr(AstExpr):
    target: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class TableField:
    key: AstExpr
    value: AstExpr


@dataclass(frozen=True)
class TableLiteralExpr(AstExpr):
    array_items: tuple[AstExpr, ...] = ()
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class ArrayLiteralExpr(AstExpr):
    items: tuple[AstExpr, ...] = ()


@dataclass(frozen=True)
class SetLiteralExpr(AstExpr):
    items: tuple[AstExpr, ...] = ()


@dataclass(frozen=True)
class CollectionProjectionExpr(AstExpr):
    kind: Literal["list", "set"] = "list"
    target: VarRef = field(default_factory=VarRef)
    iterable: AstExpr = field(default_factory=AstExpr)
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class ObjectLiteralExpr(AstExpr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class MapLiteralExpr(AstExpr):
    fields: tuple[TableField, ...] = ()


@dataclass(frozen=True)
class NewObjectExpr(AstExpr):
    type_name: str = "unknown"
    args: tuple[AstExpr, ...] = ()


@dataclass(frozen=True)
class GetAttrExpr(AstExpr):
    obj: AstExpr = field(default_factory=AstExpr)
    attr: str = ""


@dataclass(frozen=True)
class GetItemExpr(AstExpr):
    obj: AstExpr = field(default_factory=AstExpr)
    key: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class IndirectCallExpr(AstExpr):
    selector: AstExpr = field(default_factory=AstExpr)
    signature: str = "unknown"


@dataclass(frozen=True)
class AstStmt:
    source: SourceRef | None = None


@dataclass(frozen=True)
class AssignStmt(AstStmt):
    target: AstExpr = field(default_factory=VarRef)
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class AssignManyStmt(AstStmt):
    targets: tuple[VarRef, ...] = ()
    values: tuple[AstExpr, ...] = ()


@dataclass(frozen=True)
class StoreAttrStmt(AstStmt):
    obj: AstExpr = field(default_factory=AstExpr)
    attr: str = ""
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class StoreItemStmt(AstStmt):
    obj: AstExpr = field(default_factory=AstExpr)
    key: AstExpr = field(default_factory=AstExpr)
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class ExprStmt(AstStmt):
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class ReturnStmt(AstStmt):
    values: tuple[AstExpr, ...] = ()


@dataclass(frozen=True)
class IfStmt(AstStmt):
    condition: AstExpr = field(default_factory=AstExpr)
    then_body: tuple[AstStmt, ...] = ()
    else_body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class SwitchStmt(AstStmt):
    selector: AstExpr = field(default_factory=AstExpr)
    cases: tuple[tuple[AstExpr, tuple[AstStmt, ...]], ...] = ()
    default_body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class WhileStmt(AstStmt):
    condition: AstExpr = field(default_factory=AstExpr)
    body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class BreakStmt(AstStmt):
    pass


@dataclass(frozen=True)
class ContinueStmt(AstStmt):
    pass


@dataclass(frozen=True)
class LabelStmt(AstStmt):
    name: str = ""


@dataclass(frozen=True)
class GotoStmt(AstStmt):
    target: str = ""


@dataclass(frozen=True)
class IfGotoStmt(AstStmt):
    condition: AstExpr = field(default_factory=AstExpr)
    true_target: str = ""
    false_target: str = ""


@dataclass(frozen=True)
class SwitchGotoStmt(AstStmt):
    selector: AstExpr = field(default_factory=AstExpr)
    cases: tuple[tuple[AstExpr, str], ...] = ()
    default_target: str = ""


@dataclass(frozen=True)
class ForRangeStmt(AstStmt):
    target: VarRef = field(default_factory=VarRef)
    start: AstExpr = field(default_factory=AstExpr)
    stop: AstExpr = field(default_factory=AstExpr)
    step: AstExpr = field(default_factory=AstExpr)
    body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class ForEachStmt(AstStmt):
    target: VarRef = field(default_factory=VarRef)
    iterable: AstExpr = field(default_factory=AstExpr)
    body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class UnsupportedStmt(AstStmt):
    message: str = ""
    raw: tuple[str, ...] = ()


@dataclass(frozen=True)
class RaiseStmt(AstStmt):
    value: AstExpr = field(default_factory=AstExpr)
    cause: AstExpr | None = None


@dataclass(frozen=True)
class ReraiseStmt(AstStmt):
    pass


@dataclass(frozen=True)
class YieldStmt(AstStmt):
    value: AstExpr = field(default_factory=AstExpr)


@dataclass(frozen=True)
class ExceptHandlerStmt:
    exception_type: AstExpr = field(default_factory=AstExpr)
    binding: VarRef | None = None
    body: tuple[AstStmt, ...] = ()


@dataclass(frozen=True)
class TryStmt(AstStmt):
    body: tuple[AstStmt, ...] = ()
    handlers: tuple[ExceptHandlerStmt, ...] = ()


@dataclass(frozen=True)
class FunctionDecl:
    name: str
    params: tuple[str, ...] = ()
    body: tuple[AstStmt, ...] = ()
    nested_functions: tuple["FunctionDecl", ...] = ()
    source: SourceRef | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleDecl:
    name: str
    functions: tuple[FunctionDecl, ...] = ()
    source_language: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
