from __future__ import annotations

from dataclasses import dataclass
import struct

from unidecompiler_plugin_lua.luac import (
    LuaChunk,
    LuaConstantListing,
    LuacDecodeError,
    LuacHeader,
    LuaFunctionListing,
    LuaInstructionListing,
    LuaLocalListing,
    LuaUpvalueListing,
)


LUA_SIGNATURE = b"\x1bLua"
LUA_54_VERSION = 0x54
LUAC_DATA = b"\x19\x93\r\n\x1a\n"
LUAC_INT = 0x5678
LUAC_NUM = 370.5


class Lua54ChunkError(LuacDecodeError):
    pass


@dataclass(frozen=True)
class _ParsedFunction:
    kind: str
    source: str
    line_start: int
    line_end: int
    param_count: int
    slot_count: int
    instructions: tuple[LuaInstructionListing, ...]
    constants: tuple[LuaConstantListing, ...]
    locals: tuple[LuaLocalListing, ...]
    upvalues: tuple[LuaUpvalueListing, ...]
    child_count: int
    children: tuple["_ParsedFunction", ...]
    inferred_name: str | None = None
    child_function_names: tuple[str, ...] = ()


class _Reader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read(self, size: int) -> bytes:
        end = self.offset + size
        if end > len(self.data):
            raise Lua54ChunkError("truncated chunk")
        result = self.data[self.offset:end]
        self.offset = end
        return result

    def byte(self) -> int:
        return self.read(1)[0]

    def varint(self) -> int:
        value = 0
        while True:
            byte = self.byte()
            value = (value << 7) | (byte & 0x7F)
            if byte & 0x80:
                return value

    def unpack(self, fmt: str):
        fmt = "<" + fmt
        return struct.unpack(fmt, self.read(struct.calcsize(fmt)))[0]

    def lua_integer(self, size: int) -> int:
        if size == 8:
            return int(self.unpack("q"))
        if size == 4:
            return int(self.unpack("i"))
        raise Lua54ChunkError(f"unsupported lua integer size: {size}")

    def lua_number(self, size: int) -> float:
        if size == 8:
            return float(self.unpack("d"))
        if size == 4:
            return float(self.unpack("f"))
        raise Lua54ChunkError(f"unsupported lua number size: {size}")


_OPCODES = (
    "MOVE", "LOADI", "LOADF", "LOADK", "LOADKX", "LOADFALSE", "LFALSESKIP", "LOADTRUE",
    "LOADNIL", "GETUPVAL", "SETUPVAL", "GETTABUP", "GETTABLE", "GETI", "GETFIELD",
    "SETTABUP", "SETTABLE", "SETI", "SETFIELD", "NEWTABLE", "SELF", "ADDI", "ADDK",
    "SUBK", "MULK", "MODK", "POWK", "DIVK", "IDIVK", "BANDK", "BORK", "BXORK",
    "SHRI", "SHLI", "ADD", "SUB", "MUL", "MOD", "POW", "DIV", "IDIV", "BAND",
    "BOR", "BXOR", "SHL", "SHR", "MMBIN", "MMBINI", "MMBINK", "UNM", "BNOT", "NOT",
    "LEN", "CONCAT", "CLOSE", "TBC", "JMP", "EQ", "LT", "LE", "EQK", "EQI", "LTI",
    "LEI", "GTI", "GEI", "TEST", "TESTSET", "CALL", "TAILCALL", "RETURN", "RETURN0",
    "RETURN1", "FORLOOP", "FORPREP", "TFORPREP", "TFORCALL", "TFORLOOP", "SETLIST",
    "CLOSURE", "VARARG", "VARARGPREP", "EXTRAARG",
)


def decode_lua54_chunk(data: bytes, filename: str | None = None) -> LuaChunk:
    reader = _Reader(data)
    header = _read_header(reader)
    reader.byte()
    parsed = _read_function(reader, parent_source=None, kind="main")
    functions = _flatten_and_infer(parsed)
    return LuaChunk(
        header=header,
        raw=data,
        filename=filename,
        functions=tuple(_to_listing(function) for function in functions),
        decoder_id="lua54-binary",
    )


