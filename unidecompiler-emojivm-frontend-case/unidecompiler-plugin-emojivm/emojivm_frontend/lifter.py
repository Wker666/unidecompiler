from __future__ import annotations

from dataclasses import replace

from unidecompiler.core.effects import (
    Binary,
    BuildCall,
    CallStackArgs,
    Pop,
    Push,
    ReturnVoid,
    Swap,
    UnknownOpcode,
)
from unidecompiler.core.ir import BinaryOp, Const, Expr, Global, SourceRef
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_steps, lift_vm_step_function, recover_vm_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import VMRegionOpcodeClasses, VMStatefulCallbacks, build_hint_region_profile

from .model import EmojiInstruction, EmojiVMProgram

FRONTEND_ID = "emojivm"
OPERATOR_NAMES = {
    "add": "+",
    "sub": "-",
    "mul": "*",
    "mod": "%",
    "xor": "^",
    "and": "&",
}
CONTROL = frozenset({"jmp", "jnz", "jz"})
REGION_CLASSES = VMRegionOpcodeClasses(
    control=CONTROL,
    jumps=frozenset({"jmp", "jnz", "jz"}),
    forward_jumps=frozenset({"jmp", "jnz", "jz"}),
    backward_jumps=frozenset({"jmp", "jnz", "jz"}),
    conditional_jumps=frozenset({"jnz", "jz"}),
)


def _effects(_program: EmojiVMProgram, instruction: EmojiInstruction, source: SourceRef):
    opcode = instruction.opcode
    if opcode == "nop":
        return ()
    if opcode == "push":
        return (Push(source=source, value=Const(value=instruction.operands[0], source=source)),)
    if opcode in OPERATOR_NAMES:
        if opcode in {"sub", "mod"}:
            return (
                Swap(source=source, depth=2),
                Binary(source=source, op=OPERATOR_NAMES[opcode], semantics="static"),
            )
        return (Binary(source=source, op=OPERATOR_NAMES[opcode], semantics="static"),)
    if opcode == "lt":
        return (
            Swap(source=source, depth=2),
            Binary(source=source, op="<", semantics="static"),
        )
    if opcode == "eq":
        return (Binary(source=source, op="==", semantics="static"),)
    if opcode == "pop":
        return (Pop(source=source),)
    if opcode == "print":
        return (
            BuildCall(
                source=source,
                callee=Global(name="print", source=source),
                arg_count=1,
                returns=0,
            ),
        )
    if opcode == "puts":
        return (
            CallStackArgs(source=source, callee_name="puts_until_zero", arg_count=1, returns=0),
        )
    if opcode == "load":
        return (
            Swap(source=source, depth=2),
            CallStackArgs(source=source, callee_name="load_byte", arg_count=2),
        )
    if opcode == "store":
        return (
            Swap(source=source, depth=3),
            CallStackArgs(source=source, callee_name="store_byte", arg_count=3, returns=0),
        )
    if opcode == "alloc":
        return (CallStackArgs(source=source, callee_name="alloc", arg_count=1, returns=0),)
    if opcode == "free":
        return (CallStackArgs(source=source, callee_name="free", arg_count=1, returns=0),)
    if opcode == "read":
        return (CallStackArgs(source=source, callee_name="read_buffer", arg_count=1, returns=0),)
    if opcode == "write":
        return (CallStackArgs(source=source, callee_name="write_buffer", arg_count=1, returns=0),)
    if opcode == "halt":
        return (ReturnVoid(source=source),)
    if opcode == "jmp":
        return (Pop(source=source),)
    if opcode in {"jnz", "jz"}:
        # Conditional jumps consume both condition and target at runtime, but
        # the core branch recoverer needs them available to build a low-level
        # CFG Branch.  branch_stack_width() below declares the consumed width.
        return ()
    return (UnknownOpcode(source=source, opcode=opcode, raw=instruction.raw),)


EFFECTS = VMEffectTable(
    opcode_attr="opcode",
    exact={opcode: (lambda program, instruction, source: _effects(program, instruction, source)) for opcode in {
        "nop", "push", "add", "sub", "mul", "mod", "xor", "and", "lt", "eq",
        "pop", "print", "puts", "load", "store", "alloc", "free", "read", "write",
        "halt", "jmp", "jnz", "jz",
    }},
    fallback=lambda _program, instruction, source: (
        UnknownOpcode(source=source, opcode=instruction.opcode, raw=instruction.raw),
    ),
)


