from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
import re
from typing import Protocol

from unidecompiler.plugins import FrontendDecodeError


LUA_SIGNATURE = b"\x1bLua"
LUA_51_VERSION = 0x51


class LuacDecodeError(FrontendDecodeError):
    pass


@dataclass(frozen=True)
class LuacHeader:
    version: int
    format: int
    little_endian: bool | None = None
    int_size: int | None = None
    size_t_size: int | None = None
    instruction_size: int | None = None
    lua_number_size: int | None = None
    integral_numbers: bool | None = None

    @property
    def version_label(self) -> str:
        major = self.version >> 4
        minor = self.version & 0x0F
        return f"{major}.{minor}"


@dataclass(frozen=True)
class LuaChunk:
    header: LuacHeader
    raw: bytes
    filename: str | None = None
    disassembly: str | None = None
    functions: tuple["LuaFunctionListing", ...] = ()
    decoder_id: str | None = None


@dataclass(frozen=True)
class LuaInstructionListing:
    pc: int
    line: int | None
    opcode: str
    operands: tuple[str, ...]
    comment: str | None = None
    artifact_offset: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class LuaLocalListing:
    slot: int
    name: str
    start_pc: int
    end_pc: int


@dataclass(frozen=True)
class LuaUpvalueListing:
    index: int
    name: str | None = None
    instack: int | None = None
    slot: int | None = None
    kind: int | None = None


@dataclass(frozen=True)
class LuaConstantListing:
    index: int
    kind: str
    value: object


@dataclass(frozen=True)
class LuaFunctionListing:
    kind: str
    source: str
    line_start: int | None
    line_end: int | None
    instruction_count: int
    param_count: int
    slot_count: int
    upvalue_count: int
    local_count: int
    constant_count: int
    child_function_count: int
    instructions: tuple[LuaInstructionListing, ...]
    constants: tuple[LuaConstantListing, ...]
    locals: tuple[LuaLocalListing, ...]
    upvalues: tuple[LuaUpvalueListing, ...] = ()
    inferred_name: str | None = None
    child_function_names: tuple[str, ...] = ()


class LuaChunkDecoder(Protocol):
    """Adapter seam for Lua bytecode decoding.

    Implementations may use third-party libraries, official Lua tools, or a
    small internal fallback. The Lua frontend plugin depends on this seam rather
    than on one concrete parser.
    """

    id: str

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        ...

    def decode(self, data: bytes, filename: str | None = None) -> LuaChunk:
        ...


def looks_like_luac(data: bytes) -> bool:
    return data.startswith(LUA_SIGNATURE)


def decode_lua51_header(data: bytes) -> LuacHeader:
    """Decode a Lua 5.1 chunk header.

    V1 intentionally pins Lua 5.1. Later frontends can add version-specific
    decoders without changing the frontend plugin seam.
    """

    if len(data) < 12:
        raise LuacDecodeError("truncated Lua chunk header")
    if not looks_like_luac(data):
        raise LuacDecodeError("missing Lua chunk signature")

    version = data[4]
    if version != LUA_51_VERSION:
        raise LuacDecodeError(
            f"unsupported Lua bytecode version 0x{version:02x}; V1 supports 0x51"
        )

    fmt = data[5]
    if fmt != 0:
        raise LuacDecodeError(f"unsupported Lua chunk format: {fmt}")

    endianness = data[6]
    if endianness not in (0, 1):
        raise LuacDecodeError(f"invalid Lua 5.1 endianness flag: {endianness}")

    return LuacHeader(
        version=version,
        format=fmt,
        little_endian=endianness == 1,
        int_size=data[7],
        size_t_size=data[8],
        instruction_size=data[9],
        lua_number_size=data[10],
        integral_numbers=data[11] == 1,
    )


