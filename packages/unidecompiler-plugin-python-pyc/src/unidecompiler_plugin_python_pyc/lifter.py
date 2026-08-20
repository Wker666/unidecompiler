from __future__ import annotations

from unidecompiler.core.effects import (
    AssignValue,
    Binary,
    BuildArray,
    BuildConstKeyMap,
    BuildCall,
    BuildMap,
    BuildSet,
    BuildString,
    BuildShapeTest,
    BuildArrayCall,
    CallTopAs,
    Copy,
    DropBelowTop,
    Effect,
    Compare,
    ExceptionMatch,
    Emit,
    ExtendArray,
    FormatTop,
    LoadAttr,
    LoadAttrFromTop,
    LoadSuperAttr,
    LoadItem,
    LoadLocal,
    Invoke,
    InvokeExpanded,
    InvokeMethod,
    InvokeKw,
    Iterate,
    MakeFunctionValue,
    MergeMap,
    Pop,
    Push,
    RaiseTop,
    RaiseWithCause,
    ReraiseTop,
    ReturnTop,
    ReturnVoid,
    StoreAttr,
    StoreLocal,
    StoreMany,
    StoreManyFromPopOrder,
    StoreItemAtDepth,
    StoreItemEffect,
    SetAdd,
    Swap,
    Truthy,
    Unpack,
    UnknownOpcode,
    YieldTop,
    Unary,
)
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.ir import (
    Assign,
    BinaryOp,
    CapturedVar,
    Const,
    Expr,
    ExprStmt,
    GetAttr,
    Global,
    MapLiteral,
    Raise,
    SourceRef,
    Var,
    Yield,
)
from unidecompiler.core.vm_effect_table import VMEffectRule, VMEffectTable
from unidecompiler.core.vm_function import (
    VMFunctionSpec,
    recover_vm_function,
    lift_vm_step_function,
    lift_steps,
)
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.provenance import ByteRange
from unidecompiler.core.vm_region import (
    VMLinearState,
    VMRegionCallbacks,
    VMRegionOpcodeClasses,
    VMRegionProfile,
    VMStatefulCallbacks,
    build_hint_region_profile,
    lift_control_region as lift_vm_control_region,
)
from unidecompiler.core.vm_structures import (
    vm_unsupported,
)
from unidecompiler_plugin_python_pyc.pyc import PycCodeObject, PycExceptionRegion, PycModule


BINARY_SYMBOLS = {
    "+": "+",
    "+=": "+",
    "-": "-",
    "-=": "-",
    "*": "*",
    "*=": "*",
    "/": "/",
    "/=": "/",
    "//": "//",
    "//=": "//",
    "%": "%",
    "%=": "%",
    "^": "^",
    "^=": "^",
    "&": "&",
    "&=": "&",
    "|": "|",
    "|=": "|",
    "<<": "<<",
    "<<=": "<<",
    ">>": ">>",
    ">>=": ">>",
}
COMPLEX_OPS = {
}
IGNORED_OPS = {
    "RESUME",
    "CACHE",
    "NOP",
    "EXTENDED_ARG",
    "MAKE_CELL",
    "COPY_FREE_VARS",
}
PYTHON_REGION_OPCODE_CLASSES = VMRegionOpcodeClasses(
    noise=frozenset({"END_FOR", "POP_TOP", "NOP", "RESUME"}),
    control=frozenset(
        {
            "POP_JUMP_IF_FALSE",
            "POP_JUMP_IF_TRUE",
            "POP_JUMP_IF_NOT_NONE",
            "POP_JUMP_IF_NONE",
            "JUMP_BACKWARD",
            "JUMP_BACKWARD_NO_INTERRUPT",
            "JUMP_FORWARD",
            "FOR_ITER",
            "GET_ITER",
            "GET_AITER",
            "GET_ANEXT",
            "PUSH_EXC_INFO",
            "RERAISE",
        }
    ),
    jumps=frozenset({"JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT", "JUMP_FORWARD"}),
    forward_jumps=frozenset({"JUMP_FORWARD"}),
    backward_jumps=frozenset({"JUMP_BACKWARD", "JUMP_BACKWARD_NO_INTERRUPT"}),
    iter_starts=frozenset({"GET_ITER"}),
    async_iter_starts=frozenset({"GET_AITER"}),
    conditional_jumps=frozenset(
        {
            "POP_JUMP_IF_FALSE",
            "POP_JUMP_IF_TRUE",
            "POP_JUMP_IF_NOT_NONE",
            "POP_JUMP_IF_NONE",
        }
    ),
    cleanup=frozenset({"PUSH_EXC_INFO", "RERAISE", "CHECK_EG_MATCH", "CHECK_EXC_MATCH", "WITH_EXCEPT_START", "END_ASYNC_FOR", "CLEANUP_THROW"}),
    null_jumps=frozenset({"POP_JUMP_IF_NONE"}),
    not_null_jumps=frozenset({"POP_JUMP_IF_NOT_NONE"}),
    truthy_jumps=frozenset({"POP_JUMP_IF_TRUE"}),
)


