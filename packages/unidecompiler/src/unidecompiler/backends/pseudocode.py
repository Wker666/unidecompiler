from __future__ import annotations

from contextvars import ContextVar

from unidecompiler.backends.base import OutputArtifact
from unidecompiler.core.ast import (
    AssignManyStmt,
    ArrayLiteralExpr,
    AssignStmt,
    AstExpr,
    BreakStmt,
    BinaryExpr,
    CallExpr,
    CapturedVarRef,
    CollectionProjectionExpr,
    ConstExpr,
    ExprStmt,
    ForEachStmt,
    ForRangeStmt,
    FunctionDecl,
    GetAttrExpr,
    GetItemExpr,
    GlobalRef,
    GotoStmt,
    IndirectCallExpr,
    IfGotoStmt,
    IfStmt,
    IndirectRefExpr,
    LabelStmt,
    MapLiteralExpr,
    ModuleDecl,
    MultiReturnExpr,
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
    SwitchGotoStmt,
    TableField,
    TableLiteralExpr,
    TryStmt,
    UnaryExpr,
    UnsupportedStmt,
    VarRef,
    YieldStmt,
    ContinueStmt,
    WhileStmt,
)
from unidecompiler.core.astify import module_to_ast
from unidecompiler.core.ir import ModuleIR, SourceRef


_render_function_ordinal: ContextVar[int | None] = ContextVar(
    "render_function_ordinal", default=None
)


class _RenderedLine(str):
    """A renderer-owned line with optional VM-neutral provenance."""

    def __new__(
        cls,
        text: str,
        source: SourceRef | None = None,
        function_ordinal: int | None = None,
    ):
        line = super().__new__(cls, text)
        line.source = source
        line.function_ordinal = function_ordinal
        return line


class GenericPseudocodeBackend:
    id = "pseudocode"
    display_name = "Generic pseudocode"

    def emit(self, module: ModuleIR) -> OutputArtifact:
        ast_module = module_to_ast(module)
        lines = _emit_module(ast_module)
        return OutputArtifact(
            kind="text",
            text="\n".join(lines),
            metadata={
                "frontend": module.metadata.get("frontend", {}),
                "backend": {
                    "id": self.id,
                    "diagnostics": [],
                },
                "source_spans": _source_spans(lines),
            },
        )


def _emit_module(module: ModuleDecl) -> list[str]:
    lines = [f"// module: {module.name}"]
    source = module.metadata.get("frontend", {})
    if source.get("format") and source.get("version"):
        lines.append(f"// input: {source['format']} {source['version']}")

    ordinals = {
        id(function): ordinal
        for ordinal, function in enumerate(_walk_functions(module.functions))
    }
    for function in module.functions:
        lines.extend(_emit_function(function, module.source_language, ordinals))

    return lines


def _emit_function(
    function: FunctionDecl,
    source_language: str,
    ordinals: dict[int, int],
) -> list[str]:
    token = _render_function_ordinal.set(ordinals[id(function)])
    try:
        return _emit_function_body(function, source_language, ordinals)
    finally:
        _render_function_ordinal.reset(token)


def _emit_function_body(
    function: FunctionDecl,
    source_language: str,
    ordinals: dict[int, int],
) -> list[str]:
    params = ", ".join(function.params)
    lines = [f"function {function.name}({params}) {{"]
    annotation_member = function.metadata.get("annotation_member")
    if isinstance(annotation_member, dict):
        descriptor = annotation_member.get("descriptor") or ""
        default = annotation_member.get("default")
        suffix = f", default={default!r}" if default is not None else ""
        lines.append(f"    // metadata: annotation member {function.name}{descriptor}{suffix}")
        lines.append("}")
        return lines
    if function.metadata.get("decompile_status") == "unsupported":
        reason = function.metadata.get("unsupported_reason", "unsupported")
        opcodes = ", ".join(function.metadata.get("unsupported_opcodes", ()))
        lines.append(f"    // unsupported: {reason}")
        if opcodes:
            lines.append(f"    // unsupported opcodes: {opcodes}")
        for raw in function.metadata.get("unsupported_raw", ()):
            lines.append(f"    // raw: {raw}")
        lines.append("}")
        return lines
    if not function.body:
        lines.append("    // body unavailable")
        lines.append("}")
        return lines

    declared: set[str] = set(function.params)
    inline_values: dict[str, AstExpr] = {}
    body_lines, declared = _emit_stmt_sequence(
        function.body,
        source_language=source_language,
        local_names=set(function.metadata.get("local_names", ())),
        declared=declared,
        inline_values=inline_values,
    )
    lines.extend(body_lines)

    for nested in function.nested_functions:
        lines.extend(_indent(_emit_function(nested, source_language, ordinals), "    "))

    lines.append("}")
    return lines


