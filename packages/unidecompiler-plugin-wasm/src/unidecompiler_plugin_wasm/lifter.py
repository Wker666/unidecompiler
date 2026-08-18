from __future__ import annotations

from unidecompiler.core.effects import (
    Binary,
    BuildCall,
    BuildIndirectCall,
    CallStackArgs,
    CallTopAs,
    DropBelowTop,
    DuplicateTop,
    LoadLocal,
    Pop,
    Push,
    ReturnTop,
    SelectValue,
    StoreLocal,
    Unary,
    UnknownOpcode,
)
from unidecompiler.core.ir import BinaryOp, Const, Expr, Global, SourceRef, Var
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectRule, VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_steps, lift_vm_step_function, recover_vm_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import VMRegionOpcodeClasses, VMStatefulCallbacks, build_hint_region_profile
from unidecompiler_plugin_wasm.module import WasmFunctionListing, WasmInstruction, WasmModule


WASM_FRONTEND_ID = "wasm"

BINARY_OPS = {
    "i32.add": "+",
    "i32.sub": "-",
    "i32.mul": "*",
    "i32.div_s": "/",
    "i32.div_u": "/",
    "i32.rem_s": "%",
    "i32.rem_u": "%",
    "i32.and": "&",
    "i32.or": "|",
    "i32.xor": "^",
    "i32.shl": "<<",
    "i32.shr_s": ">>",
    "i32.shr_u": ">>",
    "i64.add": "+",
    "i64.sub": "-",
    "i64.mul": "*",
    "i64.div_s": "/",
    "i64.div_u": "/",
    "i64.rem_s": "%",
    "i64.rem_u": "%",
    "i64.and": "&",
    "i64.or": "|",
    "i64.xor": "^",
    "i64.shl": "<<",
    "i64.shr_s": ">>",
    "i64.shr_u": ">>",
    "f32.add": "+",
    "f32.sub": "-",
    "f32.mul": "*",
    "f32.div": "/",
    "f64.add": "+",
    "f64.sub": "-",
    "f64.mul": "*",
    "f64.div": "/",
}

COMPARE_OPS = {
    "i32.eq": "==",
    "i32.ne": "!=",
    "i32.lt_s": "<",
    "i32.lt_u": "<",
    "i32.gt_s": ">",
    "i32.gt_u": ">",
    "i32.le_s": "<=",
    "i32.le_u": "<=",
    "i32.ge_s": ">=",
    "i32.ge_u": ">=",
    "i64.eq": "==",
    "i64.ne": "!=",
    "i64.lt_s": "<",
    "i64.lt_u": "<",
    "i64.gt_s": ">",
    "i64.gt_u": ">",
    "i64.le_s": "<=",
    "i64.le_u": "<=",
    "i64.ge_s": ">=",
    "i64.ge_u": ">=",
    "f32.eq": "==",
    "f32.ne": "!=",
    "f32.lt": "<",
    "f32.gt": ">",
    "f32.le": "<=",
    "f32.ge": ">=",
    "f64.eq": "==",
    "f64.ne": "!=",
    "f64.lt": "<",
    "f64.gt": ">",
    "f64.le": "<=",
    "f64.ge": ">=",
}

UNARY_OPS = {
    "f32.neg": "-",
    "f64.neg": "-",
}

NUMERIC_CALLS = {
    "f32.abs": "abs",
    "f32.sqrt": "sqrt",
    "f64.abs": "abs",
    "f64.sqrt": "sqrt",
    "i32.clz": "clz",
    "i32.ctz": "ctz",
    "i32.popcnt": "popcnt",
    "i64.clz": "clz",
    "i64.ctz": "ctz",
    "i64.popcnt": "popcnt",
}

BINARY_NUMERIC_CALLS = {
    "i32.rotl": "rotl",
    "i32.rotr": "rotr",
    "i64.rotl": "rotl",
    "i64.rotr": "rotr",
}

SIMD_BINARY_CALLS = {
    "i8x16.swizzle",
    "i32x4.eq",
    "i32x4.lt_s",
    "i32x4.add",
    "i32x4.min_s",
    "f32x4.add",
}

SIMD_UNARY_CALLS = {
    "i32x4.splat",
    "i32x4.neg",
    "i32x4.extract_lane",
}

