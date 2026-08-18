"""Public, frontend-neutral facade for application hosts."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from unidecompiler.backends.pseudocode import GenericPseudocodeBackend
from unidecompiler.analysis import BytecodeControlFlowInstruction, BrowseIndex, FunctionControlFlow, SymbolIndex, build_browse_index, build_control_flow_index, build_symbol_index
from unidecompiler.core.ast import ModuleDecl
from unidecompiler.core.astify import module_to_ast
from unidecompiler.core.diagnostics import Diagnostic
from unidecompiler.core.ir import ModuleIR, SourceRef
from unidecompiler.core.reporting import ModuleReport, build_module_report
from unidecompiler.input_sources import InputArtifact, expand_input_path
from unidecompiler.plugin_registry import FrontendRegistry, FrontendSelectionError
from unidecompiler.plugins import FrontendDecodeError


@dataclass(frozen=True)
class BytecodeInstruction:
    function_id: str
    offset: int | None
    opcode: str
    operands: tuple[str, ...]
    raw: str
    source: SourceRef


@dataclass(frozen=True)
class FunctionResult:
    id: str
    name: str
    status: str
    unsupported_reason: str | None
    raw_context: tuple[str, ...]
    source: SourceRef | None = None


@dataclass(frozen=True)
class PseudocodeRange:
    start: int
    end: int
    source: SourceRef
    function_id: str | None = None


@dataclass(frozen=True)
class PseudocodeDocument:
    text: str
    source_map: tuple[PseudocodeRange, ...] = ()


@dataclass(frozen=True)
class DecompileResult:
    display_path: str
    status: str
    frontend_id: str | None
    module: ModuleIR | None
    ast: ModuleDecl | None
    pseudocode: PseudocodeDocument | None
    report: ModuleReport | None
    functions: tuple[FunctionResult, ...]
    diagnostics: tuple[Diagnostic, ...]
    instructions: tuple[BytecodeInstruction, ...]
    symbols: SymbolIndex = SymbolIndex()
    control_flow: tuple[FunctionControlFlow, ...] = ()
    browse: BrowseIndex = BrowseIndex()


class DecompilerEngine:
    """Stable read-only facade shared by graphical and command-line hosts."""

    def __init__(self, registry: FrontendRegistry | None = None) -> None:
        self._registry = registry or FrontendRegistry.discover()

    @classmethod
    def discover(cls) -> "DecompilerEngine":
        return cls(FrontendRegistry.discover())

    @classmethod
    def from_registry(cls, registry: FrontendRegistry) -> "DecompilerEngine":
        return cls(registry)

    @property
    def registry(self) -> FrontendRegistry:
        return self._registry

    def register_frontend_directory(self, directory: Path | str):
        return self._registry.register_directory(directory)

    def unregister_frontend(self, frontend_id: str):
        return self._registry.unregister(frontend_id)

    def decompile_bytes(self, data: bytes, display_path: str, frontend_id: str | None = None) -> DecompileResult:
        try:
            frontend = self._registry.select(data, display_path, explicit_id=frontend_id)
        except FrontendSelectionError as error:
            return _empty(display_path, "resource", None, "input.unsupported", str(error), "warning")
        try:
            decoded = frontend.decode(data, display_path)
        except FrontendDecodeError as error:
            return _empty(display_path, "error", frontend.id, "frontend.decode", str(error), "error")
        except Exception as error:
            return _empty(display_path, "error", frontend.id, "frontend.decode-error", _error(error), "error")
        try:
            return _present(display_path, frontend.id, frontend.lift(decoded), decoded.metadata)
        except Exception as error:
            return _empty(display_path, "error", frontend.id, "core.lift-error", _error(error), "error")

    def decompile_artifacts(self, input_path: Path | str | Iterable[InputArtifact], frontend_id: str | None = None) -> tuple[DecompileResult, ...]:
        artifacts = expand_input_path(Path(input_path)) if isinstance(input_path, str | Path) else tuple(input_path)
        return tuple(self.decompile_artifact(item, frontend_id) for item in artifacts)

    def decompile_artifact(
        self,
        artifact: InputArtifact,
        frontend_id: str | None = None,
    ) -> DecompileResult:
        """Decompile one already-expanded artifact without mutating host state."""
        return self.decompile_bytes(artifact.data, artifact.display_path, frontend_id)


def _present(display_path: str, frontend_id: str, module: ModuleIR, metadata: dict) -> DecompileResult:
    ast = module_to_ast(module)
    emitted = GenericPseudocodeBackend().emit(module)
    diagnostics = _metadata_diagnostics(metadata, frontend_id)
    functions: list[FunctionResult] = []
    instructions: list[BytecodeInstruction] = []
    control_instructions: list[tuple[str, tuple[BytecodeControlFlowInstruction, ...]]] = []
    for ordinal, function in enumerate(_walk(module.functions)):
        function_id = f"{module.name}:{ordinal}:{function.name}"
        rows = tuple(function.metadata.get("bytecode_instructions", ()))
        source = function.source or (rows[0].get("source") if rows else None)
        raw = tuple(function.metadata.get("unsupported_raw", ()))
        status = function.metadata.get("decompile_status", "unknown")
        functions.append(FunctionResult(function_id, function.name, status, function.metadata.get("unsupported_reason"), raw, source))
        if status in {"partial", "unsupported"}:
            diagnostics.append(Diagnostic(f"recovery.{status}", function.metadata.get("unsupported_reason", "Partial recovery"), "warning", frontend_id, function_id, None if source is None else source.offset, source, raw))
        function_control: list[BytecodeControlFlowInstruction] = []
        for row in rows:
            instructions.append(BytecodeInstruction(function_id, row["offset"], row["opcode"], tuple(value["text"] for value in row["operands"]), row["raw"], row["source"]))
            offset = row.get("offset")
            source_ref = row.get("source")
            control = tuple(row.get("control", ()))
            targets = tuple(
                item["target"] for item in control
                if isinstance(item, dict) and isinstance(item.get("target"), int)
            )
            flows = {item.get("flow") for item in control if isinstance(item, dict)}
            flow = next(iter(flows)) if len(flows) == 1 else None
            if isinstance(offset, int) and isinstance(source_ref, SourceRef):
                function_control.append(BytecodeControlFlowInstruction(
                    offset=offset,
                    source=source_ref,
                    flow=flow if flow in {"conditional", "unconditional", "multiway"} else None,
                    targets=targets,
                ))
        control_instructions.append((function_id, tuple(function_control)))
    index = build_symbol_index(
        ast,
        tuple((function.id, function.name, function.source) for function in functions),
    )
    browse = build_browse_index(
        ast,
        tuple((function.id, function.name, function.source) for function in functions),
    )
    control_flow = build_control_flow_index(
        tuple((function.id, ir) for function, ir in zip(functions, _walk(module.functions), strict=True)),
        tuple(control_instructions),
    )
    return DecompileResult(
        display_path=display_path,
        status="ok",
        frontend_id=frontend_id,
        module=module,
        ast=ast,
        pseudocode=PseudocodeDocument(emitted.text, _source_map(emitted.text, emitted.metadata, functions)),
        report=build_module_report(module),
        functions=tuple(functions),
        diagnostics=tuple(diagnostics),
        instructions=tuple(instructions),
        symbols=index,
        control_flow=control_flow,
        browse=browse,
    )


def _walk(functions):
    for function in functions:
        yield function
        yield from _walk(function.nested_functions)


def _source_map(
    text: str,
    metadata: dict,
    functions: list[FunctionResult],
) -> tuple[PseudocodeRange, ...]:
    result: list[PseudocodeRange] = []
    cursor = 0
    for function in functions:
        if function.source is None:
            continue
        start = text.find(f"function {function.name}(", cursor)
        if start < 0:
            continue
        end = text.find("\n}", start)
        end = len(text) if end < 0 else end + 2
        result.append(PseudocodeRange(start, end, function.source, function.id))
        cursor = start + 1
    for span in metadata.get("source_spans", ()):
        ordinal = span.get("function_ordinal")
        if not isinstance(ordinal, int) or ordinal >= len(functions):
            continue
        source = span.get("source")
        if not isinstance(source, SourceRef):
            continue
        result.append(
            PseudocodeRange(
                start=span["start"],
                end=span["end"],
                source=source,
                function_id=functions[ordinal].id,
            )
        )
    return tuple(result)


def _metadata_diagnostics(metadata: dict, frontend_id: str) -> list[Diagnostic]:
    diagnostics: list[Diagnostic] = []
    for item in metadata.get("diagnostics", ()):
        if isinstance(item, dict):
            severity = item.get("severity", "warning")
            diagnostics.append(Diagnostic(str(item.get("code", "frontend.diagnostic")), str(item.get("message", "Frontend diagnostic")), severity if severity in {"info", "warning", "error"} else "warning", frontend_id, item.get("function"), item.get("offset"), raw_context=tuple(str(raw) for raw in item.get("raw", ()))))
    return diagnostics


def _empty(path: str, status: str, frontend: str | None, code: str, message: str, severity: str) -> DecompileResult:
    diagnostic = Diagnostic(code, message, severity, frontend)  # type: ignore[arg-type]
    return DecompileResult(path, status, frontend, None, None, None, None, (), (diagnostic,), ())


def _error(error: Exception) -> str:
    return f"{type(error).__name__}: {error}"
