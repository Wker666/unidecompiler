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
        return JavaClassFile(
            minor_version=parsed.version.minor,
            major_version=parsed.version.major,
            class_name=class_name,
            is_annotation=bool(parsed.access_flags.acc_annotation),
            methods=tuple(_jawa_method_listing(parsed, method) for method in parsed.methods),
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


def _jawa_method_listing(class_file, method) -> JavaMethodListing:
    raw_name = _jawa_utf8(method.name) or "<method>"
    class_name = _jawa_utf8(class_file.this.name)
    name = class_name.rsplit("/", 1)[-1] if raw_name == "<init>" and class_name else raw_name
    code = method.code
    instructions: tuple[JavaInstruction, ...] = ()
    if code is not None:
        instructions = tuple(
            _jawa_instruction(class_file, instruction) for instruction in code.disassemble()
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


def _jawa_instruction(class_file, instruction) -> JavaInstruction:
    if instruction.mnemonic == "lookupswitch":
        operands = _jawa_lookupswitch_operands(instruction)
        if operands is not None:
            return JavaInstruction(
                offset=instruction.pos,
                opcode=instruction.mnemonic,
                operands=operands,
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
    )


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
