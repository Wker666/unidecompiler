from __future__ import annotations

from unidecompiler.core.ast import (
    AssignManyStmt,
    ArrayLiteralExpr,
    AssignStmt,
    AstExpr,
    BinaryExpr,
    CallExpr,
    CapturedVarRef,
    CollectionProjectionExpr,
    ConstExpr,
    ExprStmt,
    ForRangeStmt,
    FunctionDecl,
    GetAttrExpr,
    GetItemExpr,
    GlobalRef,
    GotoStmt,
    IndirectCallExpr,
    BreakStmt,
    IfGotoStmt,
    ForEachStmt,
    IfStmt,
    LabelStmt,
    IndirectRefExpr,
    MapLiteralExpr,
    SwitchGotoStmt,
    MultiReturnExpr,
    ModuleDecl,
    NewObjectExpr,
    ObjectLiteralExpr,
    PhiExpr,
    RaiseStmt,
    ReraiseStmt,
    ReturnStmt,
    SetLiteralExpr,
    StoreAttrStmt,
    StoreItemStmt,
    SwitchStmt,
    TableField,
    TableLiteralExpr,
    ExceptHandlerStmt,
    TryStmt,
    UnaryExpr,
    UnsupportedStmt,
    VarRef,
    YieldStmt,
    ContinueStmt,
    WhileStmt,
)
from unidecompiler.core.ir import (
    AssignMany,
    ArrayLiteral,
    Assign,
    BinaryOp,
    Branch,
    Break,
    Call,
    CapturedVar,
    CollectionProjection,
    Const,
    Continue,
    Expr,
    ForEach,
    ForRange,
    ExprStmt as IrExprStmt,
    FunctionIR,
    GetAttr,
    GetItem,
    IndirectCall,
    IndirectRef,
    Global,
    If,
    Jump,
    MapLiteral,
    MultiBranch,
    MultiReturn,
    ModuleIR,
    NewObject,
    ObjectLiteral,
    Phi,
    Raise,
    Reraise,
    Return,
    SetLiteral,
    StoreAttr,
    StoreItem,
    Switch,
    TableField as IrTableField,
    TableLiteral,
    ExceptHandler,
    Try,
    UnaryOp,
    Unsupported,
    Var,
    While,
    Yield,
)
from unidecompiler.core.structuring import (
    StructuredBlock,
    StructuredForRange,
    StructuredIfElse,
    StructuredWhile,
    structure_function,
)
from unidecompiler.core.ssa import index_assignments
from unidecompiler.core.ssa import insert_phi_nodes


def module_to_ast(module: ModuleIR) -> ModuleDecl:
    return ModuleDecl(
        name=module.name,
        source_language=module.source_language,
        metadata=module.metadata,
        functions=tuple(function_to_ast(function) for function in module.functions),
    )


def function_to_ast(function: FunctionIR) -> FunctionDecl:
    ssa_index = index_assignments(function)
    function = insert_phi_nodes(function)
    if function.metadata.get("decompile_status") == "unsupported":
        return FunctionDecl(
            name=function.name,
            params=tuple(_logical_name(param) for param in function.params),
            source=function.source,
            metadata={**function.metadata, "ssa_index": _ssa_index_to_metadata(ssa_index)},
            body=(
                UnsupportedStmt(
                    source=function.source,
                    message=function.metadata.get("unsupported_reason", "unsupported"),
                ),
            ),
        )

    if function.recovery_kind == "generic-vm-low-level-cfg":
        body: list[object] = []
        for block in function.blocks:
            body.extend(_low_level_block_to_ast(block))
        return FunctionDecl(
            name=function.name,
            params=tuple(_logical_name(param) for param in function.params),
            source=function.source,
            metadata={**function.metadata, "ssa_index": _ssa_index_to_metadata(ssa_index)},
            body=tuple(body),
            nested_functions=tuple(function_to_ast(nested) for nested in function.nested_functions),
        )

    structured = structure_function(function)
    body: list[object] = []
    for node in structured.nodes:
        body.extend(_node_to_ast(node))
    return FunctionDecl(
        name=function.name,
        params=tuple(_logical_name(param) for param in function.params),
        source=function.source,
        metadata={**function.metadata, "ssa_index": _ssa_index_to_metadata(ssa_index)},
        body=tuple(body),
        nested_functions=tuple(function_to_ast(nested) for nested in function.nested_functions),
    )