def decode_lua_header(data: bytes) -> LuacHeader:
    if len(data) < 6:
        raise LuacDecodeError("truncated Lua chunk header")
    if not looks_like_luac(data):
        raise LuacDecodeError("missing Lua chunk signature")

    if data[4] == LUA_51_VERSION:
        return decode_lua51_header(data)

    return LuacHeader(version=data[4], format=data[5])


def decode_lua51_chunk(data: bytes, filename: str | None = None) -> LuaChunk:
    return LuaChunk(header=decode_lua51_header(data), raw=data, filename=filename)


FUNCTION_HEADER_RE = re.compile(
    r"^(main|function) <(?P<source>.*):(?P<start>\d+),(?P<end>\d+)> "
    r"\((?P<instructions>\d+) instructions at .*\)$"
)
FUNCTION_STATS_RE = re.compile(
    r"^(?P<params>\d+)\+? params?, (?P<slots>\d+) slots, "
    r"(?P<upvalues>\d+) upvalues?, (?P<locals>\d+) locals?, "
    r"(?P<constants>\d+) constants?, (?P<functions>\d+) functions?$"
)
INSTRUCTION_RE = re.compile(
    r"^\s*(?P<pc>\d+)\s+\[(?P<line>-|\d+)\]\s+"
    r"(?P<opcode>[A-Z0-9_]+)\s*(?P<rest>.*)$"
)
LOCAL_RE = re.compile(
    r"^\s*(?P<slot>\d+)\s+(?P<name>\S+)\s+"
    r"(?P<start>\d+)\s+(?P<end>\d+)\s*$"
)
CONSTANT_RE = re.compile(
    r"^\s*(?P<index>\d+)\s+(?P<kind>\S+)\s*(?P<value>.*)$"
)


def parse_luac_listing(disassembly: str) -> tuple[LuaFunctionListing, ...]:
    """Parse the stable parts of ``luac -l -l`` output.

    The official listing format is not a formal interchange format, so this
    parser intentionally extracts only the pieces the first lifter needs:
    function headers, instructions, and local-variable tables.
    """

    lines = disassembly.splitlines()
    functions: list[LuaFunctionListing] = []
    index = 0

    while index < len(lines):
        header_match = FUNCTION_HEADER_RE.match(lines[index].strip())
        if header_match is None:
            index += 1
            continue

        kind = lines[index].strip().split(" ", 1)[0]
        index += 1
        if index >= len(lines):
            break

        stats_match = FUNCTION_STATS_RE.match(lines[index].strip())
        if stats_match is None:
            index += 1
            continue

        index += 1
        instructions: list[LuaInstructionListing] = []
        constants: list[LuaConstantListing] = []
        locals_: list[LuaLocalListing] = []
        upvalues: list[LuaUpvalueListing] = []

        while index < len(lines):
            line = lines[index]
            if FUNCTION_HEADER_RE.match(line.strip()):
                break

            instruction_match = INSTRUCTION_RE.match(line)
            if instruction_match is not None:
                rest = instruction_match.group("rest").strip()
                operands_text, _, comment_text = rest.partition(";")
                instructions.append(
                    LuaInstructionListing(
                        pc=int(instruction_match.group("pc")),
                        line=(
                            None
                            if instruction_match.group("line") == "-"
                            else int(instruction_match.group("line"))
                        ),
                        opcode=instruction_match.group("opcode"),
                        operands=tuple(operands_text.split()),
                        comment=comment_text.strip() or None,
                    )
                )
                index += 1
                continue

            if line.strip().startswith("locals "):
                index += 1
                while index < len(lines):
                    local_match = LOCAL_RE.match(lines[index])
                    if local_match is None:
                        break
                    locals_.append(
                        LuaLocalListing(
                            slot=int(local_match.group("slot")),
                            name=local_match.group("name"),
                            start_pc=int(local_match.group("start")),
                            end_pc=int(local_match.group("end")),
                        )
                    )
                    index += 1
                continue

            if line.strip().startswith("constants "):
                index += 1
                while index < len(lines):
                    constant_match = CONSTANT_RE.match(lines[index])
                    if constant_match is None:
                        break
                    constants.append(
                        LuaConstantListing(
                            index=int(constant_match.group("index")),
                            kind=constant_match.group("kind"),
                            value=_parse_lua_constant(
                                constant_match.group("kind"),
                                constant_match.group("value").strip(),
                            ),
                        )
                    )
                    index += 1
                continue

            if line.strip().startswith("upvalues "):
                index += 1
                upvalue_index = 0
                while index < len(lines):
                    parts = lines[index].split()
                    if len(parts) < 4 or not parts[0].isdigit():
                        break
                    upvalues.append(
                        LuaUpvalueListing(
                            index=int(parts[0]),
                            name=parts[1],
                            instack=_parse_int(parts[2]),
                            slot=_parse_int(parts[3]),
                        )
                    )
                    upvalue_index += 1
                    index += 1
                continue

            index += 1

        functions.append(
            LuaFunctionListing(
                kind=kind,
                source=header_match.group("source"),
                line_start=int(header_match.group("start")),
                line_end=int(header_match.group("end")),
                instruction_count=int(header_match.group("instructions")),
                param_count=int(stats_match.group("params")),
                slot_count=int(stats_match.group("slots")),
                upvalue_count=int(stats_match.group("upvalues")),
                local_count=int(stats_match.group("locals")),
                constant_count=int(stats_match.group("constants")),
                child_function_count=int(stats_match.group("functions")),
                instructions=tuple(instructions),
                constants=tuple(constants),
                locals=tuple(locals_),
                upvalues=tuple(upvalues),
            )
        )

    return _infer_function_names(tuple(functions))


