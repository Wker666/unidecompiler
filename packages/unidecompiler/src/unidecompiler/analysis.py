"""VM-neutral, read-only semantic indexes for decompiler hosts."""
from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Literal

from unidecompiler.core.ast import (
    AstExpr, CallExpr, ConstExpr, FunctionDecl, GetAttrExpr, GlobalRef,
    ModuleDecl, NewObjectExpr, StoreAttrStmt, VarRef,
)
from unidecompiler.core.cfg import build_cfg
from unidecompiler.core.ir import FunctionIR, SourceRef


SymbolKind = Literal["function", "parameter"]
ReferenceKind = Literal["call", "global-read", "parameter-read"]


@dataclass(frozen=True)
class Symbol:
    id: str
    name: str
    kind: SymbolKind
    function_id: str
    source: SourceRef | None


@dataclass(frozen=True)
class Reference:
    id: str
    name: str
    kind: ReferenceKind
    function_id: str
    source: SourceRef | None
    target_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SymbolIndex:
    symbols: tuple[Symbol, ...] = ()
    references: tuple[Reference, ...] = ()

    def definitions(self, name: str) -> tuple[Symbol, ...]:
        return tuple(symbol for symbol in self.symbols if symbol.name == name)

    def usages(self, symbol_id: str) -> tuple[Reference, ...]:
        return tuple(reference for reference in self.references if symbol_id in reference.target_ids)


BrowseKind = Literal["constant", "string", "type", "member", "global"]


@dataclass(frozen=True)
class BrowseEntry:
    """A generic recovered-AST fact, with provenance when it is available."""

    id: str
    kind: BrowseKind
    name: str
    value: str
    function_id: str
    source: SourceRef | None


@dataclass(frozen=True)
class BrowseIndex:
    entries: tuple[BrowseEntry, ...] = ()

    def by_kind(self, kind: BrowseKind) -> tuple[BrowseEntry, ...]:
        return tuple(entry for entry in self.entries if entry.kind == kind)


@dataclass(frozen=True)
class ControlFlowBlock:
    id: str
    statement_count: int
    terminator: str | None
    source: SourceRef | None = None


@dataclass(frozen=True)
class BytecodeControlFlowInstruction:
    """Neutral control-transfer facts retained for read-only presentation."""

    offset: int
    source: SourceRef
    flow: Literal["conditional", "unconditional", "multiway"] | None
    targets: tuple[int, ...] = ()


@dataclass(frozen=True)
class ControlFlowEdge:
    source: str
    target: str
    kind: str


@dataclass(frozen=True)
class FunctionControlFlow:
    function_id: str
    entry: str | None
    blocks: tuple[ControlFlowBlock, ...]
    edges: tuple[ControlFlowEdge, ...]
    diagnostics: tuple[str, ...] = ()


def build_control_flow_index(
    functions: tuple[tuple[str, FunctionIR], ...],
    bytecode: tuple[tuple[str, tuple[BytecodeControlFlowInstruction, ...]], ...] = (),
) -> tuple[FunctionControlFlow, ...]:
    bytecode_by_function = dict(bytecode)
    output: list[FunctionControlFlow] = []
    for function_id, function in functions:
        cfg = build_cfg(function)
        structured = FunctionControlFlow(
            function_id=function_id,
            entry=cfg.entry,
            blocks=tuple(ControlFlowBlock(
                id=block.id,
                statement_count=len(block.statements),
                terminator=None if block.terminator is None else type(block.terminator).__name__,
                source=next((statement.source for statement in block.statements if statement.source is not None), None),
            ) for block in function.blocks),
            edges=tuple(ControlFlowEdge(edge.source, edge.target, edge.kind) for edge in cfg.edges),
            diagnostics=cfg.diagnostics,
        )
        projected = _bytecode_control_flow(function_id, bytecode_by_function.get(function_id, ()))
        output.append(projected if projected is not None else structured)
    return tuple(output)


def _bytecode_control_flow(
    function_id: str,
    instructions: tuple[BytecodeControlFlowInstruction, ...],
) -> FunctionControlFlow | None:
    if not instructions or not any(instruction.targets for instruction in instructions):
        return None
    if any(instruction.targets and instruction.flow is None for instruction in instructions):
        return None
    ordered = tuple(sorted(instructions, key=lambda instruction: instruction.offset))
    offsets = tuple(instruction.offset for instruction in ordered)
    by_offset = {instruction.offset: instruction for instruction in ordered}
    leaders = {offsets[0]}
    for index, instruction in enumerate(ordered):
        leaders.update(target for target in instruction.targets if target in by_offset)
        if instruction.targets and index + 1 < len(ordered):
            leaders.add(offsets[index + 1])
    ordered_leaders = tuple(sorted(leaders))
    leader_for_offset = {
        offset: max(leader for leader in ordered_leaders if leader <= offset)
        for offset in offsets
    }
    blocks: list[ControlFlowBlock] = []
    edges: list[ControlFlowEdge] = []
    for index, leader in enumerate(ordered_leaders):
        next_leader = ordered_leaders[index + 1] if index + 1 < len(ordered_leaders) else None
        members = tuple(item for item in ordered if item.offset >= leader and (next_leader is None or item.offset < next_leader))
        if not members:
            continue
        last = members[-1]
        block_id = f"offset_{leader}"
        terminator = _presentation_terminator(last, next_leader)
        blocks.append(ControlFlowBlock(block_id, len(members), terminator, members[0].source))
        for target in last.targets:
            target_leader = leader_for_offset.get(target)
            if target_leader is not None:
                edges.append(ControlFlowEdge(block_id, f"offset_{target_leader}", "branch" if last.flow == "conditional" else "jump"))
        if last.flow == "conditional" and next_leader is not None:
            edges.append(ControlFlowEdge(block_id, f"offset_{next_leader}", "fallthrough"))
        elif not last.targets and next_leader is not None:
            edges.append(ControlFlowEdge(block_id, f"offset_{next_leader}", "fallthrough"))
    if len(blocks) <= 1 or not edges:
        return None
    return FunctionControlFlow(
        function_id=function_id,
        entry=blocks[0].id,
        blocks=tuple(blocks),
        edges=tuple(edges),
        diagnostics=("Bytecode CFG shown from VM-neutral control hints",),
    )


