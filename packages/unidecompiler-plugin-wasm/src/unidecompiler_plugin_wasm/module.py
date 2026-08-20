from __future__ import annotations

from dataclasses import dataclass
import collections
import collections.abc
from typing import Protocol

from unidecompiler.plugins import FrontendDecodeError


WASM_MAGIC = b"\x00asm"
WASM_VERSION = b"\x01\x00\x00\x00"


class WasmDecodeError(FrontendDecodeError):
    pass


@dataclass(frozen=True)
class WasmInstruction:
    offset: int
    opcode: str
    operands: tuple[str, ...] = ()
    size: int = 1


@dataclass(frozen=True)
class WasmFunctionListing:
    name: str
    index: int
    type_index: int | None = None
    param_count: int = 0
    result_count: int = 0
    local_count: int = 0
    instructions: tuple[WasmInstruction, ...] = ()
    function_type_params: tuple[int, ...] = ()
    function_type_results: tuple[int, ...] = ()
    module_function_signatures: tuple[tuple[int, int, int], ...] = ()
    module_types: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()


@dataclass(frozen=True)
class WasmModule:
    filename: str | None = None
    version: int = 1
    functions: tuple[WasmFunctionListing, ...] = ()
    types: tuple[tuple[tuple[int, ...], tuple[int, ...]], ...] = ()
    decoder_id: str | None = None


class WasmModuleDecoder(Protocol):
    id: str

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        ...

    def decode(self, data: bytes, filename: str | None = None) -> WasmModule:
        ...


def looks_like_wasm(data: bytes) -> bool:
    return data.startswith(WASM_MAGIC + WASM_VERSION)


class WasmLibraryModuleDecoder:
    id = "wasmtime-wasm-library"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_wasm(data) and _wasmtime_module() is not None and _wasm_decode_bytecode() is not None

    def decode(self, data: bytes, filename: str | None = None) -> WasmModule:
        wasmtime = _wasmtime_module()
        if wasmtime is None:
            raise WasmDecodeError("wasmtime is not installed")
        try:
            wasmtime.Module(wasmtime.Engine(), data)
        except Exception as error:
            raise WasmDecodeError(f"wasmtime failed to validate module: {error}") from error
        return _decode_wasm_binary(data, filename, decoder_id=self.id)


def _wasmtime_module():
    try:
        import wasmtime
    except ImportError:
        return None
    return wasmtime


def _wasm_decode_bytecode():
    if not hasattr(collections, "Callable"):
        collections.Callable = collections.abc.Callable
    try:
        from wasm.decode import decode_bytecode
    except Exception:
        return None
    return decode_bytecode


def _decode_wasm_binary(data: bytes, filename: str | None, decoder_id: str) -> WasmModule:
    reader = _Reader(data)
    if reader.read(4) != WASM_MAGIC:
        raise WasmDecodeError("missing WebAssembly magic")
    if reader.read(4) != WASM_VERSION:
        raise WasmDecodeError("unsupported WebAssembly binary version")

    types: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    function_type_indices: list[int] = []
    code_bodies: list[tuple[int, tuple[WasmInstruction, ...]]] = []
    export_names: dict[int, str] = {}
    imported_function_count = 0

    while not reader.eof:
        section_id = reader.byte()
        section_size = reader.u32()
        section_data = _Reader(reader.read(section_size))
        if section_id == 1:
            types = _read_type_section(section_data)
        elif section_id == 2:
            imported_function_count = _read_imported_function_count(section_data)
        elif section_id == 3:
            function_type_indices = _read_function_section(section_data)
        elif section_id == 7:
            export_names = _read_export_section(section_data)
        elif section_id == 10:
            code_bodies = _read_code_section(section_data)

    functions: list[WasmFunctionListing] = []
    for body_index, (local_count, instructions) in enumerate(code_bodies):
        function_index = imported_function_count + body_index
        type_index = function_type_indices[body_index] if body_index < len(function_type_indices) else None
        params, results = types[type_index] if type_index is not None and type_index < len(types) else ((), ())
        functions.append(
            WasmFunctionListing(
                name=export_names.get(function_index) or f"$func{function_index}",
                index=function_index,
                type_index=type_index,
                param_count=len(params),
                result_count=len(results),
                local_count=local_count,
                instructions=instructions,
                function_type_params=params,
                function_type_results=results,
            )
        )

    return WasmModule(filename=filename, functions=tuple(functions), types=tuple(types), decoder_id=decoder_id)


