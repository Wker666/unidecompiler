from __future__ import annotations

import dis
import importlib.util
import marshal
import types
from dataclasses import dataclass

from unidecompiler.plugins import FrontendDecodeError


PYC_MIN_HEADER_SIZE = 16


class PycDecodeError(FrontendDecodeError):
    pass


@dataclass(frozen=True)
class PycInstruction:
    offset: int
    opname: str
    arg: int | None
    argval: object
    argrepr: str
    starts_line: int | None
    artifact_offset: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class PycExceptionRegion:
    """Decoded CPython exception-table entry expressed as bytecode offsets."""

    start: int
    end: int
    target: int
    depth: int
    lasti: bool


@dataclass(frozen=True)
class PycCodeObject:
    name: str
    argcount: int
    kwonlyargcount: int
    flags: int
    varnames: tuple[str, ...]
    cellvars: tuple[str, ...]
    freevars: tuple[str, ...]
    names: tuple[str, ...]
    consts: tuple[object, ...]
    instructions: tuple[PycInstruction, ...]
    exception_regions: tuple[PycExceptionRegion, ...]
    children: tuple["PycCodeObject", ...]
    artifact_code_offset: int | None = None


@dataclass(frozen=True)
class PycModule:
    magic: bytes
    flags: int
    code: PycCodeObject
    filename: str | None = None


def looks_like_pyc(data: bytes) -> bool:
    return len(data) > PYC_MIN_HEADER_SIZE and data[:4] == importlib.util.MAGIC_NUMBER and data[4:8] in {
        b"\x00\x00\x00\x00",
        b"\x01\x00\x00\x00",
        b"\x03\x00\x00\x00",
    }


def decode_pyc(data: bytes, filename: str | None = None) -> PycModule:
    if len(data) <= PYC_MIN_HEADER_SIZE:
        raise PycDecodeError("truncated pyc file")

    flags = int.from_bytes(data[4:8], "little")
    try:
        code = marshal.loads(data[PYC_MIN_HEADER_SIZE:])
    except Exception as error:  # marshal gives several low-level exceptions.
        raise PycDecodeError(f"failed to unmarshal pyc code object: {error}") from error

    if not isinstance(code, types.CodeType):
        raise PycDecodeError("pyc payload is not a code object")

    return PycModule(
        magic=data[:4],
        flags=flags,
        code=_decode_code_object(code, _marshal_code_offsets(data, code)),
        filename=filename,
    )


def _decode_code_object(code: types.CodeType, code_offsets: dict[int, int]) -> PycCodeObject:
    children = tuple(
        _decode_code_object(const, code_offsets)
        for const in code.co_consts
        if isinstance(const, types.CodeType)
    )
    instructions = tuple(dis.get_instructions(code))
    code_start = code_offsets.get(id(code))
    return PycCodeObject(
        name=code.co_name,
        argcount=code.co_argcount,
        kwonlyargcount=code.co_kwonlyargcount,
        flags=code.co_flags,
        varnames=tuple(code.co_varnames),
        cellvars=tuple(code.co_cellvars),
        freevars=tuple(code.co_freevars),
        names=tuple(code.co_names),
        consts=tuple(
            f"<code {const.co_name}>"
            if isinstance(const, types.CodeType)
            else const
            for const in code.co_consts
        ),
        instructions=tuple(
            PycInstruction(
                offset=instruction.offset,
                opname=instruction.opname,
                arg=instruction.arg,
                argval=instruction.argval,
                argrepr=instruction.argrepr,
                starts_line=instruction.starts_line,
                artifact_offset=None if code_start is None else code_start + instruction.offset,
                size=None if code_start is None else _instruction_size(instructions, index, len(code.co_code)),
            )
            for index, instruction in enumerate(instructions)
        ),
        exception_regions=tuple(
            PycExceptionRegion(
                start=entry.start,
                end=entry.end,
                target=entry.target,
                depth=entry.depth,
                lasti=entry.lasti,
            )
            for entry in dis.Bytecode(code).exception_entries
        ),
        children=children,
        artifact_code_offset=code_start,
    )


def _instruction_size(instructions: tuple[dis.Instruction, ...], index: int, code_size: int) -> int:
    next_offset = instructions[index + 1].offset if index + 1 < len(instructions) else code_size
    return next_offset - instructions[index].offset


def _marshal_code_offsets(data: bytes, root: types.CodeType) -> dict[int, int]:
    """Find only unambiguous marshal byte-string records for ``co_code`` values.

    CPython serializes code bytes as a marshal ``TYPE_STRING`` record.  The
    frontend verifies both that record's tag and its length, then accepts a
    location only when that code byte sequence has exactly one candidate.  A
    repeated or otherwise ambiguous value is deliberately left unmapped.
    """
    code_objects = tuple(_walk_code_objects(root))
    candidates: dict[bytes, list[int]] = {}
    for code in code_objects:
        payload = code.co_code
        if payload in candidates:
            continue
        candidates[payload] = _marshal_string_payload_offsets(data, payload)
    assigned: set[int] = set()
    offsets: dict[int, int] = {}
    for code in code_objects:
        matches = [offset for offset in candidates[code.co_code] if offset not in assigned]
        if len(matches) == 1:
            offsets[id(code)] = matches[0]
            assigned.add(matches[0])
    return offsets


def _walk_code_objects(code: types.CodeType):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from _walk_code_objects(value)


def _marshal_string_payload_offsets(data: bytes, payload: bytes) -> list[int]:
    if not payload:
        return []
    result: list[int] = []
    size = len(payload)
    for header in range(PYC_MIN_HEADER_SIZE, len(data) - size - 4):
        if (data[header] & 0x7F) != ord("s"):
            continue
        if int.from_bytes(data[header + 1 : header + 5], "little", signed=True) != size:
            continue
        start = header + 5
        if data[start : start + size] == payload:
            result.append(start)
    return result
