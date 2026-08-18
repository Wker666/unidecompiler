from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from unidecompiler.core.ir import (
    Assign,
    AssignMany,
    ArrayLiteral,
    BinaryOp,
    CapturedVar,
    Call,
    Const,
    GetAttr,
    GetItem,
    IndirectCall,
    IndirectRef,
    Expr,
    ExprStmt,
    Global,
    MapLiteral,
    NewObject,
    Placeholder,
    SetLiteral,
    UnaryOp,
    SourceRef,
    StoreItem,
    StoreAttr as IRStoreAttr,
    Stmt,
    TableField,
    Var,
    NumericDomain,
    Unsupported,
)
from unidecompiler.core.stack_machine import StackMachineState


@dataclass(frozen=True)
class Effect:
    """VM-neutral stack/value effect before high-level IR recovery.

    Frontends should prefer emitting these small facts over directly mutating a
    stack machine or constructing structured IR. Core passes interpret effects
    uniformly across bytecode families and future VM adapters.
    """

    source: SourceRef | None = None


@dataclass(frozen=True)
class UnknownOpcode(Effect):
    opcode: str = ""
    raw: str = ""


@dataclass(frozen=True)
class Push(Effect):
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class LoadLocal(Effect):
    name: str = ""
    fallback: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class StoreLocal(Effect):
    name: str = ""
    materialize: bool = True
    target: Expr | None = None
    missing_value: Expr | None = None


@dataclass(frozen=True)
class AssignValue(Effect):
    name: str = ""
    target: Var | None = None
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class UpdateLocal(Effect):
    """Update a local while preserving any precomputed stack uses.

    A number of VMs have an increment instruction which mutates a local
    without consuming operand-stack values.  A value loaded before that
    instruction must continue to denote the pre-update value.
    """

    name: str = ""
    target: Var | None = None
    op: str = "+"
    value: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class AssignValueOnBranch(Effect):
    name: str = ""
    target: Var | None = None
    value: Expr = field(default_factory=Expr)
    branch: Literal["true", "false"] = "true"


@dataclass(frozen=True)
class AssignManyValues(Effect):
    names: tuple[str, ...] = ()
    targets: tuple[Var, ...] | None = None
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class StoreMany(Effect):
    names: tuple[str, ...] = ()
    materialize: bool = True


@dataclass(frozen=True)
class StoreManyFromPopOrder(Effect):
    names: tuple[str, ...] = ()
    materialize: bool = True
    values_reversed: bool = False


@dataclass(frozen=True)
class Pop(Effect):
    count: int = 1
    emit_calls: bool = False
    allow_missing: bool = False


@dataclass(frozen=True)
class Binary(Effect):
    op: str = ""
    semantics: Literal["static", "dynamic"] = "dynamic"
    numeric_domain: NumericDomain = "default"
    bit_width: int | None = None


@dataclass(frozen=True)
class Unary(Effect):
    op: str = ""


@dataclass(frozen=True)
class Compare(Effect):
    op: str = ""
    negate: bool = False
    numeric_domain: NumericDomain = "default"
    bit_width: int | None = None


@dataclass(frozen=True)
class Truthy(Effect):
    """Normalize the top stack value into a VM-neutral truth condition."""

    pass


@dataclass(frozen=True)
class SelectValue(Effect):
    """Select between two stack values using a stack condition."""

    pass


@dataclass(frozen=True)
class LoadAttr(Effect):
    attr: str = ""


@dataclass(frozen=True)
class LoadSuperAttr(Effect):
    attr: str = ""
    arg_count: int = 3


@dataclass(frozen=True)
class LoadAttrFromTop(Effect):
    attr: str = ""
    fallback_obj: Expr = field(default_factory=Expr)


@dataclass(frozen=True)
class StoreAttr(Effect):
    attr: str = ""
    order: Literal["obj-value", "value-obj"] = "obj-value"


@dataclass(frozen=True)
class StoreStaticMember(Effect):
    owner: str = ""
    field_name: str = ""


@dataclass(frozen=True)
class LoadItem(Effect):
    pass


@dataclass(frozen=True)
class LoadItemAddress(Effect):
    pass


@dataclass(frozen=True)
class StoreItemEffect(Effect):
    order: Literal["obj-key-value", "value-obj-key"] = "obj-key-value"


@dataclass(frozen=True)
class StoreItemAtDepth(Effect):
    depth: int = 1


@dataclass(frozen=True)
class LoadIndirect(Effect):
    pass