def _read_type_section(reader: "_Reader") -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    types: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    for _ in range(reader.u32()):
        form = reader.byte()
        if form != 0x60:
            raise WasmDecodeError(f"unsupported wasm type form: 0x{form:02x}")
        params = tuple(reader.byte() for _ in range(reader.u32()))
        results = tuple(reader.byte() for _ in range(reader.u32()))
        types.append((params, results))
    return types


def _read_imported_function_count(reader: "_Reader") -> int:
    count = 0
    for _ in range(reader.u32()):
        _read_name(reader)
        _read_name(reader)
        kind = reader.byte()
        if kind == 0:
            reader.u32()
            count += 1
        elif kind == 1:
            _skip_table_type(reader)
        elif kind == 2:
            _skip_limits(reader)
        elif kind == 3:
            reader.byte()
            reader.byte()
        else:
            raise WasmDecodeError(f"unsupported wasm import kind: {kind}")
    return count


def _read_function_section(reader: "_Reader") -> list[int]:
    return [reader.u32() for _ in range(reader.u32())]


def _read_export_section(reader: "_Reader") -> dict[int, str]:
    names: dict[int, str] = {}
    for _ in range(reader.u32()):
        name = _read_name(reader)
        kind = reader.byte()
        index = reader.u32()
        if kind == 0:
            names[index] = name
    return names


def _read_code_section(reader: "_Reader") -> list[tuple[int, tuple[WasmInstruction, ...]]]:
    bodies: list[tuple[int, tuple[WasmInstruction, ...]]] = []
    for _ in range(reader.u32()):
        body_reader = _Reader(reader.read(reader.u32()))
        local_count = 0
        for _ in range(body_reader.u32()):
            count = body_reader.u32()
            body_reader.byte()
            local_count += count
        code_offset = body_reader.offset
        bodies.append((local_count, _decode_instruction_stream(body_reader.read(len(body_reader.data) - body_reader.offset), code_offset)))
    return bodies


def _decode_instruction_stream(code: bytes, base_offset: int) -> tuple[WasmInstruction, ...]:
    decode_bytecode = _wasm_decode_bytecode()
    if decode_bytecode is None:
        raise WasmDecodeError("wasm decode library is not available")
    instructions: list[WasmInstruction] = []
    offset = 0
    while offset < len(code):
        prefix = code[offset]
        if prefix in {0xFC, 0xFD}:
            instruction, length = _decode_prefixed_instruction(code, offset)
            instructions.append(
                WasmInstruction(
                    offset=base_offset + offset,
                    opcode=instruction[0],
                    operands=instruction[1],
                    size=length,
                )
            )
            offset += length
            continue
        post_mvp_opcode = _POST_MVP_OPCODES.get(prefix)
        if post_mvp_opcode is not None:
            operands: tuple[str, ...] = ()
            length = 1
            if prefix in {0xD0, 0xD2}:
                value, cursor = _read_uleb128(code, offset + 1)
                operands = (str(value),)
                length = cursor - offset
            instructions.append(
                WasmInstruction(
                    offset=base_offset + offset,
                    opcode=post_mvp_opcode,
                    operands=operands,
                    size=length,
                )
            )
            offset += length
            continue
        try:
            instruction = next(decode_bytecode(code[offset:]))
        except (KeyError, IndexError, ValueError) as error:
            raise WasmDecodeError(
                f"unsupported wasm opcode 0x{prefix:02x} at byte {base_offset + offset}"
            ) from error
        operands = _library_operands(instruction.imm)
        instructions.append(
            WasmInstruction(
                offset=base_offset + offset,
                opcode=_normalize_opcode(instruction.op.mnemonic),
                operands=operands,
                size=instruction.len,
            )
        )
        offset += instruction.len
    return tuple(instructions)


_SIMD_OPCODES = {
    0x0C: "v128.const",
    0x0E: "i8x16.swizzle",
    0x11: "i32x4.splat",
    0x1B: "i32x4.extract_lane",
    0x37: "i32x4.eq",
    0x39: "i32x4.lt_s",
    0x52: "v128.bitselect",
    0xA1: "i32x4.neg",
    0xAE: "i32x4.add",
    0xB6: "i32x4.min_s",
    0xE4: "f32x4.add",
}

_POST_MVP_OPCODES = {
    0xC0: "i32.extend8_s",
    0xC1: "i32.extend16_s",
    0xC2: "i64.extend8_s",
    0xC3: "i64.extend16_s",
    0xC4: "i64.extend32_s",
    0xD1: "ref.is_null",
    0xD0: "ref.null",
    0xD2: "ref.func",
}