def _no_effect(_context, _instruction, _source: SourceRef) -> tuple[Effect, ...]:
    return ()


def _unknown_opcode_effect(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (UnknownOpcode(source=source, opcode=str(getattr(instruction, "opname", "")), raw=_raw_instruction_line(instruction)),)


def _pop_top(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Pop(source=source, emit_calls=True, allow_missing=True),)


def _setup_annotations(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (AssignValue(source=source, name="__annotations__", value=MapLiteral(source=source)),)


def _load_build_class(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Push(source=source, value=Global(name="build_class", source=source)),)


def _import_name(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (
        Pop(source=source, count=2, allow_missing=True),
        Push(source=source, value=Global(name=str(instruction.argval), source=source)),
    )


def _import_from(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (
        LoadAttrFromTop(
            source=source,
            attr=str(instruction.argval),
            fallback_obj=Global(name="<module>", source=source),
        ),
    )


def _load_const(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Push(source=source, value=Const(value=instruction.argval, source=source)),)


def _load_name(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (LoadLocal(source=source, name=name, fallback=Global(name=name, source=source)),)


def _load_local(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)),)


def _load_closure(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (Push(source=source, value=CapturedVar(name=name, source=source)),)


def _load_deref(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (Push(source=source, value=CapturedVar(name=name, source=source)),)


def _load_fast_pair(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    names = tuple(name for name in str(instruction.argrepr).split(", ") if name)
    return tuple(
        LoadLocal(source=source, name=name, fallback=Var(name=name, source=source))
        for name in names
    )


def _load_global(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Push(source=source, value=Global(name=str(instruction.argval), source=source)),)


def _load_from_dict_or_globals(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (
        Pop(source=source, count=1, allow_missing=True),
        Push(source=source, value=Global(name=name, source=source)),
    )


def _make_function(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (MakeFunctionValue(source=source, fallback_name=str(instruction.argval or instruction.argrepr or "<function>")),)


def _copy(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Copy(source=source, depth=_instruction_count(instruction), allow_missing=True),)


def _swap(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Swap(source=source, depth=_instruction_count(instruction), allow_missing=True),)


def _load_attr(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (LoadAttr(source=source, attr=str(instruction.argval)),)


def _load_super_attr(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    attr = str(instruction.argval or instruction.argrepr).split()[0]
    return (LoadSuperAttr(source=source, attr=attr),)


def _build_list(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildArray(source=source, kind="list", count=_instruction_count(instruction)),)


def _build_tuple(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildArray(source=source, kind="tuple", count=_instruction_count(instruction)),)


def _build_set(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildSet(source=source, count=_instruction_count(instruction)),)


def _get_iter(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Iterate(source=source),)


def _unpack_sequence(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Unpack(source=source, count=_instruction_count(instruction)),)


def _compare(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Compare(source=source, op=str(instruction.argval)),)


def _contains(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Compare(source=source, op="in", negate=bool(instruction.arg)),)


def _is_op(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Compare(source=source, op="is not" if instruction.arg else "is"),)


def _binary(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (
        Binary(
            source=source,
            op=BINARY_SYMBOLS.get(str(instruction.argrepr), str(instruction.argrepr)),
            semantics="dynamic",
        ),
    )


def _unary(op: str):
    def factory(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
        return (Unary(source=source, op=op),)

    return factory


def _store_global(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (StoreLocal(source=source, name=name, target=Var(name=name, source=source), materialize=False),)


def _store_deref(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (
        StoreLocal(
            source=source,
            name=name,
            target=CapturedVar(name=name, source=source),
            materialize=True,
        ),
    )


def _delete_global(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Pop(source=source, count=1, allow_missing=True),)


def _delete_attr(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Pop(source=source, count=1, allow_missing=True),)


def _delete_subscr(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Pop(source=source, count=2, allow_missing=True),)


def _build_slice(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildCall(source=source, arg_count=_instruction_count(instruction), callee=Global(name="slice", source=source)),)


def _binary_slice(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (
        BuildCall(source=source, arg_count=2, callee=Global(name="slice", source=source)),
        LoadItem(source=source),
    )


def _store_slice(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (StoreItemEffect(source=source),)


def _list_extend(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (ExtendArray(source=source),)


def _dict_merge(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (MergeMap(source=source, depth=_instruction_count(instruction)),)


def _set_update(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (InvokeMethod(source=source, attr="update", arg_count=1, depth=1, returns=0),)


def _load_locals(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Push(source=source, value=Global(name="locals", source=source)),)


def _call_function_ex(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (
        InvokeExpanded(
            source=source,
            has_keywords=bool(_instruction.arg),
        ),
    )


def _call_intrinsic_2(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval or instruction.argrepr or "intrinsic")
    return (BuildCall(source=source, arg_count=2, callee=Global(name=name, source=source)),)


def _invoke(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Invoke(source=source, arg_count=_instruction_count(instruction)),)


def _invoke_kw(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (InvokeKw(source=source, arg_count=_instruction_count(instruction)),)


def _convert_value(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (FormatTop(source=source, converter=str(instruction.argrepr or instruction.argval or "convert")),)


def _build_string(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildString(source=source, count=_instruction_count(instruction)),)


def _call_top_as(name: str):
    def factory(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
        return (CallTopAs(source=source, callee_name=name),)

    return factory


def _build_map(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildMap(source=source, count=_instruction_count(instruction)),)


def _build_const_key_map(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (BuildConstKeyMap(source=source, count=_instruction_count(instruction)),)


def _invoke_collection_method(attr: str, *, depth_from_instruction: bool = False):
    def factory(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
        return (
            InvokeMethod(
                source=source,
                attr=attr,
                arg_count=1,
                depth=_instruction_count(instruction) if depth_from_instruction else 1,
                returns=0,
            ),
        )

    return factory


def _map_add(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (StoreItemAtDepth(source=source, depth=_instruction_count(instruction)),)


def _set_add(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (SetAdd(source=source),)


def _store_local(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    name = str(instruction.argval)
    return (StoreLocal(source=source, name=name, target=Var(name=name, source=source)),)


def _store_many(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (StoreManyFromPopOrder(source=source, names=_instruction_names(instruction)),)


def _store_fast_load_fast(_context, instruction, source: SourceRef) -> tuple[Effect, ...] | None:
    names = _instruction_names(instruction)
    if not names:
        return None
    return (
        StoreMany(source=source, names=(names[0],)),
        *tuple(LoadLocal(source=source, name=name, fallback=Var(name=name, source=source)) for name in names[1:]),
    )


def _return_const(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Push(source=source, value=Const(value=instruction.argval, source=source)), ReturnTop(source=source))


def _raise_varargs(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    if instruction.arg == 0:
        return (ReraiseTop(source=source),)
    if instruction.arg == 2:
        return (RaiseWithCause(source=source),)
    return (RaiseTop(source=source),)


def _yield_value(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    if instruction.arg in {1, 2}:
        return ()
    return (YieldTop(source=source, default=Const(value=None, source=source)),)


def _send(_context, _instruction, source: SourceRef) -> tuple[Effect, ...]:
    return (Pop(source=source, count=1, allow_missing=True),)


def _unpack_ex(_context, instruction, source: SourceRef) -> tuple[Effect, ...]:
    before = instruction.arg if isinstance(instruction.arg, int) else 0
    return (Unpack(source=source, before=before & 0xFF, after=before >> 8),)


PYTHON_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opname",
    ignored=frozenset(IGNORED_OPS)
    | {
        "RETURN_GENERATOR",
        "PUSH_NULL",
        "END_SEND",
        "INSTRUMENTED_END_SEND",
        "GET_AITER",
        "FOR_ITER",
        "INSTRUMENTED_FOR_ITER",
        "END_FOR",
        "INSTRUMENTED_END_FOR",
        "POP_JUMP_IF_FALSE",
        "POP_JUMP_IF_TRUE",
        "POP_JUMP_IF_NONE",
        "POP_JUMP_IF_NOT_NONE",
        "INSTRUMENTED_POP_JUMP_IF_FALSE",
        "INSTRUMENTED_POP_JUMP_IF_TRUE",
        "INSTRUMENTED_POP_JUMP_IF_NONE",
        "INSTRUMENTED_POP_JUMP_IF_NOT_NONE",
        "JUMP_BACKWARD_NO_INTERRUPT",
        "JUMP_BACKWARD",
        "JUMP_FORWARD",
        "JUMP",
        "JUMP_NO_INTERRUPT",
        "INSTRUMENTED_JUMP_BACKWARD",
        "INSTRUMENTED_JUMP_FORWARD",
        "CALL_INTRINSIC_1",
        "BEFORE_WITH",
        "CLEANUP_THROW",
        "CHECK_EG_MATCH",
        "END_ASYNC_FOR",
        "LOOKUP_METHOD",
        "PUSH_EXC_INFO",
        "WITH_EXCEPT_START",
        "DELETE_FAST",
        "DELETE_NAME",
        "DELETE_DEREF",
        "DELETE_GLOBAL",
        "ENTER_EXECUTOR",
        "EXIT_INIT_CHECK",
        "INSTRUMENTED_INSTRUCTION",
        "INSTRUMENTED_LINE",
        "INSTRUMENTED_RESUME",
        "INTERPRETER_EXIT",
        "POP_BLOCK",
        "RESERVED",
        "SETUP_CLEANUP",
        "SETUP_FINALLY",
        "SETUP_WITH",
    },
    exact={
        "POP_TOP": _pop_top,
        "SETUP_ANNOTATIONS": _setup_annotations,
        "LOAD_BUILD_CLASS": _load_build_class,
        "IMPORT_NAME": _import_name,
        "IMPORT_FROM": _import_from,
        "LOAD_CONST": _load_const,
        "LOAD_NAME": _load_name,
        "LOAD_DEREF": _load_deref,
        "LOAD_CLASSDEREF": _load_deref,
        "LOAD_FROM_DICT_OR_DEREF": _load_deref,
        "LOAD_CLOSURE": _load_closure,
        "LOAD_FAST": _load_local,
        "LOAD_FAST_CHECK": _load_local,
        "LOAD_FAST_AND_CLEAR": _load_local,
        "LOAD_FAST_LOAD_FAST": _load_fast_pair,
        "LOAD_FROM_DICT_OR_GLOBALS": _load_from_dict_or_globals,
        "LOAD_GLOBAL": _load_global,
        "MAKE_FUNCTION": _make_function,
        "STORE_GLOBAL": _store_global,
        "DELETE_GLOBAL": _delete_global,
        "DELETE_ATTR": _delete_attr,
        "DELETE_SUBSCR": _delete_subscr,
        "LOAD_ASSERTION_ERROR": lambda _context, _instruction, source: (Push(source=source, value=Global(name="AssertionError", source=source)),),
        "BUILD_SLICE": _build_slice,
        "STORE_SLICE": _store_slice,
        "LIST_EXTEND": _list_extend,
        "DICT_MERGE": _dict_merge,
        "SET_UPDATE": _set_update,
        "CALL_FUNCTION_EX": _call_function_ex,
        "INSTRUMENTED_CALL_FUNCTION_EX": _call_function_ex,
        "LOAD_LOCALS": _load_locals,
        "SET_FUNCTION_ATTRIBUTE": lambda _context, _instruction, source: (DropBelowTop(source=source, count=1),),
        "COPY": _copy,
        "SWAP": _swap,
        "LOAD_ATTR": _load_attr,
        "LOAD_METHOD": _load_attr,
        "LOAD_SUPER_ATTR": _load_super_attr,
        "LOAD_SUPER_METHOD": _load_super_attr,
        "LOAD_ZERO_SUPER_ATTR": _load_super_attr,
        "LOAD_ZERO_SUPER_METHOD": _load_super_attr,
        "INSTRUMENTED_LOAD_SUPER_ATTR": _load_super_attr,
        "GET_LEN": _call_top_as("len"),
        "MATCH_MAPPING": lambda _context, _instruction, source: (
            BuildShapeTest(source=source, descriptor_count=0),
        ),
        "MATCH_SEQUENCE": lambda _context, _instruction, source: (
            BuildShapeTest(source=source, descriptor_count=0),
        ),
        "MATCH_KEYS": lambda _context, _instruction, source: (
            BuildShapeTest(source=source, descriptor_count=0),
        ),
        "TO_BOOL": lambda _context, _instruction, source: (Truthy(source=source),),
        "MATCH_CLASS": lambda _context, instruction, source: (
            BuildShapeTest(source=source, descriptor_count=_instruction_count(instruction) + 2),
        ),
        "BUILD_LIST": _build_list,
        "BUILD_TUPLE": _build_tuple,
        "BUILD_SET": _build_set,
        "SET_ADD": _set_add,
        "GET_ITER": _get_iter,
        "GET_YIELD_FROM_ITER": _get_iter,
        "UNPACK_SEQUENCE": _unpack_sequence,
        "BINARY_SUBSCR": lambda _context, _instruction, source: (LoadItem(source=source),),
        "BINARY_SLICE": _binary_slice,
        "COMPARE_OP": _compare,
        "CONTAINS_OP": _contains,
        "BINARY_OP": _binary,
        "UNARY_NEGATIVE": _unary("-"),
        "UNARY_INVERT": _unary("~"),
        "UNARY_NOT": _unary("not"),
        "CALL": _invoke,
        "INSTRUMENTED_CALL": _invoke,
        "CALL_KW": _invoke_kw,
        "INSTRUMENTED_CALL_KW": _invoke_kw,
        "CALL_INTRINSIC_2": _call_intrinsic_2,
        "FORMAT_SIMPLE": lambda _context, _instruction, source: (FormatTop(source=source),),
        "FORMAT_WITH_SPEC": lambda _context, _instruction, source: (BuildString(source=source, count=2),),
        "CONVERT_VALUE": _convert_value,
        "BUILD_STRING": _build_string,
        "GET_AWAITABLE": _call_top_as("await"),
        "GET_ANEXT": _call_top_as("anext"),
        "BEFORE_ASYNC_WITH": _no_effect,
        "BUILD_MAP": _build_map,
        "BUILD_CONST_KEY_MAP": _build_const_key_map,
        "DICT_UPDATE": _invoke_collection_method("update"),
        "LIST_APPEND": _invoke_collection_method("append", depth_from_instruction=True),
        "MAP_ADD": _map_add,
        "STORE_SUBSCR": lambda _context, _instruction, source: (StoreItemEffect(source=source, order="value-obj-key"),),
        "STORE_ATTR": lambda _context, instruction, source: (StoreAttr(source=source, attr=str(instruction.argval), order="value-obj"),),
        "STORE_FAST": _store_local,
        "STORE_FAST_MAYBE_NULL": _store_local,
        "STORE_NAME": _store_local,
        "STORE_DEREF": _store_deref,
        "STORE_FAST_STORE_FAST": _store_many,
        "STORE_FAST_LOAD_FAST": _store_fast_load_fast,
        "RETURN_VALUE": lambda _context, _instruction, source: (ReturnTop(source=source, empty_is_void=True),),
        "INSTRUMENTED_RETURN_VALUE": lambda _context, _instruction, source: (ReturnTop(source=source, empty_is_void=True),),
        "RETURN_CONST": _return_const,
        "INSTRUMENTED_RETURN_CONST": _return_const,
        "RAISE_VARARGS": _raise_varargs,
        "RERAISE": lambda _context, _instruction, source: (ReraiseTop(source=source),),
        "YIELD_VALUE": _yield_value,
        "INSTRUMENTED_YIELD_VALUE": _yield_value,
        "SEND": _send,
        "INSTRUMENTED_SEND": _send,
        "IS_OP": _is_op,
        "POP_EXCEPT": _no_effect,
        "CHECK_EXC_MATCH": lambda _context, _instruction, source: (ExceptionMatch(source=source),),
        "POP_JUMP_IF_NONE": _no_effect,
        "POP_JUMP_IF_NOT_NONE": _no_effect,
    },
    rules=(
        VMEffectRule(matches=lambda opcode, _instruction: opcode == "UNPACK_EX", factory=_unpack_ex),
    ),
    fallback=_unknown_opcode_effect,
)


def lift_pyc_module(pyc_module: PycModule, metadata: dict) -> ModuleIR:
    functions = list(_lift_code_object_tree(pyc_module.code))
    return assemble_vm_module(
        name=pyc_module.filename or "<python-pyc>",
        source_language="python",
        functions=tuple(functions),
        metadata={
            "frontend": metadata,
            "bytecode_format": "pyc",
        },
    )


def lift_code_object(code: PycCodeObject) -> FunctionIR:
    instructions = tuple(code.instructions)
    steps = _python_bytecode_steps(instructions, code.exception_regions)
    function = lift_vm_step_function(
        _python_function_spec(code),
        steps,
        profile=_python_region_profile(steps, instructions),
        callbacks=_python_region_callbacks(code, instructions),
        stateful_callbacks=_python_stateful_callbacks(code, instructions),
        initial_locals=_python_initial_locals(code),
        raw_window=lambda index: _raw_instruction_window(instructions, index),
    )
    return function


def _lift_code_object_tree(code: PycCodeObject) -> tuple[FunctionIR, ...]:
    spec = _python_function_spec(code)
    functions = [
        recover_vm_function(
            spec,
            lambda: lift_code_object(code),
            raw=tuple(_raw_instruction_line(instruction) for instruction in code.instructions),
        )
    ]
    for child in code.children:
        functions.extend(_lift_code_object_tree(child))
    return tuple(functions)


def _python_low_effects(instruction, source: SourceRef) -> tuple[Effect, ...] | None:
    return PYTHON_EFFECT_TABLE.effects_for(None, instruction, source)


def _python_bytecode_step(instruction, source: SourceRef | None = None) -> VMBytecodeStep:
    source = source or _python_source(instruction)
    decoded = _python_decoded_instruction(instruction, source)
    return VMBytecodeStep(
        opcode=decoded.opcode,
        source=source,
        effects=_python_low_effects(instruction, source),
        raw=decoded.raw,
        decoded=decoded,
        hints=_python_instruction_hints(instruction, source),
    )


def _python_source(instruction) -> SourceRef:
    return SourceRef(
        frontend="python-pyc",
        offset=instruction.offset,
        line=instruction.starts_line,
    )


def _python_bytecode_steps(
    instructions: tuple[object, ...],
    exception_regions: tuple[PycExceptionRegion, ...] = (),
) -> tuple[VMBytecodeStep, ...]:
    steps: list[VMBytecodeStep] = []
    previous_opcode = ""
    for instruction in instructions:
        step = _python_bytecode_step(instruction)
        region_hints = tuple(
            VMHint(
                kind="exception-region",
                source=step.source,
                target=region.target,
                value={
                    "start": region.start,
                    "end": region.end,
                    "target": region.target,
                    "depth": region.depth,
                    "lasti": region.lasti,
                },
                label="protected-region",
            )
            for region in exception_regions
            if region.start == instruction.offset
        )
        if instruction.opname == "STORE_FAST_STORE_FAST" and previous_opcode in {"UNPACK_SEQUENCE", "UNPACK_EX"}:
            step = VMBytecodeStep(
                opcode=step.opcode,
                source=step.source,
                effects=(StoreMany(source=step.source, names=_instruction_names(instruction)),),
                raw=step.raw,
                decoded=step.decoded,
                hints=(*step.hints, *region_hints),
            )
        if _is_branch_value_start(instructions, len(steps)):
            step = VMBytecodeStep(
                opcode=step.opcode,
                source=step.source,
                effects=step.effects,
                raw=step.raw,
                decoded=step.decoded,
                hints=(*step.hints, *region_hints, VMHint(kind="branch-value", source=step.source, label="short-circuit-value")),
            )
        elif region_hints:
            step = VMBytecodeStep(
                opcode=step.opcode,
                source=step.source,
                effects=step.effects,
                raw=step.raw,
                decoded=step.decoded,
                hints=(*step.hints, *region_hints),
            )
        steps.append(step)
        previous_opcode = instruction.opname
    return tuple(steps)


def _is_branch_value_start(instructions: tuple[object, ...], index: int) -> bool:
    """Identify a VM-neutral short-circuit value shape without structuring it."""

    window = instructions[index : index + 4]
    return (
        len(window) == 4
        and getattr(window[0], "opname", None) == "COPY"
        and getattr(window[1], "opname", None) == "TO_BOOL"
        and getattr(window[2], "opname", None) in {"POP_JUMP_IF_TRUE", "POP_JUMP_IF_FALSE"}
        and getattr(window[3], "opname", None) == "POP_TOP"
    )


def _python_decoded_instruction(instruction, source: SourceRef) -> VMDecodedInstruction:
    operands: list[VMOperand] = []
    if instruction.arg is not None:
        operands.append(VMOperand(role="immediate", value=instruction.arg, text=str(instruction.arg)))
    if instruction.argval is not None:
        operands.append(VMOperand(role=_python_operand_role(instruction.opname), value=instruction.argval, text=str(instruction.argrepr or instruction.argval)))
    return VMDecodedInstruction(
        opcode=instruction.opname,
        source=source,
        operands=tuple(operands),
        raw=_raw_instruction_line(instruction),
        artifact_range=(None if instruction.artifact_offset is None or instruction.size is None else ByteRange(instruction.artifact_offset, instruction.size)),
    )


def _python_operand_role(opname: str):
    if "CONST" in opname:
        return "constant"
    if "FAST" in opname or "DEREF" in opname or opname in {"LOAD_NAME", "STORE_NAME"}:
        return "local"
    if "GLOBAL" in opname:
        return "global"
    if "JUMP" in opname or opname == "FOR_ITER":
        return "target"
    if "ATTR" in opname or "METHOD" in opname:
        return "attribute"
    return "raw"


def _python_instruction_hints(instruction, source: SourceRef) -> tuple[VMHint, ...]:
    if instruction.opname in {"PUSH_EXC_INFO", "CHECK_EXC_MATCH", "WITH_EXCEPT_START"}:
        return (VMHint(kind="exception-region", source=source, label=instruction.opname),)
    if "JUMP" in instruction.opname or instruction.opname == "FOR_ITER":
        target = instruction.argval if isinstance(instruction.argval, int) else None
        label = "loop-backedge" if target is not None and target <= instruction.offset else "branch-target"
        detail = "iter-next-target-if-false" if instruction.opname == "FOR_ITER" else None
        flow = "conditional" if instruction.opname == "FOR_ITER" or "IF" in instruction.opname or instruction.opname.startswith("POP_JUMP") else "unconditional"
        return (VMHint(kind=label, source=source, target=target, label=instruction.opname, detail=detail, flow=flow),)
    return ()


def _python_function_spec(
    code: PycCodeObject,
    *,
    local_names: tuple[str, ...] | None = None,
) -> VMFunctionSpec:
    argument_names = _python_argument_names(code)
    closure_names = (*code.cellvars, *code.freevars)
    locals_for_metadata = local_names if local_names is not None else (*code.varnames, *closure_names)
    return VMFunctionSpec(
        name=code.name,
        params=argument_names,
        frontend="python-pyc",
        instruction_count=len(code.instructions),
        local_names=tuple(name for name in locals_for_metadata if name not in argument_names),
    )


def _python_argument_names(code: PycCodeObject) -> tuple[str, ...]:
    positional_end = code.argcount
    keyword_only_end = positional_end + code.kwonlyargcount
    names = list(code.varnames[:keyword_only_end])
    cursor = keyword_only_end
    if code.flags & 0x04:  # CO_VARARGS
        names.append(code.varnames[cursor])
        cursor += 1
    if code.flags & 0x08:  # CO_VARKEYWORDS
        names.append(code.varnames[cursor])
    return tuple(names)


def _python_initial_locals(code: PycCodeObject) -> dict[str, Expr]:
    locals_ = {
        name: Var(name=name, source=SourceRef(frontend="python-pyc", detail=f"local:{name}"))
        for name in code.varnames
    }
    for name in (*code.cellvars, *code.freevars):
        locals_.setdefault(name, CapturedVar(name=name, source=SourceRef(frontend="python-pyc", detail=f"capture:{name}")))
    return locals_


def _python_region_callbacks(
    code: PycCodeObject,
    instructions: tuple[object, ...],
) -> VMRegionCallbacks[VMBytecodeStep]:
    return VMRegionCallbacks(
        lift_slice=lambda slice_start, slice_end, stack: _lift_instruction_slice(
            code,
            instructions[slice_start:slice_end],
            [],
            stack,
        ),
        lift_expr=lambda slice_start, slice_end, stack: _lift_stack_expr(
            code,
            instructions[slice_start:slice_end],
            [],
            stack,
        ),
        lift_iter_loop=lambda _get_iter_index, _iterable: None,
        lift_async_iter_loop=lambda _prefix_start, _get_aiter_index, _region_end: None,
        lift_comprehension=lambda _prefix_start, _get_iter_index, _region_end, _iterable: None,
    )


def _python_stateful_callbacks(
    code: PycCodeObject,
    instructions: tuple[object, ...],
) -> VMStatefulCallbacks[VMBytecodeStep]:
    return VMStatefulCallbacks(
        initial_locals=lambda: _python_initial_locals(code),
        lift_linear=lambda start, end, locals, stack: _python_linear_state(
            instructions[start:end],
            locals,
            stack,
        ),
        branch_condition=_python_branch_condition,
    )


def _python_linear_state(
    instructions: tuple[object, ...],
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
) -> VMLinearState | None:
    result = _run_python_stack_slice(
        _PycLinearLocals(tuple(initial_locals)),
        instructions,
        dict(initial_locals),
        initial_stack,
    )
    stopped_at_terminal_end = (
        getattr(result.stopped_at, "source", None) == _python_source(instructions[-1])
        if instructions and result.state.terminator is not None
        else False
    )
    if result.state.diagnostics or (result.stopped_at is not None and not stopped_at_terminal_end):
        return None
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
    )


def _python_branch_condition(branch: VMBytecodeStep, stack: tuple[Expr, ...]) -> Expr | None:
    if not stack:
        return None
    condition = stack[-1]
    source = branch.source
    if branch.opcode == "TO_BOOL":
        return condition
    if branch.opcode == "POP_JUMP_IF_TRUE":
        return BinaryOp(source=source, op="==", left=condition, right=Const(value=False, source=source))
    if branch.opcode == "POP_JUMP_IF_NONE":
        return BinaryOp(source=source, op="!=", left=condition, right=Const(value=None, source=source))
    if branch.opcode == "POP_JUMP_IF_NOT_NONE":
        return BinaryOp(source=source, op="==", left=condition, right=Const(value=None, source=source))
    return condition


class _PycLinearLocals:
    def __init__(self, varnames: tuple[str, ...]) -> None:
        self.varnames = varnames


def _python_region_profile(
    steps: tuple[VMBytecodeStep, ...],
    instructions: tuple[object, ...],
) -> VMRegionProfile[VMBytecodeStep]:
    return build_hint_region_profile(
        steps,
        frontend="python-pyc",
        opcode_classes=PYTHON_REGION_OPCODE_CLASSES,
        raw_window=lambda index: _raw_instruction_window(instructions, index),
        await_region_end=lambda await_start, await_end: _await_region_end(instructions, await_start, await_end),
    )


def _await_region_end(instructions: tuple[object, ...], start: int, end: int) -> int | None:
    for index in range(start, min(end, start + 12)):
        if instructions[index].opname != "SEND":
            continue
        cursor = index + 1
        if cursor < end and instructions[cursor].opname == "YIELD_VALUE":
            cursor += 1
        if cursor < end and instructions[cursor].opname == "RESUME":
            cursor += 1
        if cursor < end and instructions[cursor].opname == "JUMP_BACKWARD_NO_INTERRUPT":
            cursor += 1
        if cursor < end and instructions[cursor].opname == "END_SEND":
            cursor += 1
        if cursor < end and instructions[cursor].opname == "POP_TOP":
            cursor += 1
        return cursor
    return None


def _lift_instruction_slice(
    code: PycCodeObject,
    instructions: tuple[object, ...],
    assignments: list[Assign],
    preloaded_stack: tuple[Expr, ...] = (),
) -> tuple[object, ...]:
    if not instructions:
        return ()
    initial_locals = {
        name: Var(name=name, source=SourceRef(frontend="python-pyc", detail=f"local:{name}"))
        for name in code.varnames
    }
    for assignment in assignments:
        initial_locals[assignment.target.name] = assignment.target
    result = _run_python_stack_slice(code, instructions, initial_locals, preloaded_stack)
    if result.state.diagnostics:
        return ()
    output: list[object] = list(result.state.statements)
    if result.stopped_at is not None and result.state.terminator is None:
        try:
            stopped_index = instructions.index(result.stopped_at)
        except ValueError:
            stopped_index = 0
        output.append(
            vm_unsupported(
                source=result.stopped_at.source,
                message="unsupported region",
                detail=f"stopped at {result.stopped_at.opcode}",
                raw=(result.stopped_at.raw,) if result.stopped_at.raw else _raw_instruction_window(instructions, stopped_index),
            )
        )
    if result.state.terminator is not None:
        output.append(result.state.terminator)
    return tuple(output)


def _lift_stack_expr(
    code: PycCodeObject,
    instructions: tuple[object, ...],
    assignments: list[Assign],
    preloaded_stack: tuple[Expr, ...] = (),
) -> Expr | None:
    initial_locals = {
        name: Var(name=name, source=SourceRef(frontend="python-pyc", detail=f"local:{name}"))
        for name in code.varnames
    }
    for assignment in assignments:
        initial_locals[assignment.target.name] = assignment.target
    result = _run_python_stack_slice(code, instructions, initial_locals, preloaded_stack)
    if result.state.diagnostics or not result.state.stack:
        return None
    return result.state.stack[-1]


def _run_python_stack_slice(
    code: PycCodeObject,
    instructions: tuple[object, ...],
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...] = (),
):
    steps = _python_bytecode_steps(instructions)
    return lift_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)


def _instruction_count(instruction) -> int:
    return instruction.arg if isinstance(instruction.arg, int) else 0


def _instruction_names(instruction) -> tuple[str, ...]:
    value = instruction.argval
    if isinstance(value, tuple):
        return tuple(str(name) for name in value)
    return tuple(name for name in str(instruction.argrepr).split(", ") if name)


def _raw_instruction_window(instructions: tuple[object, ...], index: int, radius: int = 3) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(_raw_instruction_line(instruction) for instruction in instructions[start:end])


def _raw_instruction_line(instruction) -> str:
    arg = getattr(instruction, "argrepr", "")
    if not arg:
        arg = repr(getattr(instruction, "argval", ""))
    return f"@{instruction.offset} {instruction.opname} {arg}".rstrip()