@dataclass(frozen=True)
class StoreIndirect(Effect):
    pass


@dataclass(frozen=True)
class BuildArray(Effect):
    kind: Literal["list", "tuple"] = "list"
    count: int = 0


@dataclass(frozen=True)
class ExtendArray(Effect):
    pass


@dataclass(frozen=True)
class BuildSet(Effect):
    count: int = 0


@dataclass(frozen=True)
class SetAdd(Effect):
    pass


@dataclass(frozen=True)
class Iterate(Effect):
    pass


@dataclass(frozen=True)
class BuildMap(Effect):
    count: int = 0
    keys: tuple[Expr, ...] | None = None


@dataclass(frozen=True)
class MergeMap(Effect):
    """Merge the top mapping into a mapping below it on the operand stack."""

    depth: int = 1


@dataclass(frozen=True)
class BuildConstKeyMap(Effect):
    count: int = 0


@dataclass(frozen=True)
class BuildString(Effect):
    count: int = 0


@dataclass(frozen=True)
class Unpack(Effect):
    count: int = 0
    before: int = 0
    after: int = 0


@dataclass(frozen=True)
class Copy(Effect):
    depth: int = 1
    allow_missing: bool = False


@dataclass(frozen=True)
class DuplicateTop(Effect):
    materialized_name: str | None = None


@dataclass(frozen=True)
class DuplicateTopWide(Effect):
    pass


@dataclass(frozen=True)
class DuplicateTopBelow(Effect):
    """Duplicate the top value, inserting the copy below stack values.

    This represents stack permutations such as ``dup_x1`` without
    making an adapter emulate the permutation through unrelated effects.
    """

    below_count: int = 1


@dataclass(frozen=True)
class DropBelowTop(Effect):
    count: int = 1


@dataclass(frozen=True)
class Swap(Effect):
    depth: int = 1
    allow_missing: bool = False


@dataclass(frozen=True)
class Invoke(Effect):
    arg_count: int = 0
    receiver: Expr | None = None
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class InvokeKw(Effect):
    arg_count: int = 0
    receiver: Expr | None = None
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class InvokeExpanded(Effect):
    """Invoke a stack callee with a positional container and optional keyword map."""

    has_keywords: bool = False
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class InvokeMethod(Effect):
    attr: str = ""
    arg_count: int = 0
    depth: int = 1
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class InvokeMember(Effect):
    owner: str = ""
    member: str = ""
    arg_count: int = 0
    static: bool = False
    returns: int | Literal["unknown"] = 1
    constructor_type: str | None = None


@dataclass(frozen=True)
class BuildCall(Effect):
    callee: Expr = field(default_factory=Expr)
    arg_count: int = 0
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class BuildIndirectCall(Effect):
    arg_count: int = 0
    signature: str = "unknown"
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class CallStackArgs(Effect):
    callee_name: str = ""
    arg_count: int = 0
    returns: int | Literal["unknown"] = 1


@dataclass(frozen=True)
class MakeFunctionValue(Effect):
    fallback_name: str = "<function>"


@dataclass(frozen=True)
class FormatTop(Effect):
    converter: str = "format"


@dataclass(frozen=True)
class CallTopAs(Effect):
    callee_name: str = ""


@dataclass(frozen=True)
class BuildArrayCall(Effect):
    kind: str = "array"


@dataclass(frozen=True)
class BuildShapeTest(Effect):
    """Build a generic shape/pattern-test value from stack operands."""

    descriptor_count: int = 0


@dataclass(frozen=True)
class Emit(Effect):
    statement: Stmt = field(default_factory=Stmt)


@dataclass(frozen=True)
class ReturnTop(Effect):
    empty_is_void: bool = False


@dataclass(frozen=True)
class ReturnVoid(Effect):
    pass


@dataclass(frozen=True)
class ReturnValues(Effect):
    values: tuple[Expr, ...] = ()


@dataclass(frozen=True)
class RaiseTop(Effect):
    pass


@dataclass(frozen=True)
class RaiseWithCause(Effect):
    """Raise the exception value below an explicit causal value."""

    pass


@dataclass(frozen=True)
class ReraiseTop(Effect):
    """Reraise the active exception without manufacturing a new value."""

    pass


@dataclass(frozen=True)
class ExceptionMatch(Effect):
    """Compare the active exception with a handler type on the stack."""

    pass


@dataclass(frozen=True)
class YieldTop(Effect):
    default: Expr | None = None


