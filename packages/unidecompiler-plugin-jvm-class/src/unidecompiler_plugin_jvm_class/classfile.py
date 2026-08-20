from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from typing import Protocol

from unidecompiler.plugins import FrontendDecodeError


CLASS_MAGIC = b"\xca\xfe\xba\xbe"


class ClassDecodeError(FrontendDecodeError):
    pass


@dataclass(frozen=True)
class JavaInstruction:
    offset: int
    opcode: str
    operands: str = ""
    artifact_offset: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class JavaExceptionRegion:
    start: int
    end: int
    target: int
    exception_type: str | None = None


@dataclass(frozen=True)
class JavaMethodListing:
    name: str
    descriptor: str | None = None
    is_static: bool = False
    is_annotation_member: bool = False
    annotation_default: object | None = None
    instructions: tuple[JavaInstruction, ...] = ()
    exception_regions: tuple[JavaExceptionRegion, ...] = ()


@dataclass(frozen=True)
class JavaClassFile:
    major_version: int
    minor_version: int
    class_name: str | None = None
    is_annotation: bool = False
    methods: tuple[JavaMethodListing, ...] = ()
    filename: str | None = None
    decoder_id: str | None = None


class ClassFileDecoder(Protocol):
    id: str

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        ...

    def decode(self, data: bytes, filename: str | None = None) -> JavaClassFile:
        ...


def looks_like_class(data: bytes) -> bool:
    return data.startswith(CLASS_MAGIC)


def decode_class_header(data: bytes, filename: str | None = None) -> JavaClassFile:
    if len(data) < 8:
        raise ClassDecodeError("truncated class file")
    if not looks_like_class(data):
        raise ClassDecodeError("missing class magic")
    return JavaClassFile(
        minor_version=int.from_bytes(data[4:6], "big"),
        major_version=int.from_bytes(data[6:8], "big"),
        filename=filename,
        decoder_id="class-header-only",
    )


class HeaderOnlyClassFileDecoder:
    id = "class-header-only"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_class(data)

    def decode(self, data: bytes, filename: str | None = None) -> JavaClassFile:
        return decode_class_header(data, filename)


class JawaClassFileDecoder:
    """JVM classfile decoder backed by the third-party ``jawa`` library."""

    id = "jawa"

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_class(data) and _jawa_class_file_type() is not None

    def decode(self, data: bytes, filename: str | None = None) -> JavaClassFile:
        class_file_type = _jawa_class_file_type()
        if class_file_type is None:
            raise ClassDecodeError("jawa is not installed")
        if not looks_like_class(data):
            raise ClassDecodeError("missing class magic")
        try:
            parsed = class_file_type(BytesIO(data))
        except Exception as error:  # pragma: no cover - jawa owns parse details.
            raise ClassDecodeError(f"jawa failed to decode class: {error}") from error
        class_name = _jawa_utf8(parsed.this.name)
        try:
            code_ranges = _class_code_ranges(data)
        except ClassDecodeError:
            code_ranges = ({},) * len(tuple(parsed.methods))
        return JavaClassFile(
            minor_version=parsed.version.minor,
            major_version=parsed.version.major,
            class_name=class_name,
            is_annotation=bool(parsed.access_flags.acc_annotation),
            methods=tuple(_jawa_method_listing(parsed, method, code_ranges[index]) for index, method in enumerate(parsed.methods)),
            filename=filename,
            decoder_id=self.id,
        )


class PreferredClassFileDecoder:
    id = "class-preferred"

    def __init__(self, decoders: tuple[ClassFileDecoder, ...] | None = None) -> None:
        self.decoders = decoders or (
            JawaClassFileDecoder(),
            HeaderOnlyClassFileDecoder(),
        )

    def can_decode(self, data: bytes, filename: str | None = None) -> bool:
        return any(decoder.can_decode(data, filename) for decoder in self.decoders)

    def decode(self, data: bytes, filename: str | None = None) -> JavaClassFile:
        errors: list[str] = []
        for decoder in self.decoders:
            if not decoder.can_decode(data, filename):
                continue
            try:
                return decoder.decode(data, filename)
            except ClassDecodeError as error:
                errors.append(f"{decoder.id}: {error}")
        if errors:
            raise ClassDecodeError("; ".join(errors))
        raise ClassDecodeError("no JVM class decoder can decode this input")


def _jawa_class_file_type():
    try:
        from jawa.cf import ClassFile
    except ImportError:
        return None
    return ClassFile


