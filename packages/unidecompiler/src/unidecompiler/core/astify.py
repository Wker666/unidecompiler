from __future__ import annotations

from dataclasses import fields, is_dataclass

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
    CurrentExceptionRef,
    DeleteStmt,
    ResumeInputExpr,
    UndefinedLiteralExpr,
    ExprStmt,
    ForRangeStmt,
    FunctionDecl,
    GetAttrExpr,
    GetItemExpr,
    GlobalRef,
    GotoStmt,
    OnExceptionGotoStmt,
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
    UnsupportedExpr,
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
    CurrentException,
    Delete,
    ResumeInput,
    UndefinedLiteral,
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
    Placeholder,
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
    if not function.metadata.get("recovery_phi_materialized"):
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
            metadata={
                **function.metadata,
                "ssa_index": _ssa_index_to_metadata(ssa_index),
                "predeclared_names": _predeclared_names(tuple(body)),
            },
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
        metadata={
            **function.metadata,
            "ssa_index": _ssa_index_to_metadata(ssa_index),
            "predeclared_names": _predeclared_names(tuple(body)),
        },
        body=tuple(body),
        nested_functions=tuple(function_to_ast(nested) for nested in function.nested_functions),
    )


def _predeclared_names(statements: tuple[object, ...]) -> tuple[str, ...]:
    """Return names that must be visible before a nested control statement.

    This is a core data-flow fact used by renderers to keep temporaries in the
    enclosing scope.  Computing it here avoids making a backend infer
    liveness or control-flow semantics while printing already-recovered AST.
    Only names assigned inside a control statement and read later in that
    statement's sequence are included.
    """

    result: set[str] = set()

    def names(value: object) -> set[str]:
        if isinstance(value, VarRef):
            return {value.name}
        if value is None or isinstance(value, (str, bytes, int, float, bool)):
            return set()
        if isinstance(value, dict):
            output: set[str] = set()
            for key, item in value.items():
                output.update(names(key))
                output.update(names(item))
            return output
        if isinstance(value, (tuple, list, set, frozenset)):
            output: set[str] = set()
            for item in value:
                output.update(names(item))
            return output
        if is_dataclass(value):
            output: set[str] = set()
            for field in fields(value):
                if field.name in {"source", "type"}:
                    continue
                output.update(names(getattr(value, field.name)))
            return output
        return set()

    def uses(statement: object) -> set[str]:
        if isinstance(statement, AssignStmt):
            output = names(statement.value)
            if isinstance(statement.target, (GetAttrExpr, GetItemExpr)):
                output.update(names(statement.target.obj))
                if isinstance(statement.target, GetItemExpr):
                    output.update(names(statement.target.key))
            return output
        if isinstance(statement, AssignManyStmt):
            return {name for value in statement.values for name in names(value)}
        return names(statement)

    def assigned(statement: object) -> set[str]:
        if isinstance(statement, AssignStmt):
            if isinstance(statement.target, VarRef):
                return {statement.target.name}
            if isinstance(statement.target, (GetAttrExpr, GetItemExpr)):
                return names(statement.target.obj)
            return set()
        if isinstance(statement, AssignManyStmt):
            return {target.name for target in statement.targets}
        if isinstance(statement, (StoreAttrStmt, StoreItemStmt)):
            return names(statement.obj)
        if isinstance(statement, IfStmt):
            return assigned_sequence(statement.then_body) | assigned_sequence(statement.else_body)
        if isinstance(statement, SwitchStmt):
            output = assigned_sequence(statement.default_body)
            for _value, body in statement.cases:
                output.update(assigned_sequence(body))
            return output
        if isinstance(statement, (WhileStmt, ForEachStmt, ForRangeStmt)):
            output = assigned_sequence(statement.body)
            if isinstance(statement, (ForEachStmt, ForRangeStmt)):
                output.add(statement.target.name)
            return output
        if isinstance(statement, TryStmt):
            output = assigned_sequence(statement.body)
            for handler in statement.handlers:
                output.update(assigned_sequence(handler.body))
                if handler.binding is not None:
                    output.add(handler.binding.name)
            return output
        return set()

    def children(statement: object) -> tuple[tuple[object, ...], ...]:
        if isinstance(statement, IfStmt):
            return (statement.then_body, statement.else_body)
        if isinstance(statement, SwitchStmt):
            return tuple(body for _value, body in statement.cases) + (statement.default_body,)
        if isinstance(statement, (WhileStmt, ForEachStmt, ForRangeStmt)):
            return (statement.body,)
        if isinstance(statement, TryStmt):
            return (statement.body, *(handler.body for handler in statement.handlers))
        return ()

    def assigned_sequence(sequence: tuple[object, ...]) -> set[str]:
        output: set[str] = set()
        for statement in sequence:
            output.update(assigned(statement))
        return output

    def visit(sequence: tuple[object, ...], inherited: frozenset[str] = frozenset()) -> None:
        suffix: set[str] = set()
        uses_after: list[set[str]] = [set() for _ in sequence]
        for index in range(len(sequence) - 1, -1, -1):
            uses_after[index] = set(suffix)
            suffix.difference_update(assigned(sequence[index]))
            suffix.update(uses(sequence[index]))
        for index, statement in enumerate(sequence):
            if children(statement):
                protected = inherited | frozenset(uses_after[index])
                result.update(
                    name
                    for name in assigned(statement) & protected
                    if _is_temporary_name(name)
                )
                for child in children(statement):
                    visit(child, protected)

    visit(statements)
    return tuple(sorted(result))