def apply_effect(state: StackMachineState, effect: Effect) -> bool:
    """Apply one VM-neutral effect to a generic stack-machine state."""

    if isinstance(effect, UnknownOpcode):
        state.append_statement(
            Unsupported(
                source=effect.source,
                message="unsupported opcode",
                detail=effect.opcode,
                raw=(effect.raw,) if effect.raw else (),
            )
        )
        return True
    if isinstance(effect, Push):
        state.push(effect.value)
        return True
    if isinstance(effect, LoadLocal):
        state.load_local(effect.name, effect.fallback)
        return True
    if isinstance(effect, StoreLocal):
        value = state.pop()
        if value is None:
            if effect.missing_value is None:
                return False
            value = effect.missing_value
        target = effect.target or Var(name=effect.name, source=effect.source)
        state.locals[effect.name] = target if isinstance(target, Var) else value
        if effect.materialize:
            if isinstance(target, Var):
                state.append_statement(Assign(source=effect.source, target=target, value=value))
            elif isinstance(target, CapturedVar):
                state.append_statement(Assign(source=effect.source, target=target, value=value))
            elif isinstance(target, GetItem):
                state.append_statement(StoreItem(source=effect.source, obj=target.obj, key=target.key, value=value))
            elif isinstance(target, GetAttr):
                state.append_statement(IRStoreAttr(source=effect.source, obj=target.obj, attr=target.attr, value=value))
            elif isinstance(target, IndirectRef):
                resolved = target.target
                if isinstance(resolved, Var):
                    state.append_statement(Assign(source=effect.source, target=resolved, value=value))
                elif isinstance(resolved, CapturedVar):
                    state.append_statement(Assign(source=effect.source, target=resolved, value=value))
                elif isinstance(resolved, GetItem):
                    state.append_statement(StoreItem(source=effect.source, obj=resolved.obj, key=resolved.key, value=value))
                elif isinstance(resolved, GetAttr):
                    state.append_statement(IRStoreAttr(source=effect.source, obj=resolved.obj, attr=resolved.attr, value=value))
                else:
                    state.diagnostics.append("invalid-local-store-target")
                    return False
            else:
                state.diagnostics.append("invalid-local-store-target")
                return False
        return True
    if isinstance(effect, AssignValue):
        target = effect.target or Var(name=effect.name, source=effect.source)
        state.locals[effect.name] = target
        state.append_statement(Assign(source=effect.source, target=target, value=effect.value))
        return True
    if isinstance(effect, UpdateLocal):
        target = effect.target or Var(name=effect.name, source=effect.source)
        snapshot = Var(
            name=f"order_tmp_{getattr(effect.source, 'offset', 'local')}_{effect.name}_before_update",
            source=effect.source,
        )
        active_uses = [
            index
            for index, value in enumerate(state.stack)
            if isinstance(value, Var) and value.name == effect.name
        ]
        if active_uses:
            state.append_statement(Assign(source=effect.source, target=snapshot, value=target))
            for index in active_uses:
                state.stack[index] = snapshot
        state.locals[effect.name] = target
        state.append_statement(
            Assign(
                source=effect.source,
                target=target,
                value=BinaryOp(
                    source=effect.source,
                    op=effect.op,
                    left=target,
                    right=effect.value,
                    semantics="static",
                ),
            )
        )
        return True
    if isinstance(effect, AssignManyValues):
        targets = effect.targets or tuple(Var(name=name, source=effect.source) for name in effect.names)
        if len(targets) != len(effect.names):
            state.diagnostics.append("assign-many-target-count-mismatch")
            return False
        for name, target in zip(effect.names, targets, strict=True):
            state.locals[name] = target
        if len(targets) == 1 and len(effect.values) == 1:
            state.append_statement(Assign(source=effect.source, target=targets[0], value=effect.values[0]))
        else:
            state.append_statement(AssignMany(source=effect.source, targets=targets, values=effect.values))
        return True
    if isinstance(effect, StoreMany):
        values = state.pop_many(len(effect.names))
        if values is None:
            return False
        targets = tuple(Var(name=name, source=effect.source) for name in effect.names)
        for name, target in zip(effect.names, targets, strict=True):
            state.locals[name] = target
        if effect.materialize:
            if len(targets) == 1:
                state.append_statement(Assign(source=effect.source, target=targets[0], value=values[0]))
            else:
                state.append_statement(AssignMany(source=effect.source, targets=targets, values=values))
        return True
    if isinstance(effect, StoreManyFromPopOrder):
        values = state.pop_many(len(effect.names))
        if values is None:
            return False
        names = tuple(reversed(effect.names))
        if effect.values_reversed:
            values = tuple(reversed(values))
        targets = tuple(Var(name=name, source=effect.source) for name in names)
        for name, target in zip(names, targets, strict=True):
            state.locals[name] = target
        if effect.materialize:
            state.append_statement(AssignMany(source=effect.source, targets=targets, values=values))
        return True
    if isinstance(effect, Pop):
        if effect.allow_missing and len(state.stack) < effect.count:
            state.stack.clear()
            return True
        values = state.pop_many(effect.count)
        if values is None:
            return False
        if effect.emit_calls:
            for value in values:
                if isinstance(value, Call):
                    state.append_statement(ExprStmt(source=effect.source, value=value))
        return True
    if isinstance(effect, Binary):
        values = state.pop_many(2)
        if values is None:
            return False
        left, right = values
        state.push(
            BinaryOp(
                source=effect.source,
                op=effect.op,
                left=left,
                right=right,
                semantics=effect.semantics,
                numeric_domain=effect.numeric_domain,
                bit_width=effect.bit_width,
            )
        )
        return True
    if isinstance(effect, Unary):
        value = state.pop()
        if value is None:
            return False
        state.push(
            UnaryOp(
                source=effect.source,
                op=effect.op,
                value=value,
            )
        )
        return True
    if isinstance(effect, Compare):
        values = state.pop_many(2)
        if values is None:
            return False
        left, right = values
        expr: Expr = BinaryOp(
            source=effect.source,
            op=effect.op,
            left=left,
            right=right,
            semantics="dynamic",
            numeric_domain=effect.numeric_domain,
            bit_width=effect.bit_width,
        )
        if effect.negate:
            expr = UnaryOp(source=effect.source, op="not ", value=expr)
        state.push(expr)
        return True
    if isinstance(effect, Truthy):
        value = state.pop()
        if value is None:
            return False
        state.push(value)
        return True
    if isinstance(effect, LoadAttr):
        obj = state.pop()
        if obj is None:
            return False
        state.push(GetAttr(source=effect.source, obj=obj, attr=effect.attr))
        return True
    if isinstance(effect, LoadSuperAttr):
        values = state.pop_many(effect.arg_count)
        if values is None:
            return False
        if len(values) < 1:
            state.diagnostics.append("super-attr-missing-callee")
            return False
        callee, *args = values
        state.push(
            GetAttr(
                source=effect.source,
                obj=Call(source=effect.source, callee=callee, args=tuple(args)),
                attr=effect.attr,
            )
        )
        return True
    if isinstance(effect, SelectValue):
        values = state.pop_many(3)
        if values is None:
            return False
        when_true, when_false, condition = values
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="select", source=effect.source),
                args=(when_true, when_false, condition),
            )
        )
        return True
    if isinstance(effect, LoadAttrFromTop):
        obj = state.stack[-1] if state.stack else effect.fallback_obj
        state.push(GetAttr(source=effect.source, obj=obj, attr=effect.attr))
        return True
    if isinstance(effect, StoreAttr):
        values = state.pop_many(2)
        if values is None:
            return False
        if effect.order == "value-obj":
            value, obj = values
        else:
            obj, value = values
        state.append_statement(IRStoreAttr(source=effect.source, obj=obj, attr=effect.attr, value=value))
        return True
    if isinstance(effect, StoreStaticMember):
        value = state.pop()
        if value is None:
            return False
        state.append_statement(
            IRStoreAttr(
                source=effect.source,
                obj=Global(name=effect.owner, source=effect.source),
                attr=effect.field_name,
                value=value,
            )
        )
        return True
    if isinstance(effect, LoadItem):
        values = state.pop_many(2)
        if values is None:
            return False
        obj, key = values
        state.push(GetItem(source=effect.source, obj=obj, key=key))
        return True
    if isinstance(effect, LoadItemAddress):
        values = state.pop_many(2)
        if values is None:
            return False
        obj, key = values
        state.push(IndirectRef(source=effect.source, target=GetItem(source=effect.source, obj=obj, key=key)))
        return True
    if isinstance(effect, StoreItemEffect):
        values = state.pop_many(3)
        if values is None:
            return False
        if effect.order == "value-obj-key":
            value, obj, key = values
        else:
            obj, key, value = values
        state.append_statement(StoreItem(source=effect.source, obj=obj, key=key, value=value))
        return True
    if isinstance(effect, StoreItemAtDepth):
        values = state.pop_many(2)
        if values is None:
            return False
        if effect.depth <= 0 or effect.depth > len(state.stack):
            state.diagnostics.append(f"invalid-store-depth:{effect.depth}")
            return False
        key, value = values
        obj = state.stack[-effect.depth]
        state.append_statement(StoreItem(source=effect.source, obj=obj, key=key, value=value))
        return True
    if isinstance(effect, LoadIndirect):
        if not state.stack:
            state.diagnostics.append("stack-underflow")
            return False
        top = state.stack[-1]
        if isinstance(top, IndirectRef):
            state.push(top.target)
        return True
    if isinstance(effect, StoreIndirect):
        values = state.pop_many(2)
        if values is None:
            return False
        target, value = values
        if isinstance(target, IndirectRef):
            target = target.target
        if isinstance(target, GetItem):
            state.append_statement(StoreItem(source=effect.source, obj=target.obj, key=target.key, value=value))
            return True
        if isinstance(target, GetAttr):
            state.append_statement(IRStoreAttr(source=effect.source, obj=target.obj, attr=target.attr, value=value))
            return True
        if isinstance(target, Var):
            state.append_statement(Assign(source=effect.source, target=target, value=value))
            return True
        if isinstance(target, CapturedVar):
            state.append_statement(Assign(source=effect.source, target=target, value=value))
            return True
        state.diagnostics.append("invalid-indirect-store-target")
        return False
    if isinstance(effect, BuildArray):
        items = state.pop_many(effect.count)
        if items is None:
            return False
        state.push(ArrayLiteral(source=effect.source, items=items))
        return True
    if isinstance(effect, ExtendArray):
        iterable = state.pop()
        if iterable is None:
            return False
        target = state.pop()
        if target is None:
            return False
        if isinstance(target, ArrayLiteral):
            items = _static_iterable_items(iterable, effect.source)
            if items is not None:
                state.push(ArrayLiteral(source=target.source or effect.source, items=(*target.items, *items)))
                return True
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="extend_array", source=effect.source),
                args=(target, iterable),
            )
        )
        return True
    if isinstance(effect, BuildSet):
        items = state.pop_many(effect.count)
        if items is None:
            return False
        state.push(SetLiteral(source=effect.source, items=items))
        return True
    if isinstance(effect, SetAdd):
        value = state.pop()
        if value is None:
            return False
        target = state.pop()
        if target is None:
            return False
        if isinstance(target, SetLiteral):
            state.push(SetLiteral(source=effect.source, items=(*target.items, value)))
        else:
            state.push(target)
        return True
    if isinstance(effect, Iterate):
        value = state.pop()
        if value is None:
            return False
        state.push(value)
        return True
    if isinstance(effect, BuildMap):
        if effect.keys is not None:
            values = state.pop_many(effect.count)
            if values is None:
                return False
            if len(effect.keys) != len(values):
                state.diagnostics.append("invalid-map-keys")
                return False
            fields = tuple(TableField(key=key, value=value) for key, value in zip(effect.keys, values, strict=True))
        else:
            values = state.pop_many(effect.count * 2)
            if values is None:
                return False
            fields = tuple(
                TableField(key=values[index], value=values[index + 1])
                for index in range(0, len(values), 2)
            )
        state.push(MapLiteral(source=effect.source, fields=fields))
        return True
    if isinstance(effect, MergeMap):
        if effect.depth <= 0 or effect.depth >= len(state.stack):
            state.diagnostics.append(f"invalid-map-merge-depth:{effect.depth}")
            return False
        mapping = state.pop()
        if mapping is None:
            return False
        target_index = len(state.stack) - effect.depth
        target = state.stack[target_index]
        state.stack[target_index] = Call(
            source=effect.source,
            callee=Global(name="merge", source=effect.source),
            args=(target, mapping),
        )
        return True
    if isinstance(effect, BuildConstKeyMap):
        key_tuple = state.pop()
        if key_tuple is None:
            return False
        values = state.pop_many(effect.count)
        if values is None:
            return False
        if not isinstance(key_tuple, Const) or not isinstance(key_tuple.value, tuple):
            state.diagnostics.append("invalid-const-key-map")
            return False
        if len(key_tuple.value) != len(values):
            state.diagnostics.append("invalid-const-key-count")
            return False
        state.push(
            MapLiteral(
                source=effect.source,
                fields=tuple(
                    TableField(key=Const(value=key, source=effect.source), value=value)
                    for key, value in zip(key_tuple.value, values, strict=True)
                ),
            )
        )
        return True
    if isinstance(effect, BuildString):
        items = state.pop_many(effect.count)
        if items is None:
            return False
        expr: Expr = Const(value="", source=effect.source)
        for item in items:
            expr = BinaryOp(source=effect.source, op="+", left=expr, right=item)
        state.push(expr)
        return True
    if isinstance(effect, Unpack):
        value = state.pop()
        if value is None:
            return False
        # A destructuring operation projects several fields from one VM stack
        # value. Materialize effectful calls once so every projection observes
        # the same return value instead of running the call again.
        if isinstance(value, Call) and effect.count != 1:
            item = Var(
                name=f"unpack_value_{effect.source.offset if effect.source and effect.source.offset is not None else len(state.statements)}",
                source=effect.source,
            )
            state.append_statement(Assign(source=effect.source, target=item, value=value))
            value = item
        if effect.count:
            for index in range(effect.count):
                state.push(GetItem(source=effect.source, obj=value, key=Const(value=index, source=effect.source)))
            return True
        if effect.before < 0 or effect.after < 0:
            state.diagnostics.append("invalid-unpack-span")
            return False
        for index in range(1, effect.before):
            state.push(GetItem(source=effect.source, obj=value, key=Const(value=index, source=effect.source)))
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="list_slice", source=effect.source),
                args=(
                    value,
                    Const(value=effect.before, source=effect.source),
                    Const(value=-effect.after if effect.after else None, source=effect.source),
                ),
            )
        )
        for index in range(effect.after, 0, -1):
            state.push(GetItem(source=effect.source, obj=value, key=Const(value=-index, source=effect.source)))
        if effect.before:
            state.push(GetItem(source=effect.source, obj=value, key=Const(value=0, source=effect.source)))
        return True
    if isinstance(effect, Copy):
        if effect.depth <= 0 or effect.depth > len(state.stack):
            if effect.allow_missing:
                return True
            state.diagnostics.append(f"invalid-copy-depth:{effect.depth}")
            return False
        state.push(state.stack[-effect.depth])
        return True
    if isinstance(effect, DuplicateTop):
        value = state.pop()
        if value is None:
            return False
        if effect.materialized_name and _should_materialize_stack_value(value):
            local = Var(name=effect.materialized_name, source=effect.source)
            state.append_statement(Assign(source=effect.source, target=local, value=value))
            value = local
        state.push(value)
        state.push(value)
        return True
    if isinstance(effect, DuplicateTopWide):
        if not state.stack:
            return False
        if len(state.stack) == 1:
            value = state.stack[-1]
            state.push(value)
            return True
        values = state.stack[-2:]
        state.push(values[0])
        state.push(values[1])
        return True
    if isinstance(effect, DuplicateTopBelow):
        if effect.below_count < 1 or len(state.stack) < effect.below_count + 1:
            state.diagnostics.append(f"invalid-duplicate-below-depth:{effect.below_count}")
            return False
        value = state.pop()
        if value is None:
            return False
        insert_at = len(state.stack) - effect.below_count
        state.stack.insert(insert_at, value)
        state.push(value)
        return True
    if isinstance(effect, DropBelowTop):
        if effect.count < 0:
            state.diagnostics.append(f"invalid-drop-below-top:{effect.count}")
            return False
        if not state.stack:
            state.diagnostics.append("stack-underflow")
            return False
        if len(state.stack) < effect.count + 1:
            state.diagnostics.append("stack-underflow")
            return False
        top = state.stack.pop()
        del state.stack[-effect.count:]
        state.stack.append(top)
        return True
    if isinstance(effect, Swap):
        if effect.depth <= 0 or effect.depth > len(state.stack):
            if effect.allow_missing:
                return True
            state.diagnostics.append(f"invalid-swap-depth:{effect.depth}")
            return False
        state.stack[-1], state.stack[-effect.depth] = state.stack[-effect.depth], state.stack[-1]
        return True
    if isinstance(effect, Invoke):
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        callee = effect.receiver or state.pop()
        if callee is None:
            return False
        if effect.arg_count == 0 and _looks_like_function_value(callee) and state.stack:
            decorator = state.pop()
            if decorator is not None and _looks_like_callable_value(decorator):
                call = Call(source=effect.source, callee=decorator, args=(callee,), returns=effect.returns)
                if effect.returns == 0:
                    state.append_statement(ExprStmt(source=effect.source, value=call))
                else:
                    state.push(call)
                return True
            if decorator is not None:
                state.push(decorator)
        call = Call(source=effect.source, callee=callee, args=args, returns=effect.returns)
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, InvokeKw):
        key_tuple = state.pop()
        if key_tuple is None:
            return False
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        callee = effect.receiver or state.pop()
        if callee is None:
            return False
        if isinstance(key_tuple, Const) and isinstance(key_tuple.value, tuple):
            keyword_count = len(key_tuple.value)
            keyword_fields = tuple(
                TableField(key=Const(value=key, source=effect.source), value=value)
                for key, value in zip(key_tuple.value, args[-keyword_count:], strict=False)
            )
            args = args[: len(args) - keyword_count]
        else:
            keyword_fields = ()
        call = Call(source=effect.source, callee=callee, args=args, keywords=keyword_fields, returns=effect.returns)
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, InvokeExpanded):
        keyword_map = state.pop() if effect.has_keywords else None
        if effect.has_keywords and keyword_map is None:
            return False
        positional_args = state.pop()
        if positional_args is None:
            return False
        callee = state.pop()
        if callee is None:
            return False
        args = (callee, positional_args) if keyword_map is None else (callee, positional_args, keyword_map)
        call = Call(
            source=effect.source,
            callee=Global(name="call_ex", source=effect.source),
            args=args,
            returns=effect.returns,
        )
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, InvokeMethod):
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        if effect.depth <= 0 or effect.depth > len(state.stack):
            state.diagnostics.append(f"invalid-invoke-depth:{effect.depth}")
            return False
        receiver = state.stack[-effect.depth]
        call = Call(
            source=effect.source,
            callee=GetAttr(source=effect.source, obj=receiver, attr=effect.attr),
            args=args,
            returns=effect.returns,
        )
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, InvokeMember):
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        receiver = None if effect.static else state.pop()
        if not effect.static and receiver is None:
            return False
        if effect.constructor_type is not None:
            if isinstance(receiver, Var) and receiver.name == "this":
                state.append_statement(
                    ExprStmt(
                        source=effect.source,
                        value=Call(
                            source=effect.source,
                            callee=GetAttr(
                                source=effect.source,
                                obj=Global(name=effect.constructor_type, source=effect.source),
                                attr="<init>",
                            ),
                            args=(receiver, *args),
                            returns=0,
                        ),
                    )
                )
                return True
            constructed = NewObject(
                source=effect.source,
                type_name=effect.constructor_type,
                args=args,
            )
            if receiver is None:
                state.push(constructed)
                return True
            _replace_stack_value(state, receiver, constructed)
            return True
        callee_obj: Expr = Global(name=effect.owner, source=effect.source) if receiver is None else receiver
        call = Call(
            source=effect.source,
            callee=GetAttr(source=effect.source, obj=callee_obj, attr=effect.member),
            args=args,
            returns=effect.returns,
        )
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, BuildCall):
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        call = Call(source=effect.source, callee=effect.callee, args=args, returns=effect.returns)
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, BuildIndirectCall):
        selector = state.pop()
        if selector is None:
            return False
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        call = Call(
            source=effect.source,
            callee=IndirectCall(source=effect.source, selector=selector, signature=effect.signature),
            args=args,
            returns=effect.returns,
        )
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, CallStackArgs):
        args = state.pop_many(effect.arg_count)
        if args is None:
            return False
        call = Call(
            source=effect.source,
            callee=Global(name=effect.callee_name, source=effect.source),
            args=args,
            returns=effect.returns,
        )
        if effect.returns == 0:
            state.append_statement(ExprStmt(source=effect.source, value=call))
        else:
            state.push(call)
        return True
    if isinstance(effect, MakeFunctionValue):
        fn = state.pop()
        if fn is None:
            return False
        name = getattr(fn.value, "co_name", fn.value) if isinstance(fn, Const) else effect.fallback_name
        state.push(Global(name=f"<function {name}>", source=effect.source))
        return True
    if isinstance(effect, FormatTop):
        value = state.pop()
        if value is None:
            return False
        state.push(
            Call(
                source=effect.source,
                callee=Global(name=effect.converter, source=effect.source),
                args=(value,),
            )
        )
        return True
    if isinstance(effect, CallTopAs):
        value = state.pop()
        if value is None:
            return False
        state.push(
            Call(
                source=effect.source,
                callee=Global(name=effect.callee_name, source=effect.source),
                args=(value,),
            )
        )
        return True
    if isinstance(effect, BuildArrayCall):
        size = state.pop()
        if size is None:
            return False
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="new_array", source=effect.source),
                args=(Const(value=effect.kind, source=effect.source), size),
            )
        )
        return True
    if isinstance(effect, BuildShapeTest):
        descriptors = state.pop_many(effect.descriptor_count)
        subject = state.stack[-1] if state.stack else None
        if descriptors is None or subject is None:
            return False
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="shape_test", source=effect.source),
                args=(subject, *descriptors),
                returns="unknown",
            )
        )
        return True
    if isinstance(effect, Emit):
        state.append_statement(effect.statement)
        return True
    if isinstance(effect, ReturnTop):
        if effect.empty_is_void and not state.stack:
            state.return_void(source=effect.source)
            return True
        return state.return_top(source=effect.source)
    if isinstance(effect, ReturnVoid):
        state.return_void(source=effect.source)
        return True
    if isinstance(effect, ReturnValues):
        state.return_values(effect.values, source=effect.source)
        return True
    if isinstance(effect, RaiseTop):
        value = state.pop() if state.stack else Const(value=None, source=effect.source)
        from unidecompiler.core.ir import Raise

        state.append_statement(Raise(source=effect.source, value=value))
        return True
    if isinstance(effect, RaiseWithCause):
        cause = state.pop()
        value = state.pop()
        if value is None or cause is None:
            return False
        from unidecompiler.core.ir import Raise

        state.append_statement(Raise(source=effect.source, value=value, cause=cause))
        return True
    if isinstance(effect, ReraiseTop):
        from unidecompiler.core.ir import Reraise

        state.append_statement(Reraise(source=effect.source))
        return True
    if isinstance(effect, ExceptionMatch):
        expected = state.pop()
        active = state.stack[-1] if state.stack else Global(name="current_exception", source=effect.source)
        if expected is None:
            return False
        state.push(
            Call(
                source=effect.source,
                callee=Global(name="exception_matches", source=effect.source),
                args=(active, expected),
            )
        )
        return True
    if isinstance(effect, YieldTop):
        value = state.pop() if state.stack else effect.default
        if value is None:
            return False
        from unidecompiler.core.ir import Yield

        state.append_statement(Yield(source=effect.source, value=value))
        return True
    state.diagnostics.append(f"unknown-effect:{type(effect).__name__}")
    return False