def _jawa_method_listing(class_file, method, ranges: dict[int, tuple[int, int]] | None = None) -> JavaMethodListing:
    raw_name = _jawa_utf8(method.name) or "<method>"
    class_name = _jawa_utf8(class_file.this.name)
    name = class_name.rsplit("/", 1)[-1] if raw_name == "<init>" and class_name else raw_name
    code = method.code
    instructions: tuple[JavaInstruction, ...] = ()
    if code is not None:
        instructions = tuple(
            _jawa_instruction(class_file, instruction, ranges or {}) for instruction in code.disassemble()
        )
    return JavaMethodListing(
        name=name,
        descriptor=_jawa_utf8(method.descriptor),
        is_static=bool(method.access_flags.acc_static),
        is_annotation_member=bool(class_file.access_flags.acc_annotation) and code is None,
        annotation_default=_jawa_annotation_default(class_file, method),
        instructions=instructions,
        exception_regions=tuple(
            JavaExceptionRegion(
                start=entry.start_pc,
                end=entry.end_pc,
                target=entry.handler_pc,
                exception_type=_jawa_exception_type(class_file, entry.catch_type),
            )
            for entry in (getattr(code, "exception_table", ()) if code is not None else ())
        ),
    )


def _jawa_exception_type(class_file, index: int) -> str | None:
    if not index:
        return None
    try:
        constant = class_file.constants.get(index)
        name = _jawa_utf8(constant.name)
    except Exception:
        return None
    return name.replace("/", ".") if name else None


def _jawa_annotation_default(class_file, method) -> object | None:
    for attribute in method.attributes:
        if _jawa_utf8(attribute.name) != "AnnotationDefault":
            continue
        info = getattr(attribute, "info", None)
        if not isinstance(info, (bytes, bytearray)):
            return "<annotation-default>"
        value, _offset = _parse_jvm_element_value(class_file, bytes(info), 0)
        return value
    return None


def _parse_jvm_element_value(class_file, data: bytes, offset: int) -> tuple[object, int]:
    if offset >= len(data):
        return ("<truncated-annotation-default>", offset)
    tag = chr(data[offset])
    offset += 1
    if tag in "BCDFIJSZs":
        if offset + 2 > len(data):
            return ("<truncated-annotation-constant>", offset)
        index = int.from_bytes(data[offset : offset + 2], "big")
        return (_jawa_constant_value(class_file, index), offset + 2)
    if tag == "e":
        if offset + 4 > len(data):
            return ("<truncated-annotation-enum>", offset)
        type_index = int.from_bytes(data[offset : offset + 2], "big")
        const_index = int.from_bytes(data[offset + 2 : offset + 4], "big")
        return (
            f"{_jawa_constant_value(class_file, type_index)}.{_jawa_constant_value(class_file, const_index)}",
            offset + 4,
        )
    if tag == "c":
        if offset + 2 > len(data):
            return ("<truncated-annotation-class>", offset)
        index = int.from_bytes(data[offset : offset + 2], "big")
        return (f"{_jawa_constant_value(class_file, index)}.class", offset + 2)
    return (f"<annotation-default:{tag}>", len(data))


def _jawa_constant_value(class_file, index: int) -> object:
    try:
        constant = class_file.constants.get(index)
    except Exception:
        return f"#{index}"
    if constant is None:
        return f"#{index}"
    value = getattr(constant, "value", None)
    if value is not None:
        return value
    for attr in ("string", "name"):
        nested = getattr(constant, attr, None)
        text = _jawa_utf8(nested)
        if text is not None:
            return text
    return f"#{index}"


def _jawa_instruction(class_file, instruction, ranges: dict[int, tuple[int, int]]) -> JavaInstruction:
    artifact_range = ranges.get(instruction.pos)
    artifact_offset = None if artifact_range is None else artifact_range[0]
    size = None if artifact_range is None else artifact_range[1]
    if instruction.mnemonic == "lookupswitch":
        operands = _jawa_lookupswitch_operands(instruction)
        if operands is not None:
            return JavaInstruction(
                offset=instruction.pos,
                opcode=instruction.mnemonic,
                operands=operands,
                artifact_offset=artifact_offset,
                size=size,
            )
    operands = tuple(
        rendered
        for rendered in (
            _jawa_operand(class_file, instruction, operand) for operand in instruction.operands
        )
        if rendered
    )
    return JavaInstruction(
        offset=instruction.pos,
        opcode=instruction.mnemonic,
        operands=", ".join(operands),
        artifact_offset=artifact_offset,
        size=size,
    )


