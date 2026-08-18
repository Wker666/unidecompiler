from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from unidecompiler.core.ir import BasicBlock, FunctionIR, ModuleIR, SourceRef, Stmt, Terminator


@dataclass(frozen=True)
class FunctionBlockSpec:
    id: str
    statements: tuple[Stmt, ...] = ()
    terminator: Terminator | None = None


def assemble_entry_function(
    *,
    name: str,
    params: tuple[str, ...] = (),
    frontend: str,
    statements: tuple[Stmt, ...] = (),
    terminator: Terminator | None = None,
    metadata: dict[str, Any] | None = None,
    recovery_kind: str | None = None,
) -> FunctionIR:
    return assemble_function(
        name=name,
        params=params,
        frontend=frontend,
        blocks=(FunctionBlockSpec(id="entry", statements=statements, terminator=terminator),),
        metadata=metadata,
        recovery_kind=recovery_kind,
    )


def assemble_function(
    *,
    name: str,
    params: tuple[str, ...] = (),
    frontend: str,
    blocks: tuple[FunctionBlockSpec, ...] = (),
    metadata: dict[str, Any] | None = None,
    recovery_kind: str | None = None,
) -> FunctionIR:
    return FunctionIR(
        name=name,
        params=params,
        blocks=tuple(
            BasicBlock(
                id=block.id,
                statements=block.statements,
                terminator=block.terminator,
            )
            for block in blocks
        ),
        source=SourceRef(frontend=frontend),
        recovery_kind=recovery_kind,
        metadata=metadata or {},
    )


def assemble_function_without_blocks(
    *,
    name: str,
    params: tuple[str, ...] = (),
    frontend: str,
    metadata: dict[str, Any] | None = None,
    recovery_kind: str | None = None,
) -> FunctionIR:
    return FunctionIR(
        name=name,
        params=params,
        source=SourceRef(frontend=frontend),
        recovery_kind=recovery_kind,
        metadata=metadata or {},
    )


def assemble_module(
    *,
    name: str,
    source_language: str,
    functions: tuple[FunctionIR, ...] = (),
    metadata: dict[str, Any] | None = None,
) -> ModuleIR:
    return ModuleIR(
        name=name,
        source_language=source_language,
        metadata=metadata or {},
        functions=functions,
    )
