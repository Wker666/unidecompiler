from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import (
    AssignMany,
    ArrayLiteral,
    Assign,
    BasicBlock,
    BinaryOp,
    Branch,
    Break,
    Call,
    Const,
    Continue,
    Expr,
    ExprStmt,
    ForEach,
    ForRange,
    FunctionIR,
    GetAttr,
    GetItem,
    If,
    MapLiteral,
    MultiBranch,
    MultiReturn,
    NewObject,
    ObjectLiteral,
    Phi,
    Raise,
    Reraise,
    UnaryOp,
    Return,
    StoreAttr,
    StoreItem,
    TableField,
    TableLiteral,
    ExceptHandler,
    Try,
    Unsupported,
    Var,
    While,
    Yield,
)


@dataclass(frozen=True)
class Definition:
    name: str
    version: int

    @property
    def ssa_name(self) -> str:
        return f"{self.name}_{self.version}"


@dataclass(frozen=True)
class SSAIndex:
    definitions: dict[str, tuple[Definition, ...]]


@dataclass(frozen=True)
class SSAConversion:
    function: FunctionIR
    diagnostics: tuple[str, ...] = ()


@dataclass(frozen=True)
class PhiPlacement:
    block_id: str
    variable: str
    incoming: tuple[tuple[str, Var], ...]


def index_assignments(function: FunctionIR) -> SSAIndex:
    """Build a conservative definition index.

    This is not full SSA conversion yet. It is the first value-model seam:
    frontends and tests can ask "how many times was this logical value defined"
    without depending on a VM-specific register model.
    """

    counters: dict[str, int] = {}
    definitions: dict[str, list[Definition]] = {}

    for block in function.blocks:
        for statement in block.statements:
            for name in _assigned_names(statement):
                version = counters.get(name, 0) + 1
                counters[name] = version
                definitions.setdefault(name, []).append(
                    Definition(name=name, version=version)
                )

    for nested in function.nested_functions:
        for name, nested_definitions in index_assignments(nested).definitions.items():
            definitions.setdefault(name, []).extend(nested_definitions)

    return SSAIndex(
        definitions={
            name: tuple(name_definitions)
            for name, name_definitions in definitions.items()
        }
    )


def insert_phi_nodes(function: FunctionIR) -> FunctionIR:
    cfg = build_cfg(function)
    placements = _phi_placements(function)
    if not placements:
        return function

    updated_blocks: list[BasicBlock] = []
    for block in function.blocks:
        block_placements = [placement for placement in placements if placement.block_id == block.id]
        if not block_placements:
            updated_blocks.append(block)
            continue
        phi_statements = [
            Assign(
                target=Var(name=placement.variable),
                value=Phi(incoming=placement.incoming),
            )
            for placement in block_placements
        ]
        updated_blocks.append(
            BasicBlock(
                id=block.id,
                statements=tuple(phi_statements + list(block.statements)),
                terminator=block.terminator,
            )
        )
    return FunctionIR(
        name=function.name,
        params=function.params,
        blocks=tuple(updated_blocks),
        nested_functions=function.nested_functions,
        source=function.source,
        recovery_kind=function.recovery_kind,
        metadata={**function.metadata, "ssa_phi_blocks": _phi_metadata(placements)},
    )


def convert_straight_line_to_ssa(function: FunctionIR) -> SSAConversion:
    """Rename a single basic block into SSA form.

    Full SSA requires dominators and phi placement. This intentionally refuses
    branching CFGs instead of producing invalid value names.
    """

    if len(function.blocks) != 1:
        return SSAConversion(
            function=function,
            diagnostics=("full SSA requires CFG dominators and phi placement",),
        )

    block = function.blocks[0]
    if block.terminator is not None and not isinstance(block.terminator, Return):
        return SSAConversion(
            function=function,
            diagnostics=("full SSA requires CFG dominators and phi placement",),
        )

    versions = {param: 0 for param in function.params}
    current = {param: f"{param}_0" for param in function.params}
    statements = []

    for statement in block.statements:
        rewritten = _rewrite_statement(statement, current, versions)
        statements.append(rewritten)

    terminator = block.terminator
    if isinstance(terminator, Return):
        terminator = Return(
            source=terminator.source,
            values=tuple(_rewrite_expr(value, current) for value in terminator.values),
        )

    return SSAConversion(
        function=FunctionIR(
            name=function.name,
            params=tuple(current.get(param, f"{param}_0") for param in function.params),
            blocks=(
                BasicBlock(
                    id=block.id,
                    statements=tuple(statements),
                    terminator=terminator,
                ),
            ),
            nested_functions=tuple(
                convert_straight_line_to_ssa(nested).function
                for nested in function.nested_functions
            ),
            source=function.source,
            recovery_kind=function.recovery_kind,
            metadata={**function.metadata, "ssa_status": "straight-line"},
        )
    )


