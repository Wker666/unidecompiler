from __future__ import annotations

from unidecompiler.core.effects import Push, RaiseTop, ReturnTop
from unidecompiler.core.ir import Call, Const, Global, SourceRef
from unidecompiler.core.vm_bytecode import VMBytecodeStep, run_vm_steps
from unidecompiler.core.vm_function import VMFunctionSpec, lift_vm_step_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_region import (
    VMLinearState,
    VMRegionProfile,
    VMStatefulCallbacks,
)


def _source(offset: int) -> SourceRef:
    return SourceRef(frontend="test-vm", offset=offset)


def _step(
    offset: int,
    opcode: str,
    effects: tuple[object, ...] = (),
    hints: tuple[VMHint, ...] = (),
) -> VMBytecodeStep:
    return VMBytecodeStep(
        opcode=opcode,
        source=_source(offset),
        effects=effects,
        hints=hints,
    )


def _profile(branching: bool = False) -> VMRegionProfile[VMBytecodeStep]:
    def targets(step: VMBytecodeStep) -> tuple[int, ...]:
        return tuple(
            hint.target
            for hint in step.hints
            if hint.kind == "branch-target" and hint.target is not None
        )

    return VMRegionProfile(
        frontend="test-vm",
        is_noise=lambda _step: False,
        is_control=lambda step: branching and step.opcode in {"BRANCH", "JUMP"},
        is_jump=lambda step: branching and step.opcode in {"BRANCH", "JUMP"},
        is_forward_jump=lambda step: branching
        and step.opcode in {"BRANCH", "JUMP"},
        is_backward_jump=lambda _step: False,
        is_iter_start=lambda _step: False,
        is_async_iter_start=lambda _step: False,
        is_conditional_jump=lambda step: branching and step.opcode == "BRANCH",
        is_cleanup=lambda _step: False,
        is_null_jump=lambda _step: False,
        is_not_null_jump=lambda _step: False,
        is_truthy_jump=lambda step: branching and step.opcode == "BRANCH",
        target_offset=lambda step: next(iter(targets(step)), None),
        target_offsets=targets,
        offset=lambda step: step.source.offset,
        raw_window=lambda _index: (),
    )


def _function(
    steps: tuple[VMBytecodeStep, ...], *, branching: bool = False
):
    def lift_linear(start, end, locals_, stack):
        result = run_vm_steps(
            steps[start:end], initial_locals=locals_, initial_stack=stack
        )
        return VMLinearState(
            locals=result.state.locals,
            stack=tuple(result.state.stack),
            statements=tuple(result.state.statements),
            terminator=result.state.terminator,
        )

    return lift_vm_step_function(
        VMFunctionSpec(
            name="handler_context",
            params=(),
            frontend="test-vm",
            instruction_count=len(steps),
        ),
        steps,
        profile=_profile(branching),
        stateful_callbacks=VMStatefulCallbacks(
            initial_locals=dict,
            lift_linear=lift_linear,
            branch_condition=lambda _step, _stack: Const(value=True),
            branch_stack_width=lambda _step: 0,
        ),
        raw_window=lambda index: (steps[index].opcode,),
    )


def _call(offset: int, *, handler: int | None) -> VMBytecodeStep:
    source = _source(offset)
    value: dict[str, object] = {"stack_depth": 0, "push_exception": False}
    if handler is not None:
        value["handler"] = handler
    return _step(
        offset,
        "CALL_AND_RETURN",
        effects=(
            Push(
                source=source,
                value=Call(source=source, callee=Global(name="work")),
            ),
            ReturnTop(source=source),
        ),
        hints=(VMHint(kind="exception-edge-state", source=source, value=value),),
    )


def _handler(offset: int) -> tuple[VMBytecodeStep, VMBytecodeStep]:
    return (
        _step(
            offset,
            "HANDLER_VALUE",
            effects=(Push(source=_source(offset), value=Const(value="caught")),),
        ),
        _step(
            offset + 1,
            "HANDLER_RETURN",
            effects=(ReturnTop(source=_source(offset + 1)),),
        ),
    )


