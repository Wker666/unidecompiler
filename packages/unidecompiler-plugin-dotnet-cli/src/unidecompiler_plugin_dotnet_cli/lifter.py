from __future__ import annotations

from unidecompiler.core.effects import (
    Binary,
    BuildArrayCall,
    BuildCall,
    CallTopAs,
    DuplicateTop,
    InvokeMember,
    LoadIndirect,
    LoadAttr,
    LoadItem,
    LoadItemAddress,
    LoadLocal,
    Pop,
    Push,
    RaiseTop,
    ReturnTop,
    StoreAttr,
    StoreIndirect,
    StoreItemEffect,
    StoreLocal,
    StoreStaticMember,
    UnknownOpcode,
)
from unidecompiler.core.ir import BinaryOp, Const, Expr, Global, IndirectRef, SourceRef, Var
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectRule, VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_steps, lift_vm_step_function, recover_vm_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import (
    VMLinearState,
    VMRegionOpcodeClasses,
    VMRegionProfile,
    VMStatefulCallbacks,
    build_hint_region_profile,
)
from unidecompiler_plugin_dotnet_cli.assembly import (
    DotNetAssembly,
    DotNetInstruction,
    DotNetMethodListing,
)


DOTNET_FRONTEND_ID = "dotnet-cli"

BINARY_OPS = {
    "add": "+",
    "add.ovf": "+",
    "add.ovf.un": "+",
    "sub": "-",
    "sub.ovf": "-",
    "sub.ovf.un": "-",
    "mul": "*",
    "mul.ovf": "*",
    "mul.ovf.un": "*",
    "div": "/",
    "div.un": "/",
    "rem": "%",
    "rem.un": "%",
    "and": "&",
    "or": "|",
    "xor": "^",
    "shl": "<<",
    "shr": ">>",
    "shr.un": ">>",
}

COMPARE_OPS = {
    "ceq": "==",
    "cgt": ">",
    "cgt.un": ">",
    "clt": "<",
    "clt.un": "<",
}

CONTROL_OPS = {
    "br",
    "br.s",
    "brfalse",
    "brfalse.s",
    "brtrue",
    "brtrue.s",
    "beq",
    "beq.s",
    "bge",
    "bge.s",
    "bge.un",
    "bge.un.s",
    "bgt",
    "bgt.s",
    "bgt.un",
    "bgt.un.s",
    "ble",
    "ble.s",
    "ble.un",
    "ble.un.s",
    "blt",
    "blt.s",
    "blt.un",
    "blt.un.s",
    "bne.un",
    "bne.un.s",
    "leave",
    "leave.s",
    "switch",
}

DOTNET_CONDITIONAL_OPS = {
    "brfalse": "truthy",
    "brfalse.s": "truthy",
    "brtrue": "falsey",
    "brtrue.s": "falsey",
    "beq": "!=",
    "beq.s": "!=",
    "bne.un": "==",
    "bne.un.s": "==",
    "bge": "<",
    "bge.s": "<",
    "bge.un": "<",
    "bge.un.s": "<",
    "bgt": "<=",
    "bgt.s": "<=",
    "bgt.un": "<=",
    "bgt.un.s": "<=",
    "ble": ">",
    "ble.s": ">",
    "ble.un": ">",
    "ble.un.s": ">",
    "blt": ">=",
    "blt.s": ">=",
    "blt.un": ">=",
    "blt.un.s": ">=",
}

DOTNET_REGION_OPCODE_CLASSES = VMRegionOpcodeClasses(
    control=frozenset(CONTROL_OPS),
    jumps=frozenset({"br", "br.s", "leave", "leave.s"}),
    forward_jumps=frozenset({"br", "br.s", "leave", "leave.s"}),
    backward_jumps=frozenset({"br", "br.s", "leave", "leave.s"}),
    conditional_jumps=frozenset(DOTNET_CONDITIONAL_OPS),
)

IGNORED_OPS = {
    "nop",
    "constrained.",
    "readonly.",
    "tail.",
    "volatile.",
    "unaligned.",
    "prefix",
    "castclass",
    "box",
    "unbox",
    "unbox.any",
    "conv.i",
    "conv.i1",
    "conv.i2",
    "conv.i4",
    "conv.i8",
    "conv.u",
    "conv.u1",
    "conv.u2",
    "conv.u4",
    "conv.u8",
    "conv.r4",
    "conv.r8",
    "conv.r.un",
    "conv.ovf.i",
    "conv.ovf.i.un",
    "conv.ovf.i1.un",
    "conv.ovf.i2.un",
    "conv.ovf.i4.un",
    "conv.ovf.i8.un",
    "conv.ovf.u",
    "conv.ovf.u.un",
    "conv.ovf.u1.un",
    "conv.ovf.u2.un",
    "conv.ovf.u4.un",
    "conv.ovf.u8.un",
    "endfinally",
    "endfilter",
    "rethrow",
    *CONTROL_OPS,
}