SIMD_MEMORY_OPS = {"v128.load", "v128.store"}

CONVERSIONS = {
    "i32.extend8_s",
    "i32.extend16_s",
    "i64.extend8_s",
    "i64.extend16_s",
    "i64.extend32_s",
    "i32.wrap/i64",
    "i32.trunc_s/f32",
    "i32.trunc_u/f32",
    "i32.trunc_s/f64",
    "i32.trunc_u/f64",
    "i64.extend_s/i32",
    "i64.extend_u/i32",
    "i64.trunc_s/f32",
    "i64.trunc_u/f32",
    "i64.trunc_s/f64",
    "i64.trunc_u/f64",
    "f32.convert_s/i32",
    "f32.convert_u/i32",
    "f32.convert_s/i64",
    "f32.convert_u/i64",
    "f32.demote/f64",
    "f64.convert_s/i32",
    "f64.convert_u/i32",
    "f64.convert_s/i64",
    "f64.convert_u/i64",
    "f64.promote/f32",
    "i32.reinterpret/f32",
    "i64.reinterpret/f64",
    "f32.reinterpret/i32",
    "f64.reinterpret/i64",
}

CONTROL_OPS = {"block", "loop", "if", "else", "end", "br", "br_if", "br_table"}
IGNORED_OPS = {"nop", "unreachable", "block", "loop"}
WASM_REGION_OPCODE_CLASSES = VMRegionOpcodeClasses(
    noise=frozenset({"nop", "block", "loop", "end"}),
    control=frozenset({"if", "else", "br", "br_if"}),
    jumps=frozenset({"else", "br"}),
    forward_jumps=frozenset({"else", "br"}),
    backward_jumps=frozenset({"br"}),
    conditional_jumps=frozenset({"if", "br_if"}),
)

MEMORY_OPS = {
    "i32.load", "i64.load", "f32.load", "f64.load",
    "i32.load8_s", "i32.load8_u", "i32.load16_s", "i32.load16_u",
    "i64.load8_s", "i64.load8_u", "i64.load16_s", "i64.load16_u", "i64.load32_s", "i64.load32_u",
    "i32.store", "i64.store", "f32.store", "f64.store",
    "i32.store8", "i32.store16", "i64.store8", "i64.store16", "i64.store32",
    "memory.init", "data.drop", "memory.copy", "memory.fill",
}
TABLE_OPS = {
    "table.get", "table.set", "table.init", "elem.drop", "table.copy", "table.grow", "table.size", "table.fill",
}
REFERENCE_OPS = {"ref.null", "ref.is_null", "ref.func"}
SATURATING_CONVERSIONS = {
    "i32.trunc_sat_f32_s", "i32.trunc_sat_f32_u", "i32.trunc_sat_f64_s", "i32.trunc_sat_f64_u",
    "i64.trunc_sat_f32_s", "i64.trunc_sat_f32_u", "i64.trunc_sat_f64_s", "i64.trunc_sat_f64_u",
}
WASM_CORE_OPCODES = (
    set(IGNORED_OPS)
    | {"return", "call", "call_indirect", "drop", "select", "memory.size", "memory.grow", "local.get", "local.set", "local.tee", "global.get", "global.set"}
    | set(BINARY_OPS)
    | set(COMPARE_OPS)
    | set(UNARY_OPS)
    | set(NUMERIC_CALLS)
    | set(BINARY_NUMERIC_CALLS)
    | set(CONVERSIONS)
    | {"i32.eqz", "i64.eqz", "i32.const", "i64.const", "f32.const", "f64.const"}
    | MEMORY_OPS
    | TABLE_OPS
    | REFERENCE_OPS
    | SATURATING_CONVERSIONS
    | SIMD_BINARY_CALLS
    | SIMD_UNARY_CALLS
    | SIMD_MEMORY_OPS
    | {"v128.const", "v128.bitselect"}
)


def _wasm_unknown_opcode_effect(
    _function: WasmFunctionListing,
    instruction: WasmInstruction,
    source: SourceRef,
) -> tuple:
    return (UnknownOpcode(source=source, opcode=instruction.opcode, raw=_wasm_raw_instruction_line(instruction)),)


