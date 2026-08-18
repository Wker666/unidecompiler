from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Protocol

from unidecompiler.plugins import FrontendDecodeError


PE_MAGIC = b"MZ"


class DotNetDecodeError(FrontendDecodeError):
    pass


@dataclass(frozen=True)
class DotNetInstruction:
    offset: int
    opcode: str
    operands: str = ""
    token: int | None = None
    operand_kind: str | None = None
    member_name: str | None = None
    owner_name: str | None = None
    arg_count: int | None = None
    returns_void: bool | None = None
    is_static: bool | None = None


@dataclass(frozen=True)
class DotNetMethodListing:
    name: str
    token: int
    rva: int
    is_static: bool = False
    param_count: int = 0
    max_stack: int | None = None
    code_size: int = 0
    instructions: tuple[DotNetInstruction, ...] = ()


@dataclass(frozen=True)
class DotNetAssembly:
    name: str
    filename: str | None = None
    methods: tuple[DotNetMethodListing, ...] = ()
    decoder_id: str | None = None


class DotNetAssemblyDecoder(Protocol):
    id: str

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        ...

    def decode(self, data: bytes, filename: str | None = None) -> DotNetAssembly:
        ...


def looks_like_dotnet(data: bytes) -> bool:
    if not data.startswith(PE_MAGIC):
        return False
    try:
        import dnfile

        pe = dnfile.dnPE(data=data)
        return pe.net is not None
    except Exception:
        return False


class DnfileAssemblyDecoder:
    id = "dnfile"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_dotnet(data) and _dnfile_module() is not None

    def decode(self, data: bytes, filename: str | None = None) -> DotNetAssembly:
        dnfile = _dnfile_module()
        if dnfile is None:
            raise DotNetDecodeError("dnfile is not installed")
        try:
            pe = dnfile.dnPE(data=data)
        except Exception as error:  # pragma: no cover - parser owns details.
            raise DotNetDecodeError(f"dnfile failed to decode assembly: {error}") from error
        if pe.net is None:
            raise DotNetDecodeError("missing .NET metadata")
        return DotNetAssembly(
            name=_assembly_name(pe, filename),
            filename=filename,
            methods=tuple(_method_listing(pe, row_index, row) for row_index, row in enumerate(_method_rows(pe), start=1)),
            decoder_id=self.id,
        )


def _dnfile_module():
    try:
        import dnfile
    except ImportError:
        return None
    return dnfile


def _method_rows(pe) -> tuple:
    tables = getattr(pe.net, "mdtables", None)
    method_def = getattr(tables, "MethodDef", None)
    return tuple(getattr(method_def, "rows", ()) or ())


def _assembly_name(pe, filename: str | None) -> str:
    tables = getattr(pe.net, "mdtables", None)
    assembly = getattr(tables, "Assembly", None)
    rows = getattr(assembly, "rows", ()) or ()
    if rows:
        name = _heap_text(rows[0].Name)
        if name:
            return name
    if filename:
        return filename
    return "<dotnet-assembly>"


def _method_listing(pe, row_index: int, row) -> DotNetMethodListing:
    rva = int(getattr(row, "Rva", 0) or 0)
    body = _read_method_body(pe, rva) if rva else None
    return DotNetMethodListing(
        name=_heap_text(row.Name) or f"<method_{row_index}>",
        token=0x06000000 | row_index,
        rva=rva,
        is_static=bool(getattr(row.Flags, "mdStatic", False)),
        param_count=_signature_param_count(row.Signature),
        max_stack=None if body is None else body.max_stack,
        code_size=0 if body is None else len(body.code),
        instructions=() if body is None else _decode_il(pe, body.code),
    )


@dataclass(frozen=True)
class _MethodBody:
    max_stack: int
    code: bytes