def _is_temporary_name(name: str) -> bool:
    """Match neutral VM temporary naming conventions used by renderers."""

    return (
        (name.startswith("r") and name[1:].isdigit())
        or (name.startswith("t") and name[1:].isdigit())
        or name.startswith("tmp")
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
    if block.exception_edge is not None:
        statements.append(
            OnExceptionGotoStmt(
                source=block.exception_edge.source,
                target=block.exception_edge.target,
            )
        )
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
            source=statement.source or statement.condition.source,
            condition=_expr_to_ast(statement.condition),
            then_body=tuple(_statement_to_ast(inner) for inner in statement.then_body),
            else_body=tuple(_statement_to_ast(inner) for inner in statement.else_body),
        )
    if isinstance(statement, Switch):
        return SwitchStmt(
            source=statement.source or statement.selector.source,
            selector=_expr_to_ast(statement.selector),
            cases=tuple(
                (_expr_to_ast(value), tuple(_statement_to_ast(inner) for inner in body))
                for value, body in statement.cases
            ),
            default_body=tuple(_statement_to_ast(inner) for inner in statement.default_body),
        )
    if isinstance(statement, While):
        return WhileStmt(
            source=statement.source or statement.condition.source,
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
    if isinstance(statement, Delete):
        return DeleteStmt(source=statement.source, target=_expr_to_ast(statement.target))
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
    if isinstance(expr, UndefinedLiteral):
        return UndefinedLiteralExpr(source=expr.source, type=expr.type)
    if isinstance(expr, CurrentException):
        return CurrentExceptionRef(source=expr.source, type=expr.type)
    if isinstance(expr, ResumeInput):
        return ResumeInputExpr(source=expr.source, type=expr.type)
    if isinstance(expr, Placeholder):
        return UnsupportedExpr(
            source=expr.source,
            type=expr.type,
            message="unresolved placeholder",
            detail=f"{expr.label}:{expr.token}",
        )
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
            numeric_domain=expr.numeric_domain,
            bit_width=expr.bit_width,
            overflow_policy=expr.overflow_policy,
        )
    if isinstance(expr, Call):
        keyword_names = tuple(_keyword_name(field.key) for field in expr.keywords)
        if any(name is None for name in keyword_names):
            return UnsupportedExpr(
                source=expr.source,
                type=expr.type,
                message="unsupported keyword call",
                detail="keyword name is not a static string",
            )
        return CallExpr(
            source=expr.source,
            type=expr.type,
            callee=_expr_to_ast(expr.callee),
            args=tuple(_expr_to_ast(arg) for arg in expr.args),
            keywords=tuple(
                (name, _expr_to_ast(field.value))
                for name, field in zip(keyword_names, expr.keywords, strict=True)
                if name is not None
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
            kind=expr.kind,
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
            constructor=None if expr.constructor is None else _expr_to_ast(expr.constructor),
            args=tuple(_expr_to_ast(arg) for arg in expr.args),
        )
    return UnsupportedExpr(
        source=expr.source,
        type=expr.type,
        message="unsupported expression node",
        detail=type(expr).__name__,
    )


def _keyword_name(expr: Expr) -> str | None:
    if isinstance(expr, Const) and isinstance(expr.value, str):
        return expr.value
    return None


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