def _step(
    program: EmojiVMProgram,
    instruction: EmojiInstruction,
    targets: dict[int, int] | None = None,
) -> VMBytecodeStep:
    source = SourceRef(frontend=FRONTEND_ID, offset=instruction.offset)
    operand_values = instruction.operands
    if targets is not None and instruction.opcode in CONTROL and instruction.offset in targets:
        operand_values = (targets[instruction.offset],)
    operands = tuple(
        VMOperand(role="constant" if instruction.opcode == "push" else "target", value=value, text=str(value))
        for value in operand_values
    )
    decoded = VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=operands,
        raw=instruction.raw,
        artifact_range=instruction.artifact_range,
    )
    hints: tuple[VMHint, ...] = ()
    if targets is not None and instruction.opcode in CONTROL:
        target = targets.get(instruction.offset)
        if target is not None:
            source_hint = VMHint(
                kind="loop-backedge" if target <= instruction.offset else "branch-target",
                source=source,
                target=target,
                label=instruction.opcode,
                detail="target-if-true" if instruction.opcode in {"jnz", "jz"} else None,
                flow="unconditional" if instruction.opcode == "jmp" else "conditional",
            )
            if instruction.opcode in {"jnz", "jz"}:
                condition_hint = VMHint(
                    kind="materialized-condition",
                    source=source,
                    label=instruction.opcode,
                    detail="stack",
                    flow="conditional",
                )
                hints = (source_hint, condition_hint)
            else:
                hints = (source_hint,)
    return VMBytecodeStep(
        opcode=instruction.opcode,
        source=source,
        effects=EFFECTS.effects_for(program, instruction, source),
        raw=instruction.raw,
        decoded=decoded,
        hints=hints,
    )


def _branch_condition(branch: VMBytecodeStep, stack: tuple[Expr, ...]) -> Expr | None:
    if not stack:
        return None
    value = stack[0]
    if branch.opcode == "jnz":
        return BinaryOp(source=value.source, op="!=", left=value, right=Const(value=0, source=value.source), semantics="static")
    return BinaryOp(source=value.source, op="==", left=value, right=Const(value=0, source=value.source), semantics="static")


def _raw_window(instructions: tuple[EmojiInstruction, ...], index: int, radius: int = 3) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(instruction.raw for instruction in instructions[start:end])


def _lift_linear(program: EmojiVMProgram, start: int, end: int, _locals: dict[str, Expr], stack: tuple[Expr, ...]):
    result = lift_steps(
        tuple(_step(program, instruction) for instruction in program.instructions[start:end]),
        initial_locals=_locals,
        initial_stack=stack,
    )
    from unidecompiler.core.vm_region import VMLinearState

    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
        stopped_at=None if result.stopped_at is None else start,
    )


def lift_program(program: EmojiVMProgram, metadata: dict) -> "ModuleIR":
    spec = VMFunctionSpec(
        name="main",
        params=(),
        frontend=FRONTEND_ID,
        instruction_count=len(program.instructions),
        metadata={"filename": program.filename},
    )
    function = recover_vm_function(
        spec,
        lambda: _lift_function(program),
        raw=tuple(instruction.raw for instruction in program.instructions),
    )
    return assemble_vm_module(
        name=program.filename or "<emojivm-program>",
        source_language="EmojiVM",
        metadata={"frontend": metadata, "bytecode_format": "emojivm"},
        functions=(function,),
    )


