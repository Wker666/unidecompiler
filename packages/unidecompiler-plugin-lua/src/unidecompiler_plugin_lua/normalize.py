from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from unidecompiler_plugin_lua.luac import LuaFunctionListing, LuaInstructionListing


BranchKind = Literal["conditional-pair", "unconditional"]

BRANCH_OPCODES = {
    "EQ",
    "LT",
    "LE",
    "TEST",
    "TESTSET",
}
UNCONDITIONAL_JUMP_OPCODES = {"JMP"}
TERMINATOR_OPCODES = {
    "RETURN",
    "RETURN0",
    "RETURN1",
    "RETURN2",
    "RETURNI",
    "RETURNK",
}
UNSUPPORTED_FOR_LINEAR_LIFTING = {
    "FORPREP",
    "FORLOOP",
    "TFORPREP",
    "TFORCALL",
    "TFORLOOP",
    "JMP",
    "TEST",
    "TESTSET",
    "NEWTABLE",
    "SETLIST",
    "LEN",
}
TARGET_COMMENT_RE = re.compile(r"\bto\s+(?P<target>\d+)\b")


@dataclass(frozen=True)
class NormalizedLuaBranch:
    pc: int
    opcode: str
    target_pc: int
    kind: BranchKind
    fallthrough_pc: int | None

    def to_metadata(self) -> dict:
        data = {
            "pc": self.pc,
            "opcode": self.opcode,
            "target_pc": self.target_pc,
            "kind": self.kind,
        }
        if self.fallthrough_pc is not None:
            data["fallthrough_pc"] = self.fallthrough_pc
        return data


@dataclass(frozen=True)
class NormalizedLuaInstruction:
    pc: int
    line: int | None
    opcode: str
    operands: tuple[str, ...]
    next_pc: int | None
    explicit_target_pc: int | None

    def to_metadata(self) -> dict:
        return {
            "pc": self.pc,
            "line": self.line,
            "opcode": self.opcode,
            "operands": list(self.operands),
            "next_pc": self.next_pc,
            "explicit_target_pc": self.explicit_target_pc,
        }


@dataclass(frozen=True)
class NormalizedLuaFunction:
    name: str
    source: str
    line_start: int | None
    line_end: int | None
    entry_pc: int | None
    instructions: tuple[NormalizedLuaInstruction, ...]
    branches: tuple[NormalizedLuaBranch, ...]
    basic_block_leaders: tuple[int, ...]
    unsupported_opcodes: tuple[str, ...]

    def to_metadata(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "entry_pc": self.entry_pc,
            "instruction_pcs": [instruction.pc for instruction in self.instructions],
            "instructions": [
                instruction.to_metadata() for instruction in self.instructions
            ],
            "branch_targets": [branch.to_metadata() for branch in self.branches],
            "basic_block_leaders": list(self.basic_block_leaders),
            "unsupported_opcodes": list(self.unsupported_opcodes),
        }


def normalize_lua_functions(
    functions: tuple[LuaFunctionListing, ...],
) -> tuple[NormalizedLuaFunction, ...]:
    return tuple(normalize_lua_function(function) for function in functions)


def normalize_lua_function(function: LuaFunctionListing) -> NormalizedLuaFunction:
    instructions = _normalize_instructions(function.instructions)
    branches = _normalize_branches(function.instructions)
    leaders = _basic_block_leaders(function.instructions, branches)
    unsupported_opcodes = tuple(
        sorted(
            {
                instruction.opcode
                for instruction in function.instructions
                if instruction.opcode in UNSUPPORTED_FOR_LINEAR_LIFTING
            }
        )
    )

    return NormalizedLuaFunction(
        name=function.inferred_name or "<function>",
        source=function.source,
        line_start=function.line_start,
        line_end=function.line_end,
        entry_pc=instructions[0].pc if instructions else None,
        instructions=instructions,
        branches=branches,
        basic_block_leaders=leaders,
        unsupported_opcodes=unsupported_opcodes,
    )


def normalized_functions_metadata(
    functions: tuple[LuaFunctionListing, ...],
) -> list[dict]:
    return [
        normalized.to_metadata() for normalized in normalize_lua_functions(functions)
    ]


def _normalize_instructions(
    instructions: tuple[LuaInstructionListing, ...],
) -> tuple[NormalizedLuaInstruction, ...]:
    return tuple(
        NormalizedLuaInstruction(
            pc=instruction.pc,
            line=instruction.line,
            opcode=instruction.opcode,
            operands=instruction.operands,
            next_pc=_next_pc(instructions, index),
            explicit_target_pc=_target_from_comment(instruction.comment),
        )
        for index, instruction in enumerate(instructions)
    )


def _normalize_branches(
    instructions: tuple[LuaInstructionListing, ...],
) -> tuple[NormalizedLuaBranch, ...]:
    branches: list[NormalizedLuaBranch] = []

    for index, instruction in enumerate(instructions):
        target_pc = _target_from_comment(instruction.comment)
        if target_pc is None:
            continue

        if instruction.opcode in UNCONDITIONAL_JUMP_OPCODES:
            previous = instructions[index - 1] if index > 0 else None
            kind: BranchKind = (
                "conditional-pair"
                if previous is not None and previous.opcode in BRANCH_OPCODES
                else "unconditional"
            )
            branches.append(
                NormalizedLuaBranch(
                    pc=instruction.pc,
                    opcode=instruction.opcode,
                    target_pc=target_pc,
                    kind=kind,
                    fallthrough_pc=_next_pc(instructions, index),
                )
            )

    return tuple(branches)


def _basic_block_leaders(
    instructions: tuple[LuaInstructionListing, ...],
    branches: tuple[NormalizedLuaBranch, ...],
) -> tuple[int, ...]:
    if not instructions:
        return ()

    instruction_pcs = {instruction.pc for instruction in instructions}
    leaders = {instructions[0].pc}

    for branch in branches:
        if branch.target_pc in instruction_pcs:
            leaders.add(branch.target_pc)
        if branch.fallthrough_pc in instruction_pcs:
            leaders.add(branch.fallthrough_pc)

    for index, instruction in enumerate(instructions):
        if instruction.opcode in TERMINATOR_OPCODES:
            next_pc = _next_pc(instructions, index)
            if next_pc in instruction_pcs:
                leaders.add(next_pc)

    return tuple(sorted(leaders))


def _next_pc(
    instructions: tuple[LuaInstructionListing, ...],
    index: int,
) -> int | None:
    next_index = index + 1
    if next_index >= len(instructions):
        return None
    return instructions[next_index].pc


def _target_from_comment(comment: str | None) -> int | None:
    if comment is None:
        return None
    match = TARGET_COMMENT_RE.search(comment)
    if match is None:
        return None
    return int(match.group("target"))