def _wasm_numeric_facts(opcode: str) -> tuple[str, int | None]:
    if opcode.startswith("f32"):
        return "float", 32
    if opcode.startswith("f64"):
        return "float", 64
    if opcode.startswith("i32"):
        return ("unsigned" if "_u" in opcode else "signed"), 32
    if opcode.startswith("i64"):
        return ("unsigned" if "_u" in opcode else "signed"), 64
    return "default", None


def _wasm_binary(opcode: str, op: str):
    def factory(_function: WasmFunctionListing, _instruction: WasmInstruction, source: SourceRef) -> tuple:
        numeric_domain, bit_width = _wasm_numeric_facts(opcode)
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


def _wasm_unary(op: str):
    def factory(_function: WasmFunctionListing, _instruction: WasmInstruction, source: SourceRef) -> tuple:
        return (Unary(source=source, op=op),)

    return factory


def _wasm_call_top(name: str):
    def factory(_function: WasmFunctionListing, _instruction: WasmInstruction, source: SourceRef) -> tuple:
        return (CallTopAs(source=source, callee_name=name),)

    return factory


def _wasm_call_stack_args(name: str, arg_count: int):
    def factory(_function: WasmFunctionListing, _instruction: WasmInstruction, source: SourceRef) -> tuple:
        return (CallStackArgs(source=source, callee_name=name, arg_count=arg_count),)

    return factory


def _wasm_memory_load(
    _function: WasmFunctionListing,
    instruction: WasmInstruction,
    source: SourceRef,
) -> tuple:
    """Represent a VM memory read as a generic one-operand value operation."""

    return (CallTopAs(source=source, callee_name=instruction.opcode),)