def _lift_function(program: EmojiVMProgram):
    targets = _static_branch_targets(program)
    steps = tuple(_step(program, instruction, targets) for instruction in program.instructions)
    profile = build_hint_region_profile(
        steps,
        frontend=FRONTEND_ID,
        opcode_classes=REGION_CLASSES,
        raw_window=lambda index: _raw_window(program.instructions, index),
    )
    spec = VMFunctionSpec(
        name="main",
        params=(),
        frontend=FRONTEND_ID,
        instruction_count=len(steps),
    )
    function = lift_vm_step_function(
        spec,
        steps,
        profile=profile,
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=lambda: {},
            lift_linear=lambda start, end, locals_, stack: _lift_linear(program, start, end, locals_, stack),
            branch_condition=_branch_condition,
            branch_stack_width=lambda instruction: 2 if instruction.opcode in {"jnz", "jz"} else 1,
        ),
        raw_window=lambda index: _raw_window(program.instructions, index),
    )
    return _with_displayable_control_metadata(function, program, targets)


def _static_branch_targets(program: EmojiVMProgram) -> dict[int, int]:
    """Recover branch targets that are explicit constants on the EmojiVM stack."""

    targets: dict[int, int] = {}
    stack: list[int | None] = []
    valid_offsets = {instruction.offset for instruction in program.instructions}
    for instruction in program.instructions:
        opcode = instruction.opcode
        if opcode == "push":
            stack.append(int(instruction.operands[0]))
        elif opcode in OPERATOR_NAMES or opcode in {"lt", "eq"}:
            right = stack.pop() if stack else None
            left = stack.pop() if stack else None
            stack.append(_const_binary(opcode, left, right))
        elif opcode == "pop":
            if stack:
                stack.pop()
        elif opcode == "load":
            _pop_many(stack, 2)
            stack.append(None)
        elif opcode in {"store", "print", "puts", "alloc", "free", "read", "write"}:
            _pop_many(stack, {"store": 3, "puts": len(stack)}.get(opcode, 1))
        elif opcode == "jmp":
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[instruction.offset] = target
            _pop_many(stack, 1)
        elif opcode in {"jnz", "jz"}:
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[instruction.offset] = target
            _pop_many(stack, 2)
        elif opcode == "halt":
            stack.clear()
        else:
            stack.clear()
    return targets


def _displayable_branch_targets(
    program: EmojiVMProgram,
    targets: dict[int, int],
) -> dict[int, int]:
    """Avoid control hints that collapse to same-block edges in the public CFG view."""

    if not targets:
        return targets
    offsets = tuple(instruction.offset for instruction in program.instructions)
    offset_set = set(offsets)
    leaders = {offsets[0]}
    offset_to_index = {offset: index for index, offset in enumerate(offsets)}
    for source, target in targets.items():
        if target in offset_set:
            leaders.add(target)
        source_index = offset_to_index.get(source)
        if source_index is not None and source_index + 1 < len(offsets):
            leaders.add(offsets[source_index + 1])
    ordered_leaders = tuple(sorted(leaders))
    if not ordered_leaders:
        return targets

    leader_for_offset = {
        offset: max(leader for leader in ordered_leaders if leader <= offset)
        for offset in offsets
    }
    filtered: dict[int, int] = {}
    for source, target in targets.items():
        source_leader = leader_for_offset.get(source)
        target_leader = leader_for_offset.get(target)
        if source_leader is not None and source_leader == target_leader:
            continue
        filtered[source] = target
    return filtered


def _with_displayable_control_metadata(function, program: EmojiVMProgram, targets: dict[int, int]):
    display_targets = _displayable_branch_targets(program, targets)
    suppressed_offsets = {
        source for source, target in targets.items()
        if display_targets.get(source) != target
    }
    if not suppressed_offsets:
        return function
    rows = []
    for row in function.metadata.get("bytecode_instructions", ()):
        offset = row.get("offset")
        if offset in suppressed_offsets:
            row = {**row, "control": ()}
        rows.append(row)
    return replace(
        function,
        metadata={**function.metadata, "bytecode_instructions": tuple(rows)},
    )


def _pop_many(stack: list[int | None], count: int) -> None:
    for _ in range(min(count, len(stack))):
        stack.pop()


def _const_binary(opcode: str, left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    if opcode == "add":
        return left + right
    if opcode == "mul":
        return left * right
    if opcode == "sub":
        return right - left
    if opcode == "mod":
        return None if left == 0 else right % left
    if opcode == "xor":
        return left ^ right
    if opcode == "and":
        return left & right
    if opcode == "lt":
        return 1 if right < left else 0
    if opcode == "eq":
        return 1 if right == left else 0
    return None