def _read_method_body(pe, rva: int) -> _MethodBody | None:
    data = pe.get_data(rva, 16)
    if not data:
        return None
    first = data[0]
    kind = first & 0x3
    if kind == 0x2:
        code_size = first >> 2
        return _MethodBody(max_stack=8, code=pe.get_data(rva + 1, code_size))
    if kind == 0x3:
        header = pe.get_data(rva, 12)
        if len(header) < 12:
            return None
        _flags_size, max_stack, code_size, _local_sig = struct.unpack_from("<HHII", header, 0)
        header_size = (_flags_size >> 12) * 4
        if header_size < 12:
            return None
        return _MethodBody(max_stack=max_stack, code=pe.get_data(rva + header_size, code_size))
    return None


def _heap_text(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = getattr(value, "value", None)
    return text if isinstance(text, str) else None


def _signature_param_count(signature) -> int:
    data = bytes(getattr(signature, "value", b"") or b"")
    if not data:
        return 0
    reader = _BlobReader(data)
    calling_convention = reader.byte()
    if calling_convention & 0x10:
        reader.compressed_uint()
    if reader.eof:
        return 0
    return reader.compressed_uint()


class _BlobReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    @property
    def eof(self) -> bool:
        return self.offset >= len(self.data)

    def byte(self) -> int:
        if self.eof:
            return 0
        value = self.data[self.offset]
        self.offset += 1
        return value

    def compressed_uint(self) -> int:
        first = self.byte()
        if first & 0x80 == 0:
            return first
        if first & 0xC0 == 0x80:
            return ((first & 0x3F) << 8) | self.byte()
        return ((first & 0x1F) << 24) | (self.byte() << 16) | (self.byte() << 8) | self.byte()


_ONE_BYTE_OPCODES = {
    0x00: ("nop", "InlineNone"),
    0x02: ("ldarg.0", "InlineNone"),
    0x03: ("ldarg.1", "InlineNone"),
    0x04: ("ldarg.2", "InlineNone"),
    0x05: ("ldarg.3", "InlineNone"),
    0x06: ("ldloc.0", "InlineNone"),
    0x07: ("ldloc.1", "InlineNone"),
    0x08: ("ldloc.2", "InlineNone"),
    0x09: ("ldloc.3", "InlineNone"),
    0x0A: ("stloc.0", "InlineNone"),
    0x0B: ("stloc.1", "InlineNone"),
    0x0C: ("stloc.2", "InlineNone"),
    0x0D: ("stloc.3", "InlineNone"),
    0x0E: ("ldarg.s", "ShortInlineVar"),
    0x0F: ("ldarga.s", "ShortInlineVar"),
    0x10: ("starg.s", "ShortInlineVar"),
    0x11: ("ldloc.s", "ShortInlineVar"),
    0x12: ("ldloca.s", "ShortInlineVar"),
    0x13: ("stloc.s", "ShortInlineVar"),
    0x14: ("ldnull", "InlineNone"),
    0x15: ("ldc.i4.m1", "InlineNone"),
    0x16: ("ldc.i4.0", "InlineNone"),
    0x17: ("ldc.i4.1", "InlineNone"),
    0x18: ("ldc.i4.2", "InlineNone"),
    0x19: ("ldc.i4.3", "InlineNone"),
    0x1A: ("ldc.i4.4", "InlineNone"),
    0x1B: ("ldc.i4.5", "InlineNone"),
    0x1C: ("ldc.i4.6", "InlineNone"),
    0x1D: ("ldc.i4.7", "InlineNone"),
    0x1E: ("ldc.i4.8", "InlineNone"),
    0x1F: ("ldc.i4.s", "ShortInlineI"),
    0x20: ("ldc.i4", "InlineI"),
    0x21: ("ldc.i8", "InlineI8"),
    0x22: ("ldc.r4", "ShortInlineR"),
    0x23: ("ldc.r8", "InlineR"),
    0x25: ("dup", "InlineNone"),
    0x26: ("pop", "InlineNone"),
    0x27: ("jmp", "InlineMethod"),
    0x28: ("call", "InlineMethod"),
    0x2A: ("ret", "InlineNone"),
    0x2B: ("br.s", "ShortInlineBrTarget"),
    0x2C: ("brfalse.s", "ShortInlineBrTarget"),
    0x2D: ("brtrue.s", "ShortInlineBrTarget"),
    0x2E: ("beq.s", "ShortInlineBrTarget"),
    0x2F: ("bge.s", "ShortInlineBrTarget"),
    0x30: ("bgt.s", "ShortInlineBrTarget"),
    0x31: ("ble.s", "ShortInlineBrTarget"),
    0x32: ("blt.s", "ShortInlineBrTarget"),
    0x33: ("bne.un.s", "ShortInlineBrTarget"),
    0x34: ("bge.un.s", "ShortInlineBrTarget"),
    0x35: ("bgt.un.s", "ShortInlineBrTarget"),
    0x36: ("ble.un.s", "ShortInlineBrTarget"),
    0x37: ("blt.un.s", "ShortInlineBrTarget"),
    0x38: ("br", "InlineBrTarget"),
    0x39: ("brfalse", "InlineBrTarget"),
    0x3A: ("brtrue", "InlineBrTarget"),
    0x3B: ("beq", "InlineBrTarget"),
    0x3C: ("bge", "InlineBrTarget"),
    0x3D: ("bgt", "InlineBrTarget"),
    0x3E: ("ble", "InlineBrTarget"),
    0x3F: ("blt", "InlineBrTarget"),
    0x40: ("bne.un", "InlineBrTarget"),
    0x41: ("bge.un", "InlineBrTarget"),
    0x42: ("bgt.un", "InlineBrTarget"),
    0x43: ("ble.un", "InlineBrTarget"),
    0x44: ("blt.un", "InlineBrTarget"),
    0x45: ("switch", "InlineSwitch"),
    0x46: ("ldind.i1", "InlineNone"),
    0x47: ("ldind.u1", "InlineNone"),
    0x48: ("ldind.i2", "InlineNone"),
    0x49: ("ldind.u2", "InlineNone"),
    0x4A: ("ldind.i4", "InlineNone"),
    0x4B: ("ldind.u4", "InlineNone"),
    0x4C: ("ldind.i8", "InlineNone"),
    0x4D: ("ldind.i", "InlineNone"),
    0x4E: ("ldind.r4", "InlineNone"),
    0x4F: ("ldind.r8", "InlineNone"),
    0x50: ("ldind.ref", "InlineNone"),
    0x51: ("stind.ref", "InlineNone"),
    0x52: ("stind.i1", "InlineNone"),
    0x53: ("stind.i2", "InlineNone"),
    0x54: ("stind.i4", "InlineNone"),
    0x55: ("stind.i8", "InlineNone"),
    0x56: ("stind.r4", "InlineNone"),
    0x57: ("stind.r8", "InlineNone"),
    0x58: ("add", "InlineNone"),
    0x59: ("sub", "InlineNone"),
    0x5A: ("mul", "InlineNone"),
    0x5B: ("div", "InlineNone"),
    0x5C: ("div.un", "InlineNone"),
    0x5D: ("rem", "InlineNone"),
    0x5E: ("rem.un", "InlineNone"),
    0x5F: ("and", "InlineNone"),
    0x60: ("or", "InlineNone"),
    0x61: ("xor", "InlineNone"),
    0x62: ("shl", "InlineNone"),
    0x63: ("shr", "InlineNone"),
    0x64: ("shr.un", "InlineNone"),
    0x65: ("neg", "InlineNone"),
    0x66: ("not", "InlineNone"),
    0x67: ("conv.i1", "InlineNone"),
    0x68: ("conv.i2", "InlineNone"),
    0x69: ("conv.i4", "InlineNone"),
    0x6A: ("conv.i8", "InlineNone"),
    0x6B: ("conv.r4", "InlineNone"),
    0x6C: ("conv.r8", "InlineNone"),
    0x6D: ("conv.u4", "InlineNone"),
    0x6E: ("conv.u8", "InlineNone"),
    0x6F: ("callvirt", "InlineMethod"),
    0x70: ("cpobj", "InlineType"),
    0x71: ("ldobj", "InlineType"),
    0x72: ("ldstr", "InlineString"),
    0x73: ("newobj", "InlineMethod"),
    0x74: ("castclass", "InlineType"),
    0x75: ("isinst", "InlineType"),
    0x76: ("conv.r.un", "InlineNone"),
    0x79: ("unbox", "InlineType"),
    0x7A: ("throw", "InlineNone"),
    0x7B: ("ldfld", "InlineField"),
    0x7C: ("ldflda", "InlineField"),
    0x7D: ("stfld", "InlineField"),
    0x7E: ("ldsfld", "InlineField"),
    0x7F: ("ldsflda", "InlineField"),
    0x80: ("stsfld", "InlineField"),
    0x81: ("ldelema", "InlineType"),
    0x8C: ("box", "InlineType"),
    0x8D: ("newarr", "InlineType"),
    0x8E: ("ldlen", "InlineNone"),
    0x8F: ("ldelema", "InlineType"),
    0x90: ("ldelem.i1", "InlineNone"),
    0x91: ("ldelem.u1", "InlineNone"),
    0x92: ("ldelem.i2", "InlineNone"),
    0x93: ("ldelem.u2", "InlineNone"),
    0x94: ("ldelem.i4", "InlineNone"),
    0x95: ("ldelem.u4", "InlineNone"),
    0x96: ("ldelem.i8", "InlineNone"),
    0x97: ("ldelem.i", "InlineNone"),
    0x98: ("ldelem.r4", "InlineNone"),
    0x99: ("ldelem.r8", "InlineNone"),
    0x9A: ("ldelem.ref", "InlineNone"),
    0x9B: ("stelem.i", "InlineNone"),
    0x9C: ("stelem.i1", "InlineNone"),
    0x9D: ("stelem.i2", "InlineNone"),
    0x9E: ("stelem.i4", "InlineNone"),
    0x9F: ("stelem.i8", "InlineNone"),
    0xA0: ("stelem.r4", "InlineNone"),
    0xA1: ("stelem.r8", "InlineNone"),
    0xA2: ("stelem.ref", "InlineNone"),
    0xA3: ("ldelem", "InlineType"),
    0xA4: ("stelem", "InlineType"),
    0xA5: ("unbox.any", "InlineType"),
    0xB6: ("tail.", "InlineNone"),
    0xB7: ("conv.u2", "InlineNone"),
    0xB8: ("conv.u1", "InlineNone"),
    0xB9: ("conv.i", "InlineNone"),
    0xBA: ("conv.ovf.i", "InlineNone"),
    0xBB: ("conv.ovf.u", "InlineNone"),
    0xC2: ("refanyval", "InlineType"),
    0xC3: ("ckfinite", "InlineNone"),
    0xC6: ("mkrefany", "InlineType"),
    0xD0: ("ldtoken", "InlineTok"),
    0xD1: ("conv.u", "InlineNone"),
    0xD2: ("add.ovf", "InlineNone"),
    0xD3: ("add.ovf.un", "InlineNone"),
    0xD4: ("mul.ovf", "InlineNone"),
    0xD5: ("mul.ovf.un", "InlineNone"),
    0xD6: ("sub.ovf", "InlineNone"),
    0xD7: ("sub.ovf.un", "InlineNone"),
    0xD8: ("endfinally", "InlineNone"),
    0xDC: ("endfilter", "InlineNone"),
    0xDD: ("leave", "InlineBrTarget"),
    0xDE: ("leave.s", "ShortInlineBrTarget"),
    0xDF: ("stind.i", "InlineNone"),
    0xE0: ("conv.ovf.i1.un", "InlineNone"),
    0xE1: ("conv.ovf.i2.un", "InlineNone"),
    0xE2: ("conv.ovf.i4.un", "InlineNone"),
    0xE3: ("conv.ovf.i8.un", "InlineNone"),
    0xE4: ("conv.ovf.u1.un", "InlineNone"),
    0xE5: ("conv.ovf.u2.un", "InlineNone"),
    0xE6: ("conv.ovf.u4.un", "InlineNone"),
    0xE7: ("conv.ovf.u8.un", "InlineNone"),
    0xE8: ("conv.ovf.i.un", "InlineNone"),
    0xE9: ("conv.ovf.u.un", "InlineNone"),
    0xFE: ("prefix", "Prefix"),
}

_TWO_BYTE_OPCODES = {
    0x00: ("arglist", "InlineNone"),
    0x01: ("ceq", "InlineNone"),
    0x02: ("cgt", "InlineNone"),
    0x03: ("cgt.un", "InlineNone"),
    0x04: ("clt", "InlineNone"),
    0x05: ("clt.un", "InlineNone"),
    0x06: ("ldftn", "InlineMethod"),
    0x07: ("ldvirtftn", "InlineMethod"),
    0x09: ("ldarg", "InlineVar"),
    0x0A: ("ldarga", "InlineVar"),
    0x0B: ("starg", "InlineVar"),
    0x0C: ("ldloc", "InlineVar"),
    0x0D: ("ldloca", "InlineVar"),
    0x0E: ("stloc", "InlineVar"),
    0x0F: ("localloc", "InlineNone"),
    0x11: ("endfilter", "InlineNone"),
    0x12: ("unaligned.", "ShortInlineI"),
    0x13: ("volatile.", "InlineNone"),
    0x14: ("tail.", "InlineNone"),
    0x15: ("initobj", "InlineType"),
    0x16: ("constrained.", "InlineType"),
    0x17: ("cpblk", "InlineNone"),
    0x18: ("initblk", "InlineNone"),
    0x1A: ("rethrow", "InlineNone"),
    0x1C: ("sizeof", "InlineType"),
    0x1D: ("refanytype", "InlineNone"),
    0x1E: ("readonly.", "InlineNone"),
}


def _decode_il(pe, code: bytes) -> tuple[DotNetInstruction, ...]:
    instructions: list[DotNetInstruction] = []
    offset = 0
    while offset < len(code):
        start = offset
        op = code[offset]
        offset += 1
        if op == 0xFE and offset < len(code):
            op2 = code[offset]
            offset += 1
            opcode, operand_kind = _TWO_BYTE_OPCODES.get(op2, (f"unknown.fe{op2:02x}", "InlineNone"))
        else:
            opcode, operand_kind = _ONE_BYTE_OPCODES.get(op, (f"unknown.{op:02x}", "InlineNone"))
        operand, token, offset = _read_operand(pe, code, offset, start, operand_kind)
        instructions.append(
            DotNetInstruction(
                offset=start,
                opcode=opcode,
                operands=operand.text,
                token=token,
                operand_kind=operand_kind,
                member_name=operand.member_name,
                owner_name=operand.owner_name,
                arg_count=operand.arg_count,
                returns_void=operand.returns_void,
                is_static=operand.is_static,
            )
        )
    return tuple(instructions)


@dataclass(frozen=True)
class _DecodedOperand:
    text: str = ""
    member_name: str | None = None
    owner_name: str | None = None
    arg_count: int | None = None
    returns_void: bool | None = None
    is_static: bool | None = None


def _read_operand(pe, code: bytes, offset: int, instruction_offset: int, kind: str) -> tuple[_DecodedOperand, int | None, int]:
    if kind in {"InlineNone", "Prefix"}:
        return _DecodedOperand(), None, offset
    if kind in {"ShortInlineI", "ShortInlineVar"}:
        if offset >= len(code):
            return _DecodedOperand(), None, offset
        value = struct.unpack_from("<b" if kind == "ShortInlineI" else "<B", code, offset)[0]
        return _DecodedOperand(str(value)), None, offset + 1
    if kind == "InlineVar":
        if offset + 2 > len(code):
            return _DecodedOperand(), None, offset
        return _DecodedOperand(str(struct.unpack_from("<H", code, offset)[0])), None, offset + 2
    if kind == "InlineI":
        if offset + 4 > len(code):
            return _DecodedOperand(), None, offset
        return _DecodedOperand(str(struct.unpack_from("<i", code, offset)[0])), None, offset + 4
    if kind == "InlineI8":
        if offset + 8 > len(code):
            return _DecodedOperand(), None, offset
        return _DecodedOperand(str(struct.unpack_from("<q", code, offset)[0])), None, offset + 8
    if kind == "ShortInlineR":
        if offset + 4 > len(code):
            return _DecodedOperand(), None, offset
        return _DecodedOperand(str(struct.unpack_from("<f", code, offset)[0])), None, offset + 4
    if kind == "InlineR":
        if offset + 8 > len(code):
            return _DecodedOperand(), None, offset
        return _DecodedOperand(str(struct.unpack_from("<d", code, offset)[0])), None, offset + 8
    if kind == "ShortInlineBrTarget":
        if offset >= len(code):
            return _DecodedOperand(), None, offset
        delta = struct.unpack_from("<b", code, offset)[0]
        return _DecodedOperand(str(offset + 1 + delta)), None, offset + 1
    if kind == "InlineBrTarget":
        if offset + 4 > len(code):
            return _DecodedOperand(), None, offset
        delta = struct.unpack_from("<i", code, offset)[0]
        return _DecodedOperand(str(offset + 4 + delta)), None, offset + 4
    if kind == "InlineSwitch":
        if offset + 4 > len(code):
            return _DecodedOperand(), None, offset
        count = struct.unpack_from("<I", code, offset)[0]
        offset += 4
        base = offset + count * 4
        targets: list[str] = []
        for _ in range(count):
            if offset + 4 > len(code):
                break
            targets.append(str(base + struct.unpack_from("<i", code, offset)[0]))
            offset += 4
        return _DecodedOperand(",".join(targets)), None, offset
    if kind in {"InlineMethod", "InlineField", "InlineType", "InlineTok", "InlineString", "InlineSig"}:
        if offset + 4 > len(code):
            return _DecodedOperand(), None, offset
        token = struct.unpack_from("<I", code, offset)[0]
        return _resolve_metadata_operand(pe, token, kind), token, offset + 4
    return _DecodedOperand(), None, offset


def _resolve_metadata_operand(pe, token: int, kind: str) -> _DecodedOperand:
    if kind == "InlineString":
        value = _user_string(pe, token)
        return _DecodedOperand(value if value is not None else f"0x{token:08x}")
    table_id = token >> 24
    row_index = token & 0x00FFFFFF
    row = _metadata_row(pe, table_id, row_index)
    if row is None:
        return _DecodedOperand(f"0x{token:08x}")
    if table_id == 0x06:
        name = _heap_text(getattr(row, "Name", None)) or f"0x{token:08x}"
        owner = _owner_for_method(pe, row_index)
        arg_count, returns_void, _has_this = _signature_shape(getattr(row, "Signature", None))
        is_static = bool(getattr(row.Flags, "mdStatic", False))
        return _DecodedOperand(_qualified_member(owner, name), name, owner, arg_count, returns_void, is_static)
    if table_id == 0x0A:
        name = _heap_text(getattr(row, "Name", None)) or f"0x{token:08x}"
        owner = _type_name_from_coded(getattr(row, "Class", None)) or "<member>"
        arg_count, returns_void, has_this = _signature_shape(getattr(row, "Signature", None))
        return _DecodedOperand(_qualified_member(owner, name), name, owner, arg_count, returns_void, not has_this)
    if table_id == 0x2B:
        method = _methodspec_target(pe, row_index)
        if method is None:
            return _DecodedOperand(f"0x{token:08x}")
        return method
    if table_id == 0x04:
        name = _heap_text(getattr(row, "Name", None)) or f"0x{token:08x}"
        owner = _owner_for_field(pe, row_index)
        return _DecodedOperand(_qualified_member(owner, name), name, owner)
    if table_id in {0x01, 0x02}:
        name = _type_name(row) or f"0x{token:08x}"
        return _DecodedOperand(name, owner_name=name)
    return _DecodedOperand(f"0x{token:08x}")


def _metadata_row(pe, table_id: int, row_index: int):
    table_names = {
        0x01: "TypeRef",
        0x02: "TypeDef",
        0x04: "Field",
        0x06: "MethodDef",
        0x0A: "MemberRef",
        0x2B: "MethodSpec",
    }
    table = getattr(getattr(pe.net, "mdtables", None), table_names.get(table_id, ""), None)
    rows = getattr(table, "rows", ()) or ()
    if row_index <= 0 or row_index > len(rows):
        return None
    return rows[row_index - 1]


def _user_string(pe, token: int) -> str | None:
    heap = getattr(pe.net, "user_strings", None)
    if heap is None:
        return None
    value = heap.get(token & 0x00FFFFFF)
    return _heap_text(value)


def _type_name(row) -> str | None:
    name = _heap_text(getattr(row, "TypeName", None))
    namespace = _heap_text(getattr(row, "TypeNamespace", None))
    if name and namespace:
        return f"{namespace}.{name}"
    return name


def _qualified_member(owner: str | None, name: str) -> str:
    return f"{owner}.{name}" if owner else name


def _type_name_from_coded(value) -> str | None:
    row = getattr(value, "row", None)
    return _type_name(row)


def _owner_for_method(pe, row_index: int) -> str | None:
    typedef = getattr(getattr(pe.net, "mdtables", None), "TypeDef", None)
    for row in getattr(typedef, "rows", ()) or ():
        method_indices = [getattr(index, "row_index", None) for index in getattr(row, "MethodList", ()) or ()]
        if row_index in method_indices:
            return _type_name(row)
    return None


def _owner_for_field(pe, row_index: int) -> str | None:
    typedef = getattr(getattr(pe.net, "mdtables", None), "TypeDef", None)
    for row in getattr(typedef, "rows", ()) or ():
        field_indices = [getattr(index, "row_index", None) for index in getattr(row, "FieldList", ()) or ()]
        if row_index in field_indices:
            return _type_name(row)
    return None


def _methodspec_target(pe, row_index: int) -> _DecodedOperand | None:
    methodspec = getattr(getattr(pe.net, "mdtables", None), "MethodSpec", None)
    rows = getattr(methodspec, "rows", ()) or ()
    if row_index <= 0 or row_index > len(rows):
        return None
    row = rows[row_index - 1]
    method_ref = getattr(row, "Method", None)
    if method_ref is None:
        return None
    target_row = getattr(method_ref, "table", None)
    target_index = getattr(method_ref, "row_index", None)
    if target_row is None or target_index is None:
        return None
    if target_row.__class__.__name__ == "MemberRef":
        member_ref = _metadata_row(pe, 0x0A, target_index)
        if member_ref is None:
            return None
        name = _heap_text(getattr(member_ref, "Name", None)) or f"0x{row_index:08x}"
        owner = _type_name_from_coded(getattr(member_ref, "Class", None)) or "<member>"
        arg_count, returns_void, has_this = _signature_shape(getattr(member_ref, "Signature", None))
        return _DecodedOperand(_qualified_member(owner, name), name, owner, arg_count, returns_void, not has_this)
    if target_row.__class__.__name__ == "MethodDef":
        method_def = _metadata_row(pe, 0x06, target_index)
        if method_def is None:
            return None
        name = _heap_text(getattr(method_def, "Name", None)) or f"0x{row_index:08x}"
        owner = _owner_for_method(pe, target_index)
        arg_count, returns_void, _has_this = _signature_shape(getattr(method_def, "Signature", None))
        is_static = bool(getattr(method_def.Flags, "mdStatic", False))
        return _DecodedOperand(_qualified_member(owner, name), name, owner, arg_count, returns_void, is_static)
    return None


def _signature_shape(signature) -> tuple[int, bool | None, bool]:
    data = bytes(getattr(signature, "value", b"") or b"")
    if not data:
        return 0, None, False
    reader = _BlobReader(data)
    calling_convention = reader.byte()
    has_this = bool(calling_convention & 0x20)
    is_generic = bool(calling_convention & 0x10)
    calling_convention &= 0x0F
    if calling_convention == 0x06:
        return 0, None, has_this
    if is_generic:
        reader.compressed_uint()
    if reader.eof:
        return 0, None, has_this
    param_count = reader.compressed_uint()
    return_type = reader.byte()
    return param_count, return_type == 0x01, has_this