def _node_to_ast(node) -> tuple[object, ...]:
    if isinstance(node, StructuredIfElse):
        then_body = (
            _block_statements_to_ast(node.then_block)
            if node.continuation_block is not None
            else _block_to_ast(node.then_block)
        )
        else_body = (
            _block_statements_to_ast(node.else_block)
            if node.continuation_block is not None
            else _block_to_ast(node.else_block)
        )
        continuation = (
            _block_to_ast(node.continuation_block)
            if node.continuation_block is not None
            else ()
        )
        return (
            *_block_statements_to_ast(node.prelude),
            IfStmt(
                condition=_expr_to_ast(node.condition),
                then_body=then_body,
                else_body=else_body,
            ),
            *continuation,
        )
    if isinstance(node, StructuredWhile):
        return (
            *_block_statements_to_ast(node.setup),
            WhileStmt(
                condition=_expr_to_ast(node.condition),
                body=_block_statements_to_ast(node.body),
            ),
            *_block_to_ast(node.exit_block),
        )
    if isinstance(node, StructuredForRange):
        return (
            ForRangeStmt(
                target=VarRef(name=_logical_name(node.target.name)),
                start=_expr_to_ast(node.start),
                stop=_expr_to_ast(node.stop),
                step=_expr_to_ast(node.step),
                body=_block_statements_to_ast(node.body),
            ),
            *_block_to_ast(node.exit_block),
        )
    if isinstance(node, StructuredBlock):
        return _block_to_ast(node.block)
    return (UnsupportedStmt(message=f"unsupported structured node: {type(node).__name__}"),)


def _block_to_ast(block) -> tuple[object, ...]:
    statements: list[object] = []
    for statement in block.statements:
        statements.append(_statement_to_ast(statement))
    if block.terminator is not None:
        statements.append(_terminator_to_ast(block.terminator))
    return tuple(statements)


def _low_level_block_to_ast(block) -> tuple[object, ...]:
    statements: list[object] = [LabelStmt(name=block.id)]
    for statement in block.statements:
        statements.append(_statement_to_ast(statement))
    if block.terminator is not None:
        statements.append(_terminator_to_ast(block.terminator))
    return tuple(statements)


def _block_statements_to_ast(block) -> tuple[object, ...]:
    return tuple(_statement_to_ast(statement) for statement in block.statements)