def _static_iterable_items(value: Expr, source: SourceRef | None) -> tuple[Expr, ...] | None:
    if isinstance(value, ArrayLiteral):
        return value.items
    if isinstance(value, SetLiteral):
        return value.items
    if isinstance(value, Const) and isinstance(value.value, (tuple, list, set)):
        return tuple(Const(source=source, value=item) for item in value.value)
    return None


def apply_effects(state: StackMachineState, effects: tuple[Effect, ...]) -> bool:
    for effect in effects:
        if not apply_effect(state, effect):
            return False
        if state.diagnostics:
            return False
    return True


def _should_materialize_stack_value(value: Expr) -> bool:
    if isinstance(value, Placeholder):
        return False
    return isinstance(value, (Call, NewObject, ArrayLiteral, SetLiteral, GetAttr, GetItem, BinaryOp))


def _looks_like_function_value(value: Expr) -> bool:
    if isinstance(value, Global) and value.name.startswith("<function "):
        return True
    return isinstance(value, Call) and len(value.args) == 1 and _looks_like_function_value(value.args[0])


def _looks_like_callable_value(value: Expr) -> bool:
    return isinstance(value, (Call, Global, GetAttr))


def _replace_stack_value(state: StackMachineState, old: Expr, new: Expr) -> None:
    state.stack[:] = [new if _same_replaceable_value(value, old) else value for value in state.stack]
    for index, statement in enumerate(state.statements):
        if isinstance(statement, Assign) and _same_replaceable_value(statement.value, old):
            state.statements[index] = Assign(source=statement.source, target=statement.target, value=new)


def _same_replaceable_value(value: Expr, old: Expr) -> bool:
    if value == old:
        return True
    if isinstance(value, Placeholder) and isinstance(old, Placeholder):
        return value.token == old.token
    return False
