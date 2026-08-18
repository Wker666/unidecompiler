from __future__ import annotations

from typing import Any

from unidecompiler.core.function_assembly import assemble_module
from unidecompiler.core.ir import FunctionIR, ModuleIR


def assemble_vm_module(
    *,
    name: str,
    source_language: str,
    functions: tuple[FunctionIR, ...],
    metadata: dict[str, Any] | None = None,
) -> ModuleIR:
    return assemble_module(
        name=name,
        source_language=source_language,
        functions=functions,
        metadata=metadata,
    )