def _decode_prefixed_instruction(code: bytes, offset: int) -> tuple[tuple[str, tuple[str, ...]], int]:
    prefix = code[offset]
    subopcode, cursor = _read_uleb128(code, offset + 1)
    if prefix == 0xFC:
        opcode = {
            0x08: "memory.init",
            0x09: "data.drop",
            0x0A: "memory.copy",
            0x0B: "memory.fill",
        }.get(subopcode)
        if opcode is None:
            raise WasmDecodeError(f"unsupported wasm 0xfc subopcode {subopcode} at byte {offset}")
        immediate_count = 2 if subopcode in {0x08, 0x0A} else 1
        operands: list[str] = []
        for _ in range(immediate_count):
            value, cursor = _read_uleb128(code, cursor)
            operands.append(str(value))
        return (opcode, tuple(operands)), cursor - offset

    opcode = _SIMD_OPCODES.get(subopcode)
    if subopcode <= 0x0B:
        opcode = {
            0x00: "v128.load",
            0x0B: "v128.store",
        }.get(subopcode)
        if opcode is None:
            raise WasmDecodeError(f"unsupported wasm SIMD memory opcode {subopcode} at byte {offset}")
        flags, cursor = _read_uleb128(code, cursor)
        displacement, cursor = _read_uleb128(code, cursor)
        return (opcode, (str(flags), str(displacement))), cursor - offset
    if subopcode == 0x0D:
        values = code[cursor:cursor + 16]
        if len(values) != 16:
            raise WasmDecodeError(f"truncated wasm SIMD shuffle at byte {offset}")
        return ("i8x16.shuffle", (values.hex(),)), cursor + 16 - offset
    if subopcode == 0x0C:
        values = code[cursor:cursor + 16]
        if len(values) != 16:
            raise WasmDecodeError(f"truncated wasm v128.const at byte {offset}")
        return ("v128.const", (values.hex(),)), cursor + 16 - offset
    if 0x15 <= subopcode <= 0x22:
        if cursor >= len(code):
            raise WasmDecodeError(f"truncated wasm SIMD lane immediate at byte {offset}")
        return (opcode or f"simd.0x{subopcode:x}", (str(code[cursor]),)), cursor + 1 - offset
    if opcode is None:
        raise WasmDecodeError(f"unsupported wasm SIMD opcode {subopcode} at byte {offset}")
    return (opcode, ()), cursor - offset


def _read_uleb128(data: bytes, offset: int) -> tuple[int, int]:
    value = 0
    shift = 0
    cursor = offset
    while cursor < len(data):
        byte = data[cursor]
        cursor += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, cursor
        shift += 7
        if shift > 35:
            break
    raise WasmDecodeError(f"truncated wasm unsigned LEB128 at byte {offset}")


def _library_operands(imm) -> tuple[str, ...]:
    if imm is None:
        return ()
    fields: list[str] = []
    for name in (
        "local_index",
        "global_index",
        "function_index",
        "type_index",
        "relative_depth",
        "default_target",
        "value",
        "offset",
        "flags",
        "sig",
        "reserved",
    ):
        if hasattr(imm, name):
            fields.append(str(getattr(imm, name)))
    if hasattr(imm, "target_table"):
        fields.append(",".join(str(value) for value in getattr(imm, "target_table")))
    return tuple(fields)


def _normalize_opcode(opcode: str) -> str:
    return {
        "get_local": "local.get",
        "set_local": "local.set",
        "tee_local": "local.tee",
        "get_global": "global.get",
        "set_global": "global.set",
        "current_memory": "memory.size",
        "grow_memory": "memory.grow",
    }.get(opcode, opcode)


def _read_name(reader: "_Reader") -> str:
    return reader.read(reader.u32()).decode("utf-8", errors="replace")


def _skip_limits(reader: "_Reader") -> None:
    flags = reader.byte()
    reader.u32()
    if flags & 0x01:
        reader.u32()


def _skip_table_type(reader: "_Reader") -> None:
    reader.byte()
    _skip_limits(reader)


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @property
    def eof(self) -> bool:
        return self.offset >= len(self.data)

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise WasmDecodeError("truncated WebAssembly module")
        data = self.data[self.offset:end]
        self.offset = end
        return data

    def byte(self) -> int:
        return self.read(1)[0]

    def u32(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.byte()
            result |= (byte & 0x7F) << shift
            if byte & 0x80 == 0:
                return result
            shift += 7