def _dotnet_no_effect(_method: DotNetMethodListing, _instruction: DotNetInstruction, _source: SourceRef) -> tuple:
    return ()


def _dotnet_unknown_opcode_effect(
    _method: DotNetMethodListing,
    instruction: DotNetInstruction,
    source: SourceRef,
) -> tuple:
    return (UnknownOpcode(source=source, opcode=instruction.opcode, raw=_dotnet_raw_instruction_line(instruction)),)


def _dotnet_numeric_facts(opcode: str) -> tuple[str, int | None]:
    if opcode.endswith(".un") or opcode == "shr.un":
        return "unsigned", 32
    return "default", None


def _dotnet_binary(opcode: str, op: str):
    def factory(_method: DotNetMethodListing, _instruction: DotNetInstruction, source: SourceRef) -> tuple:
        numeric_domain, bit_width = _dotnet_numeric_facts(opcode)
        return (
            Binary(
                source=source,
                op=op,
                semantics="static",
                numeric_domain=numeric_domain,
                bit_width=bit_width,
            ),
        )

    return factory


def _dotnet_compare(opcode: str, op: str):
    def factory(_method: DotNetMethodListing, _instruction: DotNetInstruction, source: SourceRef) -> tuple:
        numeric_domain, bit_width = _dotnet_numeric_facts(opcode)
        return (
            Binary(
                source=source,
                op=op,
                semantics="static",
                numeric_domain=numeric_domain,
                bit_width=bit_width,
            ),
        )

    return factory


def _dotnet_constant_effect(_method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple | None:
    value = _constant_value(instruction)
    if value is None:
        return None
    return (Push(source=source, value=Const(value=value, source=source)),)


def _dotnet_load_local_effect(method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple | None:
    index = _load_index(instruction.opcode, instruction.operands, "ldloc", include_address=True)
    if index is None:
        return None
    name = _local_name(method, index)
    return (LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)),)


def _dotnet_store_local_effect(method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple | None:
    index = _load_index(instruction.opcode, instruction.operands, "stloc")
    if index is None:
        return None
    name = _local_name(method, index)
    return (StoreLocal(source=source, name=name, target=Var(name=name, source=source)),)


def _dotnet_load_arg_effect(method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple | None:
    index = _load_index(instruction.opcode, instruction.operands, "ldarg", include_address=True)
    if index is None:
        return None
    name = _argument_name(method, index)
    if instruction.opcode.startswith("ldarga"):
        return (Push(source=source, value=IndirectRef(source=source, target=Var(name=name, source=source))),)
    return (LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)),)


