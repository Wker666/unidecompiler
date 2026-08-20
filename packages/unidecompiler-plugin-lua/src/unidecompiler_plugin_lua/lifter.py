from __future__ import annotations

from dataclasses import dataclass, replace
import re

from unidecompiler.core.ir import (
    Assign,
    BinaryOp,
    Call,
    CapturedVar,
    Const,
    Expr,
    Global,
    GetItem,
    MapLiteral,
    MultiReturn,
    SourceRef,
    StoreItem,
    UnaryOp,
    Var,
)
from unidecompiler.core.effects import AssignManyValues, AssignValue, AssignValueOnBranch, Emit, ReturnValues, UnknownOpcode
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.vm_function import (
    VMFunctionSpec,
    empty_vm_function,
    recover_vm_function,
    lift_steps,
    lift_vm_step_function,
)
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.provenance import ByteRange
from unidecompiler.core.vm_region import (
    VMLinearState,
    VMRegionOpcodeClasses,
    VMRegionProfile,
    VMStatefulCallbacks,
    build_hint_region_profile,
)
from unidecompiler_plugin_lua.luac import LuaChunk, LuaFunctionListing


BINARY_OPS = {
    "ADD": "+",
    "SUB": "-",
    "MUL": "*",
    "DIV": "/",
    "IDIV": "//",
    "MOD": "%",
}
IMMEDIATE_BINARY_OPS = {
    "ADDI": "+",
}
COMPARISON_OPS = {
    "LT": "<",
    "LE": "<=",
    "EQ": "==",
}
IMMEDIATE_COMPARISON_OPS = {
    "EQI": "==",
    "LTI": "<",
    "LEI": "<=",
    "GTI": ">",
    "GEI": ">=",
}
CONDITIONAL_OPS = frozenset(
    {
        "EQ",
        "LT",
        "LE",
        "EQK",
        "EQI",
        "LTI",
        "LEI",
        "GTI",
        "GEI",
        "TEST",
        "TESTSET",
    }
)
JUMP_OPS = frozenset({"JMP", "FORPREP", "FORLOOP", "TFORPREP", "TFORLOOP"})
CONTROL_OPS = CONDITIONAL_OPS | JUMP_OPS
LUA_REGION_OPCODE_CLASSES = VMRegionOpcodeClasses(
    control=CONTROL_OPS,
    jumps=JUMP_OPS,
    forward_jumps=JUMP_OPS,
    backward_jumps=JUMP_OPS,
    conditional_jumps=CONDITIONAL_OPS | frozenset({"FORLOOP", "TFORLOOP"}),
)
IGNORED_OPS = {
    "MMBIN",
    "MMBINI",
    "MMBINK",
    "VARARGPREP",
    "EXTRAARG",
    "LFALSESKIP",
    "CLOSE",
    "TBC",
    *(CONTROL_OPS - frozenset({"FORPREP", "FORLOOP", "TESTSET", "TFORPREP", "TFORLOOP"})),
}
@dataclass(frozen=True)
class LuaEffectContext:
    listing: LuaFunctionListing
    constants: dict[int, object]


def _lua_no_effect(_context: LuaEffectContext, _instruction, _source: SourceRef) -> tuple:
    return ()


def _lua_unknown_opcode_effect(
    _context: LuaEffectContext,
    instruction,
    source: SourceRef,
) -> tuple:
    return (
        UnknownOpcode(
            source=source,
            opcode=instruction.opcode,
            raw=f"{instruction.pc}: {instruction.opcode} {' '.join(instruction.operands)}".strip(),
        ),
    )


def _lua_load_const_value(value):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple:
        operands = instruction.operands
        if len(operands) < 1:
            return None
        return (_lua_assign(context.listing, int(operands[0]), instruction.pc, Const(value=value, source=source), source),)

    return factory


def _lua_load_i(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    return (_lua_assign(context.listing, int(operands[0]), instruction.pc, Const(value=int(operands[1]), source=source), source),)


def _lua_load_nil(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    start = int(operands[0])
    count = int(operands[1]) + 1
    return tuple(
        _lua_assign(context.listing, register, instruction.pc, Const(value=None, source=source), source)
        for register in range(start, start + count)
    )


def _lua_load_k(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    return (_lua_assign(context.listing, int(operands[0]), instruction.pc, Const(value=context.constants.get(int(operands[1])), source=source), source),)


def _lua_move(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    return (
        _lua_assign(
            context.listing,
            int(operands[0]),
            instruction.pc,
            Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
            source,
        ),
    )


def _lua_binary(op: str):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 3:
            return None
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                BinaryOp(
                    source=source,
                    op=op,
                    left=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                    right=Var(name=_read_register_name(context.listing, int(operands[2]), instruction.pc), source=source),
                    semantics="dynamic",
                ),
                source,
            ),
        )

    return factory


def _lua_unary(op: str):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 2:
            return None
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                UnaryOp(
                    source=source,
                    op=op,
                    value=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                ),
                source,
            ),
        )

    return factory


def _lua_immediate_binary(op: str):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 3:
            return None
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                BinaryOp(
                    source=source,
                    op=op,
                    left=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                    right=Const(value=int(operands[2]), source=source),
                    semantics="dynamic",
                ),
                source,
            ),
        )

    return factory


def _lua_shift_immediate(op: str, immediate_on_left: bool = False):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 3:
            return None
        shift_op = op
        immediate_value = int(operands[2])
        if immediate_value < 0 and op in {"<<", ">>"}:
            shift_op = "<<" if op == ">>" else ">>"
            immediate_value = -immediate_value
        immediate = Const(value=immediate_value, source=source)
        register = Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source)
        left, right = (immediate, register) if immediate_on_left else (register, immediate)
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                BinaryOp(source=source, op=shift_op, left=left, right=right, semantics="dynamic"),
                source,
            ),
        )

    return factory