def _presentation_terminator(
    instruction: BytecodeControlFlowInstruction,
    next_leader: int | None,
) -> str:
    if instruction.flow == "conditional":
        return "Branch"
    if instruction.flow == "unconditional":
        return "Jump"
    if instruction.flow == "multiway":
        return "Switch"
    return "Return" if next_leader is None else "Fallthrough"


def build_symbol_index(
    module: ModuleDecl,
    functions: tuple[tuple[str, str, SourceRef | None], ...],
) -> SymbolIndex:
    """Build only facts provable from the generic AST; never infer VM semantics."""
    ast_functions = tuple(_walk_functions(module.functions))
    if len(ast_functions) != len(functions):
        raise ValueError("function result and AST function order differ")

    symbols: list[Symbol] = []
    references: list[Reference] = []
    function_symbols: dict[str, list[str]] = {}
    parameter_symbols: dict[tuple[str, str], str] = {}
    for function, (function_id, name, source) in zip(ast_functions, functions, strict=True):
        symbol_id = f"{function_id}:function"
        symbols.append(Symbol(symbol_id, name, "function", function_id, source))
        function_symbols.setdefault(name, []).append(symbol_id)
        for parameter in function.params:
            parameter_id = f"{function_id}:parameter:{parameter}"
            symbols.append(Symbol(parameter_id, parameter, "parameter", function_id, function.source or source))
            parameter_symbols[function_id, parameter] = parameter_id

    for function, (function_id, _name, _source) in zip(ast_functions, functions, strict=True):
        ordinal = 0
        for node in _walk_nodes(function.body):
            if isinstance(node, CallExpr):
                callee_name = _call_name(node)
                if callee_name is not None:
                    references.append(Reference(
                        id=f"{function_id}:reference:{ordinal}",
                        name=callee_name,
                        kind="call",
                        function_id=function_id,
                        source=node.source,
                        target_ids=tuple(function_symbols.get(callee_name, ())),
                    ))
                    ordinal += 1
            elif isinstance(node, GlobalRef):
                references.append(Reference(
                    id=f"{function_id}:reference:{ordinal}",
                    name=node.name,
                    kind="global-read",
                    function_id=function_id,
                    source=node.source,
                ))
                ordinal += 1
            elif isinstance(node, VarRef):
                parameter_id = parameter_symbols.get((function_id, node.name))
                if parameter_id is not None:
                    references.append(Reference(
                        id=f"{function_id}:reference:{ordinal}",
                        name=node.name,
                        kind="parameter-read",
                        function_id=function_id,
                        source=node.source,
                        target_ids=(parameter_id,),
                    ))
                    ordinal += 1
    return SymbolIndex(tuple(symbols), tuple(references))


def build_browse_index(
    module: ModuleDecl,
    functions: tuple[tuple[str, str, SourceRef | None], ...],
) -> BrowseIndex:
    """Index only facts explicitly represented by the generic recovered AST.

    The index intentionally avoids frontend metadata and does not guess source
    language semantics. Occurrences remain separate because each one may have
    a different source location for host navigation.
    """
    ast_functions = tuple(_walk_functions(module.functions))
    if len(ast_functions) != len(functions):
        raise ValueError("function result and AST function order differ")

    entries: list[BrowseEntry] = []
    for function, (function_id, _name, _source) in zip(ast_functions, functions, strict=True):
        ordinal = 0
        for node in _walk_nodes(function.body):
            kind: BrowseKind | None = None
            name = ""
            value = ""
            source = getattr(node, "source", None)
            if isinstance(node, ConstExpr):
                kind = "string" if isinstance(node.value, str) else "constant"
                name = node.value if isinstance(node.value, str) else type(node.value).__name__
                value = repr(node.value)
            elif isinstance(node, NewObjectExpr):
                kind, name, value = "type", node.type_name, node.type_name
            elif isinstance(node, (GetAttrExpr, StoreAttrStmt)):
                kind, name, value = "member", node.attr, node.attr
            elif isinstance(node, GlobalRef):
                kind, name, value = "global", node.name, node.name
            elif isinstance(node, AstExpr) and node.type.name != "unknown":
                kind, name, value = "type", node.type.name, node.type.name
            if kind is None:
                continue
            entries.append(BrowseEntry(
                id=f"{function_id}:browse:{ordinal}",
                kind=kind,
                name=str(name),
                value=str(value),
                function_id=function_id,
                source=source,
            ))
            ordinal += 1
    return BrowseIndex(tuple(entries))


def _walk_functions(functions: tuple[FunctionDecl, ...]):
    for function in functions:
        yield function
        yield from _walk_functions(function.nested_functions)


def _walk_nodes(value):
    if isinstance(value, tuple | list):
        for item in value:
            yield from _walk_nodes(item)
    elif is_dataclass(value):
        yield value
        for field in fields(value):
            yield from _walk_nodes(getattr(value, field.name))


def _call_name(call: CallExpr) -> str | None:
    if isinstance(call.callee, (GlobalRef, VarRef)):
        return call.callee.name
    return None