def _read_header(reader: _Reader) -> LuacHeader:
    if reader.read(4) != LUA_SIGNATURE:
        raise Lua54ChunkError("missing Lua chunk signature")
    version = reader.byte()
    if version != LUA_54_VERSION:
        raise Lua54ChunkError(f"unsupported Lua bytecode version 0x{version:02x}")
    fmt = reader.byte()
    if fmt != 0:
        raise Lua54ChunkError(f"unsupported Lua chunk format: {fmt}")
    if reader.read(6) != LUAC_DATA:
        raise Lua54ChunkError("corrupted Lua 5.4 chunk data")
    instruction_size = reader.byte()
    integer_size = reader.byte()
    number_size = reader.byte()
    if reader.lua_integer(integer_size) != LUAC_INT:
        raise Lua54ChunkError("integer format mismatch")
    if reader.lua_number(number_size) != LUAC_NUM:
        raise Lua54ChunkError("number format mismatch")
    return LuacHeader(
        version=version,
        format=fmt,
        little_endian=True,
        int_size=integer_size,
        size_t_size=None,
        instruction_size=instruction_size,
        lua_number_size=number_size,
        integral_numbers=False,
    )


def _read_function(reader: _Reader, parent_source: str | None, kind: str) -> _ParsedFunction:
    source = _read_string(reader) or parent_source or "<chunk>"
    line_start = reader.varint()
    line_end = reader.varint()
    param_count = reader.byte()
    reader.byte()
    slot_count = reader.byte()
    instructions = _read_code(reader)
    constants = _read_constants(reader)
    upvalue_descriptors = _read_upvalues(reader)
    children = _read_children(reader, source)
    line_info, locals_, upvalue_names = _read_debug(reader)
    locals_ = _assign_local_register_slots(locals_, tuple(instructions), param_count)
    upvalues = tuple(
        LuaUpvalueListing(
            index=index,
            name=upvalue_names[index] if index < len(upvalue_names) else None,
            instack=descriptor[0],
            slot=descriptor[1],
            kind=descriptor[2],
        )
        for index, descriptor in enumerate(upvalue_descriptors)
    )
    return _ParsedFunction(
        kind=kind,
        source=source,
        line_start=line_start,
        line_end=line_end,
        param_count=param_count,
        slot_count=slot_count,
        instructions=tuple(
            LuaInstructionListing(
                pc=instruction.pc,
                line=_line_for_pc(line_start, line_info, instruction.pc),
                opcode=instruction.opcode,
                operands=instruction.operands,
                comment=_target_comment(instruction),
            )
            for instruction in instructions
        ),
        constants=constants,
        locals=locals_,
        upvalues=upvalues,
        child_count=len(children),
        children=children,
    )


def _read_code(reader: _Reader) -> tuple[LuaInstructionListing, ...]:
    count = reader.varint()
    instructions: list[LuaInstructionListing] = []
    for pc in range(1, count + 1):
        raw = reader.unpack("I")
        opcode = _OPCODES[raw & 0x7F]
        instructions.append(
            LuaInstructionListing(
                pc=pc,
                line=None,
                opcode=opcode,
                operands=_decode_operands(opcode, raw),
            )
        )
    return tuple(instructions)