def _lua_constant_binary(op: str):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 3:
            return None
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                BinaryOp(
                    source=source,
                    op=op,
                    left=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                    right=Const(value=context.constants.get(int(operands[2])), source=source),
                    semantics="dynamic",
                ),
                source,
            ),
        )

    return factory


def _lua_get_table(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        _lua_assign(
            context.listing,
            int(operands[0]),
            instruction.pc,
            GetItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                key=Var(name=_read_register_name(context.listing, int(operands[2]), instruction.pc), source=source),
            ),
            source,
        ),
    )


def _lua_get_tabup(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    key = _constant_operand_expr(context.constants, operands[2], source)
    upvalue = _lua_upvalue_expr(context.listing, operands[1], source)
    if not _is_lua_env_upvalue(upvalue):
        return (
            _lua_assign(
                context.listing,
                int(operands[0]),
                instruction.pc,
                GetItem(source=source, obj=upvalue, key=key),
                source,
            ),
        )
    value: Expr = (
        Global(name=key.value, source=source)
        if isinstance(key, Const) and isinstance(key.value, str)
        else GetItem(source=source, obj=upvalue, key=key)
    )
    return (_lua_assign(context.listing, int(operands[0]), instruction.pc, value, source),)


def _lua_get_field(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        _lua_assign(
            context.listing,
            int(operands[0]),
            instruction.pc,
            GetItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                key=_constant_operand_expr(context.constants, operands[2], source),
            ),
            source,
        ),
    )


def _lua_get_i(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        _lua_assign(
            context.listing,
            int(operands[0]),
            instruction.pc,
            GetItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source),
                key=Const(value=int(operands[2]), source=source),
            ),
            source,
        ),
    )


def _lua_set_table(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        Emit(
            source=source,
            statement=StoreItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[0]), instruction.pc), source=source),
                key=_operand_expr(context.listing, context.constants, operands[1], instruction.pc, source),
                value=_operand_expr(context.listing, context.constants, operands[2], instruction.pc, source),
            ),
        ),
    )


def _lua_set_tabup(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        Emit(
            source=source,
            statement=StoreItem(
                source=source,
                obj=_lua_upvalue_expr(context.listing, operands[0], source),
                key=_constant_operand_expr(context.constants, operands[1], source),
                value=_operand_expr(context.listing, context.constants, operands[2], instruction.pc, source),
            ),
        ),
    )


def _lua_set_i(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        Emit(
            source=source,
            statement=StoreItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[0]), instruction.pc), source=source),
                key=Const(value=int(operands[1]), source=source),
                value=_operand_expr(context.listing, context.constants, operands[2], instruction.pc, source),
            ),
        ),
    )


