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
        code=_decode_code_object(code),
        filename=filename,
    )


def _decode_code_object(code: types.CodeType) -> PycCodeObject:
    children = tuple(
        _decode_code_object(const)
        for const in code.co_consts
        if isinstance(const, types.CodeType)
    )
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
            )
            for instruction in dis.get_instructions(code)
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
    )