def _decode_operands(opcode: str, raw: int) -> tuple[str, ...]:
    a = (raw >> 7) & 0xFF
    k = (raw >> 15) & 0x1
    b = (raw >> 16) & 0xFF
    c = (raw >> 24) & 0xFF
    sc = c - 127
    sb = b - 127
    bx = (raw >> 15) & 0x1FFFF
    sx = bx - ((1 << 16) - 1)
    sj = ((raw >> 7) & 0x1FFFFFF) - ((1 << 24) - 1)
    if opcode in {"LOADI", "LOADF"}:
        return (str(a), str(sx))
    if opcode in {"FORLOOP", "FORPREP", "TFORPREP"}:
        return (str(a), str(bx))
    if opcode == "JMP":
        return (str(sj),)
    if opcode in {"RETURN0"}:
        return ()
    if opcode in {"RETURN1", "LOADFALSE", "LFALSESKIP", "LOADTRUE", "VARARGPREP", "TBC", "CLOSE"}:
        return (str(a),)
    if opcode in {"LOADK", "CLOSURE"}:
        return (str(a), str(bx))
    if opcode in {"ADDI", "SHRI", "SHLI"}:
        return (str(a), str(b), str(sc))
    if opcode in {"EQI", "LTI", "LEI", "GTI", "GEI"}:
        return (str(a), str(sb), str(k))
    if opcode in {"EQ", "LT", "LE", "EQK"}:
        # These comparison opcodes use the iABCk layout. The final operand
        # is the k flag, not the ordinary C field.
        return (str(a), str(b), str(k))
    if opcode == "TEST":
        return (str(a), str(k))
    if opcode == "TESTSET":
        return (str(a), str(b), str(k))
    if opcode == "CONCAT":
        return (str(a), str(b))
    if opcode == "MMBINI":
        return (str(a), str(sb), str(c), str(k))
    if opcode in {"MMBINK", "NEWTABLE"}:
        return (str(a), str(b), str(c), str(k))
    if opcode in {"SETTABLE", "SETI", "SETFIELD"}:
        return (str(a), str(b), f"{c}k" if k else str(c))
    return tuple(str(part) for part in (a, b, c))


def _read_constants(reader: _Reader) -> tuple[LuaConstantListing, ...]:
    count = reader.varint()
    constants: list[LuaConstantListing] = []
    for index in range(count):
        tag = reader.byte()
        if tag == 0:
            constants.append(LuaConstantListing(index=index, kind="N", value=None))
        elif tag == 1:
            constants.append(LuaConstantListing(index=index, kind="b", value=False))
        elif tag == 17:
            constants.append(LuaConstantListing(index=index, kind="b", value=True))
        elif tag == 3:
            constants.append(LuaConstantListing(index=index, kind="I", value=reader.lua_integer(8)))
        elif tag == 19:
            constants.append(LuaConstantListing(index=index, kind="F", value=reader.lua_number(8)))
        elif tag in {4, 20}:
            constants.append(LuaConstantListing(index=index, kind="S", value=_read_string(reader)))
        else:
            raise Lua54ChunkError(f"unsupported Lua 5.4 constant tag: {tag}")
    return tuple(constants)


def _read_upvalues(reader: _Reader) -> tuple[tuple[int, int, int], ...]:
    return tuple((reader.byte(), reader.byte(), reader.byte()) for _ in range(reader.varint()))


def _read_children(reader: _Reader, source: str) -> tuple[_ParsedFunction, ...]:
    return tuple(_read_function(reader, source, "function") for _ in range(reader.varint()))


def _read_debug(
    reader: _Reader,
) -> tuple[tuple[int, ...], tuple[LuaLocalListing, ...], tuple[str | None, ...]]:
    line_info = tuple(_signed_byte(reader.byte()) for _ in range(reader.varint()))
    abs_count = reader.varint()
    for _ in range(abs_count):
        reader.varint()
        reader.varint()
    locals_: list[LuaLocalListing] = []
    for slot in range(reader.varint()):
        locals_.append(
            LuaLocalListing(
                slot=slot,
                name=_read_string(reader) or f"local{slot}",
                start_pc=reader.varint() + 1,
                end_pc=reader.varint() + 1,
            )
        )
    upvalue_names = tuple(_read_string(reader) for _ in range(reader.varint()))
    return line_info, tuple(locals_), upvalue_names


def _assign_local_register_slots(
    locals_: tuple[LuaLocalListing, ...],
    instructions: tuple[LuaInstructionListing, ...],
    param_count: int,
) -> tuple[LuaLocalListing, ...]:
    assigned: list[LuaLocalListing] = []
    for index, local in enumerate(locals_):
        slot = _infer_local_register_slot(local, locals_[:index], instructions, param_count, index)
        assigned.append(
            LuaLocalListing(
                slot=slot,
                name=local.name,
                start_pc=local.start_pc,
                end_pc=local.end_pc,
            )
        )
    return tuple(assigned)


