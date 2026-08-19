from __future__ import annotations

from pathlib import PurePath

from unidecompiler.plugins import FrontendDecodeError

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
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        decoded = INSTRUCTIONS.get(char)
        if decoded is None:
            raise EmojiVMDecodeError(
                f"unknown EmojiVM character {char!r} at source offset {index}"
            )
        opcode, size = decoded
        operands: tuple[object, ...] = ()
        raw = char
        if opcode == "push":
            if index + 1 >= len(text):
                raise EmojiVMDecodeError(f"PUSH at source offset {index} has no digit")
            digit = DIGITS.get(text[index + 1])
            if digit is None:
                raise EmojiVMDecodeError(
                    f"PUSH at source offset {index} is followed by a non-digit emoji"
                )
            operands = (digit,)
            raw += text[index + 1]
        instructions.append(
            EmojiInstruction(
                offset=index,
                opcode=opcode,
                emoji=char,
                size=size,
                operands=operands,
                raw=raw,
            )
        )
        index += size
    if not instructions:
        raise EmojiVMDecodeError("EmojiVM source is empty")
    return EmojiVMProgram(filename=filename, source=text, instructions=tuple(instructions))