def _dotnet_store_arg_effect(method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple | None:
    index = _load_index(instruction.opcode, instruction.operands, "starg")
    if index is None:
        return None
    name = _argument_name(method, index)
    return (StoreLocal(source=source, name=name, target=IndirectRef(source=source, target=Var(name=name, source=source))),)


def _dotnet_call(_method: DotNetMethodListing, instruction: DotNetInstruction, source: SourceRef) -> tuple:
    if instruction.opcode in {"call", "callvirt", "newobj", "jmp"} and not instruction.member_name:
        return _dotnet_unknown_opcode_effect(_method, instruction, source)
    arg_count = instruction.arg_count or 0
    returns = 0 if instruction.returns_void else "unknown"
    if instruction.opcode == "newobj":
        return (
            BuildCall(
                source=source,
                callee=Global(name=instruction.member_name or _member_token_name(instruction.operands), source=source),
                arg_count=arg_count,
                returns="unknown",
            ),
        )
    if instruction.is_static is False or instruction.opcode == "callvirt":
        return (
            InvokeMember(
                source=source,
                owner=instruction.owner_name or "",
                member=instruction.member_name or _member_token_name(instruction.operands),
                arg_count=arg_count,
                static=False,
                returns=returns,
            ),
        )
    if instruction.owner_name and instruction.member_name:
        return (
            InvokeMember(
                source=source,
                owner=instruction.owner_name,
                member=instruction.member_name,
                arg_count=arg_count,
                static=True,
                returns=returns,
            ),
        )
    return (
        BuildCall(
            source=source,
            callee=Global(name=_member_token_name(instruction.operands), source=source),
            arg_count=arg_count,
            returns=returns,
        ),
    )


DOTNET_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    ignored=frozenset(IGNORED_OPS),
    exact={
        **{opcode: _dotnet_binary(opcode, op) for opcode, op in BINARY_OPS.items()},
        **{opcode: _dotnet_compare(opcode, op) for opcode, op in COMPARE_OPS.items()},
        "ldnull": lambda _method, _instruction, source: (Push(source=source, value=Const(value=None, source=source)),),
        "dup": lambda _method, instruction, source: (DuplicateTop(source=source, materialized_name=f"local_stack_{instruction.offset}"),),
        "pop": lambda _method, _instruction, source: (Pop(source=source, count=1, emit_calls=True),),
        "neg": lambda _method, _instruction, source: (CallTopAs(source=source, callee_name="neg"),),
        "not": lambda _method, _instruction, source: (CallTopAs(source=source, callee_name="bitnot"),),
        "call": _dotnet_call,
        "callvirt": _dotnet_call,
        "newobj": _dotnet_call,
        "jmp": _dotnet_call,
        "ldftn": lambda _method, instruction, source: (Push(source=source, value=Global(name=_member_token_name(instruction.operands), source=source)),),
        "ldvirtftn": lambda _method, instruction, source: (Push(source=source, value=Global(name=_member_token_name(instruction.operands), source=source)),),
        "initobj": lambda _method, _instruction, source: (Pop(source=source, count=1, allow_missing=True),),
        "arglist": lambda _method, _instruction, source: (Push(source=source, value=Global(name="arglist", source=source)),),
        "ckfinite": lambda _method, _instruction, source: (CallTopAs(source=source, callee_name="check_finite"),),
        "cpblk": lambda _method, _instruction, source: (Pop(source=source, count=3, allow_missing=True),),
        "cpobj": lambda _method, _instruction, source: (Pop(source=source, count=2, allow_missing=True),),
        "initblk": lambda _method, _instruction, source: (Pop(source=source, count=3, allow_missing=True),),
        "localloc": lambda _method, _instruction, source: (BuildCall(source=source, callee=Global(name="localloc", source=source), arg_count=1),),
        "mkrefany": lambda _method, instruction, source: (BuildCall(source=source, callee=Global(name=f"mkrefany<{instruction.operands or 'type'}>", source=source), arg_count=1),),
        "refanytype": lambda _method, _instruction, source: (CallTopAs(source=source, callee_name="refanytype"),),
        "refanyval": lambda _method, instruction, source: (BuildCall(source=source, callee=Global(name=f"refanyval<{instruction.operands or 'type'}>", source=source), arg_count=1),),
        "sizeof": lambda _method, instruction, source: (Push(source=source, value=Global(name=f"sizeof<{instruction.operands or 'type'}>", source=source)),),
        "ldstr": lambda _method, instruction, source: (Push(source=source, value=Const(value=instruction.operands, source=source)),),
        "ldtoken": lambda _method, instruction, source: (Push(source=source, value=Global(name=instruction.operands, source=source)),),
        "isinst": lambda _method, instruction, source: (BuildCall(source=source, callee=Global(name="instanceof", source=source), arg_count=1, returns=1),),
        "ldobj": lambda _method, instruction, source: (Pop(source=source, count=1, allow_missing=True), Push(source=source, value=Global(name=_member_token_name(instruction.operands), source=source))),
        "ldfld": lambda _method, instruction, source: (LoadAttr(source=source, attr=_member_name(instruction)),),
        "ldflda": lambda _method, instruction, source: (LoadAttr(source=source, attr=_member_name(instruction)),),
        "stfld": lambda _method, instruction, source: (StoreAttr(source=source, attr=_member_name(instruction)),),
        "ldsfld": lambda _method, instruction, source: (Push(source=source, value=Global(name=_member_token_name(instruction.operands), source=source)),),
        "ldsflda": lambda _method, instruction, source: (Push(source=source, value=Global(name=_member_token_name(instruction.operands), source=source)),),
        "stsfld": lambda _method, instruction, source: (
            StoreStaticMember(
                source=source,
                owner=_member_owner(instruction),
                field_name=_member_name(instruction),
            ),
        ),
        "ldlen": lambda _method, _instruction, source: (LoadAttr(source=source, attr="length"),),
        "newarr": lambda _method, instruction, source: (BuildArrayCall(source=source, kind=instruction.operands or "array"),),
        "throw": lambda _method, _instruction, source: (RaiseTop(source=source),),
        "ret": lambda _method, _instruction, source: (ReturnTop(source=source, empty_is_void=True),),
    },
    rules=(
        VMEffectRule(matches=lambda opcode, instruction: _constant_value(instruction) is not None, factory=_dotnet_constant_effect),
        VMEffectRule(matches=lambda opcode, instruction: _load_index(opcode, instruction.operands, "ldloc", include_address=True) is not None, factory=_dotnet_load_local_effect),
        VMEffectRule(matches=lambda opcode, instruction: _load_index(opcode, instruction.operands, "stloc") is not None, factory=_dotnet_store_local_effect),
        VMEffectRule(matches=lambda opcode, instruction: _load_index(opcode, instruction.operands, "ldarg", include_address=True) is not None, factory=_dotnet_load_arg_effect),
        VMEffectRule(matches=lambda opcode, instruction: _load_index(opcode, instruction.operands, "starg") is not None, factory=_dotnet_store_arg_effect),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.startswith("ldind."), factory=lambda _method, _instruction, source: (LoadIndirect(source=source),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.startswith("stind."), factory=lambda _method, _instruction, source: (StoreIndirect(source=source),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.startswith("ldelema"), factory=lambda _method, _instruction, source: (LoadItemAddress(source=source),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.startswith("ldelem"), factory=lambda _method, _instruction, source: (LoadItem(source=source),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.startswith("stelem"), factory=lambda _method, _instruction, source: (StoreItemEffect(source=source),)),
    ),
    fallback=_dotnet_unknown_opcode_effect,
)


def lift_dotnet_assembly(assembly: DotNetAssembly, metadata: dict) -> "ModuleIR":
    return assemble_vm_module(
        name=assembly.name,
        source_language="dotnet",
        metadata={"frontend": metadata, "bytecode_format": "cli-assembly"},
        functions=tuple(_recover_dotnet_method(method) for method in assembly.methods),
    )


def _recover_dotnet_method(method: DotNetMethodListing) -> "FunctionIR":
    spec = VMFunctionSpec(
        name=method.name,
        params=tuple(_parameter_name(method, index) for index in range(method.param_count)),
        frontend=DOTNET_FRONTEND_ID,
        instruction_count=len(method.instructions),
        metadata={"token": f"0x{method.token:08x}", "rva": method.rva},
    )
    return recover_vm_function(
        spec,
        lambda: lift_dotnet_method(method),
        raw=tuple(_dotnet_raw_instruction_line(instruction) for instruction in method.instructions),
    )


def lift_dotnet_method(method: DotNetMethodListing) -> "FunctionIR":
    steps = _dotnet_bytecode_steps(method)
    return lift_vm_step_function(
        VMFunctionSpec(
            name=method.name,
            params=tuple(_parameter_name(method, index) for index in range(method.param_count)),
            frontend=DOTNET_FRONTEND_ID,
            instruction_count=len(method.instructions),
            metadata={"token": f"0x{method.token:08x}", "rva": method.rva},
        ),
        steps,
        profile=_dotnet_region_profile(steps, method.instructions),
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=lambda: _initial_locals(method),
            lift_linear=lambda start, end, locals, stack: _dotnet_linear_state(method, method.instructions, start, end, locals, stack),
            branch_condition=lambda branch, stack: _dotnet_branch_condition(_dotnet_instruction_for_step(method.instructions, branch), stack),
            branch_stack_width=lambda branch: _dotnet_branch_stack_width(_dotnet_instruction_for_step(method.instructions, branch)),
        ),
        initial_locals=_initial_locals(method),
        raw_window=lambda index: _dotnet_raw_instruction_window(method.instructions, index),
    )


def _dotnet_region_profile(
    steps: tuple[VMBytecodeStep, ...],
    instructions: tuple[DotNetInstruction, ...],
) -> VMRegionProfile[VMBytecodeStep]:
    return build_hint_region_profile(
        steps,
        frontend=DOTNET_FRONTEND_ID,
        opcode_classes=DOTNET_REGION_OPCODE_CLASSES,
        raw_window=lambda index: _dotnet_raw_instruction_window(instructions, index),
    )


def _dotnet_linear_state(
    method: DotNetMethodListing,
    instructions: tuple[DotNetInstruction, ...],
    start: int,
    end: int,
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
) -> VMLinearState | None:
    steps = tuple(_dotnet_bytecode_step(method, instruction) for instruction in instructions[start:end])
    result = lift_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)
    if result.state.diagnostics:
        return None
    if result.stopped_at is not None and result.state.terminator is None:
        return None
    if result.state.terminator is not None and result.stopped_at != steps[-1]:
        return None
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
    )