def _statement_to_ast(statement) -> object:
    if isinstance(statement, Assign):
        return AssignStmt(
            source=statement.source,
            target=_expr_to_ast(statement.target),
            value=_expr_to_ast(statement.value),
        )
    if isinstance(statement, AssignMany):
        return AssignManyStmt(
            source=statement.source,
            targets=tuple(
                VarRef(name=_logical_name(target.name), source=target.source)
                for target in statement.targets
            ),
            values=tuple(_expr_to_ast(value) for value in statement.values),
        )
    if isinstance(statement, StoreAttr):
        return StoreAttrStmt(
            source=statement.source,
            obj=_expr_to_ast(statement.obj),
            attr=statement.attr,
            value=_expr_to_ast(statement.value),
        )
    if isinstance(statement, StoreItem):
        return StoreItemStmt(
            source=statement.source,
            obj=_expr_to_ast(statement.obj),
            key=_expr_to_ast(statement.key),
            value=_expr_to_ast(statement.value),
        )
    if isinstance(statement, If):
        return IfStmt(
            source=statement.source,
            condition=_expr_to_ast(statement.condition),
            then_body=tuple(_statement_to_ast(inner) for inner in statement.then_body),
            else_body=tuple(_statement_to_ast(inner) for inner in statement.else_body),
        )
    if isinstance(statement, Switch):
        return SwitchStmt(
            source=statement.source,
            selector=_expr_to_ast(statement.selector),
            cases=tuple(
                (_expr_to_ast(value), tuple(_statement_to_ast(inner) for inner in body))
                for value, body in statement.cases
            ),
            default_body=tuple(_statement_to_ast(inner) for inner in statement.default_body),
        )
    if isinstance(statement, While):
        return WhileStmt(
            source=statement.source,
            condition=_expr_to_ast(statement.condition),
            body=tuple(_statement_to_ast(inner) for inner in statement.body),
        )
    if isinstance(statement, ForEach):
        return ForEachStmt(
            source=statement.source,
            target=VarRef(name=_logical_name(statement.target.name)),
            iterable=_expr_to_ast(statement.iterable),
            body=tuple(_statement_to_ast(inner) for inner in statement.body),
        )
    if isinstance(statement, ForRange):
        return ForRangeStmt(
            source=statement.source,
            target=VarRef(name=_logical_name(statement.target.name)),
            start=_expr_to_ast(statement.start),
            stop=_expr_to_ast(statement.stop),
            step=_expr_to_ast(statement.step),
            body=tuple(_statement_to_ast(inner) for inner in statement.body),
        )
    if isinstance(statement, Break):
        return BreakStmt(source=statement.source)
    if isinstance(statement, Continue):
        return ContinueStmt(source=statement.source)
    if isinstance(statement, Return):
        return ReturnStmt(
            source=statement.source,
            values=tuple(_expr_to_ast(value) for value in statement.values),
        )
    if isinstance(statement, IrExprStmt):
        return ExprStmt(source=statement.source, value=_expr_to_ast(statement.value))
    if isinstance(statement, Unsupported):
        message = statement.message
        if statement.detail:
            message = f"{message}: {statement.detail}"
        return UnsupportedStmt(source=statement.source, message=message, raw=statement.raw)
    if isinstance(statement, Raise):
        return RaiseStmt(
            source=statement.source,
            value=_expr_to_ast(statement.value),
            cause=_expr_to_ast(statement.cause) if statement.cause is not None else None,
        )
    if isinstance(statement, Reraise):
        return ReraiseStmt(source=statement.source)
    if isinstance(statement, Yield):
        return YieldStmt(source=statement.source, value=_expr_to_ast(statement.value))
    if isinstance(statement, Try):
        return TryStmt(
            source=statement.source,
            body=tuple(_statement_or_terminator_to_ast(inner) for inner in statement.body),
            handlers=tuple(
                ExceptHandlerStmt(
                    exception_type=_expr_to_ast(handler.exception_type),
                    binding=(
                        VarRef(name=_logical_name(handler.binding.name), source=handler.binding.source)
                        if handler.binding is not None
                        else None
                    ),
                    body=tuple(_statement_or_terminator_to_ast(inner) for inner in handler.body),
                )
                for handler in statement.handlers
            ),
        )
    return UnsupportedStmt(source=statement.source, message=f"unsupported statement: {type(statement).__name__}")


def _statement_or_terminator_to_ast(item) -> object:
    if isinstance(item, (Return, Jump, Branch, MultiBranch)):
        return _terminator_to_ast(item)
    return _statement_to_ast(item)


def _terminator_to_ast(terminator) -> object:
    if isinstance(terminator, Return):
        return ReturnStmt(
            source=terminator.source,
            values=tuple(_expr_to_ast(value) for value in terminator.values),
        )
    if isinstance(terminator, Jump):
        return GotoStmt(source=terminator.source, target=terminator.target)
    if isinstance(terminator, Branch):
        return IfGotoStmt(
            source=terminator.source,
            condition=_expr_to_ast(terminator.condition),
            true_target=terminator.true_target,
            false_target=terminator.false_target,
        )
    if isinstance(terminator, MultiBranch):
        return SwitchGotoStmt(
            source=terminator.source,
            selector=_expr_to_ast(terminator.selector),
            cases=tuple((_expr_to_ast(value), target) for value, target in terminator.cases),
            default_target=terminator.default_target,
        )
    return UnsupportedStmt(source=terminator.source, message=f"unsupported terminator: {type(terminator).__name__}")