def _infer_function_names(
    functions: tuple[LuaFunctionListing, ...],
) -> tuple[LuaFunctionListing, ...]:
    if not functions:
        return functions

    names: list[str | None] = [None for _ in functions]
    child_function_names: list[list[str]] = [[] for _ in functions]
    names[0] = "<chunk>"
    child_index = 1

    for parent_index, parent in enumerate(functions):
        for instruction in parent.instructions:
            if instruction.opcode != "CLOSURE" or len(instruction.operands) < 1:
                continue
            if child_index >= len(functions):
                break
            register = _parse_int(instruction.operands[0])
            if register is None:
                child_index += 1
                continue
            local_name = _local_name_for_register(
                parent.locals,
                register=register,
                pc=instruction.pc + 1,
            )
            if local_name is not None:
                names[child_index] = local_name
            child_function_names[parent_index].append(names[child_index] or f"<function_{child_index}>")
            child_index += 1

    return tuple(
        LuaFunctionListing(
            kind=function.kind,
            source=function.source,
            line_start=function.line_start,
            line_end=function.line_end,
            instruction_count=function.instruction_count,
            param_count=function.param_count,
            slot_count=function.slot_count,
            upvalue_count=function.upvalue_count,
            local_count=function.local_count,
            constant_count=function.constant_count,
            child_function_count=function.child_function_count,
            instructions=function.instructions,
            constants=function.constants,
            locals=function.locals,
            upvalues=function.upvalues,
            inferred_name=names[index] or f"<function_{index}>",
            child_function_names=tuple(child_function_names[index]),
        )
        for index, function in enumerate(functions)
    )


def _local_name_for_register(
    locals_: tuple[LuaLocalListing, ...],
    register: int,
    pc: int,
) -> str | None:
    for local in locals_:
        if local.slot == register and local.start_pc <= pc <= local.end_pc:
            return local.name
    return None


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _parse_lua_constant(kind: str, value: str) -> object:
    if kind == "I":
        return int(value)
    if kind == "F":
        return float(value)
    if kind == "S":
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            return value[1:-1]
        return value
    if kind == "b":
        return value.lower() == "true"
    if kind == "N":
        return None
    return value