def _infer_local_register_slot(
    local: LuaLocalListing,
    previous_locals: tuple[LuaLocalListing, ...],
    instructions: tuple[LuaInstructionListing, ...],
    param_count: int,
    ordinal: int,
) -> int:
    if local.start_pc <= 1 and ordinal < param_count:
        return ordinal

    current = _instruction_at_pc(instructions, local.start_pc)
    if current is not None and current.opcode == "FORPREP" and current.operands:
        same_for_state = sum(
            1
            for previous in previous_locals
            if previous.start_pc == local.start_pc and previous.name == "(for state)"
        )
        if local.name == "(for state)" and same_for_state < 3:
            return int(current.operands[0]) + same_for_state

    prior = _instruction_at_pc(instructions, local.start_pc - 1)
    if prior is not None and prior.opcode == "FORPREP" and prior.operands and local.name != "(for state)":
        same_visible = sum(
            1
            for previous in previous_locals
            if previous.start_pc == local.start_pc and previous.name != "(for state)"
        )
        return int(prior.operands[0]) + 3 + same_visible

    write = _nearest_prior_register_write(local.start_pc, instructions)
    if write is not None:
        base, width = write
        same_start = sum(1 for previous in previous_locals if previous.start_pc == local.start_pc)
        if same_start < width:
            return base + same_start
        return base

    return ordinal


def _instruction_at_pc(
    instructions: tuple[LuaInstructionListing, ...],
    pc: int,
) -> LuaInstructionListing | None:
    for instruction in instructions:
        if instruction.pc == pc:
            return instruction
    return None


def _nearest_prior_register_write(
    start_pc: int,
    instructions: tuple[LuaInstructionListing, ...],
) -> tuple[int, int] | None:
    for instruction in reversed(tuple(instruction for instruction in instructions if instruction.pc < start_pc)):
        write = _register_write(instruction)
        if write is not None:
            return write
    return None


def _register_write(instruction: LuaInstructionListing) -> tuple[int, int] | None:
    if not instruction.operands or not instruction.operands[0].lstrip("-").isdigit():
        return None
    base = int(instruction.operands[0])
    if instruction.opcode == "LOADNIL" and len(instruction.operands) >= 2:
        return base, int(instruction.operands[1]) + 1
    if instruction.opcode == "CALL" and len(instruction.operands) >= 3:
        count = int(instruction.operands[2]) - 1
        return base, max(1, count)
    if instruction.opcode == "SELF":
        return base, 2
    if instruction.opcode == "VARARG" and len(instruction.operands) >= 2:
        count = int(instruction.operands[1]) - 1
        return base, max(1, count)
    if instruction.opcode in _REGISTER_DEST_OPS:
        return base, 1
    return None


_REGISTER_DEST_OPS = frozenset(
    {
        "ADDI",
        "ADDK",
        "ADD",
        "BAND",
        "BANDK",
        "BNOT",
        "BOR",
        "BORK",
        "BXOR",
        "BXORK",
        "CLOSURE",
        "CONCAT",
        "DIV",
        "DIVK",
        "GETFIELD",
        "GETI",
        "GETTABLE",
        "GETTABUP",
        "GETUPVAL",
        "IDIV",
        "IDIVK",
        "LEN",
        "LOADFALSE",
        "LOADF",
        "LOADI",
        "LOADK",
        "LOADKX",
        "LOADTRUE",
        "MOD",
        "MODK",
        "MUL",
        "MULK",
        "NEWTABLE",
        "NOT",
        "POW",
        "POWK",
        "SHL",
        "SHLI",
        "SHR",
        "SHRI",
        "SUB",
        "SUBK",
        "UNM",
    }
)