def _expr_to_ast(expr: Expr) -> AstExpr:
    if isinstance(expr, Var):
        return VarRef(source=expr.source, type=expr.type, name=_logical_name(expr.name))
    if isinstance(expr, Const):
        return ConstExpr(source=expr.source, type=expr.type, value=expr.value)
    if isinstance(expr, UnaryOp):
        return UnaryExpr(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            value=_expr_to_ast(expr.value),
        )
    if isinstance(expr, BinaryOp):
        return BinaryExpr(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            left=_expr_to_ast(expr.left),
            right=_expr_to_ast(expr.right),
            semantics=expr.semantics,
        )
    if isinstance(expr, Call):
        return CallExpr(
            source=expr.source,
            type=expr.type,
            callee=_expr_to_ast(expr.callee),
            args=tuple(_expr_to_ast(arg) for arg in expr.args),
            keywords=tuple(
                (_keyword_name(field.key), _expr_to_ast(field.value))
                for field in expr.keywords
            ),
            returns=expr.returns,
        )
    if isinstance(expr, MultiReturn):
        return MultiReturnExpr(source=expr.source, type=expr.type, value=_expr_to_ast(expr.value))
    if isinstance(expr, Phi):
        return PhiExpr(
            source=expr.source,
            type=expr.type,
            incoming=tuple((pred, _expr_to_ast(value)) for pred, value in expr.incoming),
        )
    if isinstance(expr, Global):
        return GlobalRef(source=expr.source, type=expr.type, name=expr.name)
    if isinstance(expr, CapturedVar):
        return CapturedVarRef(source=expr.source, type=expr.type, name=expr.name)
    if isinstance(expr, GetAttr):
        return GetAttrExpr(
            source=expr.source,
            type=expr.type,
            obj=_expr_to_ast(expr.obj),
            attr=expr.attr,
        )
    if isinstance(expr, GetItem):
        return GetItemExpr(
            source=expr.source,
            type=expr.type,
            obj=_expr_to_ast(expr.obj),
            key=_expr_to_ast(expr.key),
        )
    if isinstance(expr, IndirectCall):
        return IndirectCallExpr(
            source=expr.source,
            type=expr.type,
            selector=_expr_to_ast(expr.selector),
            signature=expr.signature,
        )
    if isinstance(expr, IndirectRef):
        return IndirectRefExpr(source=expr.source, type=expr.type, target=_expr_to_ast(expr.target))
    if isinstance(expr, TableLiteral):
        return TableLiteralExpr(
            source=expr.source,
            type=expr.type,
            array_items=tuple(_expr_to_ast(item) for item in expr.array_items),
            fields=tuple(
                TableField(
                    key=_expr_to_ast(field.key),
                    value=_expr_to_ast(field.value),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, ArrayLiteral):
        return ArrayLiteralExpr(
            source=expr.source,
            type=expr.type,
            items=tuple(_expr_to_ast(item) for item in expr.items),
        )
    if isinstance(expr, SetLiteral):
        return SetLiteralExpr(
            source=expr.source,
            type=expr.type,
            items=tuple(_expr_to_ast(item) for item in expr.items),
        )
    if isinstance(expr, CollectionProjection):
        return CollectionProjectionExpr(
            source=expr.source,
            type=expr.type,
            kind=expr.kind,
            target=_expr_to_ast(expr.target),
            iterable=_expr_to_ast(expr.iterable),
            value=_expr_to_ast(expr.value),
        )
    if isinstance(expr, ObjectLiteral):
        return ObjectLiteralExpr(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_expr_to_ast(field.key),
                    value=_expr_to_ast(field.value),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, MapLiteral):
        return MapLiteralExpr(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_expr_to_ast(field.key),
                    value=_expr_to_ast(field.value),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, NewObject):
        return NewObjectExpr(
            source=expr.source,
            type=expr.type,
            type_name=expr.type_name,
            args=tuple(_expr_to_ast(arg) for arg in expr.args),
        )
    return UnsupportedStmt(message=f"unsupported expr: {type(expr).__name__}")


def _keyword_name(expr: Expr) -> str:
    if isinstance(expr, Const) and isinstance(expr.value, str):
        return expr.value
    if isinstance(expr, Var):
        return expr.name
    return "<keyword>"


def _logical_name(name: str) -> str:
    if name.startswith("upvalue_") or (name.startswith("r") and name[1:].isdigit()):
        return name
    if not name:
        return "tmp"
    if not (name[0].isalpha() or name[0] == "_"):
        name = f"tmp_{name}"
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in name)
    return cleaned


def _ssa_index_to_metadata(ssa_index) -> dict[str, list[str]]:
    return {
        name: [definition.ssa_name for definition in definitions]
        for name, definitions in ssa_index.definitions.items()
    }