class HeaderOnlyLuaChunkDecoder:
    id = "lua-header-only"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_luac(data)

    def decode(self, data: bytes, filename: str | None = None) -> LuaChunk:
        return LuaChunk(
            header=decode_lua_header(data),
            raw=data,
            filename=filename,
            decoder_id=self.id,
        )


class LuacToolChunkDecoder:
    """Decode/disassemble chunks through the official ``luac`` executable."""

    id = "luac-tool"

    def __init__(self, luac_path: str = "luac") -> None:
        self.luac_path = luac_path

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_luac(data) and shutil.which(self.luac_path) is not None

    def decode(self, data: bytes, filename: str | None = None) -> LuaChunk:
        header = decode_lua_header(data)
        luac = shutil.which(self.luac_path)
        if luac is None:
            raise LuacDecodeError(f"luac executable not found: {self.luac_path}")

        with tempfile.TemporaryDirectory(prefix="unidecompiler-luac-") as temp_dir:
            chunk_path = Path(temp_dir) / "input.luac"
            chunk_path.write_bytes(data)
            result = subprocess.run(
                [luac, "-l", "-l", chunk_path.name],
                check=False,
                capture_output=True,
                text=True,
                cwd=temp_dir,
            )

        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise LuacDecodeError(f"luac failed to decode chunk: {detail}")

        return LuaChunk(
            header=header,
            raw=data,
            filename=filename,
            disassembly=result.stdout,
            functions=parse_luac_listing(result.stdout),
            decoder_id=self.id,
        )


class LuaBytecodeLibraryChunkDecoder:
    """Adapter seam for importable Lua bytecode/chunk libraries.

    This intentionally does not implement a binary chunk parser itself.  The
    project policy is that bytecode container parsing belongs to a maintained
    frontend dependency/adapter, not to the generic pipeline.  At the time this
    adapter was added, no reliable Python package exposing Lua 5.4 chunk
    functions/instructions/constants/locals was available in the environment,
    so the adapter reports unavailable instead of pretending that source-level
    parsers can decode luac bytecode.
    """

    id = "lua-bytecode-library-unavailable"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_luac(data) and self._available_backend_id() is not None

    def decode(self, data: bytes, filename: str | None = None) -> LuaChunk:
        backend_id = self._available_backend_id()
        if backend_id is None:
            raise LuacDecodeError(
                "no supported importable Lua 5.4 bytecode parser is available"
            )

        raise LuacDecodeError(
            f"Lua bytecode library backend {backend_id!r} is not wired to the "
            "LuaFunctionListing adapter yet"
        )

    def _available_backend_id(self) -> str | None:
        return None


class PreferredLuaChunkDecoder:
    """Try importable library decoders before safe internal fallbacks.

    The default path must not shell out to ``luac``.  ``LuacToolChunkDecoder`` is
    intentionally excluded here and may only be used by explicitly injecting it
    into ``LuaFrontendPlugin`` or this preferred decoder.
    """

    id = "lua-library-preferred"

    def __init__(self, decoders: tuple[LuaChunkDecoder, ...] | None = None) -> None:
        if decoders is None:
            from unidecompiler_plugin_lua.plugin import Lua54BinaryChunkDecoder

            decoders = (
                Lua54BinaryChunkDecoder(),
                LuaBytecodeLibraryChunkDecoder(),
                HeaderOnlyLuaChunkDecoder(),
            )
        self.decoders = decoders

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return any(decoder.can_decode(data, filename) for decoder in self.decoders)

    def decode(self, data: bytes, filename: str | None = None) -> LuaChunk:
        errors: list[str] = []
        for decoder in self.decoders:
            if not decoder.can_decode(data, filename):
                continue
            try:
                chunk = decoder.decode(data, filename)
                if chunk.decoder_id is None:
                    return replace(chunk, decoder_id=decoder.id)
                return chunk
            except LuacDecodeError as error:
                errors.append(f"{decoder.id}: {error}")

        if errors:
            raise LuacDecodeError("; ".join(errors))
        raise LuacDecodeError("no Lua decoder can decode this input")
