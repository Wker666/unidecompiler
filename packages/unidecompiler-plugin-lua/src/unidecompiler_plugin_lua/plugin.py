from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendModule
from unidecompiler_plugin_lua.lifter import lift_lua_chunk
from unidecompiler_plugin_lua.chunk54 import decode_lua54_chunk, Lua54ChunkError
from unidecompiler_plugin_lua.luac import (
    looks_like_luac,
    PreferredLuaChunkDecoder,
    LuaChunkDecoder,
)
from unidecompiler_plugin_lua.normalize import normalized_functions_metadata
from unidecompiler_plugin_lua.simulation import LuaSimulationAdapter
from unidecompiler_plugin_lua.support import LUA_VERSION_SUPPORT


class LuaFrontendPlugin:
    id = "lua"
    display_name = "Lua bytecode"
    supported_inputs = (".luac",)
    version_support = LUA_VERSION_SUPPORT
    simulation_adapter = LuaSimulationAdapter

    def __init__(self, decoder: LuaChunkDecoder | None = None) -> None:
        self.decoder = decoder or PreferredLuaChunkDecoder()

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_luac(data) and self.decoder.can_decode(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        chunk = self.decoder.decode(data, filename)
        normalized_functions = normalized_functions_metadata(chunk.functions)
        diagnostics = _normalization_diagnostics(normalized_functions)
        return FrontendModule(
            frontend_id=self.id,
            payload=chunk,
            metadata={
                "filename": filename,
                "format": "luac",
                "version": chunk.header.version_label,
                "endianness": (
                    None
                    if chunk.header.little_endian is None
                    else "little"
                    if chunk.header.little_endian
                    else "big"
                ),
                "debug_info_present": chunk.disassembly is not None,
                "diagnostics": diagnostics,
                "lua": {
                    "int_size": chunk.header.int_size,
                    "size_t_size": chunk.header.size_t_size,
                    "instruction_size": chunk.header.instruction_size,
                    "number_size": chunk.header.lua_number_size,
                    "integral_numbers": chunk.header.integral_numbers,
                    "decoder": chunk.decoder_id or self.decoder.id,
                    "decoder_policy": self.decoder.id,
                    "has_disassembly": chunk.disassembly is not None,
                    "normalized_functions": normalized_functions,
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(
                f"Lua frontend cannot lift module from {module.frontend_id!r}"
            )

        return lift_lua_chunk(module.payload, module.metadata)


class Lua54BinaryChunkDecoder:
    id = "lua54-binary"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_luac(data) and len(data) >= 8 and data[4] == 0x54

    def decode(self, data: bytes, filename: str | None = None):
        try:
            return decode_lua54_chunk(data, filename)
        except Lua54ChunkError as exc:
            from unidecompiler_plugin_lua.luac import LuacDecodeError

            raise LuacDecodeError(str(exc)) from exc


def _normalization_diagnostics(
    normalized_functions: list[dict],
) -> list[dict[str, object]]:
    diagnostics: list[dict[str, object]] = []
    for function in normalized_functions:
        unsupported_opcodes = function["unsupported_opcodes"]
        if not unsupported_opcodes:
            continue
        diagnostics.append(
            {
                "severity": "info",
                "code": "lua.requires-cfg-structuring",
                "function": function["name"],
                "message": (
                    "Function contains Lua instructions that require CFG/"
                    "structuring before safe pseudocode lifting."
                ),
                "opcodes": unsupported_opcodes,
            }
        )
    return diagnostics