def test_shared_call_uses_a_handler_scoped_state_only_in_protected_clone() -> None:
    steps = (
        _step(
            0,
            "BRANCH",
            hints=(
                VMHint(
                    kind="branch-target",
                    source=_source(0),
                    target=10,
                    flow="conditional",
                ),
            ),
        ),
        _step(
            1,
            "JUMP",
            hints=(
                VMHint(
                    kind="branch-target",
                    source=_source(1),
                    target=20,
                    flow="unconditional",
                ),
            ),
        ),
        _step(
            10,
            "PUSH_HANDLER",
            hints=(
                VMHint(kind="exception-handler", source=_source(10), target=100),
            ),
        ),
        _call(20, handler=100),
        *_handler(100),
    )

    function = _function(steps, branching=True)

    assert function.metadata["decompile_status"] == "ok"
    protected = next(
        block
        for block in function.blocks
        if block.id.startswith("block_20__handlers_100")
    )
    unprotected = next(block for block in function.blocks if block.id == "block_20")
    assert protected.exception_edge is not None
    assert unprotected.exception_edge is None


def test_handler_entry_can_replace_active_frame_with_new_protected_frame() -> None:
    steps = (
        _step(
            0,
            "PUSH_OLD",
            hints=(VMHint(kind="exception-handler", source=_source(0), target=100),),
        ),
        _step(
            1,
            "OLD_VALUE",
            effects=(Push(source=_source(1), value=Const(value="old")),),
        ),
        _step(2, "RAISE_OLD", effects=(RaiseTop(source=_source(2)),)),
        _step(
            100,
            "REPLACE_HANDLER",
            hints=(
                VMHint(
                    kind="exception-handler-pop",
                    source=_source(100),
                    value={
                        "handler": 100,
                        "frame_kind": "active",
                        "if_present": True,
                    },
                ),
                VMHint(kind="exception-handler", source=_source(100), target=200),
            ),
        ),
        _step(
            101,
            "NEW_VALUE",
            effects=(Push(source=_source(101), value=Const(value="new")),),
        ),
        _step(102, "RAISE_NEW", effects=(RaiseTop(source=_source(102)),)),
        *_handler(200),
    )

    function = _function(steps)

    assert function.metadata["decompile_status"] == "ok"
    assert "does not converge" not in function.metadata["diagnostics"]


def test_protected_call_without_a_matching_scoped_state_is_unsupported() -> None:
    steps = (
        _step(
            0,
            "PUSH_HANDLER",
            hints=(VMHint(kind="exception-handler", source=_source(0), target=100),),
        ),
        _call(1, handler=200),
        *_handler(100),
    )

    function = _function(steps)

    assert function.metadata["decompile_status"] == "unsupported"
    assert "cannot prove exceptional state" in function.metadata["unsupported_reason"]


def test_legacy_unscoped_state_without_a_handler_remains_strict() -> None:
    function = _function((_call(0, handler=None),))

    assert function.metadata["decompile_status"] == "unsupported"
    assert "has no active handler" in function.metadata["unsupported_reason"]


def test_conditional_pop_is_a_noop_but_legacy_pop_without_a_frame_is_an_error() -> None:
    conditional = _function(
        (
            _step(
                0,
                "POP_IF_PRESENT",
                hints=(
                    VMHint(
                        kind="exception-handler-pop",
                        source=_source(0),
                        value={
                            "handler": 100,
                            "frame_kind": "active",
                            "if_present": True,
                        },
                    ),
                ),
            ),
            _step(1, "VALUE", effects=(Push(source=_source(1), value=Const(value=1)),)),
            _step(2, "RETURN", effects=(ReturnTop(source=_source(2)),)),
        )
    )
    strict = _function(
        (
            _step(
                0,
                "POP",
                hints=(VMHint(kind="exception-handler-pop", source=_source(0)),),
            ),
            _step(1, "VALUE", effects=(Push(source=_source(1), value=Const(value=1)),)),
            _step(2, "RETURN", effects=(ReturnTop(source=_source(2)),)),
        )
    )

    assert conditional.metadata["decompile_status"] == "ok"
    assert strict.metadata["decompile_status"] == "unsupported"
    assert "has no active handler" in strict.metadata["unsupported_reason"]


def test_scoped_state_matches_an_active_handler_context() -> None:
    steps = (
        _step(
            0,
            "PUSH_HANDLER",
            hints=(VMHint(kind="exception-handler", source=_source(0), target=100),),
        ),
        _step(
            1,
            "VALUE",
            effects=(Push(source=_source(1), value=Const(value="error")),),
        ),
        _step(2, "RAISE", effects=(RaiseTop(source=_source(2)),)),
        _call(100, handler=100),
    )

    function = _function(steps)

    assert function.metadata["decompile_status"] == "ok"