def _dotnet_instruction_for_step(
    instructions: tuple[DotNetInstruction, ...],
    step: VMBytecodeStep,
) -> DotNetInstruction:
    for instruction in instructions:
        if instruction.offset == step.source.offset:
            return instruction
    return instructions[0]


def _dotnet_branch_stack_width(instruction: DotNetInstruction) -> int:
    if instruction.opcode == "switch":
        return 1
    op = DOTNET_CONDITIONAL_OPS.get(instruction.opcode)
    return 2 if op in {"!=", "==", "<", "<=", ">", ">="} else 1


def _dotnet_branch_condition(instruction: DotNetInstruction, stack: tuple[Expr, ...]) -> Expr:
    source = SourceRef(frontend=DOTNET_FRONTEND_ID, offset=instruction.offset)
    op = DOTNET_CONDITIONAL_OPS[instruction.opcode]
    if op == "truthy":
        return BinaryOp(source=source, op="!=", left=stack[0], right=Const(value=0, source=source), semantics="static")
    if op == "falsey":
        return BinaryOp(source=source, op="==", left=stack[0], right=Const(value=0, source=source), semantics="static")
    return BinaryOp(source=source, op=op, left=stack[0], right=stack[1], semantics="static")


def _dotnet_bytecode_step(method: DotNetMethodListing, instruction: DotNetInstruction) -> VMBytecodeStep:
    source = SourceRef(frontend=DOTNET_FRONTEND_ID, offset=instruction.offset)
    decoded = _dotnet_decoded_instruction(instruction, source)
    return VMBytecodeStep(
        opcode=decoded.opcode,
        source=source,
        effects=_dotnet_instruction_effects(method, instruction, source),
        raw=decoded.raw,
        decoded=decoded,
        hints=_dotnet_instruction_hints(instruction, source),
    )