def _class_code_ranges(data: bytes) -> tuple[dict[int, tuple[int, int]], ...]:
    """Locate JVM Code attributes without exposing parser state outside this frontend."""
    reader = _ClassReader(data)
    if reader.read_u4() != int.from_bytes(CLASS_MAGIC, "big"):
        raise ClassDecodeError("missing class magic")
    reader.skip(4)  # minor and major version
    constants = reader.read_constants()
    reader.skip(6)  # access flags, this class, super class
    reader.skip(reader.read_u2() * 2)  # interfaces
    reader.skip_members(constants)  # fields
    result: list[dict[int, tuple[int, int]]] = []
    method_count = reader.read_u2()
    for _ in range(method_count):
        reader.skip(6)  # access flags, name index, descriptor index
        attributes = reader.read_u2()
        ranges: dict[int, tuple[int, int]] = {}
        for _ in range(attributes):
            name_index = reader.read_u2()
            size = reader.read_u4()
            start = reader.offset
            if constants.get(name_index) == "Code" and size >= 8:
                reader.skip(4)  # max_stack and max_locals
                code_size = reader.read_u4()
                code_start = reader.offset
                if code_size <= size - 8 and code_start + code_size <= len(data):
                    ranges = _jvm_instruction_ranges(data[code_start : code_start + code_size], code_start)
            reader.seek(start + size)
        result.append(ranges)
    return tuple(result)


def _jvm_instruction_ranges(code: bytes, artifact_start: int) -> dict[int, tuple[int, int]]:
    """Return exact Code-attribute instruction ranges from JVM bytecode lengths."""
    offsets: list[int] = []
    cursor = 0
    while cursor < len(code):
        offsets.append(cursor)
        cursor += _jvm_instruction_size(code, cursor)
    if cursor != len(code):
        raise ClassDecodeError("invalid JVM instruction length in Code attribute")
    return {
        offset: (artifact_start + offset, (offsets[index + 1] if index + 1 < len(offsets) else len(code)) - offset)
        for index, offset in enumerate(offsets)
    }


def _jvm_instruction_size(code: bytes, offset: int) -> int:
    opcode = code[offset]
    fixed = _JVM_FIXED_OPERAND_SIZES.get(opcode)
    if fixed is not None:
        size = 1 + fixed
    elif opcode == 0xAA:  # tableswitch
        base = offset + 1
        padding = (4 - base % 4) % 4
        cursor = base + padding
        if cursor + 12 > len(code):
            raise ClassDecodeError("truncated tableswitch")
        low = int.from_bytes(code[cursor + 4 : cursor + 8], "big", signed=True)
        high = int.from_bytes(code[cursor + 8 : cursor + 12], "big", signed=True)
        size = 1 + padding + 12 + (high - low + 1) * 4
    elif opcode == 0xAB:  # lookupswitch
        base = offset + 1
        padding = (4 - base % 4) % 4
        cursor = base + padding
        if cursor + 8 > len(code):
            raise ClassDecodeError("truncated lookupswitch")
        pairs = int.from_bytes(code[cursor + 4 : cursor + 8], "big", signed=True)
        size = 1 + padding + 8 + pairs * 8
    elif opcode == 0xC4:  # wide
        if offset + 1 >= len(code):
            raise ClassDecodeError("truncated wide instruction")
        size = 6 if code[offset + 1] == 0x84 else 4
    else:
        raise ClassDecodeError(f"unknown JVM opcode 0x{opcode:02x}")
    if size <= 0 or offset + size > len(code):
        raise ClassDecodeError("truncated JVM instruction")
    return size