def _emit_stmt_sequence(
    statements: tuple[object, ...],
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    lines: list[str] = []
    for statement in statements:
        statement_lines, declared = _emit_stmt(
            statement,
            source_language=source_language,
            local_names=local_names,
            declared=declared,
            inline_values=inline_values,
        )
        lines.extend(_tag_statement_lines(statement_lines, statement.source))
    return lines, declared


def _tag_statement_lines(lines: list[str], source: SourceRef | None) -> list[str]:
    ordinal = _render_function_ordinal.get()
    return [
        line
        if isinstance(line, _RenderedLine)
        else _RenderedLine(line, source, ordinal)
        for line in lines
    ]


def _emit_stmt(
    statement,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    if isinstance(statement, AssignStmt):
        resolved = _resolve_expr(statement.value, inline_values)
        if not isinstance(statement.target, VarRef):
            return [f"    {_emit_expr(statement.target)} = {_emit_expr(resolved)}"], declared
        if _should_inline_assignment(statement.target.name, local_names, resolved, source_language):
            inline_values[statement.target.name] = resolved
            return [], declared
        inline_values.pop(statement.target.name, None)
        keyword = "let " if statement.target.name not in declared else ""
        declared.add(statement.target.name)
        return [f"    {keyword}{statement.target.name} = {_emit_expr(resolved)}"], declared
    if isinstance(statement, AssignManyStmt):
        targets = ", ".join(target.name for target in statement.targets)
        values = ", ".join(_emit_expr(_resolve_expr(value, inline_values)) for value in statement.values)
        keyword = "let " if any(target.name not in declared for target in statement.targets) else ""
        for target in statement.targets:
            inline_values.pop(target.name, None)
        declared.update(target.name for target in statement.targets)
        return [f"    {keyword}{targets} = {values}"], declared
    if isinstance(statement, StoreAttrStmt):
        return [
            f"    {_emit_expr(_resolve_expr(statement.obj, inline_values))}.{statement.attr} = "
            f"{_emit_expr(_resolve_expr(statement.value, inline_values))}"
        ], declared
    if isinstance(statement, StoreItemStmt):
        resolved_obj = _resolve_expr(statement.obj, inline_values)
        if isinstance(statement.obj, VarRef) and _update_inline_literal_item(
            statement.obj.name,
            resolved_obj,
            _resolve_expr(statement.key, inline_values),
            _resolve_expr(statement.value, inline_values),
            inline_values,
        ):
            return [], declared
        return [
            f"    {_emit_expr(resolved_obj)}"
            f"[{_emit_expr(_resolve_expr(statement.key, inline_values))}] = "
            f"{_emit_expr(_resolve_expr(statement.value, inline_values))}"
        ], declared
    if isinstance(statement, ExprStmt):
        return [f"    {_emit_expr(_resolve_expr(statement.value, inline_values))}"], declared
    if isinstance(statement, ReturnStmt):
        if statement.values:
            return [
                f"    return {', '.join(_emit_expr(_resolve_expr(value, inline_values)) for value in statement.values)}"
            ], declared
        return ["    return"], declared
    if isinstance(statement, BreakStmt):
        return ["    break"], declared
    if isinstance(statement, ContinueStmt):
        return ["    continue"], declared
    if isinstance(statement, LabelStmt):
        return [f"  {statement.name}:"], declared
    if isinstance(statement, GotoStmt):
        return [f"    goto {statement.target}"], declared
    if isinstance(statement, IfGotoStmt):
        condition = _emit_expr(_resolve_expr(statement.condition, inline_values))
        return [f"    if ({condition}) goto {statement.true_target} else goto {statement.false_target}"], declared
    if isinstance(statement, SwitchGotoStmt):
        selector = _emit_expr(_resolve_expr(statement.selector, inline_values))
        lines = [f"    switch ({selector}) {{"]
        for value, target in statement.cases:
            lines.append(f"      case {_emit_expr(_resolve_expr(value, inline_values))}: goto {target}")
        lines.append(f"      default: goto {statement.default_target}")
        lines.append("    }")
        return lines, declared
    if isinstance(statement, IfStmt):
        return _emit_if(statement, source_language, local_names, declared, inline_values)
    if isinstance(statement, SwitchStmt):
        return _emit_switch(statement, source_language, local_names, declared, inline_values)
    if isinstance(statement, WhileStmt):
        return _emit_while(statement, source_language, local_names, declared, inline_values)
    if isinstance(statement, ForEachStmt):
        return _emit_for_each(statement, source_language, local_names, declared, inline_values)
    if isinstance(statement, ForRangeStmt):
        return _emit_for_range(statement, source_language, local_names, declared, inline_values)
    if isinstance(statement, UnsupportedStmt):
        lines = [f"    // unsupported: {statement.message}"]
        lines.extend(f"    // raw: {raw}" for raw in statement.raw)
        return lines, declared
    if isinstance(statement, RaiseStmt):
        value = _emit_expr(_resolve_expr(statement.value, inline_values))
        if statement.cause is None:
            return [f"    raise {value}"], declared
        cause = _emit_expr(_resolve_expr(statement.cause, inline_values))
        return [f"    raise {value} from {cause}"], declared
    if isinstance(statement, ReraiseStmt):
        return ["    raise"], declared
    if isinstance(statement, YieldStmt):
        return [f"    yield {_emit_expr(_resolve_expr(statement.value, inline_values))}"], declared
    if isinstance(statement, TryStmt):
        return _emit_try(statement, source_language, local_names, declared, inline_values)
    return [f"    // unsupported statement: {type(statement).__name__}"], declared


def _emit_try(
    statement: TryStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    lines = ["    try {"]
    body_lines, _ = _emit_stmt_sequence(
        statement.body,
        source_language,
        local_names,
        declared.copy(),
        inline_values.copy(),
    )
    lines.extend(_indent(body_lines, "    "))
    lines.append("    }")
    for handler in statement.handlers:
        type_text = _emit_expr(_resolve_expr(handler.exception_type, inline_values))
        binding = f" {handler.binding.name}" if handler.binding is not None else ""
        lines[-1] += f" catch ({type_text}{binding}) {{"
        handler_lines, _ = _emit_stmt_sequence(
            handler.body,
            source_language,
            local_names,
            declared.copy(),
            inline_values.copy(),
        )
        lines.extend(_indent(handler_lines, "    "))
        lines.append("    }")
    return lines, declared


def _emit_if(
    statement: IfStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    # A value assigned on both paths is a value at the enclosing scope (a
    # generic IR phi materialization), not two block-local declarations.
    # Declare it before rendering either path so pseudocode preserves that
    # lifetime even for renderers with lexical ``let`` bindings.
    shared_assignments = _shared_branch_assignments(statement)
    prelude: list[str] = []
    for name in shared_assignments:
        if name not in declared:
            declared.add(name)
            inline_values.pop(name, None)
            prelude.append(f"    let {name} = null")

    lines = [*prelude, f"    if ({_emit_expr(statement.condition)}) {{"]
    then_declared = declared.copy()
    then_inline = inline_values.copy()
    then_lines, _ = _emit_stmt_sequence(statement.then_body, source_language, local_names, then_declared, then_inline)
    lines.extend(_indent(then_lines, "    "))
    if not statement.else_body:
        lines.append("    }")
        return lines, declared
    lines.append("    } else {")
    else_declared = declared.copy()
    else_inline = inline_values.copy()
    else_lines, _ = _emit_stmt_sequence(statement.else_body, source_language, local_names, else_declared, else_inline)
    lines.extend(_indent(else_lines, "    "))
    lines.append("    }")
    return lines, declared


def _shared_branch_assignments(statement: IfStmt) -> tuple[str, ...]:
    """Return variable names definitely assigned by both branches.

    This deliberately only considers direct assignments.  More complex path
    analysis belongs to core recovery; this is lexical materialization of a
    generic IR value that core has already proven to exist after the branch.
    """

    if not statement.then_body or not statement.else_body:
        return ()
    then_names = {
        item.target.name
        for item in statement.then_body
        if isinstance(item, AssignStmt) and isinstance(item.target, VarRef)
    }
    else_names = {
        item.target.name
        for item in statement.else_body
        if isinstance(item, AssignStmt) and isinstance(item.target, VarRef)
    }
    return tuple(sorted(then_names & else_names))


def _emit_switch(
    statement: SwitchStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    selector = _emit_expr(_resolve_expr(statement.selector, inline_values))
    lines = [f"    switch ({selector}) {{"]
    for value, body in statement.cases:
        lines.append(f"      case {_emit_expr(_resolve_expr(value, inline_values))}:")
        case_lines, _ = _emit_stmt_sequence(
            body,
            source_language,
            local_names,
            declared.copy(),
            inline_values.copy(),
        )
        lines.extend(_indent(case_lines, "      "))
        if not _switch_body_terminates(body):
            lines.append("        break")
    lines.append("      default:")
    default_lines, _ = _emit_stmt_sequence(
        statement.default_body,
        source_language,
        local_names,
        declared.copy(),
        inline_values.copy(),
    )
    lines.extend(_indent(default_lines, "      "))
    if not _switch_body_terminates(statement.default_body):
        lines.append("        break")
    lines.append("    }")
    return lines, declared


def _switch_body_terminates(body: tuple[object, ...]) -> bool:
    return bool(body) and isinstance(body[-1], (ContinueStmt, ReturnStmt, RaiseStmt, ReraiseStmt))


def _emit_while(
    statement: WhileStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    lines = [f"    while ({_emit_expr(statement.condition)}) {{"]
    body_declared = declared.copy()
    body_inline = inline_values.copy()
    body_lines, _ = _emit_stmt_sequence(statement.body, source_language, local_names, body_declared, body_inline)
    lines.extend(_indent(body_lines, "    "))
    lines.append("    }")
    return lines, declared


def _emit_for_range(
    statement: ForRangeStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    lines = [
        f"    for {statement.target.name} in range("
        f"{_emit_expr(statement.start)}, {_emit_expr(statement.stop)}, {_emit_expr(statement.step)}"
        ") {"
    ]
    body_declared = declared.copy()
    body_inline = inline_values.copy()
    body_lines, _ = _emit_stmt_sequence(statement.body, source_language, local_names, body_declared, body_inline)
    lines.extend(_indent(body_lines, "    "))
    lines.append("    }")
    return lines, declared


def _emit_for_each(
    statement: ForEachStmt,
    source_language: str,
    local_names: set[str],
    declared: set[str],
    inline_values: dict[str, AstExpr],
) -> tuple[list[str], set[str]]:
    lines = [
        f"    for {statement.target.name} in "
        f"{_emit_expr(_resolve_expr(statement.iterable, inline_values))} {{"
    ]
    body_declared = declared.copy()
    body_inline = inline_values.copy()
    body_lines, _ = _emit_stmt_sequence(statement.body, source_language, local_names, body_declared, body_inline)
    lines.extend(_indent(body_lines, "    "))
    lines.append("    }")
    return lines, declared


def _indent(lines: list[str], prefix: str) -> list[str]:
    return [
        _RenderedLine(f"{prefix}{line}", line.source, line.function_ordinal)
        if isinstance(line, _RenderedLine)
        else f"{prefix}{line}"
        for line in lines
    ]


def _walk_functions(functions: tuple[FunctionDecl, ...]):
    for function in functions:
        yield function
        yield from _walk_functions(function.nested_functions)


def _source_spans(lines: list[str]) -> tuple[dict, ...]:
    spans: list[dict] = []
    offset = 0
    for line in lines:
        end = offset + len(line)
        if isinstance(line, _RenderedLine) and line.source is not None:
            spans.append(
                {
                    "start": offset,
                    "end": end,
                    "source": line.source,
                    "function_ordinal": line.function_ordinal,
                }
            )
        offset = end + 1
    return tuple(spans)


def _emit_expr(expr: AstExpr) -> str:
    if isinstance(expr, GlobalRef):
        return expr.name
    if isinstance(expr, CapturedVarRef):
        return expr.name
    if isinstance(expr, PhiExpr):
        parts = ", ".join(f"{pred}: {_emit_expr(value)}" for pred, value in expr.incoming)
        return f"phi({parts})"
    if isinstance(expr, VarRef):
        return expr.name
    if isinstance(expr, ConstExpr):
        if expr.value is True:
            return "true"
        if expr.value is False:
            return "false"
        if expr.value is None:
            return "null"
        return repr(expr.value)
    if isinstance(expr, BinaryExpr):
        return f"{_emit_binary_operand(expr.left, expr.op, side='left')} {expr.op} {_emit_binary_operand(expr.right, expr.op, side='right')}"
    if isinstance(expr, UnaryExpr):
        return f"{expr.op}{_emit_unary_operand(expr.value)}"
    if isinstance(expr, GetItemExpr):
        return f"{_emit_access_base(expr.obj)}[{_emit_expr(expr.key)}]"
    if isinstance(expr, IndirectCallExpr):
        return f"indirect<{expr.signature}>[{_emit_expr(expr.selector)}]"
    if isinstance(expr, IndirectRefExpr):
        return _emit_expr(expr.target)
    if isinstance(expr, GetAttrExpr):
        return f"{_emit_access_base(expr.obj)}.{expr.attr}"
    if isinstance(expr, CallExpr):
        args = [_emit_expr(arg) for arg in expr.args]
        args.extend(f"{name}={_emit_expr(value)}" for name, value in expr.keywords)
        return f"{_emit_call_base(expr.callee)}({', '.join(args)})"
    if isinstance(expr, MultiReturnExpr):
        return f"multi_return({_emit_expr(expr.value)})"
    if isinstance(expr, TableLiteralExpr):
        parts = [_emit_expr(item) for item in expr.array_items]
        for field in expr.fields:
            if isinstance(field.key, ConstExpr) and isinstance(field.key.value, str):
                parts.append(f"{field.key.value} = {_emit_expr(field.value)}")
            else:
                parts.append(f"[{_emit_expr(field.key)}] = {_emit_expr(field.value)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(expr, ArrayLiteralExpr):
        return "[" + ", ".join(_emit_expr(item) for item in expr.items) + "]"
    if isinstance(expr, SetLiteralExpr):
        return "{" + ", ".join(_emit_expr(item) for item in expr.items) + "}"
    if isinstance(expr, CollectionProjectionExpr):
        opener, closer = ("[", "]") if expr.kind == "list" else ("{", "}")
        return f"{opener}{_emit_expr(expr.value)} for {_emit_expr(expr.target)} in {_emit_expr(expr.iterable)}{closer}"
    if isinstance(expr, ObjectLiteralExpr):
        parts = []
        for field in expr.fields:
            if isinstance(field.key, ConstExpr) and isinstance(field.key.value, str):
                parts.append(f"{field.key.value}: {_emit_expr(field.value)}")
            else:
                parts.append(f"[{_emit_expr(field.key)}]: {_emit_expr(field.value)}")
        return "{" + ", ".join(parts) + "}"
    if isinstance(expr, MapLiteralExpr):
        parts = [
            f"{_emit_map_key(field.key)}: {_emit_expr(field.value)}"
            for field in expr.fields
        ]
        return "{" + ", ".join(parts) + "}"
    if isinstance(expr, NewObjectExpr):
        args = ", ".join(_emit_expr(arg) for arg in expr.args)
        return f"new {expr.type_name}({args})"
    return "<expr>"


def _emit_map_key(expr: AstExpr) -> str:
    if isinstance(expr, ConstExpr) and isinstance(expr.value, str):
        return '"' + expr.value.replace('"', '\\"') + '"'
    return _emit_expr(expr)


def _emit_access_base(expr: AstExpr) -> str:
    rendered = _emit_expr(expr)
    if isinstance(expr, (BinaryExpr, UnaryExpr, PhiExpr, CollectionProjectionExpr)):
        return f"({rendered})"
    return rendered


def _emit_call_base(expr: AstExpr) -> str:
    rendered = _emit_expr(expr)
    if isinstance(expr, (BinaryExpr, UnaryExpr, PhiExpr, CollectionProjectionExpr)):
        return f"({rendered})"
    return rendered


def _emit_binary_operand(expr: AstExpr, parent_op: str, *, side: str) -> str:
    rendered = _emit_expr(expr)
    if not isinstance(expr, BinaryExpr):
        return rendered
    child_precedence = _binary_precedence(expr.op)
    parent_precedence = _binary_precedence(parent_op)
    if _is_comparison_op(parent_op) and _is_bitwise_op(expr.op):
        return f"({rendered})"
    if child_precedence < parent_precedence:
        return f"({rendered})"
    if side == "right" and child_precedence == parent_precedence and parent_op not in _ASSOCIATIVE_OPS:
        return f"({rendered})"
    if child_precedence == parent_precedence and _is_comparison_op(parent_op):
        return f"({rendered})"
    if _needs_bitwise_grouping(parent_op, expr.op):
        return f"({rendered})"
    return rendered


_ASSOCIATIVE_OPS = {"+", "*", "&", "|", "^", "and", "or"}


def _emit_unary_operand(expr: AstExpr) -> str:
    rendered = _emit_expr(expr)
    if isinstance(expr, BinaryExpr):
        return f"({rendered})"
    return rendered


def _is_comparison_op(op: str) -> bool:
    return op in {"in", "is", "is not", "==", "!=", "<", "<=", ">", ">="}


def _needs_bitwise_grouping(parent_op: str, child_op: str) -> bool:
    shift_ops = {"<<", ">>", ">>>"}
    arithmetic_ops = {"+", "-", "*", "/", "//", "%"}
    if not _is_bitwise_op(parent_op) or child_op == parent_op:
        return False
    return child_op in (_BITWISE_OPS | shift_ops | arithmetic_ops)


_BITWISE_OPS = {"|", "^", "&"}


def _is_bitwise_op(op: str) -> bool:
    return op in _BITWISE_OPS


def _binary_precedence(op: str) -> int:
    return {
        "or": 1,
        "and": 2,
        "in": 3,
        "is": 3,
        "is not": 3,
        "==": 3,
        "!=": 3,
        "<": 3,
        "<=": 3,
        ">": 3,
        ">=": 3,
        "|": 4,
        "^": 5,
        "~": 5,
        "&": 6,
        "<<": 7,
        ">>": 7,
        ">>>": 7,
        "+": 8,
        "-": 8,
        "*": 9,
        "/": 9,
        "//": 9,
        "%": 9,
    }.get(op, 10)


def _resolve_expr(expr: AstExpr, inline_values: dict[str, AstExpr]) -> AstExpr:
    if isinstance(expr, VarRef) and expr.name in inline_values:
        return inline_values[expr.name]
    if isinstance(expr, BinaryExpr):
        return BinaryExpr(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            left=_resolve_expr(expr.left, inline_values),
            right=_resolve_expr(expr.right, inline_values),
            semantics=expr.semantics,
        )
    if isinstance(expr, UnaryExpr):
        return UnaryExpr(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            value=_resolve_expr(expr.value, inline_values),
        )
    if isinstance(expr, GetItemExpr):
        return GetItemExpr(
            source=expr.source,
            type=expr.type,
            obj=_resolve_expr(expr.obj, inline_values),
            key=_resolve_expr(expr.key, inline_values),
        )
    if isinstance(expr, IndirectCallExpr):
        return IndirectCallExpr(
            source=expr.source,
            type=expr.type,
            selector=_resolve_expr(expr.selector, inline_values),
            signature=expr.signature,
        )
    if isinstance(expr, IndirectRefExpr):
        return IndirectRefExpr(
            source=expr.source,
            type=expr.type,
            target=_resolve_expr(expr.target, inline_values),
        )
    if isinstance(expr, GetAttrExpr):
        return GetAttrExpr(
            source=expr.source,
            type=expr.type,
            obj=_resolve_expr(expr.obj, inline_values),
            attr=expr.attr,
        )
    if isinstance(expr, CallExpr):
        return CallExpr(
            source=expr.source,
            type=expr.type,
            callee=_resolve_expr(expr.callee, inline_values),
            args=tuple(_resolve_expr(arg, inline_values) for arg in expr.args),
            keywords=tuple((name, _resolve_expr(value, inline_values)) for name, value in expr.keywords),
            returns=expr.returns,
        )
    if isinstance(expr, MultiReturnExpr):
        return MultiReturnExpr(
            source=expr.source,
            type=expr.type,
            value=_resolve_expr(expr.value, inline_values),
        )
    if isinstance(expr, PhiExpr):
        return PhiExpr(
            source=expr.source,
            type=expr.type,
            incoming=tuple(
                (pred, _resolve_expr(value, inline_values))
                for pred, value in expr.incoming
            ),
        )
    if isinstance(expr, TableLiteralExpr):
        return TableLiteralExpr(
            source=expr.source,
            type=expr.type,
            array_items=tuple(_resolve_expr(item, inline_values) for item in expr.array_items),
            fields=tuple(
                TableField(
                    key=_resolve_expr(field.key, inline_values),
                    value=_resolve_expr(field.value, inline_values),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, ArrayLiteralExpr):
        return ArrayLiteralExpr(
            source=expr.source,
            type=expr.type,
            items=tuple(_resolve_expr(item, inline_values) for item in expr.items),
        )
    if isinstance(expr, SetLiteralExpr):
        return SetLiteralExpr(
            source=expr.source,
            type=expr.type,
            items=tuple(_resolve_expr(item, inline_values) for item in expr.items),
        )
    if isinstance(expr, CollectionProjectionExpr):
        return CollectionProjectionExpr(
            source=expr.source,
            type=expr.type,
            kind=expr.kind,
            target=expr.target,
            iterable=_resolve_expr(expr.iterable, inline_values),
            value=_resolve_expr(expr.value, inline_values),
        )
    if isinstance(expr, ObjectLiteralExpr):
        return ObjectLiteralExpr(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_resolve_expr(field.key, inline_values),
                    value=_resolve_expr(field.value, inline_values),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, MapLiteralExpr):
        return MapLiteralExpr(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_resolve_expr(field.key, inline_values),
                    value=_resolve_expr(field.value, inline_values),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, NewObjectExpr):
        return NewObjectExpr(
            source=expr.source,
            type=expr.type,
            type_name=expr.type_name,
            args=tuple(_resolve_expr(arg, inline_values) for arg in expr.args),
        )
    return expr


def _update_inline_literal_item(
    name: str,
    obj: AstExpr,
    key: AstExpr,
    value: AstExpr,
    inline_values: dict[str, AstExpr],
) -> bool:
    if isinstance(obj, MapLiteralExpr):
        inline_values[name] = MapLiteralExpr(
            source=obj.source,
            type=obj.type,
            fields=(*obj.fields, TableField(key=key, value=value)),
        )
        return True
    if isinstance(obj, ObjectLiteralExpr):
        inline_values[name] = ObjectLiteralExpr(
            source=obj.source,
            type=obj.type,
            fields=(*obj.fields, TableField(key=key, value=value)),
        )
        return True
    if isinstance(obj, TableLiteralExpr):
        inline_values[name] = TableLiteralExpr(
            source=obj.source,
            type=obj.type,
            array_items=obj.array_items,
            fields=(*obj.fields, TableField(key=key, value=value)),
        )
        return True
    return False


def _should_inline_assignment(
    target_name: str,
    local_names: set[str],
    value: AstExpr,
    source_language: str,
) -> bool:
    if target_name in local_names:
        return False
    if target_name.startswith("order_tmp_") or target_name.startswith("tmp_value_"):
        return False
    if source_language != "lua":
        return (
            target_name.startswith("r")
            and target_name[1:].isdigit()
            or target_name.startswith("t")
            and target_name[1:].isdigit()
            or target_name.startswith("tmp")
        )
    return _is_safe_inline_expr(value)


def _is_safe_inline_expr(expr: AstExpr) -> bool:
    if isinstance(expr, ConstExpr):
        return True
    if isinstance(expr, UnaryExpr):
        return _is_safe_inline_expr(expr.value)
    if isinstance(expr, BinaryExpr):
        return _is_safe_inline_expr(expr.left) and _is_safe_inline_expr(expr.right)
    if isinstance(expr, TableLiteralExpr):
        return all(_is_safe_inline_expr(item) for item in expr.array_items) and all(
            _is_safe_inline_expr(field.key) and _is_safe_inline_expr(field.value)
            for field in expr.fields
        )
    if isinstance(expr, ArrayLiteralExpr):
        return all(_is_safe_inline_expr(item) for item in expr.items)
    if isinstance(expr, SetLiteralExpr):
        return all(_is_safe_inline_expr(item) for item in expr.items)
    if isinstance(expr, ObjectLiteralExpr):
        return all(_is_safe_inline_expr(field.key) and _is_safe_inline_expr(field.value) for field in expr.fields)
    if isinstance(expr, MapLiteralExpr):
        return all(_is_safe_inline_expr(field.key) and _is_safe_inline_expr(field.value) for field in expr.fields)
    return False