def _dotnet_bytecode_steps(method: DotNetMethodListing) -> tuple[VMBytecodeStep, ...]:
    steps: list[VMBytecodeStep] = []
    instructions = method.instructions
    for index, instruction in enumerate(instructions):
        step = _dotnet_bytecode_step(method, instruction)
        if _is_materialized_condition_branch(instructions, index):
            step = VMBytecodeStep(
                opcode=step.opcode,
                source=step.source,
                effects=step.effects,
                raw=step.raw,
                decoded=step.decoded,
                hints=(*step.hints, VMHint(kind="materialized-condition", source=step.source, label=instruction.opcode)),
            )
        steps.append(step)
    return tuple(steps)


def _is_materialized_condition_branch(instructions: tuple[DotNetInstruction, ...], index: int) -> bool:
    if instructions[index].opcode not in {"brtrue", "brtrue.s", "brfalse", "brfalse.s"} or index < 2:
        return False
    return instructions[index - 1].opcode.startswith("ldloc") and instructions[index - 2].opcode.startswith("stloc")


def _dotnet_decoded_instruction(instruction: DotNetInstruction, source: SourceRef) -> VMDecodedInstruction:
    operands = ()
    if instruction.operands:
        operands = (
            VMOperand(
                role=_dotnet_operand_role(instruction.opcode),
                value=instruction.operands,
                text=instruction.operands,
            ),
        )
    return VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=operands,
        raw=_dotnet_raw_instruction_line(instruction),
    )


def _dotnet_instruction_effects(
    method: DotNetMethodListing,
    instruction: DotNetInstruction,
    source: SourceRef,
) -> tuple[object, ...] | None:
    return DOTNET_EFFECT_TABLE.effects_for(method, instruction, source)


def _dotnet_operand_role(opcode: str):
    if opcode in CONTROL_OPS or opcode == "switch":
        return "target"
    if "arg" in opcode or "loc" in opcode:
        return "local"
    if opcode.startswith(("call", "newobj", "ldfld", "stfld", "ldsfld", "stsfld")):
        return "member"
    if opcode.startswith("ldc") or opcode == "ldstr":
        return "constant"
    return "raw"