class _ClassReader:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.offset = 0

    def read_u2(self) -> int:
        value = int.from_bytes(self.read(2), "big")
        return value

    def read_u4(self) -> int:
        value = int.from_bytes(self.read(4), "big")
        return value

    def read(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise ClassDecodeError("truncated class file")
        value = self.data[self.offset : self.offset + size]
        self.offset += size
        return value

    def skip(self, size: int) -> None:
        self.read(size)

    def seek(self, offset: int) -> None:
        if offset < self.offset or offset > len(self.data):
            raise ClassDecodeError("invalid class attribute length")
        self.offset = offset

    def read_constants(self) -> dict[int, str]:
        count = self.read_u2()
        values: dict[int, str] = {}
        index = 1
        while index < count:
            tag = self.read(1)[0]
            if tag == 1:
                length = self.read_u2()
                values[index] = self.read(length).decode("utf-8", "replace")
            elif tag in {3, 4}:
                self.skip(4)
            elif tag in {5, 6}:
                self.skip(8)
                index += 1
            elif tag in {7, 8, 16, 19, 20}:
                self.skip(2)
            elif tag in {9, 10, 11, 12, 17, 18}:
                self.skip(4)
            elif tag == 15:
                self.skip(3)
            else:
                raise ClassDecodeError(f"unknown class constant tag {tag}")
            index += 1
        return values

    def skip_members(self, constants: dict[int, str]) -> None:
        for _ in range(self.read_u2()):
            self.skip(6)
            for _ in range(self.read_u2()):
                self.skip(2)
                self.skip(self.read_u4())


_JVM_FIXED_OPERAND_SIZES = {opcode: 0 for opcode in range(256)}
for _opcode in (0x10, 0x12, 0x15, 0x16, 0x17, 0x18, 0x19, 0x36, 0x37, 0x38, 0x39, 0x3A, 0xA9, 0xBC):
    _JVM_FIXED_OPERAND_SIZES[_opcode] = 1
for _opcode in (0x11, 0x13, 0x14, 0x84, *range(0x99, 0xA9), 0xB2, 0xB3, 0xB4, 0xB5, 0xB6, 0xB7, 0xB8, 0xBB, 0xBD, 0xC0, 0xC1, 0xC6, 0xC7):
    _JVM_FIXED_OPERAND_SIZES[_opcode] = 2
for _opcode in (0xB9, 0xBA):
    _JVM_FIXED_OPERAND_SIZES[_opcode] = 4
for _opcode in (0xC5,):
    _JVM_FIXED_OPERAND_SIZES[_opcode] = 3
for _opcode in (0xC8, 0xC9):
    _JVM_FIXED_OPERAND_SIZES[_opcode] = 4
for _opcode in (0xAA, 0xAB, 0xC4):
    _JVM_FIXED_OPERAND_SIZES.pop(_opcode)


def _jawa_lookupswitch_operands(instruction) -> str | None:
    operands = instruction.operands
    if len(operands) != 2 or not isinstance(operands[0], dict):
        return None
    default_offset = getattr(operands[1], "value", None)
    if not isinstance(default_offset, int):
        return None
    pairs: list[str] = [str(instruction.pos + default_offset)]
    try:
        cases = sorted(operands[0].items())
    except TypeError:
        return None
    for value, relative_target in cases:
        if not isinstance(value, int) or not isinstance(relative_target, int):
            return None
        pairs.extend((str(value), str(instruction.pos + relative_target)))
    return ", ".join(pairs)


def _jawa_operand(class_file, instruction, operand) -> str:
    op_type = getattr(getattr(operand, "op_type", None), "name", "")
    value = getattr(operand, "value", None)
    if op_type == "BRANCH" and isinstance(value, int):
        return str(instruction.pos + value)
    if op_type == "CONSTANT_INDEX" and isinstance(value, int):
        return _jawa_constant_operand(class_file, value)
    if op_type == "PADDING":
        return ""
    return str(value)


def _jawa_constant_operand(class_file, index: int) -> str:
    try:
        constant = class_file.constants.get(index)
    except Exception:
        return f"#{index}"
    if constant is None:
        return f"#{index}"

    type_name = type(constant).__name__
    if type_name == "String":
        return f"#{index}                 // String {_jawa_utf8(constant.string)}"
    if type_name in {"Integer", "Float", "Long", "Double"}:
        return f"#{index}                 // {type_name.lower()} {constant.value}"
    if type_name == "ConstantClass":
        return f"#{index}                 // class {_jawa_utf8(constant.name)}"
    if type_name in {
        "FieldReference",
        "MethodReference",
        "InterfaceMethodReference",
        "InterfaceMethodRef",
    }:
        owner = _jawa_utf8(constant.class_.name) or "<owner>"
        name_and_type = constant.name_and_type
        name = _jawa_utf8(name_and_type.name) or "<name>"
        descriptor = _jawa_utf8(name_and_type.descriptor) or "()V"
        label = {
            "FieldReference": "Field",
            "MethodReference": "Method",
            "InterfaceMethodReference": "InterfaceMethod",
            "InterfaceMethodRef": "InterfaceMethod",
        }[type_name]
        return f"#{index}                 // {label} {owner}.{name}:{descriptor}"
    if type_name in {"InvokeDynamic", "Dynamic"} and hasattr(constant, "name_and_type"):
        name_and_type = constant.name_and_type
        name = _jawa_utf8(name_and_type.name) or "<name>"
        descriptor = _jawa_utf8(name_and_type.descriptor) or "()V"
        return f"#{index}                 // {type_name} {name}:{descriptor}"
    return f"#{index}"


def _jawa_utf8(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    text = getattr(value, "value", None)
    return text if isinstance(text, str) else None