def _rewrite_statement(
    statement,
    current: dict[str, str],
    versions: dict[str, int] | None = None,
):
    if isinstance(statement, Assign):
        value = _rewrite_expr(statement.value, current)
        target = statement.target
        if versions is not None and isinstance(statement.target, Var):
            original_name = statement.target.name
            next_version = versions.get(original_name, -1) + 1
            versions[original_name] = next_version
            ssa_target = f"{original_name}_{next_version}"
            current[original_name] = ssa_target
            target = Var(name=ssa_target, source=statement.target.source)
        return Assign(
            source=statement.source,
            target=target,
            value=value,
        )
    if isinstance(statement, AssignMany):
        values = tuple(_rewrite_expr(value, current) for value in statement.values)
        targets = statement.targets
        if versions is not None:
            renamed_targets = []
            for target in statement.targets:
                original_name = target.name
                next_version = versions.get(original_name, -1) + 1
                versions[original_name] = next_version
                ssa_target = f"{original_name}_{next_version}"
                current[original_name] = ssa_target
                renamed_targets.append(Var(name=ssa_target, source=target.source))
            targets = tuple(renamed_targets)
        return AssignMany(source=statement.source, targets=targets, values=values)
    if isinstance(statement, StoreItem):
        return StoreItem(
            source=statement.source,
            obj=_rewrite_expr(statement.obj, current),
            key=_rewrite_expr(statement.key, current),
            value=_rewrite_expr(statement.value, current),
        )
    if isinstance(statement, StoreAttr):
        return StoreAttr(
            source=statement.source,
            obj=_rewrite_expr(statement.obj, current),
            attr=statement.attr,
            value=_rewrite_expr(statement.value, current),
        )
    if isinstance(statement, ExprStmt):
        return ExprStmt(source=statement.source, value=_rewrite_expr(statement.value, current))
    if isinstance(statement, If):
        then_current = current.copy()
        else_current = current.copy()
        then_versions = versions.copy() if versions is not None else None
        else_versions = versions.copy() if versions is not None else None
        return If(
            source=statement.source,
            condition=_rewrite_expr(statement.condition, current),
            then_body=_rewrite_statement_sequence(statement.then_body, then_current, then_versions),
            else_body=_rewrite_statement_sequence(statement.else_body, else_current, else_versions),
        )
    if isinstance(statement, While):
        body_current = current.copy()
        body_versions = versions.copy() if versions is not None else None
        return While(
            source=statement.source,
            condition=_rewrite_expr(statement.condition, current),
            body=_rewrite_statement_sequence(statement.body, body_current, body_versions),
        )
    if isinstance(statement, ForEach):
        body_current = current.copy()
        body_versions = versions.copy() if versions is not None else None
        return ForEach(
            source=statement.source,
            target=statement.target,
            iterable=_rewrite_expr(statement.iterable, current),
            body=_rewrite_statement_sequence(statement.body, body_current, body_versions),
        )
    if isinstance(statement, ForRange):
        body_current = current.copy()
        body_versions = versions.copy() if versions is not None else None
        return ForRange(
            source=statement.source,
            target=statement.target,
            start=_rewrite_expr(statement.start, current),
            stop=_rewrite_expr(statement.stop, current),
            step=_rewrite_expr(statement.step, current),
            body=_rewrite_statement_sequence(statement.body, body_current, body_versions),
        )
    if isinstance(statement, Try):
        body_current = current.copy()
        body_versions = versions.copy() if versions is not None else None
        return Try(
            source=statement.source,
            body=_rewrite_statement_sequence(statement.body, body_current, body_versions),
            handlers=tuple(
                ExceptHandler(
                    exception_type=_rewrite_expr(handler.exception_type, current),
                    binding=handler.binding,
                    body=_rewrite_statement_sequence(
                        handler.body,
                        current.copy(),
                        versions.copy() if versions is not None else None,
                    ),
                )
                for handler in statement.handlers
            ),
        )
    if isinstance(statement, (Break, Continue)):
        return statement
    if isinstance(statement, Unsupported):
        return statement
    if isinstance(statement, Raise):
        return Raise(
            source=statement.source,
            value=_rewrite_expr(statement.value, current),
            cause=_rewrite_expr(statement.cause, current) if statement.cause is not None else None,
        )
    if isinstance(statement, Reraise):
        return statement
    if isinstance(statement, Yield):
        return Yield(source=statement.source, value=_rewrite_expr(statement.value, current))
    if isinstance(statement, Return):
        return Return(
            source=statement.source,
            values=tuple(_rewrite_expr(value, current) for value in statement.values),
        )
    return statement