def _lua_set_field(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    return (
        Emit(
            source=source,
            statement=StoreItem(
                source=source,
                obj=Var(name=_read_register_name(context.listing, int(operands[0]), instruction.pc), source=source),
                key=_constant_operand_expr(context.constants, operands[1], source),
                value=_operand_expr(context.listing, context.constants, operands[2], instruction.pc, source),
            ),
        ),
    )


def _lua_new_table(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 1:
        return None
    return (_lua_assign(context.listing, int(operands[0]), instruction.pc, MapLiteral(source=source), source),)


def _lua_self(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    base = int(operands[0])
    receiver = Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source)
    method = GetItem(
        source=source,
        obj=receiver,
        key=_constant_operand_expr(context.constants, operands[2], source),
    )
    return (
        _lua_assign(context.listing, base + 1, instruction.pc, receiver, source),
        _lua_assign(context.listing, base, instruction.pc, method, source),
    )


def _lua_concat(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    target = int(operands[0])
    if len(operands) == 2:
        start = target
        end = target + int(operands[1]) - 1
    else:
        start = int(operands[1])
        end = int(operands[2])
    if end < start:
        return None
    expr: Expr = Var(name=_read_register_name(context.listing, start, instruction.pc), source=source)
    for register in range(start + 1, end + 1):
        expr = BinaryOp(
            source=source,
            op="..",
            left=expr,
            right=Var(name=_read_register_name(context.listing, register, instruction.pc), source=source),
            semantics="dynamic",
        )
    return (_lua_assign(context.listing, target, instruction.pc, expr, source),)


def _lua_set_list(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    base = int(operands[0])
    count = int(operands[1])
    if count <= 0:
        return ()
    table = Var(name=_read_register_name(context.listing, base, instruction.pc), source=source)
    return tuple(
        Emit(
            source=source,
            statement=StoreItem(
                source=source,
                obj=table,
                key=Const(value=index + 1, source=source),
                value=Var(name=_read_register_name(context.listing, base + 1 + index, instruction.pc), source=source),
            ),
        )
        for index in range(count)
    )


def _lua_forloop_effect(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if not operands:
        return None
    base = int(operands[0])
    index_name = _read_register_name(context.listing, base, instruction.pc)
    step_name = _read_register_name(context.listing, base + 2, instruction.pc)
    body_target = _lua_jump_target(instruction)
    visible_index_name = _write_register_name(
        context.listing,
        base + 3,
        body_target if body_target is not None else instruction.pc,
    )
    next_index = BinaryOp(
        source=source,
        op="+",
        left=Var(name=index_name, source=source),
        right=Var(name=step_name, source=source),
        semantics="dynamic",
    )
    return (
        AssignValue(
            source=source,
            name=index_name,
            target=Var(name=index_name, source=source),
            value=next_index,
        ),
        AssignValue(
            source=source,
            name=visible_index_name,
            target=Var(name=visible_index_name, source=source),
            value=Var(name=index_name, source=source),
        ),
    )


def _lua_forprep_effect(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if not operands:
        return None
    base = int(operands[0])
    index_name = _read_register_name(context.listing, base, instruction.pc)
    step_name = _read_register_name(context.listing, base + 2, instruction.pc)
    prepared_index = BinaryOp(
        source=source,
        op="-",
        left=Var(name=index_name, source=source),
        right=Var(name=step_name, source=source),
        semantics="dynamic",
    )
    return (
        AssignValue(
            source=source,
            name=index_name,
            target=Var(name=index_name, source=source),
            value=prepared_index,
        ),
    )


def _lua_testset_effect(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    target_name = _write_register_name(context.listing, int(operands[0]), instruction.pc)
    value = Var(name=_read_register_name(context.listing, int(operands[1]), instruction.pc), source=source)
    return (
        AssignValueOnBranch(
            source=source,
            name=target_name,
            target=Var(name=target_name, source=source),
            value=value,
            branch="false",
        ),
    )


def _lua_tforcall_effect(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    base = int(operands[0])
    count = int(operands[2])
    if count <= 0:
        return ()
    names = tuple(
        _write_register_name(context.listing, register, instruction.pc)
        for register in range(base + 4, base + 4 + count)
    )
    return (
        AssignManyValues(
            source=source,
            names=names,
            targets=tuple(Var(name=name, source=source) for name in names),
            values=(
                MultiReturn(
                    source=source,
                    value=Call(
                        source=source,
                        callee=Global(name="lua_generic_for_next", source=source),
                        args=(
                            Var(name=_read_register_name(context.listing, base, instruction.pc), source=source),
                            Var(name=_read_register_name(context.listing, base + 1, instruction.pc), source=source),
                            Var(name=_read_register_name(context.listing, base + 2, instruction.pc), source=source),
                        ),
                    ),
                ),
            ),
        ),
    )


def _lua_tforloop_effect(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if not operands:
        return None
    base = int(operands[0])
    control_name = _write_register_name(context.listing, base + 2, instruction.pc)
    value_name = _read_register_name(context.listing, base + 4, instruction.pc)
    return (
        AssignValueOnBranch(
            source=source,
            name=control_name,
            target=Var(name=control_name, source=source),
            value=Var(name=value_name, source=source),
            branch="true",
        ),
    )


def _lua_vararg(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    base = int(operands[0])
    count = int(operands[1]) - 1
    varargs = Call(source=source, callee=Global(name="varargs", source=source), returns="unknown")
    if count <= 0:
        return (_lua_assign(context.listing, base, instruction.pc, varargs, source),)
    return tuple(
        _lua_assign(
            context.listing,
            base + index,
            instruction.pc,
            GetItem(source=source, obj=varargs, key=Const(value=index, source=source)),
            source,
        )
        for index in range(count)
    )


def _lua_closure(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 2:
        return None
    function_index = int(operands[1])
    function_name = (
        context.listing.child_function_names[function_index]
        if 0 <= function_index < len(context.listing.child_function_names)
        else f"<function_{function_index}>"
    )
    return (
        _lua_assign(
            context.listing,
            int(operands[0]),
            instruction.pc,
            Global(name=function_name, source=source),
            source,
        ),
    )


def _lua_call(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 3:
        return None
    base = int(operands[0])
    return_count = int(operands[2]) - 1
    arg_count = int(operands[1]) - 1
    if arg_count < 0:
        arg_count = _lua_open_call_arg_count(context.listing, instruction)
    args = tuple(
        _lua_call_argument_expr(context.listing, register, instruction.pc, source)
        for register in range(base + 1, base + 1 + arg_count)
    )
    call = Call(
        source=source,
        callee=Var(name=_read_register_name(context.listing, base, instruction.pc), source=source),
        args=args,
        returns=return_count if return_count >= 0 else "unknown",
    )
    if return_count == 0:
        if instruction.opcode == "TAILCALL":
            return (ReturnValues(source=source, values=(call,)),)
        return (_lua_assign(context.listing, base, instruction.pc, call, source),)
    if return_count > 1:
        names = tuple(
            _write_register_name(context.listing, register, instruction.pc)
            for register in range(base, base + return_count)
        )
        return (
            AssignManyValues(
                source=source,
                names=names,
                targets=tuple(Var(name=name, source=source) for name in names),
                values=(MultiReturn(source=source, value=call),),
            ),
        )
    return (_lua_assign(context.listing, base, instruction.pc, call, source),)


def _lua_return1(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 1:
        return None
    return (
        ReturnValues(
            source=source,
            values=(Var(name=_read_register_name(context.listing, int(operands[0]), instruction.pc), source=source),),
        ),
    )


def _lua_return_with_base_count(default_count: int):
    def factory(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
        operands = instruction.operands
        if len(operands) < 1:
            return None
        start = int(operands[0])
        return (
            ReturnValues(
                source=source,
                values=tuple(
                    Var(name=_read_register_name(context.listing, register, instruction.pc), source=source)
                    for register in range(start, start + default_count)
                ),
            ),
        )

    return factory


def _lua_return(context: LuaEffectContext, instruction, source: SourceRef) -> tuple | None:
    operands = instruction.operands
    if len(operands) < 1:
        return None
    start = int(operands[0])
    count = int(operands[1]) - 1 if len(operands) >= 2 else 1
    if count < 0:
        count = 1
    return (
        ReturnValues(
            source=source,
            values=tuple(
                Var(name=_read_register_name(context.listing, register, instruction.pc), source=source)
                for register in range(start, start + count)
            ),
        ),
    )


LUA_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    ignored=frozenset(IGNORED_OPS),
    exact={
        "LOADI": _lua_load_i,
        "LOADF": _lua_load_i,
        "LOADTRUE": _lua_load_const_value(True),
        "LOADFALSE": _lua_load_const_value(False),
        "LOADNIL": _lua_load_nil,
        "LOADK": _lua_load_k,
        "LOADKX": _lua_load_k,
        "MOVE": _lua_move,
        "GETUPVAL": lambda context, instruction, source: (
            _lua_assign(
                context.listing,
                int(instruction.operands[0]),
                instruction.pc,
                _lua_upvalue_expr(context.listing, instruction.operands[1], source),
                source,
            ),
        ) if len(instruction.operands) >= 2 else None,
        "SETUPVAL": lambda context, instruction, source: (
            Emit(
                source=source,
                statement=Assign(
                source=source,
                target=_lua_upvalue_expr(context.listing, instruction.operands[1], source),
                value=Var(
                    name=_read_register_name(context.listing, int(instruction.operands[0]), instruction.pc),
                    source=source,
                    ),
                ),
            ),
        ) if len(instruction.operands) >= 2 else None,
        "UNM": _lua_unary("-"),
        "BNOT": _lua_unary("~"),
        "NOT": _lua_unary("not "),
        "LEN": _lua_unary("#"),
        **{opcode: _lua_binary(op) for opcode, op in BINARY_OPS.items()},
        **{opcode: _lua_immediate_binary(op) for opcode, op in IMMEDIATE_BINARY_OPS.items()},
        **{f"{opcode}K": _lua_constant_binary(op) for opcode, op in BINARY_OPS.items()},
        "POW": _lua_binary("^"),
        "BAND": _lua_binary("&"),
        "BOR": _lua_binary("|"),
        "BXOR": _lua_binary("~"),
        "SHL": _lua_binary("<<"),
        "SHR": _lua_binary(">>"),
        "POWK": _lua_constant_binary("^"),
        "BANDK": _lua_constant_binary("&"),
        "BORK": _lua_constant_binary("|"),
        "BXORK": _lua_constant_binary("~"),
        "SHRI": _lua_shift_immediate(">>"),
        "SHLI": _lua_shift_immediate("<<", immediate_on_left=True),
        "GETTABLE": _lua_get_table,
        "GETTABUP": _lua_get_tabup,
        "GETFIELD": _lua_get_field,
        "GETI": _lua_get_i,
        "SETTABUP": _lua_set_tabup,
        "SETTABLE": _lua_set_table,
        "SETFIELD": _lua_set_field,
        "SETI": _lua_set_i,
        "NEWTABLE": _lua_new_table,
        "SELF": _lua_self,
        "CONCAT": _lua_concat,
        "SETLIST": _lua_set_list,
        "VARARG": _lua_vararg,
        "CLOSURE": _lua_closure,
        "CALL": _lua_call,
        "TAILCALL": _lua_call,
        "FORPREP": _lua_forprep_effect,
        "FORLOOP": _lua_forloop_effect,
        "TFORPREP": _lua_no_effect,
        "TFORCALL": _lua_tforcall_effect,
        "TFORLOOP": _lua_tforloop_effect,
        "TESTSET": _lua_testset_effect,
        "RETURN1": _lua_return1,
        "RETURN0": lambda _context, _instruction, source: (ReturnValues(source=source, values=()),),
        "RETURN2": _lua_return_with_base_count(2),
        "RETURNI": _lua_return_with_base_count(1),
        "RETURNK": _lua_return_with_base_count(1),
        "RETURN": _lua_return,
    },
    fallback=_lua_unknown_opcode_effect,
)


def lift_lua_chunk(chunk: LuaChunk, metadata: dict) -> ModuleIR:
    functions: tuple[FunctionIR, ...]
    if chunk.functions:
        root, next_index = _lift_lua_function_tree(chunk.functions, 0)
        if next_index != len(chunk.functions):
            raise ValueError("incomplete Lua function tree reconstruction")
        functions = (root,)
    else:
        functions = ()

    if not functions:
        functions = (
            empty_vm_function(VMFunctionSpec(name="<chunk>", params=(), frontend="lua", instruction_count=0)),
        )

    return assemble_vm_module(
        name=chunk.filename or "<lua-chunk>",
        source_language="lua",
        metadata={
            "frontend": metadata,
            "bytecode_format": "luac",
            "lua_version": chunk.header.version_label,
            "lua_disassembly": chunk.disassembly,
        },
        functions=functions,
    )


def _lift_lua_function_tree(
    listings: tuple[LuaFunctionListing, ...],
    index: int,
) -> tuple[FunctionIR, int]:
    listing = listings[index]
    function_ir = recover_vm_function(
        _lua_function_spec(listing),
        lambda: lift_lua_function(listing),
        raw=tuple(
            f"{instruction.pc}: {instruction.opcode} {' '.join(instruction.operands)}".strip()
            for instruction in listing.instructions
        ),
    )
    next_index = index + 1
    nested_functions: list[FunctionIR] = []
    for _ in range(listing.child_function_count):
        nested_function, next_index = _lift_lua_function_tree(listings, next_index)
        nested_functions.append(nested_function)
    return replace(function_ir, nested_functions=tuple(nested_functions)), next_index


def lift_lua_function(listing: LuaFunctionListing) -> FunctionIR | None:
    constants = {constant.index: constant.value for constant in listing.constants}
    local_names = {
        local.name
        for local in listing.locals
        if local.slot >= listing.param_count and _looks_user_named(local.name)
    }
    steps = tuple(_lua_bytecode_step(listing, constants, instruction) for instruction in listing.instructions)
    return lift_vm_step_function(
        _lua_function_spec(listing, tuple(sorted(local_names))),
        steps,
        profile=_lua_region_profile(steps, listing.instructions),
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=lambda: {},
            lift_linear=lambda start, end, locals, stack: _lua_linear_state(
                listing,
                constants,
                listing.instructions,
                start,
                end,
                locals,
                stack,
            ),
            branch_condition=lambda branch, stack: _lua_branch_condition(
                _lua_instruction_for_step(listing.instructions, branch),
                constants,
                listing,
                stack,
            ),
            branch_stack_width=lambda branch: _lua_branch_stack_width(_lua_instruction_for_step(listing.instructions, branch)),
        ),
        raw_window=lambda index: _lua_raw_instruction_window(listing.instructions, index),
    )


def _lua_region_profile(
    steps: tuple[VMBytecodeStep, ...],
    instructions: tuple[object, ...],
) -> VMRegionProfile[VMBytecodeStep]:
    return build_hint_region_profile(
        steps,
        frontend="lua",
        opcode_classes=LUA_REGION_OPCODE_CLASSES,
        raw_window=lambda index: _lua_raw_instruction_window(instructions, index),
    )


def _lua_linear_state(
    listing: LuaFunctionListing,
    constants: dict[int, object],
    instructions: tuple[object, ...],
    start: int,
    end: int,
    initial_locals: dict[str, Expr],
    initial_stack: tuple[Expr, ...],
) -> VMLinearState | None:
    steps = tuple(_lua_bytecode_step(listing, constants, instruction) for instruction in instructions[start:end])
    result = lift_steps(steps, initial_locals=initial_locals, initial_stack=initial_stack)
    if result.state.diagnostics:
        return None
    if result.stopped_at is not None and result.state.terminator is None:
        return None
    if result.state.terminator is not None and (
        result.stopped_at is None
        or result.stopped_at.source.offset != steps[-1].source.offset
    ):
        return None
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
    )


def _lua_instruction_for_step(instructions: tuple[object, ...], step: VMBytecodeStep):
    for instruction in instructions:
        if instruction.pc == step.source.offset:
            return instruction
    return instructions[0]


def _lua_branch_stack_width(_instruction) -> int:
    return 0


def _lua_branch_condition(
    instruction,
    constants: dict[int, object],
    listing: LuaFunctionListing,
    _stack: tuple[Expr, ...],
) -> Expr | None:
    source = SourceRef(frontend="lua", offset=instruction.pc, line=instruction.line)
    operands = instruction.operands
    if instruction.opcode in COMPARISON_OPS and len(operands) >= 3:
        condition: Expr = BinaryOp(
            source=source,
            op=COMPARISON_OPS[instruction.opcode],
            left=Var(name=_read_register_name(listing, int(operands[0]), instruction.pc), source=source),
            right=Var(name=_read_register_name(listing, int(operands[1]), instruction.pc), source=source),
            semantics="dynamic",
        )
        if len(operands) >= 3 and int(operands[2]) != 0:
            condition = UnaryOp(source=source, op="not ", value=condition)
        return condition
    if instruction.opcode == "EQK" and len(operands) >= 2:
        condition = BinaryOp(
            source=source,
            op="==",
            left=Var(name=_read_register_name(listing, int(operands[0]), instruction.pc), source=source),
            right=_constant_operand_expr(constants, operands[1], source),
            semantics="dynamic",
        )
        if len(operands) >= 3 and int(operands[2]) != 0:
            condition = UnaryOp(source=source, op="not ", value=condition)
        return condition
    if instruction.opcode in IMMEDIATE_COMPARISON_OPS and len(operands) >= 2:
        condition = BinaryOp(
            source=source,
            op=IMMEDIATE_COMPARISON_OPS[instruction.opcode],
            left=Var(name=_read_register_name(listing, int(operands[0]), instruction.pc), source=source),
            right=Const(value=int(operands[1]), source=source),
            semantics="dynamic",
        )
        if len(operands) >= 3 and int(operands[2]) != 0:
            condition = UnaryOp(source=source, op="not ", value=condition)
        return condition
    if instruction.opcode == "TESTSET" and len(operands) >= 2:
        value = Var(name=_read_register_name(listing, int(operands[1]), instruction.pc), source=source)
        if len(operands) >= 3 and int(operands[2]) != 0:
            return UnaryOp(source=source, op="not ", value=value)
        return value
    if instruction.opcode == "TEST" and operands:
        value = Var(name=_read_register_name(listing, int(operands[0]), instruction.pc), source=source)
        if len(operands) >= 2 and int(operands[1]) != 0:
            return UnaryOp(source=source, op="not ", value=value)
        return value
    if instruction.opcode == "FORLOOP" and operands:
        base = int(operands[0])
        index = Var(name=_read_register_name(listing, base, instruction.pc), source=source)
        limit = Var(name=_read_register_name(listing, base + 1, instruction.pc), source=source)
        step = Var(name=_read_register_name(listing, base + 2, instruction.pc), source=source)
        return Call(
            source=source,
            callee=Global(name="vm_forloop_continues", source=source),
            args=(index, limit, step),
        )
    if instruction.opcode == "TFORLOOP" and operands:
        base = int(operands[0])
        return BinaryOp(
            source=source,
            op="!=",
            left=Var(name=_read_register_name(listing, base + 4, instruction.pc), source=source),
            right=Const(value=None, source=source),
            semantics="dynamic",
        )
    return None


def _local_for_slot_at_pc(
    listing: LuaFunctionListing,
    slot: int,
    pc: int,
):
    for local in listing.locals:
        if local.slot == slot and local.start_pc <= pc < local.end_pc:
            return local
    return None


def _lua_bytecode_step(
    listing: LuaFunctionListing,
    constants: dict[int, object],
    instruction,
) -> VMBytecodeStep:
    source = SourceRef(frontend="lua", offset=instruction.pc, line=instruction.line, detail=f"pc={instruction.pc}")
    decoded = _lua_decoded_instruction(instruction, source)
    return VMBytecodeStep(
        opcode=decoded.opcode,
        source=source,
        effects=_lua_instruction_effects(listing, constants, instruction, source),
        raw=decoded.raw,
        decoded=decoded,
        hints=_lua_instruction_hints(listing, instruction, source),
    )


def _lua_decoded_instruction(instruction, source: SourceRef) -> VMDecodedInstruction:
    return VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=tuple(VMOperand(role=_lua_operand_role(operand), value=operand, text=operand) for operand in instruction.operands),
        raw=f"{instruction.pc}: {instruction.opcode} {' '.join(instruction.operands)}".strip(),
        artifact_range=(None if instruction.artifact_offset is None or instruction.size is None else ByteRange(instruction.artifact_offset, instruction.size)),
    )


def _lua_operand_role(operand: str):
    if operand.endswith("k"):
        return "constant"
    if operand.lstrip("-").isdigit():
        return "register"
    return "raw"


def _lua_instruction_hints(listing: LuaFunctionListing, instruction, source: SourceRef) -> tuple[VMHint, ...]:
    if instruction.opcode in CONDITIONAL_OPS:
        return (
            VMHint(
                kind="branch-target",
                source=source,
                target=instruction.pc + 2,
                label=instruction.opcode,
                detail="target-if-true",
                flow="conditional",
            ),
        )
    if instruction.opcode not in JUMP_OPS:
        return ()
    if instruction.opcode == "TFORLOOP":
        target = _lua_tforloop_body_target(listing, instruction)
        if target is None:
            return ()
        return (
            VMHint(
                kind="loop-backedge",
                source=source,
                target=target,
                label=instruction.opcode,
                detail="target-if-true",
                flow="conditional",
            ),
        )
    target = _lua_jump_target(instruction)
    kind = "loop-backedge" if target is not None and target <= instruction.pc else "branch-target"
    detail = "target-if-true" if instruction.opcode == "FORLOOP" else None
    flow = "conditional" if instruction.opcode in {"FORLOOP", "TFORLOOP"} else "unconditional"
    return (VMHint(kind=kind, source=source, target=target, label=instruction.opcode, detail=detail, flow=flow),)


def _lua_tforloop_body_target(listing: LuaFunctionListing, instruction) -> int | None:
    if not instruction.operands:
        return None
    base = instruction.operands[0]
    for prior in reversed(tuple(candidate for candidate in listing.instructions if candidate.pc < instruction.pc)):
        if prior.opcode == "TFORPREP" and prior.operands and prior.operands[0] == base:
            return prior.pc + 1
    return None


def _lua_comment_target(instruction) -> int | None:
    comment = getattr(instruction, "comment", None)
    if not comment:
        return None
    match = re.search(r"\bto\s+(-?\d+)\b", comment)
    if match is None:
        return None
    return int(match.group(1))


def _lua_jump_target(instruction) -> int | None:
    target = _lua_comment_target(instruction)
    if target is not None or not instruction.operands:
        return target
    try:
        return instruction.pc + 1 + int(instruction.operands[-1])
    except ValueError:
        return None


def _lua_function_spec(listing: LuaFunctionListing, local_names: tuple[str, ...] = ()) -> VMFunctionSpec:
    return VMFunctionSpec(
        name=listing.inferred_name or "<function>",
        params=tuple(_parameter_name(listing, index) for index in range(listing.param_count)),
        frontend="lua",
        instruction_count=len(listing.instructions),
        local_names=local_names,
    )


def _lua_upvalue_expr(listing: LuaFunctionListing, index: object, source: SourceRef) -> CapturedVar:
    upvalue_index = int(index)
    if 0 <= upvalue_index < len(listing.upvalues):
        name = listing.upvalues[upvalue_index].name
        if name:
            return CapturedVar(name=name, source=source)
    return CapturedVar(name=f"upvalue_{index}", source=source)


def _is_lua_env_upvalue(expr: Expr) -> bool:
    return isinstance(expr, CapturedVar) and expr.name == "_ENV"


def _lua_instruction_effects(
    listing: LuaFunctionListing,
    constants: dict[int, object],
    instruction,
    source: SourceRef,
) -> tuple[object, ...] | None:
    return LUA_EFFECT_TABLE.effects_for(LuaEffectContext(listing=listing, constants=constants), instruction, source)


def _lua_assign(
    listing: LuaFunctionListing,
    register: int,
    pc: int,
    value: Expr,
    source: SourceRef,
) -> AssignValue:
    target_name = _write_register_name(listing, register, pc)
    return AssignValue(
        source=source,
        name=target_name,
        target=Var(name=target_name, source=source),
        value=value,
    )


def _local_starting_near_write(
    listing: LuaFunctionListing,
    slot: int,
    pc: int,
):
    current = _local_for_slot_at_pc(listing, slot, pc)
    if current is not None and current.end_pc <= pc:
        for local in listing.locals:
            if local.slot == slot and local.start_pc == pc + 1 and _looks_user_named(local.name):
                return local
    if current is not None and _looks_user_named(current.name):
        shadowing = (
            local
            for local in listing.locals
            if local.slot == slot
            and local.start_pc <= pc < local.end_pc
            and _looks_user_named(local.name)
        )
        return max(shadowing, key=lambda local: local.start_pc, default=current)
    active = _local_for_slot_at_pc(listing, slot, pc + 1)
    if active is not None:
        return active
    for lookahead_pc in (pc + 1, pc + 2):
        for local in listing.locals:
            if local.slot == slot and local.start_pc == lookahead_pc and _looks_user_named(local.name):
                return local
    table_local = _local_starting_after_table_initializer(listing, slot, pc)
    if table_local is not None:
        return table_local
    if current is not None:
        return current
    for lookahead_pc in (pc + 1, pc + 2):
        for local in listing.locals:
            if local.slot == slot and local.start_pc == lookahead_pc:
                return local
    return None


def _parameter_name(listing: LuaFunctionListing, register: int) -> str:
    local = _local_for_slot_at_pc(listing, register, 1)
    if local is not None:
        return _local_display_name(local, register)
    return f"arg{register}"


def _read_register_name(listing: LuaFunctionListing, register: int, pc: int) -> str:
    local = _local_for_slot_at_pc(listing, register, pc)
    if local is not None:
        return _local_display_name(local, register)
    initializer_local = _table_initializer_local_for_slot_at_pc(listing, register, pc)
    if initializer_local is not None:
        return _local_display_name(initializer_local, register)
    initializer_local = _local_starting_after_setlist_read(listing, register, pc)
    if initializer_local is not None:
        return _local_display_name(initializer_local, register)
    if register < listing.param_count:
        return _parameter_name(listing, register)
    return f"r{register}"


def _write_register_name(listing: LuaFunctionListing, register: int, pc: int) -> str:
    local = _local_starting_near_write(listing, register, pc)
    if local is not None:
        if local.start_pc > pc + 1 and _next_instruction_reads_register(listing, pc, register):
            return f"r{register}"
        return _local_display_name(local, register)
    if register < listing.param_count:
        return _parameter_name(listing, register)
    return f"r{register}"


def _local_display_name(local, register: int) -> str:
    if _looks_user_named(local.name):
        return local.name
    return f"r{register}"


def _local_starting_after_table_initializer(
    listing: LuaFunctionListing,
    slot: int,
    pc: int,
):
    instruction = _instruction_at_pc(listing, pc)
    if instruction is None or instruction.opcode != "NEWTABLE" or not instruction.operands:
        return None
    if int(instruction.operands[0]) != slot:
        return None
    # Debug-local ranges begin after the final initializer instruction.  A
    # table literal can contain more than the old fixed lookahead window, so
    # find that boundary from bytecode facts instead of guessing a source-size
    # limit.  Values for SETLIST may be prepared in other registers, but the
    # table register itself must never be overwritten or cross a control-flow
    # boundary before its debug-local range starts.
    saw_table_write = False
    for candidate_instruction in listing.instructions:
        if candidate_instruction.pc <= pc:
            continue
        local = next(
            (
                item
                for item in listing.locals
                if item.slot == slot
                and item.start_pc == candidate_instruction.pc
                and _looks_user_named(item.name)
            ),
            None,
        )
        if local is not None:
            return local if saw_table_write else None
        if _is_table_initializer_write_for_slot(candidate_instruction, slot):
            saw_table_write = True
            continue
        if candidate_instruction.opcode in CONTROL_OPS:
            return None
        if _instruction_writes_register(candidate_instruction, slot):
            return None
    return None


def _is_table_initializer_write_for_slot(instruction, slot: int) -> bool:
    """Return whether an instruction can extend one freshly allocated table."""

    if instruction.opcode == "EXTRAARG":
        return True
    if instruction.opcode not in {"SETFIELD", "SETI", "SETTABLE", "SETLIST"}:
        return False
    return bool(instruction.operands) and int(instruction.operands[0]) == slot


def _table_initializer_local_for_slot_at_pc(
    listing: LuaFunctionListing,
    slot: int,
    pc: int,
):
    """Resolve a table register while its debug-local range has not opened."""

    current = _instruction_at_pc(listing, pc)
    if current is None or not _is_table_initializer_write_for_slot(current, slot):
        return None
    for prior in reversed(listing.instructions):
        if prior.pc >= pc:
            continue
        if prior.opcode != "NEWTABLE" or not prior.operands or int(prior.operands[0]) != slot:
            continue
        local = _local_starting_after_table_initializer(listing, slot, prior.pc)
        if local is not None and prior.pc < pc < local.start_pc:
            return local
        return None
    return None


def _instruction_writes_register(instruction, register: int) -> bool:
    if not instruction.operands or not instruction.operands[0].lstrip("-").isdigit():
        return False
    if instruction.opcode in {"CALL", "TAILCALL"}:
        # A call can write its base and, when it returns multiple values,
        # following registers as well.  Treat an overlapping base as a
        # clobber; this is conservative and avoids renaming a reused slot.
        return int(instruction.operands[0]) <= register
    if instruction.opcode in LUA_REGISTER_DEST_OPS:
        return int(instruction.operands[0]) == register
    return instruction.opcode == "TESTSET" and int(instruction.operands[0]) == register


def _local_starting_after_setlist_read(
    listing: LuaFunctionListing,
    slot: int,
    pc: int,
):
    instruction = _instruction_at_pc(listing, pc)
    if instruction is None or instruction.opcode != "SETLIST" or not instruction.operands:
        return None
    if int(instruction.operands[0]) != slot:
        return None
    return _nearest_user_local_start(listing, slot, pc + 1, pc + 1)


def _nearest_user_local_start(
    listing: LuaFunctionListing,
    slot: int,
    start_pc: int,
    end_pc: int,
):
    candidates = [
        local
        for local in listing.locals
        if local.slot == slot and start_pc <= local.start_pc <= end_pc and _looks_user_named(local.name)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda local: local.start_pc)


def _instruction_at_pc(listing: LuaFunctionListing, pc: int):
    return next((instruction for instruction in listing.instructions if instruction.pc == pc), None)


def _next_instruction_reads_register(
    listing: LuaFunctionListing,
    pc: int,
    register: int,
) -> bool:
    next_instruction = next((instruction for instruction in listing.instructions if instruction.pc == pc + 1), None)
    if next_instruction is None:
        return False
    return register in _read_registers_for_instruction(next_instruction)


def _read_registers_for_instruction(instruction) -> set[int]:
    operands = instruction.operands
    if not operands:
        return set()
    opcode = instruction.opcode
    result: set[int] = set()
    if opcode in {"MOVE", "UNM", "BNOT", "NOT", "LEN"} and len(operands) >= 2:
        result.add(int(operands[1]))
    elif opcode in {"GETTABLE", "GETFIELD", "GETI"} and len(operands) >= 2:
        result.add(int(operands[1]))
        if opcode == "GETTABLE" and len(operands) >= 3 and not operands[2].endswith("k"):
            result.add(int(operands[2]))
    elif opcode in {"SETTABLE", "SETFIELD", "SETI"}:
        result.add(int(operands[0]))
        if opcode == "SETTABLE" and len(operands) >= 2 and not operands[1].endswith("k"):
            result.add(int(operands[1]))
        if len(operands) >= 3 and not operands[2].endswith("k"):
            result.add(int(operands[2]))
    elif opcode == "SELF" and len(operands) >= 2:
        result.add(int(operands[1]))
    elif opcode == "CONCAT":
        start = int(operands[1]) if len(operands) >= 3 else int(operands[0])
        end = int(operands[2]) if len(operands) >= 3 else int(operands[0]) + int(operands[1]) - 1
        result.update(range(start, end + 1))
    elif opcode in BINARY_OPS or opcode in {
        "BAND",
        "BOR",
        "BXOR",
        "SHL",
        "SHR",
        "POW",
    }:
        if len(operands) >= 3:
            result.update((int(operands[1]), int(operands[2])))
    elif opcode in IMMEDIATE_BINARY_OPS or opcode in {"SHRI", "SHLI"}:
        if len(operands) >= 2:
            result.add(int(operands[1]))
    elif opcode.endswith("K") and opcode not in {"LOADK", "LOADKX"}:
        if len(operands) >= 2:
            result.add(int(operands[1]))
    elif opcode in {"CALL", "TAILCALL"} and len(operands) >= 2:
        base = int(operands[0])
        arg_count = int(operands[1]) - 1
        result.add(base)
        if arg_count >= 0:
            result.update(range(base + 1, base + 1 + arg_count))
    elif opcode in COMPARISON_OPS and len(operands) >= 2:
        result.update((int(operands[0]), int(operands[1])))
    elif opcode in set(IMMEDIATE_COMPARISON_OPS) | {"TEST"} and operands:
        result.add(int(operands[0]))
    elif opcode == "RETURN" and len(operands) >= 2:
        start = int(operands[0])
        count = int(operands[1]) - 1
        if count >= 0:
            result.update(range(start, start + count))
    elif opcode in {"RETURN1", "RETURNI", "RETURNK"}:
        result.add(int(operands[0]))
    return result


def _looks_user_named(name: str) -> bool:
    return not (name.startswith("(") and name.endswith(")"))


def _is_root_chunk(listing: LuaFunctionListing) -> bool:
    return listing.inferred_name == "<chunk>" and listing.line_start == 0


def _constant_operand_expr(
    constants: dict[int, object],
    operand: str,
    source: SourceRef,
) -> Expr:
    constant_index = int(operand.removesuffix("k"))
    return Const(value=constants.get(constant_index), source=source)


def _operand_expr(
    listing: LuaFunctionListing,
    constants: dict[int, object],
    operand: str,
    pc: int,
    source: SourceRef,
) -> Expr:
    if operand.endswith("k"):
        return _constant_operand_expr(constants, operand, source)
    return Var(name=_read_register_name(listing, int(operand), pc), source=source)


def _lua_call_argument_expr(
    listing: LuaFunctionListing,
    register: int,
    pc: int,
    source: SourceRef,
) -> Expr:
    register_name = _read_register_name(listing, register, pc)
    return Var(name=register_name, source=source)


def _lua_open_call_arg_count(listing: LuaFunctionListing, instruction) -> int:
    base = int(instruction.operands[0])
    previous = next(
        (prior for prior in reversed(listing.instructions) if prior.pc < instruction.pc),
        None,
    )
    if (
        previous is not None
        and previous.opcode == "CALL"
        and len(previous.operands) >= 3
        and int(previous.operands[0]) == base + 1
        and int(previous.operands[2]) == 0
    ):
        return 1
    highest = base
    for prior in listing.instructions:
        if prior.pc >= instruction.pc:
            break
        if not prior.operands:
            continue
        if not prior.operands[0].lstrip("-").isdigit():
            continue
        if prior.opcode not in LUA_REGISTER_DEST_OPS:
            continue
        dest = int(prior.operands[0])
        if dest > highest:
            highest = dest
    return max(0, highest - base)


LUA_REGISTER_DEST_OPS = frozenset(
    {
        "ADDI",
        "ADDK",
        "ADD",
        "BAND",
        "BANDK",
        "BOR",
        "BORK",
        "BXOR",
        "BXORK",
        "CALL",
        "CONCAT",
        "CLOSURE",
        "DIV",
        "DIVK",
        "EQ",
        "EQI",
        "EQK",
        "GETFIELD",
        "GETI",
        "GETTABLE",
        "GETTABUP",
        "GETUPVAL",
        "IDIV",
        "IDIVK",
        "LOADFALSE",
        "LOADF",
        "LOADI",
        "LOADK",
        "LOADKX",
        "LOADNIL",
        "LOADTRUE",
        "MOD",
        "MODK",
        "MUL",
        "MULK",
        "NEWTABLE",
        "POW",
        "POWK",
        "SELF",
        "SHL",
        "SHLI",
        "SHR",
        "SHRI",
        "SUB",
        "SUBK",
        "VARARG",
    }
)


def _lua_raw_instruction_window(
    instructions: tuple[object, ...],
    index: int,
    radius: int = 3,
) -> tuple[str, ...]:
    start = max(0, index - radius)
    end = min(len(instructions), index + radius + 1)
    return tuple(
        f"{instruction.pc}: {instruction.opcode} {' '.join(instruction.operands)}".strip()
        for instruction in instructions[start:end]
    )