def _read_string(reader: _Reader) -> str | None:
    size = reader.varint()
    if size == 0:
        return None
    return reader.read(size - 1).decode("utf-8", errors="replace")


def _flatten_and_infer(root: _ParsedFunction) -> tuple[_ParsedFunction, ...]:
    functions: list[_ParsedFunction] = []

    def visit(function: _ParsedFunction, inferred_name: str | None) -> None:
        named = _replace_name(function, inferred_name)
        parent_index = len(functions)
        functions.append(named)
        child_names = _child_names(named)
        final_child_names: list[str] = []
        for index, child in enumerate(named.children):
            child_name = child_names.get(index) or f"<function_{len(functions)}>"
            final_child_names.append(child_name)
            visit(child, child_name)
        functions[parent_index] = _replace_child_function_names(
            named,
            tuple(final_child_names),
        )

    visit(root, "<chunk>")
    return tuple(functions)


def _replace_name(function: _ParsedFunction, name: str | None) -> _ParsedFunction:
    return _ParsedFunction(
        kind=function.kind,
        source=function.source,
        line_start=function.line_start,
        line_end=function.line_end,
        param_count=function.param_count,
        slot_count=function.slot_count,
        instructions=function.instructions,
        constants=function.constants,
        locals=function.locals,
        upvalues=function.upvalues,
        child_count=function.child_count,
        children=function.children,
        inferred_name=name,
        child_function_names=function.child_function_names,
    )


def _replace_child_function_names(function: _ParsedFunction, names: tuple[str, ...]) -> _ParsedFunction:
    return _ParsedFunction(
        kind=function.kind,
        source=function.source,
        line_start=function.line_start,
        line_end=function.line_end,
        param_count=function.param_count,
        slot_count=function.slot_count,
        instructions=function.instructions,
        constants=function.constants,
        locals=function.locals,
        upvalues=function.upvalues,
        child_count=function.child_count,
        children=function.children,
        inferred_name=function.inferred_name,
        child_function_names=names,
    )


def _child_names(function: _ParsedFunction) -> dict[int, str]:
    names: dict[int, str] = {}
    for instruction in function.instructions:
        if instruction.opcode != "CLOSURE" or len(instruction.operands) < 2:
            continue
        register = int(instruction.operands[0])
        child_index = int(instruction.operands[1])
        local = _local_for_register(function.locals, register, instruction.pc + 1)
        if local is not None:
            names[child_index] = local.name
    return names


def _to_listing(function: _ParsedFunction) -> LuaFunctionListing:
    return LuaFunctionListing(
        kind=function.kind,
        source=function.source,
        line_start=function.line_start,
        line_end=function.line_end,
        instruction_count=len(function.instructions),
        param_count=function.param_count,
        slot_count=function.slot_count,
        upvalue_count=len(function.upvalues),
        local_count=len(function.locals),
        constant_count=len(function.constants),
        child_function_count=function.child_count,
        instructions=function.instructions,
        constants=function.constants,
        locals=function.locals,
        upvalues=function.upvalues,
        inferred_name=function.inferred_name,
        child_function_names=function.child_function_names,
    )


def _local_for_register(locals_: tuple[LuaLocalListing, ...], register: int, pc: int) -> LuaLocalListing | None:
    for local in locals_:
        if local.slot == register and local.start_pc <= pc < local.end_pc:
            return local
    return None


def _line_for_pc(line_start: int, line_info: tuple[int, ...], pc: int) -> int | None:
    if 1 <= pc <= len(line_info):
        return line_start + sum(line_info[:pc])
    return None


def _target_comment(instruction: LuaInstructionListing) -> str | None:
    if instruction.opcode not in {"JMP", "FORLOOP", "FORPREP", "TFORPREP"} or not instruction.operands:
        return None
    try:
        offset = int(instruction.operands[-1])
    except ValueError:
        return None
    if instruction.opcode == "FORLOOP":
        return f"to {instruction.pc + 1 - offset}"
    return f"to {instruction.pc + 1 + offset}"


def _signed_byte(value: int) -> int:
    return value - 256 if value > 127 else value
