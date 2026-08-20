from __future__ import annotations

from pathlib import PurePath

from unidecompiler.plugins import FrontendDecodeError
from unidecompiler.provenance import ByteRange

from .model import DIGITS, INSTRUCTIONS, EmojiInstruction, EmojiVMProgram


class EmojiVMDecodeError(FrontendDecodeError):
    """The input looks like EmojiVM text but is malformed."""


def looks_like_emojivm(data: bytes, filename: str | None = None) -> bool:
    if filename and PurePath(filename).suffix.lower() == ".evm":
        return True
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    symbols = [char for char in text if not char.isspace()]
    return bool(symbols) and symbols[0] in INSTRUCTIONS and all(
        char in INSTRUCTIONS or char in DIGITS for char in symbols
    )


def decode_emojivm(data: bytes, filename: str | None = None) -> EmojiVMProgram:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise EmojiVMDecodeError(f"EmojiVM source is not valid UTF-8: {error}") from error

    instructions: list[EmojiInstruction] = []
    codepoint_index = 0
    byte_index = 0
    while codepoint_index < len(text):
        char = text[codepoint_index]
        char_bytes = char.encode("utf-8")
        if char.isspace():
            codepoint_index += 1
            byte_index += len(char_bytes)
            continue
        decoded = INSTRUCTIONS.get(char)
        if decoded is None:
            raise EmojiVMDecodeError(
                f"unknown EmojiVM character {char!r} at source offset {codepoint_index}"
            )
        opcode, size = decoded
        operands: tuple[object, ...] = ()
        raw = char
        raw_bytes = len(char_bytes)
        if opcode == "push":
            if codepoint_index + 1 >= len(text):
                raise EmojiVMDecodeError(f"PUSH at source offset {codepoint_index} has no digit")
            digit_char = text[codepoint_index + 1]
            digit = DIGITS.get(digit_char)
            if digit is None:
                raise EmojiVMDecodeError(
                    f"PUSH at source offset {codepoint_index} is followed by a non-digit emoji"
                )
            operands = (digit,)
            raw += digit_char
            raw_bytes += len(digit_char.encode("utf-8"))
        instructions.append(
            EmojiInstruction(
                offset=codepoint_index,
                byte_offset=byte_index,
                opcode=opcode,
                emoji=char,
                size=size,
                operands=operands,
                raw=raw,
                artifact_range=ByteRange(start=byte_index, size=raw_bytes),
            )
        )
        codepoint_index += size
        byte_index += raw_bytes
    if not instructions:
        raise EmojiVMDecodeError("EmojiVM source is empty")
    return EmojiVMProgram(filename=filename, source=text, instructions=tuple(instructions))
