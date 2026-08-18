from __future__ import annotations

from dataclasses import dataclass

from unidecompiler.core.ir import BasicBlock, Branch, Expr, Return, Stmt, Terminator


@dataclass(frozen=True)
class RawInstruction:
    """VM-agnostic lifted instruction before basic-block formation.

    A frontend or binary lifter may emit one RawInstruction per bytecode/native
    instruction after it has translated VM-specific operands into Universal IR
    statements/terminators.
    """

    address: int
    statements: tuple[Stmt, ...] = ()
    terminator: Terminator | None = None
    branch_condition: Expr | None = None
    branch_target: int | None = None
    fallthrough_target: int | None = None


@dataclass(frozen=True)
class BasicBlockBuildResult:
    blocks: tuple[BasicBlock, ...]
    diagnostics: tuple[str, ...] = ()


def build_basic_blocks(raw_instructions: tuple[RawInstruction, ...]) -> BasicBlockBuildResult:
    if not raw_instructions:
        return BasicBlockBuildResult(blocks=())

    by_address = {instruction.address: instruction for instruction in raw_instructions}
    diagnostics: list[str] = []
    leaders = {raw_instructions[0].address}

    for index, instruction in enumerate(raw_instructions):
        next_address = (
            raw_instructions[index + 1].address
            if index + 1 < len(raw_instructions)
            else None
        )
        if instruction.branch_target is not None:
            _add_leader(leaders, diagnostics, by_address, instruction.branch_target)
        if instruction.fallthrough_target is not None:
            _add_leader(leaders, diagnostics, by_address, instruction.fallthrough_target)
        if instruction.terminator is not None and next_address is not None:
            leaders.add(next_address)

    sorted_leaders = sorted(leaders)
    leader_to_block = {address: f"block_{address}" for address in sorted_leaders}
    blocks: list[BasicBlock] = []
    current_statements: list[Stmt] = []
    current_leader = raw_instructions[0].address

    for index, instruction in enumerate(raw_instructions):
        if instruction.address in leaders and instruction.address != current_leader:
            blocks.append(
                BasicBlock(
                    id=leader_to_block[current_leader],
                    statements=tuple(current_statements),
                    terminator=_fallthrough_to_next_leader(
                        instruction.address,
                        leader_to_block,
                    ),
                )
            )
            current_leader = instruction.address
            current_statements = []

        current_statements.extend(instruction.statements)

        if instruction.branch_target is not None:
            fallthrough_target = instruction.fallthrough_target
            if fallthrough_target is None:
                fallthrough_target = _next_instruction_address(raw_instructions, index)
            if instruction.branch_condition is None:
                diagnostics.append(
                    f"conditional branch at {instruction.address} has no condition"
                )
                terminator = None
            else:
                terminator = Branch(
                    condition=instruction.branch_condition,
                    true_target=leader_to_block.get(
                        instruction.branch_target,
                        f"missing_{instruction.branch_target}",
                    ),
                    false_target=leader_to_block.get(
                        fallthrough_target,
                        f"missing_{fallthrough_target}",
                    ),
                )
            blocks.append(
                BasicBlock(
                    id=leader_to_block[current_leader],
                    statements=tuple(current_statements),
                    terminator=terminator,
                )
            )
            current_statements = []
            next_address = _next_instruction_address(raw_instructions, index)
            if next_address is not None:
                current_leader = next_address
            continue

        if instruction.terminator is not None:
            blocks.append(
                BasicBlock(
                    id=leader_to_block[current_leader],
                    statements=tuple(current_statements),
                    terminator=instruction.terminator,
                )
            )
            current_statements = []
            next_address = _next_instruction_address(raw_instructions, index)
            if next_address is not None:
                current_leader = next_address

    if current_statements:
        blocks.append(
            BasicBlock(
                id=leader_to_block.get(current_leader, f"block_{current_leader}"),
                statements=tuple(current_statements),
                terminator=None,
            )
        )

    return BasicBlockBuildResult(blocks=tuple(blocks), diagnostics=tuple(diagnostics))


def _add_leader(
    leaders: set[int],
    diagnostics: list[str],
    by_address: dict[int, RawInstruction],
    address: int,
) -> None:
    if address not in by_address:
        diagnostics.append(f"target address {address} does not map to an instruction")
        return
    leaders.add(address)


def _fallthrough_to_next_leader(
    next_address: int,
    leader_to_block: dict[int, str],
) -> Terminator | None:
    # Local import avoids a cycle at module import time.
    from unidecompiler.core.ir import Jump

    target = leader_to_block.get(next_address)
    return Jump(target=target) if target is not None else None


def _next_instruction_address(
    raw_instructions: tuple[RawInstruction, ...],
    index: int,
) -> int | None:
    if index + 1 >= len(raw_instructions):
        return None
    return raw_instructions[index + 1].address
