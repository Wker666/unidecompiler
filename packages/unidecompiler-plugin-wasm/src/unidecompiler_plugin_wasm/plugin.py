from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendModule
from unidecompiler_plugin_wasm.lifter import lift_wasm_module
from unidecompiler_plugin_wasm.module import (
    WasmLibraryModuleDecoder,
    WasmModuleDecoder,
    looks_like_wasm,
)
from unidecompiler_plugin_wasm.support import WASM_VERSION_SUPPORT


class WasmFrontendPlugin:
    id = "wasm"
    display_name = "WebAssembly module"
    supported_inputs = (".wasm",)
    version_support = WASM_VERSION_SUPPORT

    def __init__(self, decoder: WasmModuleDecoder | None = None) -> None:
        self.decoder = decoder or WasmLibraryModuleDecoder()

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_wasm(data) and self.decoder.can_decode(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        module = self.decoder.decode(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=module,
            metadata={
                "filename": filename,
                "format": "wasm",
                "version": str(module.version),
                "endianness": "little",
                "debug_info_present": False,
                "diagnostics": [],
                "wasm": {
                    "decoder": module.decoder_id or self.decoder.id,
                    "function_count": len(module.functions),
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(f"WASM frontend cannot lift module from {module.frontend_id!r}")

        return lift_wasm_module(module.payload, module.metadata)