def _dotnet_instruction_hints(instruction: DotNetInstruction, source: SourceRef) -> tuple[VMHint, ...]:
    if instruction.opcode not in CONTROL_OPS:
        return ()
    if instruction.opcode == "switch":
        targets = _branch_targets(instruction)
        default_target = _dotnet_switch_default_target(instruction)
        hints = [VMHint(kind="default-target", source=source, target=default_target, label=instruction.opcode, flow="multiway")]
        hints.extend(
            VMHint(kind="case-target", source=source, target=target, value=index, label=instruction.opcode, flow="multiway")
            for index, target in enumerate(targets)
        )
        return tuple(hints)
    targets = _branch_targets(instruction)
    flow = "unconditional" if instruction.opcode in {"br", "br.s", "leave", "leave.s"} else "conditional"
    return tuple(
        VMHint(
            kind="loop-backedge" if target <= instruction.offset else "branch-target",
            source=source,
            target=target,
            label=instruction.opcode,
            flow=flow,
        )
        for target in targets
    )


def _branch_targets(instruction: DotNetInstruction) -> tuple[int, ...]:
    targets: list[int] = []
    for raw in instruction.operands.split(","):
        try:
            targets.append(int(raw.strip()))
        except ValueError:
            continue
    return tuple(targets)


def _dotnet_switch_default_target(instruction: DotNetInstruction) -> int | None:
    if instruction.operand_kind != "InlineSwitch":
        return None
    count = len(_branch_targets(instruction))
    return instruction.offset + 1 + 4 + (count * 4)


def _constant_value(instruction: DotNetInstruction):
    opcode = instruction.opcode
    if opcode == "ldc.i4.m1":
        return -1
    if opcode in {"ldc.i4.s", "ldc.i4", "ldc.i8"}:
        try:
            return int(instruction.operands)
        except ValueError:
            return None
    if opcode.startswith("ldc.i4."):
        try:
            return int(opcode.removeprefix("ldc.i4."))
        except ValueError:
            return None
    if opcode in {"ldc.r4", "ldc.r8"}:
        try:
            return float(instruction.operands)
        except ValueError:
            return None
    return None


def _load_index(opcode: str, operands: str, family: str, *, include_address: bool = False) -> int | None:
    if opcode == family or opcode == f"{family}.s":
        try:
            return int(operands.split()[0])
        except (IndexError, ValueError):
            return None
    if include_address and (opcode == f"{family}a" or opcode == f"{family}a.s"):
        try:
            return int(operands.split()[0])
        except (IndexError, ValueError):
            return None
    prefix = f"{family}."
    if opcode.startswith(prefix):
        suffix = opcode.removeprefix(prefix)
        if suffix.isdigit():
            return int(suffix)
    return None


def _argument_name(method: DotNetMethodListing, index: int) -> str:
    if not method.is_static and index == 0:
        return "this"
    parameter_index = index if method.is_static else index - 1
    return _parameter_name(method, parameter_index) if 0 <= parameter_index < method.param_count else f"arg{index}"


def _parameter_name(_method: DotNetMethodListing, index: int) -> str:
    return f"arg{index}"


def _local_name(_method: DotNetMethodListing, index: int) -> str:
    return f"local{index}"


def _initial_locals(method: DotNetMethodListing) -> dict[str, Expr]:
    locals_: dict[str, Expr] = {}
    if not method.is_static:
        locals_["this"] = Var(name="this", source=SourceRef(frontend=DOTNET_FRONTEND_ID, detail="arg:0"))
    for index in range(method.param_count):
        name = _parameter_name(method, index)
        locals_[name] = Var(name=name, source=SourceRef(frontend=DOTNET_FRONTEND_ID, detail=f"arg:{index}"))
    return locals_


def _member_token_name(operand: str) -> str:
    return operand or "<metadata-token>"


def _member_name(instruction: DotNetInstruction) -> str:
    return instruction.member_name or _member_token_name(instruction.operands)


def _member_owner(instruction: DotNetInstruction) -> str:
    return instruction.owner_name or "<static>"


def _dotnet_raw_instruction_window(
    instructions: tuple[DotNetInstruction, ...],
    index: int,
    radius: int = 3,
) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(_dotnet_raw_instruction_line(instruction) for instruction in instructions[start:end])


def _dotnet_raw_instruction_line(instruction: DotNetInstruction) -> str:
    operands = f" {instruction.operands}" if instruction.operands else ""
    return f"IL_{instruction.offset:04x}: {instruction.opcode}{operands}"
