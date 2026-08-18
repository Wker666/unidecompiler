from __future__ import annotations

from unidecompiler.core.ir import (
    BinaryOp,
    Call,
    Const,
    Expr,
    ExprStmt,
    GetAttr,
    GetItem,
    Global,
    Placeholder,
    Raise,
    SourceRef,
    Var,
)
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.effects import (
    AssignValue,
    Binary,
    BuildArrayCall,
    BuildCall,
    CallTopAs,
    Copy,
    Compare,
    DuplicateTop,
    DuplicateTopBelow,
    DuplicateTopWide,
    InvokeMember,
    LoadAttr,
    LoadItem,
    LoadLocal,
    Pop,
    Push,
    RaiseTop,
    ReturnTop,
    ReturnVoid,
    StoreAttr,
    StoreStaticMember,
    StoreLocal,
    StoreItemEffect,
    Swap as SwapEffect,
    UpdateLocal,
    UnknownOpcode,
)
from unidecompiler.core.vm_effect_table import VMEffectRule, VMEffectTable
from unidecompiler.core.vm_function import (
    VMFunctionSpec,
    recover_vm_function,
    lift_steps,
    lift_vm_step_function,
)
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import (
    VMLinearState,
    VMRegionOpcodeClasses,
    VMStatefulCallbacks,
    VMRegionProfile,
    build_hint_region_profile,
)
from unidecompiler_plugin_jvm_class.classfile import (
    JavaClassFile,
    JavaInstruction,
    JavaMethodListing,
)


BINARY_OPS = {
    "iadd": "+",
    "isub": "-",
    "imul": "*",
    "idiv": "/",
    "irem": "%",
    "iand": "&",
    "ior": "|",
    "ishl": "<<",
    "ishr": ">>",
    "iushr": ">>>",
    "ixor": "^",
    "ladd": "+",
    "lsub": "-",
    "lmul": "*",
    "ldiv": "/",
    "lrem": "%",
    "land": "&",
    "lor": "|",
    "lshl": "<<",
    "lshr": ">>",
    "lushr": ">>>",
    "lxor": "^",
    "fadd": "+",
    "fsub": "-",
    "fmul": "*",
    "fdiv": "/",
    "frem": "%",
    "dadd": "+",
    "dsub": "-",
    "dmul": "*",
    "ddiv": "/",
    "drem": "%",
}

INVERTED_INTEGER_COMPARISONS = {
    "if_icmpeq": "!=",
    "if_icmpne": "==",
    "if_icmplt": ">=",
    "if_icmpge": "<",
    "if_icmpgt": "<=",
    "if_icmple": ">",
    "if_acmpeq": "!=",
    "if_acmpne": "==",
}

INVERTED_ZERO_COMPARISONS = {
    "ifeq": "!=",
    "ifne": "==",
    "iflt": ">=",
    "ifge": "<",
    "ifgt": "<=",
    "ifle": ">",
    "ifnull": "!=",
    "ifnonnull": "==",
}

JVM_FRONTEND_ID = "jvm-class"
JVM_REGION_OPCODE_CLASSES = VMRegionOpcodeClasses(
    control=frozenset(
        {
            "goto",
            "goto_w",
            "tableswitch",
            "lookupswitch",
            "jsr",
            "jsr_w",
            "ret",
            *INVERTED_INTEGER_COMPARISONS.keys(),
            *INVERTED_ZERO_COMPARISONS.keys(),
        }
    ),
    jumps=frozenset({"goto", "goto_w"}),
    forward_jumps=frozenset({"goto", "goto_w"}),
    backward_jumps=frozenset({"goto", "goto_w"}),
    conditional_jumps=frozenset((*INVERTED_INTEGER_COMPARISONS.keys(), *INVERTED_ZERO_COMPARISONS.keys())),
)


def _jvm_no_effect(_method: JavaMethodListing, _instruction: JavaInstruction, _source: SourceRef) -> tuple:
    return ()


def _jvm_unknown_opcode_effect(
    _method: JavaMethodListing,
    instruction: JavaInstruction,
    source: SourceRef,
) -> tuple:
    return (UnknownOpcode(source=source, opcode=instruction.opcode, raw=_jvm_raw_instruction_line(instruction)),)


