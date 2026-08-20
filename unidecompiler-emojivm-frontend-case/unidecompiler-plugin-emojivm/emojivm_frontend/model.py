from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.provenance import ByteRange


INSTRUCTIONS = {
    "\U0001f233": ("nop", 1),
    "\u2795": ("add", 1),
    "\u2796": ("sub", 1),
    "\u274c": ("mul", 1),
    "\u2753": ("mod", 1),
    "\u274e": ("xor", 1),
    "\U0001f46b": ("and", 1),
    "\U0001f480": ("lt", 1),
    "\U0001f4af": ("eq", 1),
    "\U0001f680": ("jmp", 1),
    "\U0001f236": ("jnz", 1),
    "\U0001f21a": ("jz", 1),
    "\u23ec": ("push", 2),
    "\U0001f51d": ("pop", 1),
    "\U0001f4e4": ("load", 1),
    "\U0001f4e5": ("store", 1),
    "\U0001f195": ("alloc", 1),
    "\U0001f193": ("free", 1),
    "\U0001f4c4": ("read", 1),
    "\U0001f4dd": ("write", 1),
    "\U0001f521": ("puts", 1),
    "\U0001f522": ("print", 1),
    "\U0001f6d1": ("halt", 1),
}

DIGITS = {
    "\U0001f600": 0,
    "\U0001f601": 1,
    "\U0001f602": 2,
    "\U0001f923": 3,
    "\U0001f61c": 4,
    "\U0001f604": 5,
    "\U0001f605": 6,
    "\U0001f606": 7,
    "\U0001f609": 8,
    "\U0001f60a": 9,
    "\U0001f60d": 10,
}


@dataclass(frozen=True)
class EmojiInstruction:
    offset: int
    byte_offset: int
    opcode: str
    emoji: str
    size: int
    operands: tuple[object, ...] = ()
    raw: str = ""
    artifact_range: ByteRange | None = None


@dataclass(frozen=True)
class EmojiVMProgram:
    filename: str | None
    source: str
    instructions: tuple[EmojiInstruction, ...]
