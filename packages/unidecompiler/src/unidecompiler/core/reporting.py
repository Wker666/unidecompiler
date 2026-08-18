from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.ir import ModuleIR


@dataclass(frozen=True)
class FunctionReport:
    name: str
    status: str
    reason: str | None = None
    unsupported_opcodes: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "reason": self.reason,
            "unsupported_opcodes": list(self.unsupported_opcodes),
        }


@dataclass(frozen=True)
class ModuleReport:
    module: str
    source_language: str
    frontend_format: str | None
    functions: tuple[FunctionReport, ...]

    def as_dict(self) -> dict:
        return {
            "module": self.module,
            "source_language": self.source_language,
            "frontend_format": self.frontend_format,
            "functions": [function.as_dict() for function in self.functions],
        }


def build_module_report(module: ModuleIR) -> ModuleReport:
    frontend = module.metadata.get("frontend", {})
    return ModuleReport(
        module=module.name,
        source_language=module.source_language,
        frontend_format=frontend.get("format"),
        functions=tuple(
            FunctionReport(
                name=function.name,
                status=function.metadata.get("decompile_status", "unknown"),
                reason=function.metadata.get("unsupported_reason"),
                unsupported_opcodes=tuple(function.metadata.get("unsupported_opcodes", ())),
            )
            for function in module.functions
        ),
    )