def _jvm_unsupported_control_effect(
    _method: JavaMethodListing,
    instruction: JavaInstruction,
    source: SourceRef,
) -> tuple:
    return (UnknownOpcode(source=source, opcode=instruction.opcode, raw=_jvm_raw_instruction_line(instruction)),)


def _jvm_dup(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (DuplicateTop(source=source, materialized_name=f"local_stack_{instruction.offset}"),)


def _jvm_null(_method: JavaMethodListing, _instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (Push(source=source, value=Const(value=None, source=source)),)


def _jvm_ldc(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (Push(source=source, value=_ldc_expr(instruction.operands, source)),)


def _jvm_new(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (
        Push(
            source=source,
            value=Placeholder(
                source=source,
                token=f"allocation:{instruction.offset}",
                label=_class_name(instruction.operands),
            ),
        ),
    )


def _jvm_newarray(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (BuildArrayCall(source=source, kind=_array_type(instruction.operands)),)


def _jvm_multianewarray(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    dimensions = _last_int_operand(instruction.operands) or 1
    return (
        BuildCall(
            source=source,
            callee=Global(name="new_multi_array", source=source),
            arg_count=dimensions,
        ),
    )


def _jvm_compare_call(name: str):
    def factory(_method: JavaMethodListing, _instruction: JavaInstruction, source: SourceRef) -> tuple:
        return (
            Compare(source=source, op=name),
        )

    return factory


def _jvm_instanceof(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (
        Push(source=source, value=Global(name=_class_name(instruction.operands), source=source)),
        BuildCall(
            source=source,
            callee=Global(name="instanceof", source=source),
            arg_count=2,
            returns=1,
        ),
    )


def _jvm_getfield(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (LoadAttr(source=source, attr=_field_name(instruction.operands)),)


def _jvm_getstatic(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    owner, field_name, _descriptor = _field_ref(instruction.operands)
    return (Push(source=source, value=GetAttr(source=source, obj=Global(name=owner, source=source), attr=field_name)),)


def _jvm_putstatic(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    owner, field_name, _descriptor = _field_ref(instruction.operands)
    return (StoreStaticMember(source=source, owner=owner, field_name=field_name),)


def _jvm_putfield(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (StoreAttr(source=source, attr=_field_name(instruction.operands)),)


def _jvm_invokedynamic(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    name = _method_name(instruction.operands)
    descriptor = _descriptor_from_operands(instruction.operands) or "()Ljava/lang/Object;"
    callable_value = Global(name=f"invokedynamic<{name}:{descriptor}>", source=source)
    arg_count = _method_argument_count(instruction.operands)
    if arg_count == 0:
        return (Push(source=source, value=callable_value),)
    return (
        BuildCall(
            source=source,
            arg_count=arg_count,
            callee=callable_value,
            returns=1,
        ),
    )


def _jvm_invoke(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    owner, method_name, _descriptor = _method_ref(instruction.operands)
    return (
        InvokeMember(
            source=source,
            owner=owner,
            member=method_name,
            arg_count=_method_argument_count(instruction.operands),
            static=instruction.opcode == "invokestatic",
            returns=0 if _method_returns_void(instruction.operands) else 1,
            constructor_type=owner if instruction.opcode == "invokespecial" and method_name == "<init>" else None,
        ),
    )


def _jvm_iinc(method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple | None:
    parts = [part.strip() for part in instruction.operands.split(",", 1)]
    if len(parts) != 2:
        return None
    try:
        local_index = int(parts[0])
        amount = _signed_iinc_amount(int(parts[1]))
    except ValueError:
        return None
    local_name = _local_name(method, local_index)
    return (
        UpdateLocal(
            source=source,
            name=local_name,
            target=Var(name=local_name, source=source),
            value=Const(value=amount, source=source),
        ),
    )


def _signed_iinc_amount(amount: int) -> int:
    if 128 <= amount <= 255:
        return amount - 256
    if 32768 <= amount <= 65535:
        return amount - 65536
    return amount


def _jvm_numeric_facts(opcode: str) -> tuple[str, int | None]:
    if opcode[0] in {"f", "d"}:
        return "float", 32 if opcode[0] == "f" else 64
    if opcode[0] == "i":
        return ("unsigned" if opcode == "iushr" else "signed"), 32
    if opcode[0] == "l":
        return ("unsigned" if opcode == "lushr" else "signed"), 64
    return "default", None


def _jvm_binary(opcode: str, op: str):
    def factory(_method: JavaMethodListing, _instruction: JavaInstruction, source: SourceRef) -> tuple:
        numeric_domain, bit_width = _jvm_numeric_facts(opcode)
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


def _jvm_pop(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (Pop(source=source, count=2 if instruction.opcode == "pop2" else 1, emit_calls=True, allow_missing=instruction.opcode == "pop2"),)


def _jvm_return_top(_method: JavaMethodListing, _instruction: JavaInstruction, source: SourceRef) -> tuple:
    return (ReturnTop(source=source),)


def _jvm_load_local_effect(method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple | None:
    local_index = _load_local_index(instruction.opcode, instruction.operands)
    if local_index is None:
        return None
    local_name = _local_name(method, local_index)
    return (LoadLocal(source=source, name=local_name, fallback=Var(name=local_name, source=source)),)


def _jvm_store_local_effect(method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple | None:
    store_index = _store_local_index(instruction.opcode, instruction.operands)
    if store_index is None:
        return None
    local_name = _local_name(method, store_index)
    return (
        StoreLocal(
            source=source,
            name=local_name,
            target=Var(name=local_name, source=source),
            missing_value=Placeholder(source=source, token=f"stack:{instruction.offset}", label="stack_value"),
        ),
    )


def _jvm_constant_effect(_method: JavaMethodListing, instruction: JavaInstruction, source: SourceRef) -> tuple | None:
    constant = _constant(instruction.opcode, instruction.operands)
    if constant is None:
        return None
    return (Push(source=source, value=Const(value=constant, source=source)),)


JVM_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    exact={
        **{opcode: _jvm_binary(opcode, op) for opcode, op in BINARY_OPS.items()},
        "pop": _jvm_pop,
        "pop2": _jvm_pop,
        "ireturn": _jvm_return_top,
        "lreturn": _jvm_return_top,
        "freturn": _jvm_return_top,
        "areturn": _jvm_return_top,
        "dreturn": _jvm_return_top,
        "return": lambda _method, _instruction, source: (ReturnVoid(source=source),),
        "dup": _jvm_dup,
        "dup2": lambda _method, _instruction, source: (
            DuplicateTopWide(source=source),
        ),
        "dup_x1": lambda _method, _instruction, source: (
            DuplicateTopBelow(source=source, below_count=1),
        ),
        "dup_x2": lambda _method, _instruction, source: (
            Copy(source=source, depth=1),
            SwapEffect(source=source, depth=4),
        ),
        "dup2_x1": lambda _method, _instruction, source: (
            DuplicateTopWide(source=source),
            SwapEffect(source=source, depth=3, allow_missing=True),
        ),
        "dup2_x2": lambda _method, _instruction, source: (
            DuplicateTopWide(source=source),
            SwapEffect(source=source, depth=4, allow_missing=True),
        ),
        "swap": lambda _method, _instruction, source: (SwapEffect(source=source, depth=2),),
        "nop": _jvm_no_effect,
        "iinc": _jvm_iinc,
        "aconst_null": _jvm_null,
        "ldc": _jvm_ldc,
        "ldc_w": _jvm_ldc,
        "ldc2_w": _jvm_ldc,
        "new": _jvm_new,
        "newarray": _jvm_newarray,
        "anewarray": _jvm_newarray,
        "multianewarray": _jvm_multianewarray,
        "arraylength": lambda _method, _instruction, source: (LoadAttr(source=source, attr="length"),),
        "lcmp": _jvm_compare_call("compare"),
        "fcmpl": _jvm_compare_call("compare"),
        "fcmpg": _jvm_compare_call("compare"),
        "dcmpl": _jvm_compare_call("compare"),
        "dcmpg": _jvm_compare_call("compare"),
        "instanceof": _jvm_instanceof,
        "aaload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "iaload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "baload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "caload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "faload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "laload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "daload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "saload": lambda _method, _instruction, source: (LoadItem(source=source),),
        "aastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "iastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "bastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "castore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "fastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "lastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "dastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "sastore": lambda _method, _instruction, source: (StoreItemEffect(source=source),),
        "getfield": _jvm_getfield,
        "getstatic": _jvm_getstatic,
        "putstatic": _jvm_putstatic,
        "putfield": _jvm_putfield,
        "invokedynamic": _jvm_invokedynamic,
        "invokespecial": _jvm_invoke,
        "invokevirtual": _jvm_invoke,
        "invokeinterface": _jvm_invoke,
        "invokestatic": _jvm_invoke,
        "checkcast": _jvm_no_effect,
        "instanceof": lambda _method, _instruction, source: (CallTopAs(source=source, callee_name="instanceof"),),
        "monitorenter": lambda _method, _instruction, source: (Pop(source=source, count=1),),
        "monitorexit": lambda _method, _instruction, source: (Pop(source=source, count=1),),
        "tableswitch": _jvm_unsupported_control_effect,
        "lookupswitch": _jvm_unsupported_control_effect,
        "wide": _jvm_no_effect,
        "breakpoint": _jvm_no_effect,
        "impdep1": _jvm_no_effect,
        "impdep2": _jvm_no_effect,
        "ret": _jvm_no_effect,
        "i2l": _jvm_no_effect,
        "i2d": _jvm_no_effect,
        "i2f": _jvm_no_effect,
        "i2b": _jvm_no_effect,
        "i2c": _jvm_no_effect,
        "i2s": _jvm_no_effect,
        "l2i": _jvm_no_effect,
        "l2f": _jvm_no_effect,
        "l2d": _jvm_no_effect,
        "f2i": _jvm_no_effect,
        "f2l": _jvm_no_effect,
        "f2d": _jvm_no_effect,
        "d2i": _jvm_no_effect,
        "d2l": _jvm_no_effect,
        "d2f": _jvm_no_effect,
        "ineg": lambda _method, _instruction, source: (
            BuildCall(source=source, arg_count=1, callee=Global(name="neg", source=source)),
        ),
        "lneg": lambda _method, _instruction, source: (
            BuildCall(source=source, arg_count=1, callee=Global(name="neg", source=source)),
        ),
        "fneg": lambda _method, _instruction, source: (
            BuildCall(source=source, arg_count=1, callee=Global(name="neg", source=source)),
        ),
        "dneg": lambda _method, _instruction, source: (
            BuildCall(source=source, arg_count=1, callee=Global(name="neg", source=source)),
        ),
        "athrow": lambda _method, _instruction, source: (RaiseTop(source=source),),
        **{opcode: _jvm_no_effect for opcode in JVM_REGION_OPCODE_CLASSES.control},
    },
    rules=(
        VMEffectRule(matches=lambda opcode, instruction: _load_local_index(opcode, instruction.operands) is not None, factory=_jvm_load_local_effect),
        VMEffectRule(matches=lambda opcode, instruction: _store_local_index(opcode, instruction.operands) is not None, factory=_jvm_store_local_effect),
        VMEffectRule(matches=lambda opcode, instruction: _constant(opcode, instruction.operands) is not None, factory=_jvm_constant_effect),
    ),
    fallback=_jvm_unknown_opcode_effect,
)


def lift_java_class(class_file: JavaClassFile, metadata: dict) -> "ModuleIR":
    return assemble_vm_module(
        name=class_file.class_name or class_file.filename or "<jvm-class>",
        source_language="jvm",
        metadata={
            "frontend": metadata,
            "bytecode_format": "class",
        },
        functions=tuple(_recover_java_method(method) for method in class_file.methods),
    )


def _recover_java_method(method: JavaMethodListing) -> "FunctionIR":
    return recover_vm_function(
        _jvm_function_spec(method),
        lambda: lift_java_method(method),
        raw=tuple(_jvm_raw_instruction_line(instruction) for instruction in method.instructions),
    )


def lift_java_method(method: JavaMethodListing) -> "FunctionIR":
    steps = _jvm_bytecode_steps(method)
    return lift_vm_step_function(
        _jvm_function_spec(method),
        steps,
        profile=_jvm_region_profile(steps, method.instructions),
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=lambda: _initial_locals(method),
            lift_linear=lambda start, end, locals, stack: _jvm_linear_state(method, method.instructions, start, end, locals, stack),
            branch_condition=lambda branch, stack: _branch_condition(_jvm_instruction_for_step(method.instructions, branch), stack),
            branch_stack_width=lambda branch: _jvm_branch_stack_width(_jvm_instruction_for_step(method.instructions, branch)),
        ),
        initial_locals=_initial_locals(method),
        raw_window=lambda index: _jvm_raw_instruction_window(method.instructions, index),
    )


def _jvm_bytecode_steps(method: JavaMethodListing) -> tuple[VMBytecodeStep, ...]:
    steps: list[VMBytecodeStep] = []
    for instruction in method.instructions:
        step = _jvm_bytecode_step(method, instruction)
        regions = tuple(
            VMHint(
                kind="exception-region",
                source=step.source,
                target=region.target,
                value={
                    "start": region.start,
                    "end": region.end,
                    "target": region.target,
                    "exception_type": region.exception_type,
                },
                label="protected-region",
            )
            for region in method.exception_regions
            if region.start == instruction.offset
        )
        if regions:
            step = VMBytecodeStep(
                opcode=step.opcode,
                source=step.source,
                effects=step.effects,
                raw=step.raw,
                decoded=step.decoded,
                hints=(*step.hints, *regions),
            )
        steps.append(step)
    return tuple(steps)


def _jvm_source(offset: int | None = None, detail: str | None = None) -> SourceRef:
    return SourceRef(frontend=JVM_FRONTEND_ID, offset=offset, detail=detail)


def _jvm_region_profile(
    steps: tuple[VMBytecodeStep, ...],
    instructions: tuple[JavaInstruction, ...],
) -> VMRegionProfile[VMBytecodeStep]:
    return build_hint_region_profile(
        steps,
        frontend=JVM_FRONTEND_ID,
        opcode_classes=JVM_REGION_OPCODE_CLASSES,
        raw_window=lambda index: _jvm_raw_instruction_window(instructions, index),
    )


def _jvm_linear_state(
    method: JavaMethodListing,
    instructions: tuple[JavaInstruction, ...],
    start: int,
    end: int,
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
) -> VMLinearState | None:
    state = _apply_linear_instructions(
        method,
        instructions[start:end],
        initial_locals,
        initial_stack,
    )
    if state is None:
        return None
    return VMLinearState(
        locals=state.locals,
        stack=tuple(state.stack),
        statements=tuple(state.statements),
        terminator=state.terminator,
    )


def _jvm_branch_stack_width(instruction: JavaInstruction) -> int:
    return 2 if instruction.opcode in INVERTED_INTEGER_COMPARISONS else 1


def _branch_condition(branch: JavaInstruction, stack: tuple[Expr, ...]) -> Expr:
    source = _jvm_source(branch.offset)
    if branch.opcode in INVERTED_INTEGER_COMPARISONS:
        return BinaryOp(
            source=source,
            op=INVERTED_INTEGER_COMPARISONS[branch.opcode],
            left=stack[0],
            right=stack[1],
            semantics="static",
        )
    return BinaryOp(
        source=source,
        op=INVERTED_ZERO_COMPARISONS[branch.opcode],
        left=stack[0],
        right=Const(value=None if branch.opcode in {"ifnull", "ifnonnull"} else 0, source=source),
        semantics="static",
    )


def _apply_linear_instructions(
    method: JavaMethodListing,
    instructions: tuple[JavaInstruction, ...],
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...] = (),
):
    steps = tuple(_jvm_bytecode_step(method, instruction) for instruction in instructions)
    result = lift_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)
    if result.state.diagnostics:
        return None
    if result.stopped_at is not None and result.state.terminator is None:
        return None
    if result.state.terminator is not None and result.stopped_at != steps[-1]:
        return None
    return result.state


def _jvm_bytecode_step(method: JavaMethodListing, instruction: JavaInstruction) -> VMBytecodeStep:
    source = _jvm_source(instruction.offset)
    decoded = _jvm_decoded_instruction(instruction, source)
    return VMBytecodeStep(
        opcode=decoded.opcode,
        source=source,
        effects=_jvm_instruction_effects(method, instruction, source),
        raw=decoded.raw,
        decoded=decoded,
        hints=_jvm_instruction_hints(instruction, source),
    )


def _jvm_decoded_instruction(instruction: JavaInstruction, source: SourceRef) -> VMDecodedInstruction:
    operands = ()
    if instruction.operands:
        operands = (VMOperand(role=_jvm_operand_role(instruction.opcode), value=instruction.operands, text=instruction.operands),)
    return VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=operands,
        raw=_jvm_raw_instruction_line(instruction),
    )


def _jvm_operand_role(opcode: str):
    if opcode.startswith("if") or opcode == "goto" or opcode.endswith("switch"):
        return "target"
    if opcode.startswith(("load", "store")) or "load" in opcode or "store" in opcode:
        return "local"
    if opcode.startswith(("invoke", "get", "put")):
        return "member"
    if opcode in {"ldc", "ldc_w", "ldc2_w"} or opcode.startswith("iconst_"):
        return "constant"
    return "raw"


def _jvm_instruction_hints(instruction: JavaInstruction, source: SourceRef) -> tuple[VMHint, ...]:
    if not _is_jvm_control_opcode(instruction.opcode):
        return ()
    if instruction.opcode == "tableswitch":
        table = _jvm_tableswitch_targets(instruction.operands)
        if table is None:
            return ()
        default, low, _high, targets = table
        hints: list[VMHint] = [VMHint(kind="default-target", source=source, target=default, label=instruction.opcode, flow="multiway")]
        hints.extend(
            VMHint(kind="case-target", source=source, target=target, value=low + index, label=instruction.opcode, flow="multiway")
            for index, target in enumerate(targets)
        )
        return tuple(hints)
    if instruction.opcode == "lookupswitch":
        lookup = _jvm_lookupswitch_targets(instruction.operands)
        if lookup is None:
            return ()
        default, pairs = lookup
        hints = [VMHint(kind="default-target", source=source, target=default, label=instruction.opcode, flow="multiway")]
        hints.extend(
            VMHint(kind="case-target", source=source, target=target, value=value, label=instruction.opcode, flow="multiway")
            for value, target in pairs
        )
        return tuple(hints)
    target = _jvm_branch_target(instruction)
    if target is None:
        return ()
    kind = "loop-backedge" if target <= instruction.offset else "branch-target"
    flow = "unconditional" if instruction.opcode in {"goto", "jsr", "ret"} else "conditional"
    return (VMHint(kind=kind, source=source, target=target, label=instruction.opcode, flow=flow),)


def _jvm_instruction_effects(
    method: JavaMethodListing,
    instruction: JavaInstruction,
    source: SourceRef,
) -> tuple[object, ...] | None:
    return JVM_EFFECT_TABLE.effects_for(method, instruction, source)


def _is_jvm_control_opcode(opcode: str) -> bool:
    return (
        opcode == "goto"
        or opcode.endswith("switch")
        or opcode.startswith("if")
        or opcode in {"jsr", "ret"}
    )


def _jvm_instruction_for_step(
    instructions: tuple[JavaInstruction, ...],
    step: VMBytecodeStep,
) -> JavaInstruction:
    for instruction in instructions:
        if instruction.offset == step.source.offset:
            return instruction
    return instructions[0]


def _jvm_branch_target(instruction: JavaInstruction) -> int | None:
    if instruction.opcode in {"tableswitch", "lookupswitch"}:
        return _jvm_default_switch_target(instruction.operands)
    try:
        return int(instruction.operands.split()[0])
    except (IndexError, ValueError):
        return None


def _jvm_default_switch_target(operands: str) -> int | None:
    try:
        return int(_csv_ints(operands)[0])
    except IndexError:
        return None


def _jvm_tableswitch_targets(operands: str) -> tuple[int, int, int, tuple[int, ...]] | None:
    values = _csv_ints(operands)
    if len(values) < 4:
        return None
    default, low, high, *targets = values
    expected = high - low + 1
    if expected < 0 or len(targets) < expected:
        return None
    return default, low, high, tuple(targets[:expected])


def _jvm_lookupswitch_targets(operands: str) -> tuple[int, tuple[tuple[int, int], ...]] | None:
    values = _csv_ints(operands)
    if len(values) < 3:
        return None
    default, *rest = values
    if len(rest) % 2 != 0:
        return None
    pairs = tuple((rest[index], rest[index + 1]) for index in range(0, len(rest), 2))
    return default, pairs


def _csv_ints(operands: str) -> tuple[int, ...]:
    values: list[int] = []
    for part in operands.replace(",", " ").split():
        try:
            values.append(int(part))
        except ValueError:
            continue
    return tuple(values)


def _parameter_names(method: JavaMethodListing) -> tuple[str, ...]:
    count = _descriptor_parameter_count(method.descriptor or "()V")
    return tuple(f"arg{index}" for index in range(count))


def _descriptor_parameter_count(descriptor: str) -> int:
    params = descriptor.partition(")")[0].removeprefix("(")
    count = 0
    index = 0
    while index < len(params):
        token = params[index]
        if token == "[":
            index += 1
            continue
        if token == "L":
            semicolon = params.find(";", index)
            if semicolon < 0:
                return count
            count += 1
            index = semicolon + 1
            continue
        count += 1
        index += 1
    return count


def _local_name(method: JavaMethodListing, index: int) -> str:
    if not method.is_static and index == 0:
        return "this"
    parameter_slots = _parameter_slots(method)
    if index in parameter_slots:
        return parameter_slots[index]
    return f"local{index}"


def _load_local_index(opcode: str, operands: str) -> int | None:
    for prefix in ("iload_", "aload_", "lload_", "fload_", "dload_"):
        if opcode.startswith(prefix):
            return int(opcode.removeprefix(prefix))
    if opcode in {"iload", "aload", "lload", "fload", "dload"}:
        try:
            return int(operands.split()[0])
        except (IndexError, ValueError):
            return None
    return None


def _store_local_index(opcode: str, operands: str) -> int | None:
    for prefix in ("istore_", "astore_", "lstore_", "fstore_", "dstore_"):
        if opcode.startswith(prefix):
            return int(opcode.removeprefix(prefix))
    if opcode in {"istore", "astore", "lstore", "fstore", "dstore"}:
        try:
            return int(operands.split()[0])
        except (IndexError, ValueError):
            return None
    return None


def _initial_locals(method: JavaMethodListing) -> dict[str, Expr]:
    locals: dict[str, Expr] = {}
    if not method.is_static:
        locals["this"] = Var(
            name="this",
            source=_jvm_source(detail="local:0"),
        )
    for local_index, name in _parameter_slots(method).items():
        locals[name] = Var(
            name=name,
            source=_jvm_source(detail=f"local:{local_index}"),
        )
    return locals


def _constant(opcode: str, operands: str):
    if opcode == "iconst_m1":
        return -1
    if opcode.startswith("iconst_"):
        try:
            return int(opcode.removeprefix("iconst_"))
        except ValueError:
            return None
    if opcode == "dconst_0":
        return 0.0
    if opcode == "dconst_1":
        return 1.0
    if opcode == "lconst_0":
        return 0
    if opcode == "lconst_1":
        return 1
    if opcode == "fconst_0":
        return 0.0
    if opcode == "fconst_1":
        return 1.0
    if opcode == "fconst_2":
        return 2.0
    if opcode in {"bipush", "sipush"}:
        try:
            return int(operands.split()[0])
        except (IndexError, ValueError):
            return None
    return None


def _last_int_operand(operands: str) -> int | None:
    for token in reversed(operands.replace(",", " ").split()):
        try:
            return int(token)
        except ValueError:
            continue
    return None


def _parameter_slots(method: JavaMethodListing) -> dict[int, str]:
    slots: dict[int, str] = {}
    slot = 0 if method.is_static else 1
    for index, width in enumerate(_descriptor_parameter_widths(method.descriptor or "()V")):
        slots[slot] = f"arg{index}"
        slot += width
    return slots


def _descriptor_parameter_widths(descriptor: str) -> tuple[int, ...]:
    params = descriptor.partition(")")[0].removeprefix("(")
    widths: list[int] = []
    index = 0
    while index < len(params):
        token = params[index]
        if token == "[":
            while index < len(params) and params[index] == "[":
                index += 1
            if index < len(params) and params[index] == "L":
                semicolon = params.find(";", index)
                index = len(params) if semicolon < 0 else semicolon + 1
            else:
                index += 1
            widths.append(1)
            continue
        if token == "L":
            semicolon = params.find(";", index)
            if semicolon < 0:
                return tuple(widths)
            widths.append(1)
            index = semicolon + 1
            continue
        widths.append(2 if token in {"J", "D"} else 1)
        index += 1
    return tuple(widths)


def _field_name(operands: str) -> str:
    marker = "// Field "
    if marker not in operands:
        return "field"
    detail = operands.split(marker, 1)[1]
    field = detail.split(":", 1)[0]
    return field.rsplit(".", 1)[-1]


def _method_argument_count(operands: str) -> int:
    descriptor = _descriptor_from_operands(operands)
    if descriptor is None:
        return 0
    return len(_descriptor_parameter_widths(descriptor))


def _descriptor_from_operands(operands: str) -> str | None:
    _owner, _name, descriptor = _method_ref(operands)
    if descriptor is None and ":" in operands:
        descriptor = operands.rsplit(":", 1)[-1].split(",", 1)[0].strip()
    if descriptor is not None and descriptor.startswith("("):
        return descriptor
    return None


def _method_returns_void(operands: str) -> bool:
    descriptor = _descriptor_from_operands(operands)
    return descriptor is not None and descriptor.endswith("V")


def _method_name(operands: str) -> str:
    _owner, name, _descriptor = _method_ref(operands)
    return name


def _method_ref(operands: str) -> tuple[str, str, str | None]:
    for marker in ("// Method ", "// InterfaceMethod ", "// InvokeDynamic "):
        if marker not in operands:
            continue
        detail = operands.split(marker, 1)[1].strip()
        member, descriptor = _split_member_descriptor(detail)
        if marker == "// InvokeDynamic ":
            return ("<dynamic>", member.rsplit(".", 1)[-1], descriptor)
        owner, _, name = member.rpartition(".")
        return (_normalize_owner(owner), name or member, descriptor)
    descriptor = None
    if ":" in operands:
        descriptor = operands.rsplit(":", 1)[-1].split(",", 1)[0].strip()
    return ("<unknown>", "call", descriptor)


def _field_ref(operands: str) -> tuple[str, str, str | None]:
    marker = "// Field "
    if marker not in operands:
        return ("<unknown>", "field", None)
    detail = operands.split(marker, 1)[1].strip()
    member, descriptor = _split_member_descriptor(detail)
    owner, _, name = member.rpartition(".")
    return (_normalize_owner(owner), name or member, descriptor)


def _split_member_descriptor(detail: str) -> tuple[str, str | None]:
    if ":" not in detail:
        return (detail, None)
    member, descriptor = detail.rsplit(":", 1)
    descriptor = descriptor.split(",", 1)[0].strip()
    return (member, descriptor)


def _normalize_owner(owner: str) -> str:
    if not owner:
        return "<unknown>"
    return owner.replace("/", ".")


def _class_name(operands: str) -> str:
    marker = "// class "
    if marker not in operands:
        return "unknown"
    return operands.split(marker, 1)[1].strip().replace("/", ".")


def _array_type(operands: str) -> str:
    if "// class " in operands:
        return _class_name(operands)
    first = operands.split(",", 1)[0].strip()
    return first or "unknown"


def _ldc_expr(operands: str, source: SourceRef) -> Expr:
    if "// String " in operands:
        return Const(value=operands.split("// String ", 1)[1], source=source)
    if "// class " in operands:
        return Global(name=_class_name(operands), source=source)
    for marker, cast in (
        ("// integer ", int),
        ("// long ", int),
        ("// float ", float),
        ("// double ", float),
    ):
        if marker in operands:
            raw = operands.split(marker, 1)[1].strip()
            try:
                return Const(value=cast(raw), source=source)
            except ValueError:
                return Const(value=raw, source=source)
    return Const(value=operands, source=source)


def _jvm_function_spec(method: JavaMethodListing) -> VMFunctionSpec:
    metadata = {}
    if method.is_annotation_member:
        metadata["annotation_member"] = {
            "name": method.name,
            "descriptor": method.descriptor,
            "default": method.annotation_default,
        }
    return VMFunctionSpec(
        name=method.name,
        params=_parameter_names(method),
        frontend=JVM_FRONTEND_ID,
        instruction_count=len(method.instructions),
        metadata=metadata,
    )


def _jvm_raw_instruction_window(
    instructions: tuple[JavaInstruction, ...],
    index: int,
    radius: int = 3,
) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(_jvm_raw_instruction_line(instruction) for instruction in instructions[start:end])


def _jvm_raw_instruction_line(instruction: JavaInstruction) -> str:
    operands = f" {instruction.operands}" if instruction.operands else ""
    return f"@{instruction.offset} {instruction.opcode}{operands}"