def _wasm_simd_extract_lane(_function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    lane = instruction.operands[0] if instruction.operands else "unknown"
    try:
        lane_value = int(lane)
    except ValueError:
        lane_value = lane
    return (
        Push(source=source, value=Const(value=lane_value, source=source)),
        CallStackArgs(source=source, callee_name=instruction.opcode, arg_count=2),
    )


def _wasm_simd_const(_function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    literal = instruction.operands[0] if instruction.operands else ""
    return (
        Push(source=source, value=Const(value=literal, source=source)),
        CallTopAs(source=source, callee_name=instruction.opcode),
    )


def _wasm_stack_call(name: str, arg_count: int, *, returns: int = 1):
    def factory(_function: WasmFunctionListing, _instruction: WasmInstruction, source: SourceRef) -> tuple:
        return (
            BuildCall(
                source=source,
                callee=Global(name=name, source=source),
                arg_count=arg_count,
                returns=returns,
            ),
        )

    return factory


def _wasm_const(_function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple | None:
    if not instruction.operands:
        return None
    value = instruction.operands[0]
    try:
        parsed = float(value) if instruction.opcode.startswith(("f32", "f64")) else int(value)
    except ValueError:
        parsed = value
    return (Push(source=source, value=Const(value=parsed, source=source)),)


def _wasm_local_load(function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple | None:
    index = _first_int_operand(instruction)
    if index is None:
        return None
    name = _local_name(function, index)
    return (LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)),)


def _wasm_local_store(function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple | None:
    index = _first_int_operand(instruction)
    if index is None:
        return None
    name = _local_name(function, index)
    if instruction.opcode == "local.tee":
        return (
            DuplicateTop(source=source, materialized_name=name),
            StoreLocal(source=source, name=name, target=Var(name=name, source=source)),
            LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)),
        )
    return (StoreLocal(source=source, name=name, target=Var(name=name, source=source)),)


def _wasm_call(_function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    target = instruction.operands[0] if instruction.operands else "unknown"
    arg_count = _wasm_target_function_arg_count(_function, target)
    returns = _wasm_target_function_return_count(_function, target)
    return (
        BuildCall(
            source=source,
            callee=Global(name=f"$func{target}", source=source),
            arg_count=arg_count,
            returns=returns,
        ),
    )


def _wasm_call_indirect(function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    type_index = _first_int_operand(instruction)
    arg_count, returns = _wasm_type_signature(function, type_index)
    return (
        BuildIndirectCall(
            source=source,
            arg_count=arg_count,
            signature=f"type{type_index if type_index is not None else 'unknown'}",
            returns=returns,
        ),
    )


def _wasm_end(function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    if function.instructions and instruction == function.instructions[-1]:
        return (ReturnTop(source=source, empty_is_void=True),)
    return ()


def _wasm_global_load(_function: WasmFunctionListing, instruction: WasmInstruction, source: SourceRef) -> tuple:
    index = _first_int_operand(instruction)
    name = f"global{index}" if index is not None else "global"
    return (Push(source=source, value=Global(name=name, source=source)),)


WASM_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    ignored=frozenset(IGNORED_OPS),
    exact={
        **{opcode: _wasm_binary(opcode, op) for opcode, op in BINARY_OPS.items()},
        **{opcode: _wasm_binary(opcode, op) for opcode, op in COMPARE_OPS.items()},
        **{opcode: _wasm_unary(op) for opcode, op in UNARY_OPS.items()},
        **{opcode: _wasm_call_top(name) for opcode, name in NUMERIC_CALLS.items()},
        **{opcode: _wasm_call_stack_args(name, 2) for opcode, name in BINARY_NUMERIC_CALLS.items()},
        **{opcode: _wasm_call_stack_args(opcode, 2) for opcode in SIMD_BINARY_CALLS},
        **{opcode: _wasm_call_top(opcode) for opcode in SIMD_UNARY_CALLS},
        **{opcode: _wasm_call_top(opcode) for opcode in CONVERSIONS},
        "i32.eqz": lambda _function, _instruction, source: (CallTopAs(source=source, callee_name="is_zero"),),
        "i64.eqz": lambda _function, _instruction, source: (CallTopAs(source=source, callee_name="is_zero"),),
        "drop": lambda _function, _instruction, source: (Pop(source=source, count=1, emit_calls=True),),
        "select": lambda _function, _instruction, source: (SelectValue(source=source),),
        "return": lambda _function, _instruction, source: (ReturnTop(source=source, empty_is_void=True),),
        "if": lambda _function, _instruction, _source: (),
        "else": lambda _function, _instruction, _source: (),
        "br": lambda _function, _instruction, _source: (),
        "br_if": lambda _function, _instruction, _source: (),
        "end": _wasm_end,
        "call": _wasm_call,
        "call_indirect": _wasm_call_indirect,
        "memory.size": lambda _function, _instruction, source: (Push(source=source, value=Global(name="memory.size", source=source)),),
        "memory.grow": lambda _function, _instruction, source: (BuildCall(source=source, callee=Global(name="memory.grow", source=source), arg_count=1),),
        "ref.null": lambda _function, _instruction, source: (Push(source=source, value=Const(value=None, source=source)),),
        "ref.is_null": lambda _function, _instruction, source: (CallTopAs(source=source, callee_name="is_null"),),
        "ref.func": lambda _function, instruction, source: (Push(source=source, value=Global(name=f"$func{instruction.operands[0] if instruction.operands else 'unknown'}", source=source)),),
        "table.get": lambda _function, instruction, source: (BuildCall(source=source, callee=Global(name=f"table{instruction.operands[0] if instruction.operands else ''}.get", source=source), arg_count=1),),
        "table.set": lambda _function, _instruction, source: (Pop(source=source, count=2, allow_missing=True),),
        "table.init": lambda _function, _instruction, source: (Pop(source=source, count=3, allow_missing=True),),
        "elem.drop": lambda _function, _instruction, source: (),
        "table.copy": lambda _function, _instruction, source: (Pop(source=source, count=3, allow_missing=True),),
        "table.grow": lambda _function, instruction, source: (BuildCall(source=source, callee=Global(name=f"table{instruction.operands[0] if instruction.operands else ''}.grow", source=source), arg_count=2),),
        "table.size": lambda _function, instruction, source: (Push(source=source, value=Global(name=f"table{instruction.operands[0] if instruction.operands else ''}.size", source=source)),),
        "table.fill": lambda _function, _instruction, source: (Pop(source=source, count=3, allow_missing=True),),
        "v128.const": _wasm_simd_const,
        "v128.load": lambda _function, _instruction, source: (CallTopAs(source=source, callee_name="v128.load"),),
        "v128.store": _wasm_stack_call("v128.store", 2, returns=0),
        "v128.bitselect": lambda _function, _instruction, source: (
            BuildCall(source=source, callee=Global(name="v128.bitselect", source=source), arg_count=3),
        ),
        "i32x4.extract_lane": _wasm_simd_extract_lane,
        "memory.init": _wasm_stack_call("memory.init", 3, returns=0),
        "memory.copy": _wasm_stack_call("memory.copy", 3, returns=0),
        "memory.fill": _wasm_stack_call("memory.fill", 3, returns=0),
        "data.drop": lambda _function, _instruction, source: (),
    },
    rules=(
        VMEffectRule(
            matches=lambda opcode, _instruction: opcode in MEMORY_OPS and ".load" in opcode,
            factory=_wasm_memory_load,
        ),
        VMEffectRule(matches=lambda opcode, _instruction: opcode.endswith(".const"), factory=_wasm_const),
        VMEffectRule(matches=lambda opcode, _instruction: opcode == "local.get", factory=_wasm_local_load),
        VMEffectRule(matches=lambda opcode, _instruction: opcode in {"local.set", "local.tee"}, factory=_wasm_local_store),
        VMEffectRule(matches=lambda opcode, _instruction: opcode == "global.get", factory=_wasm_global_load),
        VMEffectRule(matches=lambda opcode, _instruction: opcode == "global.set", factory=lambda _function, _instruction, source: (Pop(source=source, count=1, allow_missing=True),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode in SATURATING_CONVERSIONS, factory=lambda _function, instruction, source: (CallTopAs(source=source, callee_name=instruction.opcode),)),
        VMEffectRule(matches=lambda opcode, _instruction: opcode in MEMORY_OPS, factory=lambda _function, _instruction, _source: ()),
    ),
    fallback=_wasm_unknown_opcode_effect,
)


def lift_wasm_module(module: WasmModule, metadata: dict) -> "ModuleIR":
    return assemble_vm_module(
        name=module.filename or "<wasm-module>",
        source_language="wasm",
        metadata={"frontend": metadata, "bytecode_format": "wasm"},
        functions=tuple(_recover_wasm_function(function, module) for function in module.functions),
    )


def _recover_wasm_function(function: WasmFunctionListing, module: WasmModule) -> "FunctionIR":
    spec = VMFunctionSpec(
        name=function.name,
        params=tuple(_parameter_name(index) for index in range(function.param_count)),
        frontend=WASM_FRONTEND_ID,
        instruction_count=len(function.instructions),
        metadata={"index": function.index, "type_index": function.type_index},
    )
    return recover_vm_function(
        spec,
        lambda: lift_wasm_function(_with_module_facts(function, module)),
        raw=tuple(_wasm_raw_instruction_line(instruction) for instruction in function.instructions),
    )


def lift_wasm_function(function: WasmFunctionListing) -> "FunctionIR":
    steps = tuple(_wasm_bytecode_step(function, instruction) for instruction in function.instructions)
    profile = build_hint_region_profile(
        steps,
        frontend=WASM_FRONTEND_ID,
        opcode_classes=WASM_REGION_OPCODE_CLASSES,
        raw_window=lambda index: _wasm_raw_instruction_window(function.instructions, index),
    )
    return lift_vm_step_function(
        VMFunctionSpec(
            name=function.name,
            params=tuple(_parameter_name(index) for index in range(function.param_count)),
            frontend=WASM_FRONTEND_ID,
            instruction_count=len(function.instructions),
            metadata={"index": function.index, "type_index": function.type_index},
        ),
        steps,
        profile=profile,
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=lambda: _initial_locals(function),
            lift_linear=lambda start, end, locals_, stack: _wasm_linear_state(function, function.instructions, start, end, locals_, stack),
            branch_condition=lambda branch, stack: _wasm_branch_condition(branch, stack),
            branch_stack_width=lambda branch: 1 if branch.opcode in {"if", "br_if"} else 0,
        ),
        initial_locals=_initial_locals(function),
        raw_window=lambda index: _wasm_raw_instruction_window(function.instructions, index),
    )


def _wasm_bytecode_step(function: WasmFunctionListing, instruction: WasmInstruction) -> VMBytecodeStep:
    source = SourceRef(frontend=WASM_FRONTEND_ID, offset=instruction.offset)
    decoded = _wasm_decoded_instruction(instruction, source)
    return VMBytecodeStep(
        opcode=decoded.opcode,
        source=source,
        effects=_wasm_instruction_effects(function, instruction, source),
        raw=decoded.raw,
        decoded=decoded,
        hints=_wasm_instruction_hints(instruction, source),
    )


def _wasm_instruction_effects(
    function: WasmFunctionListing,
    instruction: WasmInstruction,
    source: SourceRef,
) -> tuple[object, ...] | None:
    return WASM_EFFECT_TABLE.effects_for(function, instruction, source)


def _wasm_decoded_instruction(instruction: WasmInstruction, source: SourceRef) -> VMDecodedInstruction:
    return VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=tuple(VMOperand(role=_wasm_operand_role(instruction.opcode), value=operand, text=operand) for operand in instruction.operands),
        raw=_wasm_raw_instruction_line(instruction),
    )


def _wasm_operand_role(opcode: str):
    if opcode in CONTROL_OPS:
        return "target"
    if opcode.startswith("local."):
        return "local"
    if opcode.endswith(".const"):
        return "constant"
    if opcode.startswith("call"):
        return "member"
    return "raw"


def _wasm_instruction_hints(instruction: WasmInstruction, source: SourceRef) -> tuple[VMHint, ...]:
    target = _wasm_instruction_target_index(instruction)
    if target is None:
        return ()
    flow = {"br": "unconditional", "br_if": "conditional", "br_table": "multiway"}.get(instruction.opcode)
    return (VMHint(kind="branch-target", source=source, target=target, label=instruction.opcode, flow=flow),)


def _wasm_linear_state(
    function: WasmFunctionListing,
    instructions: tuple[WasmInstruction, ...],
    start: int,
    end: int,
    locals_: dict[str, Expr],
    stack: tuple[Expr, ...],
):
    steps = tuple(_wasm_bytecode_step(function, instruction) for instruction in instructions[start:end])
    result = lift_steps(
        steps,
        initial_locals=locals_,
        initial_stack=stack,
    )
    if result.state.diagnostics or (result.stopped_at is not None and result.state.terminator is None):
        return None
    from unidecompiler.core.vm_region import VMLinearState

    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
    )


def _wasm_branch_condition(branch: VMBytecodeStep, stack: tuple[Expr, ...]) -> Expr | None:
    if not stack:
        return None
    value = stack[-1]
    if branch.opcode == "if":
        return value
    source = value.source
    return BinaryOp(source=source, op="==", left=value, right=Const(value=0, source=source), semantics="static")


def _with_module_facts(function: WasmFunctionListing, module: WasmModule) -> WasmFunctionListing:
    signatures = tuple((candidate.index, candidate.param_count, candidate.result_count) for candidate in module.functions)
    return WasmFunctionListing(
        name=function.name,
        index=function.index,
        type_index=function.type_index,
        param_count=function.param_count,
        result_count=function.result_count,
        local_count=function.local_count,
        instructions=tuple(_with_control_target(function.instructions, index) for index in range(len(function.instructions))),
        function_type_params=function.function_type_params,
        function_type_results=function.function_type_results,
        module_function_signatures=signatures,
        module_types=module.types,
    )


def _with_control_target(instructions: tuple[WasmInstruction, ...], index: int) -> WasmInstruction:
    instruction = instructions[index]
    target_index = _wasm_control_target_index(instructions, index)
    if target_index is None or target_index >= len(instructions):
        return instruction
    operands = (str(instructions[target_index].offset), *instruction.operands)
    return WasmInstruction(offset=instruction.offset, opcode=instruction.opcode, operands=operands)


def _wasm_control_target_index(instructions: tuple[WasmInstruction, ...], index: int) -> int | None:
    instruction = instructions[index]
    if instruction.opcode == "if":
        return _wasm_else_or_end_after_if(instructions, index)
    if instruction.opcode == "else":
        end_index = _wasm_matching_ends(instructions).get(_wasm_matching_if_for_else(instructions, index), index)
        return end_index + 1
    if instruction.opcode not in {"br", "br_if"}:
        return None
    depth = _first_int_operand(instruction)
    if depth is None:
        return None
    labels = _wasm_label_stack_at(instructions, index)
    if depth >= len(labels):
        return None
    label = labels[-1 - depth]
    return label["head"] if label["opcode"] == "loop" else label["end"] + 1


def _wasm_else_or_end_after_if(instructions: tuple[WasmInstruction, ...], if_index: int) -> int | None:
    depth = 0
    for index in range(if_index + 1, len(instructions)):
        opcode = instructions[index].opcode
        if opcode in {"block", "loop", "if"}:
            depth += 1
        elif opcode == "end":
            if depth == 0:
                return index + 1
            depth -= 1
        elif opcode == "else" and depth == 0:
            return index + 1
    return None


def _wasm_matching_if_for_else(instructions: tuple[WasmInstruction, ...], else_index: int) -> int:
    depth = 0
    for index in range(else_index - 1, -1, -1):
        opcode = instructions[index].opcode
        if opcode == "end":
            depth += 1
        elif opcode in {"block", "loop", "if"}:
            if depth == 0 and opcode == "if":
                return index
            depth = max(0, depth - 1)
    return else_index


def _wasm_label_stack_at(instructions: tuple[WasmInstruction, ...], limit: int) -> list[dict[str, int | str]]:
    stack: list[dict[str, int | str]] = []
    matching_ends = _wasm_matching_ends(instructions)
    for index, instruction in enumerate(instructions[:limit]):
        if instruction.opcode in {"block", "loop", "if"}:
            stack.append(
                {
                    "opcode": instruction.opcode,
                    "head": index,
                    "end": matching_ends.get(index, len(instructions) - 1),
                }
            )
        elif instruction.opcode == "end" and stack:
            stack.pop()
    return stack


def _wasm_matching_ends(instructions: tuple[WasmInstruction, ...]) -> dict[int, int]:
    stack: list[int] = []
    pairs: dict[int, int] = {}
    for index, instruction in enumerate(instructions):
        if instruction.opcode in {"block", "loop", "if"}:
            stack.append(index)
        elif instruction.opcode == "end" and stack:
            pairs[stack.pop()] = index
    return pairs


def _wasm_instruction_target_index(instruction: WasmInstruction) -> int | None:
    if instruction.opcode not in {"if", "else", "br", "br_if"}:
        return None
    return _first_int_operand(instruction)


def _wasm_target_function_arg_count(function: WasmFunctionListing, target: str) -> int:
    try:
        index = int(target)
    except ValueError:
        return 0
    for candidate_index, param_count, _result_count in function.module_function_signatures:
        if candidate_index == index:
            return param_count
    return 0


def _wasm_target_function_return_count(function: WasmFunctionListing, target: str) -> int:
    try:
        index = int(target)
    except ValueError:
        return 1
    for candidate_index, _param_count, result_count in function.module_function_signatures:
        if candidate_index == index:
            return result_count
    return 1


def _wasm_type_signature(function: WasmFunctionListing, type_index: int | None) -> tuple[int, int]:
    if type_index is None or type_index >= len(function.module_types):
        return 0, 1
    params, results = function.module_types[type_index]
    return len(params), len(results)


def _first_int_operand(instruction: WasmInstruction) -> int | None:
    if not instruction.operands:
        return None
    try:
        return int(instruction.operands[0])
    except ValueError:
        return None




def _local_name(function: WasmFunctionListing, index: int) -> str:
    if index < function.param_count:
        return _parameter_name(index)
    return f"local{index - function.param_count}"


def _parameter_name(index: int) -> str:
    return f"arg{index}"


def _initial_locals(function: WasmFunctionListing) -> dict[str, Expr]:
    return {name: Var(name=name, source=SourceRef(frontend=WASM_FRONTEND_ID, detail=name)) for name in (_parameter_name(index) for index in range(function.param_count))}


def _wasm_raw_instruction_window(
    instructions: tuple[WasmInstruction, ...],
    index: int,
    radius: int = 3,
) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(_wasm_raw_instruction_line(instruction) for instruction in instructions[start:end])


def _wasm_raw_instruction_line(instruction: WasmInstruction) -> str:
    operands = " ".join(instruction.operands)
    return f"@{instruction.offset} {instruction.opcode}{(' ' + operands) if operands else ''}"