def _rewrite_statement_sequence(
    statements: tuple,
    current: dict[str, str],
    versions: dict[str, int] | None,
) -> tuple:
    return tuple(_rewrite_statement(statement, current, versions) for statement in statements)


def _rewrite_expr(expr: Expr, current: dict[str, str]) -> Expr:
    if isinstance(expr, Var):
        return Var(
            source=expr.source,
            type=expr.type,
            name=current.get(expr.name, expr.name),
        )
    if isinstance(expr, (Const,)):
        return expr
    if isinstance(expr, BinaryOp):
        return BinaryOp(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            left=_rewrite_expr(expr.left, current),
            right=_rewrite_expr(expr.right, current),
            semantics=expr.semantics,
        )
    if isinstance(expr, UnaryOp):
        return UnaryOp(
            source=expr.source,
            type=expr.type,
            op=expr.op,
            value=_rewrite_expr(expr.value, current),
        )
    if isinstance(expr, GetItem):
        return GetItem(
            source=expr.source,
            type=expr.type,
            obj=_rewrite_expr(expr.obj, current),
            key=_rewrite_expr(expr.key, current),
        )
    if isinstance(expr, GetAttr):
        return GetAttr(
            source=expr.source,
            type=expr.type,
            obj=_rewrite_expr(expr.obj, current),
            attr=expr.attr,
        )
    if isinstance(expr, Call):
        return Call(
            source=expr.source,
            type=expr.type,
            callee=_rewrite_expr(expr.callee, current),
            args=tuple(_rewrite_expr(arg, current) for arg in expr.args),
            returns=expr.returns,
        )
    if isinstance(expr, MultiReturn):
        return MultiReturn(
            source=expr.source,
            type=expr.type,
            value=_rewrite_expr(expr.value, current),
        )
    if isinstance(expr, TableLiteral):
        return TableLiteral(
            source=expr.source,
            type=expr.type,
            array_items=tuple(_rewrite_expr(item, current) for item in expr.array_items),
            fields=tuple(
                TableField(
                    key=_rewrite_expr(field.key, current),
                    value=_rewrite_expr(field.value, current),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, ArrayLiteral):
        return ArrayLiteral(
            source=expr.source,
            type=expr.type,
            items=tuple(_rewrite_expr(item, current) for item in expr.items),
        )
    if isinstance(expr, ObjectLiteral):
        return ObjectLiteral(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_rewrite_expr(field.key, current),
                    value=_rewrite_expr(field.value, current),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, MapLiteral):
        return MapLiteral(
            source=expr.source,
            type=expr.type,
            fields=tuple(
                TableField(
                    key=_rewrite_expr(field.key, current),
                    value=_rewrite_expr(field.value, current),
                )
                for field in expr.fields
            ),
        )
    if isinstance(expr, NewObject):
        return NewObject(
            source=expr.source,
            type=expr.type,
            type_name=expr.type_name,
            args=tuple(_rewrite_expr(arg, current) for arg in expr.args),
        )
    if isinstance(expr, Phi):
        return Phi(
            source=expr.source,
            type=expr.type,
            incoming=tuple(
                (pred, _rewrite_expr(value, current))
                for pred, value in expr.incoming
            ),
        )
    return expr


def _assigned_names(statement) -> tuple[str, ...]:
    if isinstance(statement, Assign) and isinstance(statement.target, Var):
        return (statement.target.name,)
    if isinstance(statement, AssignMany):
        return tuple(target.name for target in statement.targets)
    if isinstance(statement, If):
        return tuple(
            name
            for inner in (*statement.then_body, *statement.else_body)
            for name in _assigned_names(inner)
        )
    if isinstance(statement, While):
        return tuple(name for inner in statement.body for name in _assigned_names(inner))
    if isinstance(statement, (ForEach, ForRange)):
        return tuple(name for inner in statement.body for name in _assigned_names(inner))
    if isinstance(statement, Try):
        return tuple(
            name
            for inner in statement.body
            for name in _assigned_names(inner)
        ) + tuple(
            name
            for handler in statement.handlers
            for inner in handler.body
            for name in _assigned_names(inner)
        )
    return ()


def _used_names(statement) -> tuple[str, ...]:
    if isinstance(statement, Assign):
        return _expr_used_names(statement.value)
    if isinstance(statement, AssignMany):
        return tuple(name for value in statement.values for name in _expr_used_names(value))
    if isinstance(statement, StoreItem):
        return (
            *_expr_used_names(statement.obj),
            *_expr_used_names(statement.key),
            *_expr_used_names(statement.value),
        )
    if isinstance(statement, StoreAttr):
        return (*_expr_used_names(statement.obj), *_expr_used_names(statement.value))
    if isinstance(statement, ExprStmt):
        return _expr_used_names(statement.value)
    if isinstance(statement, If):
        return (
            *_expr_used_names(statement.condition),
            *(name for inner in (*statement.then_body, *statement.else_body) for name in _used_names(inner)),
        )
    if isinstance(statement, While):
        return (
            *_expr_used_names(statement.condition),
            *(name for inner in statement.body for name in _used_names(inner)),
        )
    if isinstance(statement, ForEach):
        return (*_expr_used_names(statement.iterable), *(name for inner in statement.body for name in _used_names(inner)))
    if isinstance(statement, ForRange):
        return (
            *_expr_used_names(statement.start),
            *_expr_used_names(statement.stop),
            *_expr_used_names(statement.step),
            *(name for inner in statement.body for name in _used_names(inner)),
        )
    if isinstance(statement, Try):
        return (
            *(name for inner in statement.body for name in _used_names(inner)),
            *(
                name
                for handler in statement.handlers
                for name in _expr_used_names(handler.exception_type)
            ),
            *(name for handler in statement.handlers for inner in handler.body for name in _used_names(inner)),
        )
    if isinstance(statement, Raise):
        return (
            *_expr_used_names(statement.value),
            *(_expr_used_names(statement.cause) if statement.cause is not None else ()),
        )
    if isinstance(statement, Reraise):
        return ()
    if isinstance(statement, Return):
        return tuple(name for value in statement.values for name in _expr_used_names(value))
    if isinstance(statement, Branch):
        return _expr_used_names(statement.condition)
    if isinstance(statement, MultiBranch):
        return (
            *_expr_used_names(statement.selector),
            *(name for value, _target in statement.cases for name in _expr_used_names(value)),
        )
    return ()


def _expr_used_names(expr: Expr) -> tuple[str, ...]:
    if isinstance(expr, Var):
        return (expr.name,)
    if isinstance(expr, BinaryOp):
        return (*_expr_used_names(expr.left), *_expr_used_names(expr.right))
    if isinstance(expr, UnaryOp):
        return _expr_used_names(expr.value)
    if isinstance(expr, GetItem):
        return (*_expr_used_names(expr.obj), *_expr_used_names(expr.key))
    if isinstance(expr, GetAttr):
        return _expr_used_names(expr.obj)
    if isinstance(expr, Call):
        return (*_expr_used_names(expr.callee), *(name for arg in expr.args for name in _expr_used_names(arg)))
    if isinstance(expr, MultiReturn):
        return _expr_used_names(expr.value)
    if isinstance(expr, (TableLiteral, ObjectLiteral, MapLiteral)):
        return (
            *(name for item in getattr(expr, "array_items", ()) for name in _expr_used_names(item)),
            *(name for field in expr.fields for name in (*_expr_used_names(field.key), *_expr_used_names(field.value))),
        )
    if isinstance(expr, ArrayLiteral):
        return tuple(name for item in expr.items for name in _expr_used_names(item))
    if isinstance(expr, NewObject):
        return tuple(name for arg in expr.args for name in _expr_used_names(arg))
    if isinstance(expr, Phi):
        return tuple(name for _, value in expr.incoming for name in _expr_used_names(value))
    return ()


def _phi_placements(function: FunctionIR) -> tuple[PhiPlacement, ...]:
    cfg = build_cfg(function)
    preds = _predecessors(cfg)
    successors = {block_id: set(cfg.successors(block_id)) for block_id in cfg.blocks}
    live_in = _live_in_by_block(function, successors)
    reaching_out = _reaching_definitions(function, preds)
    placements: list[PhiPlacement] = []
    for block in function.blocks:
        predecessors = preds.get(block.id, set())
        if len(predecessors) < 2:
            continue
        incoming_vars = set().union(*(reaching_out.get(pred, {}) for pred in predecessors))
        for variable in sorted(incoming_vars & live_in.get(block.id, set())):
            reaching_sources = {
                source
                for pred in predecessors
                for source in reaching_out.get(pred, {}).get(variable, frozenset())
            }
            if len(reaching_sources) < 2:
                continue
            incoming = []
            for pred in sorted(predecessors):
                if variable in reaching_out.get(pred, {}):
                    incoming.append((pred, Var(name=variable)))
            placements.append(
                PhiPlacement(
                    block_id=block.id,
                    variable=variable,
                    incoming=tuple(incoming),
                )
            )
    return tuple(placements)


def _live_in_by_block(
    function: FunctionIR,
    successors: dict[str, set[str]],
) -> dict[str, set[str]]:
    defs: dict[str, set[str]] = {}
    uses: dict[str, set[str]] = {}
    for block in function.blocks:
        block_defs: set[str] = set()
        block_uses: set[str] = set()
        for statement in block.statements:
            block_uses.update(name for name in _used_names(statement) if name not in block_defs)
            block_defs.update(_assigned_names(statement))
        if block.terminator is not None:
            block_uses.update(name for name in _used_names(block.terminator) if name not in block_defs)
        defs[block.id] = block_defs
        uses[block.id] = block_uses

    live_in = {block.id: set() for block in function.blocks}
    live_out = {block.id: set() for block in function.blocks}
    changed = True
    while changed:
        changed = False
        for block in reversed(function.blocks):
            out = set().union(*(live_in.get(successor, set()) for successor in successors.get(block.id, set())))
            incoming = uses[block.id] | (out - defs[block.id])
            if incoming != live_in[block.id] or out != live_out[block.id]:
                live_in[block.id] = incoming
                live_out[block.id] = out
                changed = True
    return live_in


def _reaching_definitions(
    function: FunctionIR,
    preds: dict[str, set[str]],
) -> dict[str, dict[str, frozenset[str]]]:
    block_defs = {
        block.id: {name: frozenset({block.id}) for statement in block.statements for name in _assigned_names(statement)}
        for block in function.blocks
    }
    entry = function.blocks[0].id if function.blocks else None
    in_env: dict[str, dict[str, frozenset[str]]] = {block.id: {} for block in function.blocks}
    out_env: dict[str, dict[str, frozenset[str]]] = {block.id: {} for block in function.blocks}
    if entry is not None:
        in_env[entry] = {param: frozenset({f"param:{param}"}) for param in function.params}

    changed = True
    while changed:
        changed = False
        for block in function.blocks:
            if block.id != entry:
                merged: dict[str, set[str]] = {}
                for pred in preds.get(block.id, set()):
                    for name, sources in out_env.get(pred, {}).items():
                        merged.setdefault(name, set()).update(sources)
                candidate_in = {name: frozenset(sources) for name, sources in merged.items()}
                if candidate_in != in_env[block.id]:
                    in_env[block.id] = candidate_in
                    changed = True
            candidate_out = dict(in_env[block.id])
            candidate_out.update(block_defs.get(block.id, {}))
            if candidate_out != out_env[block.id]:
                out_env[block.id] = candidate_out
                changed = True
    return out_env


def _phi_metadata(placements: tuple[PhiPlacement, ...]) -> list[dict[str, object]]:
    return [
        {
            "block": placement.block_id,
            "variable": placement.variable,
            "incoming": list(placement.incoming),
        }
        for placement in placements
    ]


def _predecessors(cfg):
    preds: dict[str, set[str]] = {block: set() for block in cfg.blocks}
    for edge in cfg.edges:
        preds.setdefault(edge.target, set()).add(edge.source)
    return preds
