# Complete guide to writing a new VM Frontend

This article is an implementation manual for `unidecompiler`’s new VM/bytecode frontend. The goal is: frontend only submits VM-neutral facts, and core is responsible for stack recovery, CFG, structuring, AST, pseudocode and diagnostics.

If you are writing a plug-in for a new VM, just follow the steps in this article. Don't start with "make the GUI look like C code"; start by making sure the decoder, effects, hints, and CFG facts are complete and verifiable.

If your VM is not a stack machine, read Section 29, “Choosing your VM modeling path” first, and then come back to effects and control flow templates. Do not hard-wire stack machine templates into register VMs or three-address VMs.

If you want your frontend to support optional simulation, you must also read Section 20.1 and its simulation verification checklist. Simulation support does not turn the frontend into an interpreter; it provides data-only function lookup and runtime facts to the independent generic-IR simulator.

## 0.1 How to read this document first

If you are writing frontend for the first time, read it in this order:

- Day 1: Sessions 0, 2, 3, 4, 5, 7, 8, 11, 12, 13.
- Day 2: Sections 14, 15, 16, 17, 18, 19, 20, 20.1.
- Day 3: Sections 23, 23.1, 24, 25, 27, 28.
- Day 4: Sections 29, 30, 31, 32, 33, 34, 35, 36, 37.
- Finally: Section 39, using the complete minimal plugin as a reference.

If you only want to run the first frontend first, do the 7 steps in Section 0.3 first, then directly copy the small plug-in in Section 39.1 and replace your own opcode semantics.

If your VM is not a stack machine, read Section 29 first, and then decide which template to use in Sections 8 to 18.

## 0.2 The most important words in this document

| word | you can understand it as |
|---|---|
| frontend | The adaptation layer that is only responsible for "understanding the bytecode facts" |
| decoder | Parse the original input into a stable model |
| model | frontend's own private parsing results |
| step | A thin instruction fact that can be submitted to core |
| effect | What does this instruction do to the stack, variables, calls, and returns |
| hint | This directive tells the core's control flow/aggregation/exception facts |
| SourceRef | The original location from which this fact came |
| target | original offset to jump to |
| profile | the opcode classification table core uses to recover the CFG |
| stateful callbacks | core's interface to call back frontend under complex control flow |

The simplest mental model is:
```text
decoder answers “what it is”
effect answers “what it does”
hint answers “where it goes”
core assembles these facts back into structure
```

## 0.3 The shortest route to get started

If you want to start writing your first frontend now, just do these 7 steps:

1. First select a very small VM and only keep 3 to 5 opcodes.
2. Write `model.py` and keep only `offset`, `opcode`, `size`, `operands` and `raw`.
3. Write `decoder.py` and make `can_load()` and `decode()` stable first.
4. Write `plugin.py`, which is only responsible for registration and `FrontendModule`.
5. Write `lifter.py`, first support `CONST`, `ADD`, `RETURN`.
6. Add a `JUMP` and a conditional jump.
7. Run the verification scripts in Sections 23 to 25 to confirm that both the GUI and CFG are normal.

If you want to see the results as quickly as possible, just copy the small plug-in example in Section 39.1, and replace the opcode name and operand decoding with your VM semantics.

## 0. First clarify the boundaries of frontend

Frontend can do:

- Recognize input formats.
- Parse files, functions, instructions, constants, and debugging information.
- Generate `VMBytecodeStep` for each instruction.
- Fill in `decoded`, `raw`, `effects`, `hints` for step.
- Provide `VMRegionOpcodeClasses`.
- Provide `VMStatefulCallbacks`, allowing core to save VM stack and local state across basic blocks.
- Submit neutral facts such as branch target, loop backedge, case target, exception region, etc.

Frontend cannot do:

- Construct `If`, `While`, `Switch`, `BasicBlock`, `FunctionIR`.
- Directly call `assemble_function`, `assemble_module`.
- Register CFG structurer.
- Remediate control flow at the backend/pseudocode layer.
- Enter hard-coded recovery rules for a sample, fixture or business.
- Insert private decoder objects into core-visible operands, hints, or metadata to express program logic.
- Write VM bytecode interpreter, opcode executor or frontend-specific for simulation
  frame/stack machine.
- Implement your own function lookup, overload selection, or language runtime semantics in the GUI, CLI, or simulator.
- Let `simulation_adapter` return executable callback, frame, stack, decoder
  model or a function that does not belong to the current lifted module.

Key judgment criteria:
```text
If this information is a “bytecode fact”, the frontend may submit it.
If this information is a “source structure”, core must recover it.
```
For example:

- The fact that the `jnz` at offset 120 targets offset 80 can be submitted.
- ``offset 80..120 is a while loop`` is a source structure; the frontend must not construct it.

## 1. Overall data flow
```text
file bytes
  -> FrontendPlugin.can_load()
  -> FrontendPlugin.decode()
  -> FrontendModule(payload + metadata)
  -> frontend converts them into thin VM facts
  -> VMBytecodeStep(decoded + raw + effects + hints)
  -> lift_vm_step_function()
  -> core stack recovery / CFG / regions / SSA / AST
  -> DecompilerEngine unified result
  -> pseudocode backend / GUI / CLI
  -> [optional] generic IR simulator
  -> SimulationResult / trace
  -> CLI / GUI / embedding host
```
Frontend's private decoder payload can only be used within its own package. Core can receive `FrontendModule.metadata`, but metadata can only be provenance, diagnostics and analysis context, and cannot express control flow decisions.

## 2. Recommended directory structure

The external directory plug-in is recommended to be placed like this:
```text
my-vm-plugin/
├── unidecompiler-plugin.toml
├── README.md
├── pyproject.toml                  # optional; required when publishing as a pip package
├── my_vm_frontend/
│   ├── __init__.py
│   ├── plugin.py                   # FrontendPlugin facade
│   ├── decoder.py                  # file/bytecode parsing
│   ├── model.py                    # decoder-private model
│   ├── lifter.py                   # VMBytecodeStep/effects/hints
│   ├── simulation.py               # optional target lookup and data-only runtime adapter
│   └── support.py                  # optional version-support declaration
└── tests/
    ├── test_decoder.py
    ├── test_lifter.py
    ├── test_simulation.py          # required when simulation is supported
    └── test_integration.py
```
If using `src/` layout:
```text
my-vm-plugin/
├── unidecompiler-plugin.toml
└── src/
    └── my_vm_frontend/
        ├── __init__.py
        ├── plugin.py
        ├── decoder.py
        ├── model.py
        └── lifter.py
```
When the GUI registers an external directory, pass in the plug-in root directory:
```text
/path/to/my-vm-plugin
```

## 3. External manifest

The plugin root directory must have:
```toml
# unidecompiler-plugin.toml
[frontend]
id = "my-vm"
module = "my_vm_frontend.plugin:MyVmFrontendPlugin"
```
Rules:

- `id` must be globally unique and stable.
- `module` must be `python.module:attribute`.
- `attribute` can be a plugin instance or a zero-argument plugin class.
- The registered directory root or `src/` will be added to the Python import path.
- The GUI will not automatically execute `pip install`; third-party dependencies require users to install them in advance.

If published as Python distribution, also add entry point:
```toml
[project.entry-points."unidecompiler.frontends"]
my-vm = "my_vm_frontend.plugin:MyVmFrontendPlugin"
```
The entry point is automatically discovered by `FrontendRegistry.discover()`. External manifests are registered explicitly by the host.

## 4. FrontendPlugin facade

Minimal plugin:
```python
from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendVersionSupport

from .decoder import decode_my_vm, looks_like_my_vm
from .lifter import lift_program


class MyVmFrontendPlugin:
    id = "my-vm"
    display_name = "My VM"
    supported_inputs = (".mvm", ".mvmc")
    version_support = FrontendVersionSupport(
        family="my-vm-bytecode",
        versions=("1",),
        parser="my-vm parser 1.0",
        status="experimental",
        notes=("Initial VM frontend.",),
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_my_vm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        try:
            program = decode_my_vm(data, filename)
        except ValueError as error:
            raise FrontendDecodeError(str(error)) from error
        return FrontendModule(
            frontend_id=self.id,
            payload=program,
            metadata={
                "filename": filename,
                "format": "my-vm",
                "version": program.version,
                "endianness": program.endianness,
                "debug_info_present": bool(program.debug_lines),
                "my-vm": {
                    "function_count": len(program.functions),
                    "instruction_count": sum(len(f.instructions) for f in program.functions),
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(f"wrong frontend module: {module.frontend_id!r}")
        return lift_program(module.payload, module.metadata)
```
`can_load()` should be fast, have no side effects, and cannot execute external programs. When multiple frontends return true at the same time, the engine will report ambiguous instead of guessing.

`decode()` only does format parsing. It can return the frontend private model, but cannot construct the core IR.

`lift()` only converts the private model into thin VM facts and then calls the core helper.

## 5. Decoder model

The Decoder's responsibility is to turn the input into a stable frontend private model.

Recommended model:
```python
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MyInstruction:
    offset: int
    opcode: str
    size: int
    operands: tuple[Any, ...] = ()
    raw: str = ""
    line: int | None = None


@dataclass(frozen=True)
class MyFunction:
    name: str
    offset: int
    instructions: tuple[MyInstruction, ...]
    params: tuple[str, ...] = ()
    local_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class MyProgram:
    filename: str | None
    version: str
    endianness: str | None
    functions: tuple[MyFunction, ...]
    debug_lines: dict[int, int]
    diagnostics: tuple[str, ...] = ()
    word_size: int | None = None
```

### Flat VM program

Some VMs do not have function tables, and the entire file is just a flat instruction stream.

At this time, frontend should wrap the entire program into an entry function:
```python
main = MyFunction(
    name="main",
    offset=instructions[0].offset,
    instructions=tuple(instructions),
    params=(),
    local_names=(),
)
```
Don't skip `VMFunctionSpec` just because there is no function table. Core's recovery entry is still a function.

### Text VM with whitespace characters

Parsing of text VM must be consistent with the interpreter.

The decoder can skip whitespace if the interpreter allows whitespace separation. If the interpreter takes character-by-character instructions and treats unknown characters as an error, the decoder should also treat whitespace as an error.

Don't ignore characters loosely just to make the sample "easier to parse". Frontend's decoder is the format source of truth and should try to match the real VM.

Decoder must be retained:

- Original bytecode offset.
- opcode name.
- operand original value.
- raw disassembled text.
- Function boundaries.
- Available locals, parameter names, constant names, and debug lines.
- malformed input in wrong position.

Error handling:

- The input is clearly not in this format: `can_load()` returns `False`.
- Input formatted like this but corrupted: `decode()` throws `FrontendDecodeError`.
- Unknown opcode: When the boundary can be determined, the instruction is still generated, and `UnknownOpcode` is used during lift; when the boundary cannot be determined, a decode error is thrown.

## 6. SourceRef and offset units

Every locatable fact should have a `SourceRef`:
```python
from unidecompiler.core.ir import SourceRef

source = SourceRef(
    frontend="my-vm",
    offset=instruction.offset,
    line=instruction.line,
    detail=f"function={function.name}",
)
```
Field rules:

- `frontend`: must be equal to plugin id.
- `offset`: the original VM instruction position, not the pseudocode line number, nor the instruction index.
- `line`: VM debug/source line, if not, `None`.
- `detail`: only put provenance, such as function name, section name, method signature.

The unit of `offset` is determined by the VM format, but must use the same coordinate system as the branch target.

Common choices:

- Binary bytecode: Use in-file byte offset.
- Function bytecode in the container: Use the byte offset in the function code area, or the global byte offset, but it must be consistent throughout.
- Text VM: Use the character positions actually used by the interpreter.
- Unicode text VM: If the interpreter fetches by Unicode codepoint, use codepoint index; do not use UTF-8 byte offset.

If an instruction occupies multiple input units, `SourceRef.offset` should point to the location of the opcode, not the location of the operand. The branch target must also fall on the opcode offset.

For example a Unicode text VM:
```text
OP ARG
```
If `OP ARG` occupies two Unicode codepoints, the instruction offset is the codepoint index of `OP`, and `ARG` is operand, which is not a legal jump target.

When validating target you should check:
```python
valid_offsets = {instruction.offset for instruction in instructions}
assert target in valid_offsets
```
Don't put control flow decisions into `SourceRef.detail` or metadata. Control flow facts must use `VMHint`.

Recommended module metadata:
```python
metadata={
    "filename": filename,
    "format": FRONTEND_ID,
    "version": program.version,
    "endianness": program.endianness,
    "debug_info_present": bool(program.debug_lines),
    "diagnostics": tuple(program.diagnostics),
    FRONTEND_ID: {
        "word_size": program.word_size,
        "function_count": len(program.functions),
    },
}
```

## 7. VMOperand and VMDecodedInstruction

`VMDecodedInstruction` is a GUI/CLI displayable neutral disassembly line with no execution semantics.
```python
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand

decoded = VMDecodedInstruction(
    opcode=instruction.opcode,
    source=source,
    operands=(
        VMOperand(role="constant", value=3, text="const[3]"),
        VMOperand(role="target", value=120, text="0x78"),
    ),
    raw=instruction.raw,
)
```
Commonly used `VMOperand.role`:

| role | purpose |
|---|---|
| `constant` | Constant index or resolved constant |
| `local` | Local variable slot or name |
| `global` | Global variable identifier |
| `register` | VM register |
| `target` | branch/switch target offset |
| `attribute` | attribute name |
| `member` | member/field name |
| `immediate` | Number, mode, width and other immediate numbers |
| `raw` | An operand that cannot be classified but still needs to be displayed |

Rules:

- `value` uses stable, serializable neutral values.
- `text` is used for display.
- Don't put decoder private objects into operand.
- branch target uses `role="target"`.
- Opcode passes empty tuple when there is no operand.

## 8. Effect: Describe stack and value behavior

Effect is the minimum VM semantics that core can execute. Frontend selects the effect and does not directly operate `StackMachineState`.

Common categories:

| Category | Effect Example | Usage |
|---|---|---|
| Value/Stack | `Push`, `Pop`, `Copy`, `DuplicateTop`, `Swap`, `Unpack` | Stack shape and constant values |
| Local variables | `LoadLocal`, `StoreLocal`, `AssignValue`, `UpdateLocal`, `StoreMany` | locals |
| Operations | `Unary`, `Binary`, `Compare`, `Truthy`, `SelectValue` | Expressions and conditions |
| Properties/index | `LoadAttr`, `StoreAttr`, `LoadItem`, `StoreItemEffect`, `LoadIndirect` | member/index |
| Container | `BuildArray`, `BuildSet`, `BuildMap`, `BuildString` | aggregate |
| Call | `Invoke`, `BuildCall`, `CallStackArgs` | Call |
| Termination | `ReturnTop`, `ReturnVoid`, `RaiseTop`, `YieldTop` | terminator |
| Fallback | `UnknownOpcode` | Diagnosable unsupported |

Effect table example:
```python
from unidecompiler.core.effects import Binary, LoadLocal, Push, ReturnTop, StoreLocal, UnknownOpcode
from unidecompiler.core.ir import Const
from unidecompiler.core.vm_effect_table import VMEffectTable


MY_EFFECT_TABLE = VMEffectTable(
    opcode_attr="opcode",
    exact={
        "CONST": lambda ctx, ins, src: (
            Push(source=src, value=Const(source=src, value=ctx.constants[ins.operands[0]])),
        ),
        "LOAD_LOCAL": lambda ctx, ins, src: (
            LoadLocal(source=src, name=ctx.local_names[ins.operands[0]]),
        ),
        "STORE_LOCAL": lambda ctx, ins, src: (
            StoreLocal(source=src, name=ctx.local_names[ins.operands[0]]),
        ),
        "ADD": lambda ctx, ins, src: (
            Binary(source=src, op="+", semantics="static"),
        ),
        "RETURN": lambda ctx, ins, src: (
            ReturnTop(source=src),
        ),
    },
    fallback=lambda ctx, ins, src: (
        UnknownOpcode(source=src, opcode=ins.opcode, raw=ins.raw),
    ),
)
```
Unknown opcode Do not return empty tuple. Empty tuples are only suitable for clear and unsemantic noise opcodes, such as `nop`, padding, and line markers.

### Opcode mapping table template

When writing a frontend for a new VM, fill out a form first and then write the code.

| VM opcode | operands | stack input | stack output | effect | hints | test |
|---|---|---|---|---|---|---|
| `CONST` | const id | None | value | `Push(Const(...))` | None | The constant value is correct |
| `LOAD_LOCAL` | slot | None | local | `LoadLocal` | None | local name is correct |
| `STORE_LOCAL` | slot | value | None | `StoreLocal` | None | The assignment target is correct |
| `ADD` | None | left, right | result | `Binary("+")` | None | Operand order |
| `CALL` | argc | args | return(s) | `Invoke`/`CallStackArgs` | `call-shape` optional | number of parameters |
| `JUMP` | target or stack value | target optional | None | `Pop` if target is on stack | `branch-target`/`loop-backedge` | target exists |
| `JUMP_IF_*` | target or stack value | condition/target | None | usually not pop in advance | `branch-target`, `materialized-condition` | if/goto |
| `RETURN` | None | value optional | Function end | `ReturnTop`/`ReturnVoid` | None | terminator |
| `NOP` | None | None | None | `()` | Optional `noise` | Produce no statement |

This table should be driven by the VM specification or interpreter implementation, not by the decompilation results of a certain sample.

Answer at least: for each line:

- Does opcode change stack depth?
- Is the order of operands consistent with core default?
- Is it possible to end a function?
- Whether to generate a control flow target?
- Is the target immediate, table entry, or stack value?
- Has the condition been materialized on the stack?
- Are runtime call names needed to express side effects?

## 9. Stack operand order

The most common error in the stack machine frontend is the binary operation sequence.

Core’s stack convention:
```text
stack[-1] is the top of the stack
stack[0] is the bottom of the currently visible stack slice
```
Assume the runtime semantics are:
```text
right = pop()
left = pop()
push(left OP right)
```
The effect can usually be used directly:
```python
Binary(source=source, op="+")
```
At this time:
```text
const 5; const 2; sub
```
It should be restored to:
```text
5 - 2
```
If the VM semantics are "top of stack as left operand":
```text
left = pop()
right = pop()
push(left OP right)
```
The pair of non-swap operations must be swapped first:
```python
from unidecompiler.core.effects import Binary, Swap

if opcode in {"SUB", "MOD", "LT"}:
    return (
        Swap(source=source, depth=2),
        Binary(source=source, op=op, semantics="static"),
    )
```
Small examples must be written for these opcodes:
```text
const 5; const 2; sub; print
const 5; const 2; mod; print
const 2; const 5; lt; print
```
If this VM is "top of stack as left operand", the verification pseudocode should be similar to:
```text
print(2 - 5)
print(2 % 5)
print(5 < 2)
```
If you see `5 - 2`, `5 % 2`, `2 < 5`, it means you are modeling according to the default `left=below, right=top`. Both semantics are possible, and the target VM interpreter or format documentation must prevail.

## 10. Call, memory and VM runtime API

If the VM opcode calls the runtime API, `CallStackArgs` can be used:
```python
from unidecompiler.core.effects import CallStackArgs

if opcode == "READ":
    return (CallStackArgs(source=source, callee_name="read_buffer", arg_count=1, returns=0),)

if opcode == "LOAD_BYTE":
    return (CallStackArgs(source=source, callee_name="load_byte", arg_count=2),)

if opcode == "STORE_BYTE":
    return (CallStackArgs(source=source, callee_name="store_byte", arg_count=3, returns=0),)
```
Note that the parameter order must be consistent with the VM runtime. For example, if the order on the bytecode stack is:
```text
const index
const offset
STORE
```
And you want to output:
```c
store_byte(index, offset, value)
```
You must use `Swap` or adjust the effect to ensure that the parameters seen by the core are in the correct order.

Runtime call names should be stable, readable, and VM-neutral. Do not write the business semantics of a sample as a name like `validate_domain_rule()` unless the opcode of the VM itself has this meaning.

### Fixed parameter API

If the opcode consumes a fixed number of stack arguments, use `CallStackArgs` first.

For example:
```text
READ    consumes buffer_index
WRITE   consumes buffer_index
LOAD    consumes index, offset and returns a byte
STORE   consumes index, offset, value and returns nothing
```
The correspondence can be expressed as:
```python
CallStackArgs(source=source, callee_name="read_buffer", arg_count=1, returns=0)
CallStackArgs(source=source, callee_name="write_buffer", arg_count=1, returns=0)
CallStackArgs(source=source, callee_name="load_byte", arg_count=2, returns=1)
CallStackArgs(source=source, callee_name="store_byte", arg_count=3, returns=0)
```
If the parameter order does not match, first use the existing stack effect to adjust, such as `Swap`. If the existing effect cannot express the rearrangement losslessly, you should consider adding a new VM-neutral effect, or conservatively downgrade it to partial/unsupported.

Don't cover up ordering errors by changing function names.

### Dynamic parameter API

The consumption quantity of some opcodes is not a fixed value. For example `PRINT_UNTIL_ZERO` may keep popping until 0 sentinel is encountered.

If core does not yet have a corresponding VM-neutral effect, the recommended order is:

1. If the opcode does not affect subsequent stack merge, it can be expressed with a conservative runtime call name, such as `puts_until_zero(...)`.
2. If it will affect the subsequent stack state, return `effects=None` or `UnknownOpcode` and let core give partial/unsupported.
3. If this is a mode that multiple VMs will encounter, the VM-neutral effect should be added and supported by core.

Don't manually pop the stack to sentinel in the frontend and spell the string. That's a special case interpretation of runtime data, not a thin IR fact.

### Implicit return and implicit status

Some opcodes modify the VM internal state but do not push the results back onto the stack.

For example allocator:
```text
ALLOC_BUFFER size
```
If the VM puts the new buffer into the first empty slot when running, but the opcode does not return the slot, then it can be expressed as:
```python
CallStackArgs(source=source, callee_name="alloc", arg_count=1, returns=0)
```
This preserves the side effect call but doesn't pretend to know the return value.

If subsequent analysis must know "which slot is allocated", this is beyond the expressive capabilities of ordinary runtime calls. Consider adding VM-neutral memory/allocation fact, or accepting partial/low-level output.

## 11. General principles of control flow

Control flow recovery relies on three types of information:

1. Opcode classification: Is this a jump, conditional jump, return, noise or normal instruction.
2. Target hint: What is the offset of the jump target.
3. Conditional stack status: whether core can get the conditional expression when generating `Branch`.

Frontend does not construct a CFG, but must submit all three types of facts.

Minimum qualifying results:
```text
simple control flow -> core emits if/while/switch
complex control flow -> core emits a low-level if/goto CFG
cannot recover safely -> explicit partial/unsupported + raw context
```
At worst, complex control flows should not be linearized silently. Linearization can mislead analysis.

## 12. VMHint: branch, loop, switch, exception

`VMHint` only submits facts:
```python
from unidecompiler.core.vm_hints import VMHint

VMHint(kind="branch-target", source=source, target=target, flow="conditional")
VMHint(kind="loop-backedge", source=source, target=header_offset, flow="conditional")
VMHint(kind="case-target", source=source, target=case_offset, value=case_value)
VMHint(kind="default-target", source=source, target=default_offset)
VMHint(kind="exception-region", source=source, value={"start": start, "end": end, "target": handler})
```
Legal kind:
```text
block-boundary
branch-target
case-target
default-target
fallthrough
loop-backedge
exception-region
exception-handler
branch-value
materialized-condition
call-shape
aggregate-shape
```

### branch-target and loop-backedge

General rules:
```python
kind = "loop-backedge" if target <= instruction.offset else "branch-target"
```
Use `loop-backedge` for the backward edge and `branch-target` for the forward edge. This does not construct a while, it just tells the core that this edge is backward control flow.

### Conditional polarity

`detail` can indicate whether target is a true edge or a false edge:
```python
VMHint(
    kind="branch-target",
    source=source,
    target=target,
    flow="conditional",
    detail="target-if-true",
)
```
If `branch_condition()` returns an expression that is true when the jump target is taken, you must use:
```text
detail="target-if-true"
```
If `branch_condition()` returns an expression that is true on fallthrough, you can use the default polarity, or write explicitly:
```text
detail="target-if-false"
```
If the polarity is wrong, the `if` branch in the pseudocode will be reversed, and the failure path and success path may be read reversely.

### materialized-condition

If the VM condition is calculated on the stack first and then consumed by `jz/jnz`, for example:
```text
LOAD x
CONST 0
EQ
CONST target
JUMP_IF_TRUE
```
Should be given `JUMP_IF_TRUE` while submitting:
```python
VMHint(kind="materialized-condition", source=source, detail="stack", flow="conditional")
```
The meaning of this hint is: the condition has been materialized as a stack value, and the core should resume control flow according to the stack condition.

Without this hint, core may treat conditional calculations as ordinary linear expressions, and complex functions may not see `if/goto` in the end.

## 13. Correct way to write conditional jump

This is the most important part of the stack machine frontend.

First confirm where the target comes from. Immediate target and stack target are written differently.
```text
# immediate target
... condition
JUMP_IF_TRUE target

# stack target
... condition target
JUMP_IF_TRUE
```
If target is immediate, branch usually only consumes condition from the stack. If the target is on the stack, the branch usually consumes both condition and target.

`branch_condition(branch, stack)` receives the stack fragment that will be consumed by branch, not the complete VM stack.

Agreement:
```text
stack[-1] is the top of this slice
stack[0] is the earliest value pushed in this slice
```
If the runtime layout is:
```text
... condition target
JUMP_IF_TRUE
```
and `branch_stack_width()` returns `2`, then callback receives:
```text
stack == (condition, target)
stack[0] == condition
stack[-1] == target
```
If the runtime layout is:
```text
... target condition
JUMP_IF_TRUE
```
Then callback receives:
```text
stack == (target, condition)
stack[0] == target
stack[-1] == condition
```
Therefore documentation examples cannot be mechanically reproduced. You must first write down the branch stack layout of the target VM, and then decide whether to take the condition from `stack[0]` or `stack[-1]`.

### immediate target template

If the runtime semantics are:
```text
condition = pop()
if condition != 0:
    pc = instruction.target
```
Control effects usually don't require stack popping. Core will consume condition on the edge of the control flow according to `branch_stack_width`.
```python
from unidecompiler.core.ir import BinaryOp, Const


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def effects_for_control(instruction, source):
    if instruction.opcode in CONTROL:
        return ()


def branch_stack_width(instruction):
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        return 1
    return 0
```

`branch_condition()`：

```python
def branch_condition(branch, stack):
    if len(stack) < 1:
        return None
    condition = stack[-1]
    if branch.opcode == "JUMP_IF_TRUE":
        return BinaryOp(source=condition.source, op="!=", left=condition, right=Const(value=0, source=condition.source))
    if branch.opcode == "JUMP_IF_FALSE":
        return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))
    return None
```

### stack target template

If the runtime semantics are:
```text
target = pop()
condition = pop()
if condition != 0:
    pc = target
```
Unconditional jump can pop the target on the top of the stack during the effect phase; conditional jump should not pop the condition/target in advance during the effect phase.

The responsibilities here should be distinguished:

- `Pop` is a semantic modeling of VM stack in linear lift stage.
- `branch_stack_width` is how many control values need to be fetched/removed from the current stack state on the branch edge for stateful control recovery.
- Immediate target jump does not consume the target on the stack, so both of them usually do not consume additional stack values for the target.
- The target of stack target jump is originally on the stack, so it must be clear which layer it is consumed from. It cannot be consumed in a normal effect and consumed repeatedly in the same path.
```python
from unidecompiler.core.effects import Pop
from unidecompiler.core.ir import BinaryOp, Const


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def effects_for_control(instruction, source):
    if instruction.opcode == "JUMP":
        # Only applies to an unconditional jump with target at the stack top.
        return (Pop(source=source),)
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        # Do not pop the condition/target during the effect phase.
        # branch_stack_width tells core how many stack values the branch consumes.
        return ()


def branch_stack_width(instruction):
    if instruction.opcode == "JUMP":
        return 1
    if instruction.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
        return 2
    return 0
```

`branch_condition()`：

```python
def branch_condition(branch, stack):
    if len(stack) < 2:
        return None
    # This template assumes the runtime layout is: ... condition target
    # Therefore, with branch_stack_width=2, stack == (condition, target).
    condition = stack[0]
    if branch.opcode == "JUMP_IF_TRUE":
        return BinaryOp(source=condition.source, op="!=", left=condition, right=Const(value=0, source=condition.source))
    if branch.opcode == "JUMP_IF_FALSE":
        return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))
    return None
```
Note that what is returned here is target-taken condition. Even if the opcode is named `JUMP_IF_FALSE`, as long as `condition == 0` is returned, it means "jump to target if expression is true".

Connect callback to core:
```python

stateful_callbacks=VMStatefulCallbacks(
    initial_locals=lambda: {},
    lift_linear=lift_linear,
    branch_condition=branch_condition,
    branch_stack_width=branch_stack_width,
)
```
And submit on step:
```python
hints = (
    VMHint(
        kind="loop-backedge" if target <= instruction.offset else "branch-target",
        source=source,
        target=target,
        label=instruction.opcode,
        flow="conditional",
        detail="target-if-true",
    ),
    VMHint(
        kind="materialized-condition",
        source=source,
        label=instruction.opcode,
        detail="stack",
        flow="conditional",
    ),
)
```
This mode will enable core to generate at least low-level CFG:
```c
if (condition) goto block_target else goto block_fallthrough
```
If the function structure is simple, core may further revert to `if` or `while`. If the structure is complex, conservative `if/goto` is the correct output.

### Conditional jump checklist

Each conditional jump opcode needs to answer:

- Is target immediate or stack value?
- Is condition an immediate mode/marker, or a stack value?
- How many stack values does the runtime consume?
- Is `branch_stack_width` equal to the number of core values that need to be removed?
- Is `branch_condition()` using the correct stack location?
- Does `branch_condition()` return the target establishment condition or the fallthrough establishment condition?
- Is `detail="target-if-true"` or `detail="target-if-false"` required?
- Is the condition already materialized on the stack? Is `materialized-condition` required?
- Is `loop-backedge` used for backward target?

If any of these items are unclear, first write a minimum control flow sample to verify, rather than going directly to a real large sample.

## 14. Calculate static jump target

Some VM jump targets are immediate operands, and some are constants calculated on the stack or in registers. Different resolvers are used for different sources.

Immediate absolute target:
```python
CONTROL_WITH_IMMEDIATE_TARGET = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def immediate_absolute_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    valid_offsets = {ins.offset for ins in function.instructions}
    for ins in function.instructions:
        if ins.opcode not in CONTROL_WITH_IMMEDIATE_TARGET:
            continue
        target = int(ins.operands[0])
        if target in valid_offsets:
            targets[ins.offset] = target
    return targets
```

Immediate relative target：

```python
CONTROL_WITH_RELATIVE_TARGET = frozenset({"JUMP_REL", "JUMP_IF_TRUE_REL", "JUMP_IF_FALSE_REL"})


def immediate_relative_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    valid_offsets = {ins.offset for ins in function.instructions}
    for ins in function.instructions:
        if ins.opcode not in CONTROL_WITH_RELATIVE_TARGET:
            continue
        delta = int(ins.operands[0])
        target = ins.offset + ins.size + delta
        if target in valid_offsets:
            targets[ins.offset] = target
    return targets
```
Stack target requires constant propagation.

For example:
```text
CONST 7
CONST 10
MUL
CONST 5
ADD
JUMP
```
The target is `75`. Frontend can propagate local constants and restore target hints.

Conservative resolver example for stack-target VM:
```python
def const_binary(opcode: str, left: int | None, right: int | None) -> int | None:
    if left is None or right is None:
        return None
    if opcode == "ADD":
        return left + right
    if opcode == "SUB":
        return left - right
    if opcode == "MUL":
        return left * right
    if opcode == "MOD":
        return left % right
    if opcode == "XOR":
        return left ^ right
    if opcode == "AND":
        return left & right
    if opcode == "EQ":
        return int(left == right)
    if opcode == "LT":
        return int(left < right)
    return None


def pop_many(stack: list[int | None], count: int) -> None:
    for _ in range(count):
        if stack:
            stack.pop()


def apply_conservative_stack_effect(stack: list[int | None], ins) -> None:
    if ins.opcode == "NOP":
        return
    if ins.opcode == "DROP":
        pop_many(stack, 1)
        return
    if ins.opcode == "DUP":
        stack.append(stack[-1] if stack else None)
        return
    stack.clear()


def static_branch_targets(function) -> dict[int, int]:
    targets: dict[int, int] = {}
    stack: list[int | None] = []
    valid_offsets = {ins.offset for ins in function.instructions}

    for ins in function.instructions:
        if ins.opcode == "CONST":
            stack.append(int(ins.operands[0]))
        elif ins.opcode in {"ADD", "SUB", "MUL", "MOD", "XOR", "AND", "EQ", "LT"}:
            right = stack.pop() if stack else None
            left = stack.pop() if stack else None
            stack.append(const_binary(ins.opcode, left, right))
        elif ins.opcode == "JUMP":
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[ins.offset] = target
            pop_many(stack, 1)
        elif ins.opcode in {"JUMP_IF_TRUE", "JUMP_IF_FALSE"}:
            target = stack[-1] if stack else None
            if target in valid_offsets:
                targets[ins.offset] = target
            pop_many(stack, 2)
        else:
            apply_conservative_stack_effect(stack, ins)

    return targets
```
Rules:

- Only submit targets that can be determined and fall at the real instruction offset.
- Don't guess if you can't figure it out.
- Invalid targets should be exposed as diagnostics or unsupported context.
- target is the original bytecode offset, not the instruction index.

## 15. Opcode classification and region profile

When control flow restoration is required, submit `VMRegionOpcodeClasses`:
```python
from unidecompiler.core.vm_region import VMRegionOpcodeClasses, build_hint_region_profile

classes = VMRegionOpcodeClasses(
    noise=frozenset({"NOP"}),
    control=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    forward_jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    backward_jumps=frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
    conditional_jumps=frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"}),
)

profile = build_hint_region_profile(
    steps,
    frontend="my-vm",
    opcode_classes=classes,
    raw_window=lambda index: raw_window(instructions, index),
)
```
If the same opcode may jump forward or backward, it can be listed in both `forward_jumps` and `backward_jumps`. The specific direction is determined by hint target and source offset.

`noise` only contains truly semantic-free instructions. Don't put opcodes that failed to parse into noise.

## 16. lift_linear and stateful callbacks

Complex control flows require `stateful_callbacks`.

template:
```python
from unidecompiler.core.vm_function import lift_steps
from unidecompiler.core.vm_region import VMLinearState, VMStatefulCallbacks


def lift_linear(program, function, start, end, locals_, stack):
    steps = tuple(make_step(program, ins) for ins in function.instructions[start:end])
    result = lift_steps(
        steps,
        initial_locals=locals_,
        initial_stack=stack,
    )
    stopped_at = None
    if result.stopped_at is not None:
        stopped_at = start + steps.index(result.stopped_at)
    return VMLinearState(
        locals=result.state.locals,
        stack=tuple(result.state.stack),
        statements=tuple(result.state.statements),
        terminator=result.state.terminator,
        stopped_at=stopped_at,
    )


def make_stateful_callbacks(program, function):
    return VMStatefulCallbacks(
        initial_locals=lambda: {},
        lift_linear=lambda start, end, locals_, stack: lift_linear(program, function, start, end, locals_, stack),
        branch_condition=branch_condition,
        branch_stack_width=branch_stack_width,
    )
```
`lift_linear` only interprets the linear instruction slice of `[start, end)`. It cannot structure control flow.

Note:

- `start/end` is the index of the current function instruction tuple, not the byte offset.
- `locals_` and `stack` are the current status passed in by core.
- Pass them to `lift_steps()`.
- `result.stopped_at` is the step object in the slice; when returning `VMLinearState.stopped_at` it must be converted to the global instruction index.
- Don't merge basic blocks yourself.
- Do not write special logic based on specific offsets.

## 17. make_step complete template

The following template takes the immediate target VM as an example. In other words: the jump target is in instruction operand, not on the operand stack.

If your VM is a stack target, still keep the original decoded operands; only use the target recovered by constant propagation to `VMHint.target`, do not use it to replace the original operands.
```python
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.ir import SourceRef


CONTROL = frozenset({"JUMP", "JUMP_IF_TRUE", "JUMP_IF_FALSE"})
CONDITIONAL = frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"})
STACK_MATERIALIZED_CONDITION = frozenset({"JUMP_IF_TRUE", "JUMP_IF_FALSE"})


def operand_for(instruction, index: int, value: object) -> VMOperand:
    # A real frontend should classify operands individually according to opcode semantics.
    # Do not mark every operand as a target merely because the opcode is control-flow.
    if instruction.opcode in CONTROL and index == 0:
        return VMOperand(role="target", value=int(value), text=f"{int(value):#x}")
    if instruction.opcode == "LOAD_LOCAL":
        return VMOperand(role="local", value=int(value), text=f"local[{int(value)}]")
    if instruction.opcode == "CONST":
        return VMOperand(role="constant", value=int(value), text=f"const[{int(value)}]")
    return VMOperand(role="immediate", value=value, text=str(value))


def instruction_operands(instruction) -> tuple[VMOperand, ...]:
    return tuple(
        operand_for(instruction, index, value)
        for index, value in enumerate(instruction.operands)
    )


def control_target_for_hint(instruction, targets: dict[int, int] | None) -> int | None:
    if targets is None:
        return None
    return targets.get(instruction.offset)


def control_hints(instruction, source, targets: dict[int, int] | None) -> tuple[VMHint, ...]:
    if instruction.opcode not in CONTROL:
        return ()

    target = control_target_for_hint(instruction, targets)
    if target is None:
        return ()

    edge_hint = VMHint(
        kind="loop-backedge" if target <= instruction.offset else "branch-target",
        source=source,
        target=target,
        label=instruction.opcode,
        flow="unconditional" if instruction.opcode == "JUMP" else "conditional",
        detail="target-if-true" if instruction.opcode in CONDITIONAL else None,
    )

    if instruction.opcode in STACK_MATERIALIZED_CONDITION:
        return (
            edge_hint,
            VMHint(
                kind="materialized-condition",
                source=source,
                label=instruction.opcode,
                detail="stack",
                flow="conditional",
            ),
        )

    return (edge_hint,)


def make_step(program, instruction, targets: dict[int, int] | None = None) -> VMBytecodeStep:
    source = SourceRef(frontend=FRONTEND_ID, offset=instruction.offset, line=instruction.line)

    decoded = VMDecodedInstruction(
        opcode=instruction.opcode,
        source=source,
        operands=instruction_operands(instruction),
        raw=instruction.raw,
    )

    return VMBytecodeStep(
        opcode=instruction.opcode,
        source=source,
        decoded=decoded,
        raw=decoded.raw,
        effects=MY_EFFECT_TABLE.effects_for(program, instruction, source),
        hints=control_hints(instruction, source, targets),
    )
```
If the condition comes from a register or immediate, rather than an expression whose preceding opcode has been pushed onto the stack, remove that opcode from `STACK_MATERIALIZED_CONDITION`.

The `branch` received by `branch_condition()` is `VMBytecodeStep`. If you need to read the decoded operand, you should pass `branch.decoded.operands`, or look up the original instruction in the frontend private instruction to step mapping. Don't assume that `VMBytecodeStep` comes with the `operands` field.

## 18. lift_function complete template

The immediate target template from Section 17 continues below. If target requires constant propagation, replace `immediate_absolute_targets(function)` with your own resolver, but the type of `targets` should still be `{branch_instruction_offset: target_offset}`.
```python
from unidecompiler.core.vm_function import VMFunctionSpec, lift_vm_step_function, recover_vm_function


def lift_function(function, program):
    targets = immediate_absolute_targets(function)
    steps = tuple(make_step(program, ins, targets) for ins in function.instructions)
    profile = build_hint_region_profile(
        steps,
        frontend=FRONTEND_ID,
        opcode_classes=REGION_CLASSES,
        raw_window=lambda index: raw_window(function.instructions, index),
    )
    spec = VMFunctionSpec(
        name=function.name,
        params=function.params,
        frontend=FRONTEND_ID,
        instruction_count=len(steps),
        local_names=function.local_names,
        metadata={"function_offset": function.offset},
    )

    return recover_vm_function(
        spec,
        lambda: lift_vm_step_function(
            spec,
            steps,
            profile=profile,
            stateful_callbacks=make_stateful_callbacks(program, function),
            raw_window=lambda index: raw_window(function.instructions, index),
        ),
        raw=tuple(ins.raw for ins in function.instructions),
    )
```
`recover_vm_function()` will convert unexpected exceptions into diagnosable unsupported. During development, the unsupported value of the supported path should still be revised to zero.

## 19. Module assembly

Module assembly uses `assemble_vm_module()`:
```python
from unidecompiler.core.vm_module import assemble_vm_module


def lift_program(program, metadata):
    return assemble_vm_module(
        name=program.filename or f"<{FRONTEND_ID}-program>",
        source_language=FRONTEND_ID,
        metadata={"frontend": metadata, "bytecode_format": FRONTEND_ID},
        functions=tuple(lift_function(function, program) for function in program.functions),
    )
```
Do not call `assemble_module()`, `assemble_function()` or construct `FunctionIR` directly from the frontend.

## 20. GUI display and bytecode_instructions

`lift_vm_step_function()` will project the step into the `bytecode_instructions` of the function metadata, and the GUI uses these lines to display the disassembly and control edges.

Each display line should:

- `offset`
- `opcode`
- `operands`
- `raw`
- `source`
- `control`

If the GUI control flow view crashes, first check:

- Whether any target does not exist.
- Whether to treat instruction index as byte offset.
- Whether there is a self-loop display edge between source/target and basic block.
- Whether multiple conflicting targets are submitted for the same conditional jump.

If the core's internal CFG is correct, but there is a self-loop of the same block in the GUI display metadata, you can filter and display only the control hint in the metadata. Don't change the core CFG, and don't lose hints required for real recovery.

## 20.1 Optional simulation support

Simulating execution is not a required responsibility of the frontend. Even if a frontend can only decompile, it cannot
Simulation is also legal. Only if the VM's function boundaries, calling conventions, and necessary runtime facts
Support for mocking should only be declared when it is sufficiently explicit.

The dependency direction of the simulator and frontend must remain strictly decoupled:
```text
frontend -> core generic IR <- unidecompiler-simulator <- CLI / GUI / host
```
This means:

- core cannot import simulator, nor can it know the type or life cycle of simulator.
- The simulator can only execute `ModuleIR`, `FunctionIR` and other public generic IR produced by core.
- The simulator does not execute frontend bytecode, does not read the decoder private model, and does not interpret VM opcode.
  Does not execute `Effect` or `VMBytecodeStep` directly.
- The simulator has its own frames, calls, control flow, exceptions, step limits, cancellation and trace.
- The frontend must not add a language interpreter, opcode switch, or VM stack to support simulation.
  executor or dedicated control flow restorer.
- CLI, GUI and other hosts only call the simulator public API and do not copy the frontend functions
  Find or execute logic.

### 20.1.1 When should simulation be supported?

It is recommended to support when the following conditions are met at the same time:

1. The decoder can stably identify function boundaries, or can package flat programs into stable entry functions.
2. `lift()` can generate a semantically correct generic IR for the target function.
3. The parameter sources, return values, calling conventions and local variable scopes are clear enough.
4. The target function query can be represented by stable and serializable data.
5. Language-specific member access, closures, indirect calls, or container behavior can be accessed through a narrow runtime
   Fact expressions without the need for a frontend to execute instructions.
6. Can write repeatable tests for completions, exceptions, unsupported operations, and external calls.

If these conditions are not met, do not declare support for impersonation just to have a Run button appear in the GUI.
Preserve decompilation capabilities and have the simulator explicitly report that this frontend does not support simulation.

### 20.1.2 Responsibilities of simulation adapter

The frontend can provide an adapter via the optional `simulation_adapter` attribute of the plugin:
```python
class MyVmFrontendPlugin:
    id = "my-vm"
    display_name = "My VM"
    supported_inputs = (".mvm",)
    simulation_adapter = MyVmSimulationAdapter
```
The minimum adapter shape is as follows:
```python
from unidecompiler_simulator import (
    NotHandled,
    ResolvedFunction,
    SimulationTargetCandidate,
)


class MyVmSimulationAdapter:
    frontend_id = "my-vm"

    def resolve_function(self, query, decoded_module, lifted_module):
        if not isinstance(query, str):
            return NotHandled

        matches = tuple(
            function
            for function in self._walk(lifted_module.functions)
            if function.name == query
        )
        if len(matches) != 1:
            # Do not guess when there are zero or multiple matches.
            return NotHandled
        return ResolvedFunction(matches[0], identifier=query)

    def list_simulation_targets(self, decoded_module, lifted_module):
        functions = tuple(self._walk(lifted_module.functions))
        counts = {}
        for function in functions:
            counts[function.name] = counts.get(function.name, 0) + 1
        return tuple(
            SimulationTargetCandidate(function.name, function.name)
            for function in functions
            if counts[function.name] == 1
        )

    @staticmethod
    def _walk(functions):
        for function in functions:
            yield function
            yield from MyVmSimulationAdapter._walk(function.nested_functions)
```
The return value of `resolve_function()` must be in the current lifted module
`FunctionIR`. A function cannot be re-created, the decoder private function object cannot be returned, and the
Return Python callable. The simulator will verify the function ownership again.

The query of `list_simulation_targets()` is frontend-owned opaque data. GUI and
The CLI can save, display, and pass it, but it cannot parse it. query must be data that can be transmitted safely,
Cannot be a function, bound method, interpreter object, frame, or object containing execution behavior.

If there are overloads, anonymous functions or multiple closure instances of the function name, frontend must choose a stable
and unambiguous query, for example:
```text
Lua:     module.submodule.function
JVM:     Class.method(descriptor)
.NET:    Namespace.Type.Method(signature)
WASM:    export name or $funcN
Python:  unique function name or stable nested-function identifier
```
Do not write name resolution rules for these languages in the simulator, GUI, or CLI.

### 20.1.3 What runtime facts can the adapter provide?

The adapter can implement narrow operations detected by the simulator and is used to express the problems that the generic IR cannot directly
Express language facts without requiring execution of the VM. Common operations include:

| Operation | Purpose |
|---|---|
| `resolve_global` | Resolve global names in frontend semantics to `ResolvedFunction` or `IntrinsicCall` |
| `resolve_call` | Parse data-based dynamic call requests |
| `resolve_indirect_call` | Resolve controlled indirect call targets |
| `truthy` | Provides language-defined rules for truth and false values |
| `binary_op` | Provides language-specific binary operations |
| `unary_op` | Provides language-specific unary operations |
| `get_attr` / `set_attr` | Provide language-specific member access |
| `get_item` / `set_item` | Provide language-specific index or table access |
| `iterate` | Provides language-specific views of iterable values |
| `set_captured` | Provides data-based updates when closure captures variable assignments |

These hooks must meet:

- Only accepts public runtime values, strings, numbers and data-only contexts.
- Returns generic runtime value, `ResolvedFunction`, `IntrinsicCall` or `NotHandled`.
- The return value must pass the simulator's runtime-value check.
- `NotHandled` means it cannot be safely handled and the simulator should produce an explicit unsupported or
  Other structured failures are not guesswork.
-Hooks must not call frontend bytecode and must not recursively execute the frontend interpreter.
- hooks must not return Python functions, lambdas, file handles, threads, modules, frames or
  Other executable callbacks.

`VMStatefulCallbacks` and `simulation_adapter` are two different boundaries:
```text
VMStatefulCallbacks: frontend -> core，for lifting complex VM linear slices and stack state
simulation_adapter: frontend -> simulator，for function queries and runtime data facts
```
The simulator cannot call `VMStatefulCallbacks`, and the adapter cannot use this to execute the VM.
Logic switches back to frontend.

### 20.1.4 External functions and complementary environments

Named functions that cannot be parsed in generic IR can be handed over to those provided by host
`ExternalEnvironment`:
```python
from unidecompiler_simulator import (
    ExternalCallRequest,
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
)


class MyEnvironment:
    def call(self, request: ExternalCallRequest):
        if request.name != "print":
            return NotHandled
        # The host decides how to handle this; the return value must be a supported runtime value.
        return ExternalCallResult(
            ExternalCallStatus.RETURNED,
            values=(),
            stdout=" ".join(map(str, request.args)) + "\\n",
        )
```
Environment protocols are data boundaries, not execution control boundaries. environment:

- Receives `ExternalCallRequest`, not IR, frame, stack, adapter or runner.
- Returns `ExternalCallResult` or `NotHandled`.
- Only in-memory runtime values supported by the simulator can be returned.
- Unhandled functions must return `NotHandled` and cannot fake success results.
- Frontend private objects should not be put into request or result.

Files like `runtime.py` belong to the application host. It is a trusted one explicitly selected by the user
Python code, not sandbox. It should be read and loaded by the independent host-support package,
Cannot be loaded by core, simulator or frontend.

Note: environment can only supplement the external functions called during the execution of the target function, and cannot replace it.
`resolve_function()`, nor the simulator's objective function itself.

#### 20.1.4.1 Decide first: adapter hook or host runtime

Don't cram into `simulation_adapter` all the behavior that generic IR can't do directly.
First select according to the following boundaries:

| Situation | Correct location | Reason |
|---|---|---|
| truthiness of function queries, overload selection, language definitions | adapter | data-only language facts that are frontend |
| Pure language operations, attribute/indexing rules | Narrow hooks for adapters or `IntrinsicCall` | No I/O or mutable external resources required |
| stdin, stdout, file, network, time, random number | host `ExternalEnvironment` | They are host resources, not frontend semantics |
| Variable runtime state such as buffer, heap, handle table, etc. | host `ExternalEnvironment` | The state belongs to the trusted host environment of a simulated run |
| VM opcode dispatch, VM program counter, VM data stack | Implementation not allowed | This will turn the frontend into a second interpreter |

A practical judgment is: if the implementation needs to read the frontend decoder model and traverse the VM
instructions, maintain VM `ip` or replay the effect, then it is not a runtime fact and cannot be written
adapter or `runtime.py`.

#### 20.1.4.2 Loading ABI of Python `runtime.py`

The GUI/CLI can select a trusted Python runtime file and is provided by the host-support package
Wrap it into `ExternalEnvironment`. For the current Python-file host, runtime file
A top-level function with the exact same name as `Global(name=...)` in generic IR should be exported:
```python
# generic IR: Call(Global("write_text"), args=(value,), returns=0)
def write_text(value):
    print(value, end="")
```
Calling rules:

1. The simulator generates `ExternalCallRequest(name, args, keywords, caller, source)`.
2. host searches for the `name` function with the same name in `runtime.py`.
3. The host calls it with `function(*args, **keywords)`.
4. The Python return value of the function is converted to the external call return value; if the generic IR
   `Call.returns == 0`, the return value will be ignored, ordinary `None` will do.
5. The text written by the runtime to stdout/stderr will become the corresponding external-call event
   `stdout`/`stderr`, visible in the Output column of GUI trace.
6. Unexported functions do not "return null", but `NotHandled`, and the simulator must report
   explicit unsupported.

This is a host integration ABI, not a frontend API. frontend is not allowed to import,
Read or load the file.

Runtime files must assume that stdout/stderr may be redirected by the host as text capture objects. Therefore:

- Use `print(...)` or `sys.stdout.write(...)` when outputting text;
- Do not assume `sys.stdout.buffer` exists;
- If the VM outputs raw bytes, explicitly select the encoding, e.g.
  `bytes(data).decode("utf-8", errors="replace")`;
- Do not use buffer, file handle, thread, Python callable and other objects as function return values.

#### 20.1.4.3 Generic template for stateful buffer runtime

The generic IR of many interpreter VMs includes named calls such as
`alloc_buffer`, `load_byte`, `store_byte`, `read_buffer`, and `write_buffer`.
The frontend is responsible only for keeping the function name, parameter order,
and number of `returns` consistent with VM semantics; mutable memory is maintained
by the host runtime.

The following template is not an implementation of a specific VM. It is simply a
copyable runtime shape for fixed slots, byte buffers, and one state per simulation.
Replace the capacity, out-of-bounds behavior, read/write encoding, and function
names to match your VM documentation:
```python
# runtime.py -- trusted host code, not imported by the frontend
from __future__ import annotations

import os
import sys

MAX_SLOTS = 16
MAX_SIZE = 4096
buffers: list[bytearray | None] = [None] * MAX_SLOTS
pending_input: bytes | None = None


def _require_index(index: int) -> int:
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("buffer index must be an integer")
    if not 0 <= index < MAX_SLOTS:
        raise IndexError(f"buffer index out of range: {index}")
    return index


def _require_buffer(index: int) -> bytearray:
    buffer = buffers[_require_index(index)]
    if buffer is None:
        raise ValueError(f"buffer {index} is not allocated")
    return buffer


def _require_offset(buffer: bytearray, offset: int) -> int:
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise TypeError("buffer offset must be an integer")
    if not 0 <= offset < len(buffer):
        raise IndexError(f"buffer offset out of range: {offset}")
    return offset


def alloc_buffer(size: int) -> None:
    if not isinstance(size, int) or isinstance(size, bool):
        raise TypeError("buffer size must be an integer")
    if not 0 <= size <= MAX_SIZE:
        raise ValueError(f"buffer size must be in [0, {MAX_SIZE}]")
    try:
        slot = buffers.index(None)
    except ValueError as error:
        raise MemoryError("no free buffer slots") from error
    buffers[slot] = bytearray(size)


def free_buffer(index: int) -> None:
    index = _require_index(index)
    _require_buffer(index)
    buffers[index] = None


def load_byte(index: int, offset: int) -> int:
    buffer = _require_buffer(index)
    return buffer[_require_offset(buffer, offset)]


def store_byte(index: int, offset: int, value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("stored value must be an integer")
    buffer = _require_buffer(index)
    buffer[_require_offset(buffer, offset)] = value & 0xFF


def _take_input(limit: int) -> bytes:
    global pending_input
    if pending_input is None:
        # GUI has no universal stdin widget.  A host/user-selected convention
        # such as this environment variable makes runs deterministic.
        configured = os.environ.get("MY_VM_RUNTIME_INPUT")
        pending_input = configured.encode("utf-8") if configured is not None else b""
    value, pending_input = pending_input[:limit], pending_input[limit:]
    return value


def read_buffer(index: int) -> None:
    buffer = _require_buffer(index)
    data = _take_input(len(buffer))
    buffer[:len(data)] = data


def write_buffer(index: int) -> None:
    buffer = _require_buffer(index)
    end = buffer.find(0)
    data = buffer if end < 0 else buffer[:end]
    sys.stdout.write(bytes(data).decode("utf-8", errors="replace"))
```
This template deliberately does not do three things:

- Does not parse or execute VM bytecode;
- Do not read frontend's private program/model;
- Do not write special cases based on an artifact or flag/secret.

If your VM's `ALLOC` returns a buffer index, the frontend should model its generic IR call
For `returns=1`, the runtime's `alloc_buffer()` should also `return slot`. If the VM's
`ALLOC` does not return a value but implicitly occupies the first empty slot, then `returns=0`, and is determined by the runtime according to the VM
Rules select slots. Don't change the calling convention of generic IR just to make the runtime "convenient".

#### 20.1.4.4 Input, state life cycle and GUI

The Args control of the GUI is only used in `FunctionIR.params`, not stdin of all VMs. Therefore, with
`READ`/`read_buffer` The frontend of a type of opcode must be at the top of the README or runtime file
Describe the input source. Options:

| Solution | Applicability | Notes |
|---|---|---|
| Environment variables | Text input, GUI manual validation | Not suitable for binary samples containing NUL |
| runtime internal fixtures | unit tests, repeatable analysis | fixtures must be explicitly set in tests and cannot be artifact special cases |
| The input file path provided by the host | Large input or binary input | The runtime only reads the path selected by the user and is not determined by the frontend |
| embedding host custom environment | product-level interactive input | host responsible for cancellation, encoding and permissions |

Do not call `sys.stdin.read()` unconditionally in a GUI worker: graphical interfaces usually have no interactive
stdin, which can cause a hang. The running state must be initialized within a simulation run; do not replace the last
Run's buffer, random state, or input residues are implicitly carried over to the next Run.

#### 20.1.4.5 Runtime Observability and Debugging

Generic simulator trace will record the name, args, stdout, stderr,
source offset and caller. It does not automatically stuff runtime private heap/buffer objects
`SimulationResult.locals`. So when analyzing a stateful VM:

1. First confirm which buffer is the input, output and intermediate value through the parameters of `external-call` event.
2. For debugging runtime, you can provide the read-only `debug_snapshot()` helper function, or in the test host
   Read module private state after run.
3. Do not allow normal `write_buffer()` to output debug logs; otherwise it will pollute the program semantic output.
4. If status must be logged, use a separate, explicitly enabled debug text prefix, or by embedding
   host holds the snapshot; do not use Python `bytearray`, file object or callable as
   simulator runtime value returns.

### 20.1.5 target enumeration and recovery status

`decompile_status` is the quality information of generic-IR recovery, not the second one of simulator
execution engine. The frontend must be clear about its enumeration strategy and not leave it to GUI users to guess:

| Strategy | `list_simulation_targets()` behavior | Applicability |
|---|---|---|
| strict | List only functions with `decompile_status == "ok"` | Product features that have verified lift integrity |
| diagnostic | `partial` is also listed, but the label/README clearly indicates experimental | For reverse analysis, you need to use trace to locate the gap |
| permissive | Only enumerate by stable query, do not filter by recovery status | The host needs to compare paths or verify semantics by itself |

No matter which one you choose:

- `completed` only indicates that the generic IR reached `Return`; it **does not prove** that the frontend
  The semantics, conditional polarity, or call argument order is correct.
- For `partial` targets, verify both the original VM/reference implementation and the
  The simulator's stdout, return value, or key external call sequence.
- If partial results encounter unmodeled behavior, `unsupported`/`raised` should be retained, not for
  Display a Run button and return fake success.
- The GUI should display or be able to trace the recovery status and diagnostics of the target.

### 20.1.6 SimulationResult Semantics

The frontend is not responsible for constructing `SimulationResult`, but the test and host must handle these results correctly:

| Status | Meaning | Requirements |
|---|---|---|
| `completed` | The function returns normally | Check `values`, not just status |
| `raised` | Execution raises a language/runtime exception | Reserved exception and cause |
| `unsupported` | generic IR or runtime fact cannot be expressed safely | diagnostic and trace context reserved |
| `invalid_request` | query, parameter, or environment protocol error | Show error explicitly |
| `step_limit` | Maximum number of execution steps reached | Cannot be disguised as completed |
| `call_depth_limit` | Maximum call depth reached | Cannot continue guessing |
| `cancelled` | User or host request cancellation | Keep the generated trace |
| `yielded` | Execution encounters yield behavior outside the supported scope | Explicitly mark non-completed |

Trace limits only limit the number of logged events and must not change function execution semantics. Must be truncated
Notify the host via `trace_truncated` or equivalent diagnostics.

#### Use trace for reverse analysis

trace is not just a test log. For input validation, state machine, decryption, or interpreter artifacts, press
The following steps are analyzed without adding sample special cases in the frontend:

1. Run with empty input or minimum legal input first, and record the failure stdout and the last block entered.
2. Find the input function in `external-call` events and confirm the runtime state the input falls into.
3. Find the `load_*`/`store_*` calls in the loop, distinguishing between inputs, transformation results, and constant targets.
4. Use the source offset of the event to return to the pseudocode/AST and read the items of the generic IR
   Arithmetic, comparison and branch.
5. Trace backward from the success/failure branch: compare target constant → transform output → input bytes/parameters.
6. Run the inversion candidate in the simulator and the original reference implementation, and compare the success output with the critical status.

This is an analytics workflow that does not change the responsibilities of the frontend, adapter, or core. runtime only
External state is provided; control flow and expressions are still executed by the generic simulator.

### 20.1.7 Minimum delivery process supported by simulation

When implementing optional simulations, proceed in the following order:1. Complete the decoder, thin IR, generic IR and decompilation tests first.
2. Confirm that function boundaries, names, parameters, and return values have stabilized.
3. Implement `resolve_function()` in `simulation.py`.
4. Implement `list_simulation_targets()` to filter ambiguous targets.
5. Write a `simulate_function()` or `simulate_artifact()` test for a purely computational function.
6. Add tests for branches, loops, container/member operations.
7. If language-specific behavior is required, only add narrow data-only adapter hooks.
8. Add `ExternalEnvironment` test for an unresolved external call.
9. Verification without environment gets explicitly unsupported instead of error on success.
10. Execute the real artifact once through the CLI and confirm that the parameters and return values remain unchanged.
11. Through the GUI target discovery and Run process, confirm that the target is not automatically reset and the runtime
    The input source does not block the GUI.
12. For stateful runtime, verify that one run will not pollute the next run.
13. Check that the simulator package is not core imported, and the frontend does not have an executor or interpreter.

First support a minimal function, and then expand the coverage. Don’t design one for all language features first
frontend-specific runtime framework.

## 21. When to expect while and when to accept goto

The goal of Frontend is not to force the output to appear `while`.

Correct expectations:

- Simple single entry single exit loop: possible output `while`.
- Multi-exit loops, switch-like dispatch, shared failure blocks, complex joins: possible output of `if/goto`.
- target is missing or stack shapes cannot be merged: should be partial/unsupported.

If a complex function outputs only linear code, no `if`, no `goto`, and no unsupported, this is a high-risk signal. Usually stated:

- The conditional jump effect consumes the condition in advance.
- No `materialized-condition` was committed.
- branch target is not restored.
- `branch_stack_width` is wrong.
- Conditional polarity hint is wrong.
- profile does not mark opcode as control/jump/conditional.

Make a clear distinction when verifying:
```text
well-structured result: has_while or has_if
conservative correct result: has_if + has_goto
dangerous result: complex CFG was linearized
```

## 22. Errors, unsupported and diagnostics

Decoder error:

- Input is not in this format: `can_load=False`.
- Format matched but corrupted: `decode` throws `FrontendDecodeError`.
- Unknown version: explicitly reported in metadata or diagnostics.

Lift error:

1. Still submit the command and raw text.
2. Use `UnknownOpcode` or `effects=None`.
3. Provide `raw_window`, decoded operands, target/region hints.
4. Let core return `partial` or `unsupported`, don't guess.

`unsupported` is not a development endpoint. If unsupported appears in the support range, it should be passed:

- Fixed opcode effect.
- Added target/case/exception hints.
- Supplement stateful callbacks.
- Supplement VM-neutral thin IR concept.
- Or enhance VM-neutral recovery in core.

Don't "bypass" unsupported with the frontend special case.

## 23. Verification script

External directory plug-in smoke test:
```python
from pathlib import Path
from unidecompiler import DecompilerEngine
from unidecompiler.plugin_registry import FrontendRegistry

plugin_dir = "/path/to/my-vm-plugin"
sample = "/path/to/sample.mvm"

registry = FrontendRegistry.discover()
registry.register_directory(plugin_dir)

result = DecompilerEngine.from_registry(registry).decompile_bytes(
    Path(sample).read_bytes(),
    sample,
    "my-vm",
)

text = result.pseudocode.text if result.pseudocode is not None else ""
print("status", result.status, result.frontend_id)
print("functions", [(f.name, f.status, f.unsupported_reason) for f in result.functions])
print("diagnostics", [(d.code, d.severity) for d in result.diagnostics])
print("has_if", "if (" in text or "if " in text)
print("has_goto", "goto block_" in text)
print("has_while", "while" in text)
print("has_unsupported", "unsupported" in text)
print("cfg", [(len(g.blocks), len(g.edges)) for g in result.control_flow])
print("self_edges", [e for g in result.control_flow for e in g.edges if e.source == e.target])
```
Strictly successful sample assertion:
```python
assert result.pseudocode is not None
assert result.status == "ok"
assert result.frontend_id == "my-vm"
assert result.functions
assert "unsupported" not in text
assert result.control_flow
assert any(len(g.blocks) >= 1 for g in result.control_flow)
assert not [e for g in result.control_flow for e in g.edges if e.source == e.target]
```
partial accepts sample assertions:
```python
assert result.pseudocode is not None
assert result.status in {"ok", "partial"}
assert result.frontend_id == "my-vm"
assert result.functions
assert result.control_flow
assert any(len(g.blocks) >= 1 for g in result.control_flow)
assert not [e for g in result.control_flow for e in g.edges if e.source == e.target]
```
Don't mix the two types of assertions. Core paths within the supported scope should use strict assertions; while semantic coverage is being extended, partial acceptable assertions can be used temporarily, but unsupported reasons should be included in the fix list.

If the current sample should have fully supported:
```python
assert result.status == "ok"
assert result.frontend_id == "my-vm"
assert result.functions
assert "unsupported" not in text
```
For complex control flow samples, at least the following should be met:
```text
status ok
frontend_id is your frontend id
no unsupported text
CFG has multiple blocks/edges
pseudocode contains while/if, or at least if/goto
GUI control_flow does not crash on self-loops
```
If the goal is "semantic completeness", `partial` + `if/goto` is acceptable. If the goal is "advanced structuring", the core needs to be able to safely match the CFG shape.

Complex control flow assertions can be more restrictive:
```python
has_structured_control = "while" in text or "if (" in text or "if " in text
has_low_level_cfg = "goto block_" in text
has_cfg_shape = any(len(g.blocks) > 1 and len(g.edges) > 0 for g in result.control_flow)

assert has_cfg_shape
assert has_structured_control or has_low_level_cfg
```
If the sample is known to have conditional jumps and backward edges, you can directly check the display CFG:
```python
edges = [edge for graph in result.control_flow for edge in graph.edges]
assert any(edge.kind == "branch" for edge in edges)
assert any(int(edge.target.split("_")[-1]) <= int(edge.source.split("_")[-1]) for edge in edges)
```
These assertions do not require all VMs to output `while`. They require that complex control flows cannot be mislinearized.

### 23.1 Simulate execution of verification script

If the frontend is declared to support simulation, a simulator must be added in addition to the decompiled smoke test
Verify. The following example uses the public simulator API and does not directly call the frontend decoder.
Private function or executor:
```python
from pathlib import Path

from unidecompiler.input_sources import InputEntry, load_input_entry
from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine, SimulationStatus


sample = Path("/path/to/sample.mvm")
registry = FrontendRegistry.discover()
registry.register_directory("/path/to/my-vm-plugin")
simulator = SimulationEngine.from_registry(registry)

artifact = load_input_entry(InputEntry(sample, str(sample)))
listing = simulator.list_artifact_targets(artifact.data, artifact.display_path)
assert listing.frontend_id == "my-vm"
assert listing.diagnostic is None
assert listing.targets

target = next(target for target in listing.targets if target.label == "add")
result = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    target.query,
    args=(2, 3),
)

assert result.status is SimulationStatus.COMPLETED
assert result.values == (5,)
assert result.exception is None
assert result.diagnostic is None
assert result.steps > 0
```
Target discovery tests must cover:
```python
assert listing.targets
assert all(target.query is not None for target in listing.targets)
assert all(target.function_index >= 0 for target in listing.targets)
assert len({target.label for target in listing.targets}) == len(listing.targets)
```
Ambiguous queries cannot be guessed. You can not list ambiguous targets, or you can have execution return an unambiguous
`invalid_request`, but the function cannot be chosen randomly:
```python
ambiguous = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "overloaded",
    args=(),
)
assert ambiguous.status is SimulationStatus.INVALID_REQUEST
assert ambiguous.diagnostic
```
External environment tests must verify return values, stdout, exceptions, and unhandled calls:
```python
from unidecompiler_simulator import (
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
)


class TestEnvironment:
    def call(self, request):
        if request.name == "print":
            return ExternalCallResult(
                ExternalCallStatus.RETURNED,
                values=(),
                stdout=repr(request.args) + "\\n",
            )
        return NotHandled


completed = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "prints_value",
    args=(7,),
    environment=TestEnvironment(),
)
assert completed.status is SimulationStatus.COMPLETED
assert any(event.kind == "external-call" for event in completed.events)
assert any("7" in event.stdout for event in completed.events)

without_environment = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "prints_value",
    args=(7,),
)
assert without_environment.status is SimulationStatus.UNSUPPORTED
assert without_environment.diagnostic
```
Quotas, cancellations and trace truncation also fall within the validation scope of frontend integration:
```python
from unidecompiler_simulator import SimulationCancellation, SimulationLimits


limited = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "loop",
    limits=SimulationLimits(max_steps=20, max_trace_events=5),
)
assert limited.status is SimulationStatus.STEP_LIMIT
assert limited.trace_truncated or len(limited.events) <= 5

cancellation = SimulationCancellation()
cancellation.cancel()
cancelled = simulator.simulate_artifact(
    artifact.data,
    artifact.display_path,
    "loop",
    cancellation=cancellation,
)
assert cancelled.status is SimulationStatus.CANCELLED
```
These tests must verify the generic IR execution results, not the frontend re-execution itself
The result obtained after bytecode. It is recommended to put the source code, generated artifacts and expected values in
`simulator_projects/source/<project>` and the corresponding expected file.

## 24. Unit test checklist

Decoder：

- Correctly identify header/magic/version/endianness.
- Correct handling of extensions.
- Truncate input and report error.
- Wrong length error reported.
- An error is reported when the constant table is damaged.
- All opcodes have offset, size, operands, and raw.
- Empty input error.
- Enter `can_load=False` when not in this format.

Effect:

- constant push.
- local load/store.
- Binary order of operations.
- Call parameter order.
- return/halt.
- unknown opcode.

Control：

- unconditional jump target.
- conditional jump target.
- backward jump -> `loop-backedge`.
- materialized condition.
- branch polarity.
- invalid target.
- switch/case/default.
- exception region.

Integration:

- Linear function output pseudocode.
- if/else.
- while or low-level if/goto.
- nested control.
- Multi-function module.
- Failure of one function does not affect other functions.
- The GUI can open the sample without displaying it as a resource.

If the frontend is declared to support simulation, it must also be added:

Target discovery：

- `simulation_adapter` is recognized by the registry.
- `list_simulation_targets()` returns a stable, unique, data-only query.
- Unsupported or ambiguous functions are not randomly listed.
- Each listed query can be resolved to the `FunctionIR` in the current `ModuleIR`.
- There are tests for the naming strategy of nested functions, anonymous functions and overloaded functions.

Generic-IR execution:

- `simulate_function()` can execute a pure function without frontend private runtime dependencies.
- `simulate_artifact()` can find the same function through frontend query.
- Parameter binding order, default parameter behavior and number of return values are correct.
- Branch, loop, comparison, container, member and closure scenarios are covered by target VM support.
- Simulation failure of one function does not destroy the decompile result of other functions.

Adapter boundary:

- adapter only returns `ResolvedFunction`, `IntrinsicCall`, runtime value or `NotHandled`.
- adapter does not contain execute/run/step/eval/interpreter logic.
- adapter does not return executable callback, frame, stack or frontend private execution objects.
- The function returned by adapter belongs to the current lifted module.
- Unhandled global, call, attribute, item or iterator behavior produces explicit results.

Environment and outcomes:

- `ExternalEnvironment` can handle at least one external call.
- Without environment, unresolved calls get explicitly `unsupported` and cannot pretend to succeed.
- Host return values, stdout, stderr, and exceptions are passed through structured results.
- The function names, parameter order and `Call.returns` of the Python-file runtime are consistent with the generic IR.
- The runtime does not assume that `sys.stdout.buffer` exists and does not block reading stdin in GUI workers.
- Stateful runtime starts from a clean state in every simulation run.
- The capacity, unallocated access, out-of-bounds and input exhaustion of host states such as buffer/heap are tested.
- `completed`, `raised`, `unsupported`, `invalid_request`, step limit, call
  depth limit and cancellation cover at least the relevant state supported by the target.
- Trace truncation does not change the return value or control flow results.
- For diagnostic/permissive partial targets, at least one is compared with the reference implementation
  Path testing; `completed` cannot be relied upon as the only evidence of semantic correctness.

Host integration:

- CLI only passes frontend-owned query and displays `SimulationResult`.
- The GUI automatically enumerates targets and does not implement frontend-specific lookup.
- Target selection is not reset after GUI Run, return value, status and trace are all visible.
- Runtime file loading occurs on the application host, frontend and simulator do not load files.
- GUI/README clearly states the source of runtime input; Args only corresponds to function parameters and does not default to equal
  VM stdin.
- The GUI can display the recovery status of the target or allow the user to trace back the corresponding diagnostics.

## 25. Command line and GUI registration

Python API registration:
```python
from unidecompiler import DecompilerEngine
from unidecompiler.plugin_registry import FrontendRegistry

registry = FrontendRegistry.discover()
registry.register_directory("/path/to/my-vm-plugin")
engine = DecompilerEngine.from_registry(registry)
```
GUI registration:
```text
Frontend manager -> Register folder -> /path/to/my-vm-plugin
```
If the GUI still shows resource:

- Check if `can_load()` returns true for extensions.
- Check whether the manifest path is registered in the plugin root directory.
- Check if `module` can be imported.
- Check whether the plugin id is consistent with the decompile selection.
- Check whether the current registry of the GUI needs to be re-registered or restarted.

If the GUI can be decompiled but the Simulation tab has no target:

- Confirm that the plugin exposes `simulation_adapter`, and the adapter's `frontend_id` is the same as
  The plugin id is exactly the same.
- Confirm that the adapter implements `resolve_function()`.
- Confirm that `list_simulation_targets()` returns
  A `SimulationTargetCandidate` tuple, not a `FunctionIR` or callable.
- Confirm that each candidate's query can be uniquely resolved by `resolve_function()`.
- Verify that the listed functions have been `lift()`ed into the current `ModuleIR`, including nested functions.
- Verify that the GUI is using the same registry that contains the plugin.

The CLI/GUI should not inspect or modify the frontend to determine whether
simulation is supported. Input recognition is dynamically registered by the
frontend registry, and simulation target discovery is determined by the
corresponding adapter.

If the GUI runs, but the runtime immediately fails or freezes:

- Check the first `external-call` of the trace; its Detail is the name of the missing runtime function.
- Check whether the runtime function signature is consistent with the Args order of the event; do not pop the stack based on the VM original
  The order guess should be based on the generic IR `Call.args`.
- Check whether the runtime writes bytes to `sys.stdout.buffer`; the GUI host may write stdout
  Redirect to text capture object.
- Check input sources for `READ` class operations; GUI Args only bind `FunctionIR.params`.
- Check whether the runtime retains the heap/buffer from the previous run to the next run.
- If target is `partial`, compare the reference implementation with the simulator's stdout, return value, or
  external-call sequence; `completed` is not sufficient to prove semantic equivalence.

## 26. Common Error Table

| Error | Result | Correct approach |
|---|---|---|
| frontend constructor `If`/`While`/AST | destroy core ownership | commit only effects/hints |
| Only simple opcodes are submitted | Complex samples lose context | All decodable opcodes are submitted |
| unknown opcode returns empty tuple | misleading pseudocode | use `UnknownOpcode` or unsupported |
| branch target uses instruction index | CFG misalignment | uses original bytecode offset |
| Conditional jump effect ahead of `Pop` condition | No `if/goto` | Use `branch_stack_width` to consume |
| No `materialized-condition` | Conditions may be linearized | Condition stack VM submits this hint |
| Conditional polarity is not marked | true/false side inversion | `detail="target-if-true"` |
| Backward edges are still marked only with branch-target | loop information is weak | Use `loop-backedge` for backward edges |
| Put private objects into operand | core/frontend coupling | Only use neutral value/text |
| Metadata expresses program logic | Recovery is not testable | Use `VMHint` |
| backend inference loop/goto | multiple frontend inconsistencies | core structuring responsible |
| GUI crashes from loop edges | CFG view unavailable | Fix/filter display metadata |
| Parse with subprocess | Platform and diagnostics are unstable | Use library or local parser |
| frontend executes bytecode for simulation | simulator/frontend dual semantics | frontend only provides adapter, simulator executes generic IR |
| Adapter randomly selects overloads by name | Runs wrong function | Uses stable query, `NotHandled` when ambiguous |
| adapter returns callable or decoder object | Execution boundary leaks | Returns only data-only value, `ResolvedFunction` or `NotHandled` |
| GUI/CLI resolves class names or Lua names by itself | Host and frontend semantics are bifurcated | Only passes opaque query to adapter |
| runtime.py is put into the simulator | core/host is coupled and cannot be audited | trusted files are loaded by the independent host-support package |
| Unhandled external calls return null | Fake success results | Return `NotHandled` to let the simulator fail structured |
| Treat stdin as GUI Args | Runtime cannot receive input or function parameters are misplaced | Clear host input contract, such as environment variables or user-selected input files |
| runtime writes `sys.stdout.buffer` | GUI stdout capture `AttributeError` | writes text stdout, bytes are explicitly decoded first |
| Reuse the runtime state of the last run | The simulation is not repeatable and the results depend on the click sequence | Create/reset state for each run |
| `partial + completed` treated as verified semantics | Error control flow may also reach Return | Compare with reference implementation or known path |
| Trace stops or changes results after truncation | Observation behavior changes execution semantics | Only truncate events and continue restricted execution |

## 27. Implement sequence from scratch

Recommended order:1. Create a directory and manifest.
2. Write `model.py`.
3. Write `decoder.py`, first pass `can_load/decode`.
4. Write minimal `plugin.py`.
5. Write `lifter.py`, covering the linear opcode first.
6. Write a small sample to test binary operations and the order of calling parameters.
7. Add branch target resolver.
8. Add `VMRegionOpcodeClasses`.
9. Add `VMStatefulCallbacks`.
10. Add `branch_condition` and `branch_stack_width`.
11. Add `target-if-true` and `materialized-condition` to the conditional jump.
12. Add `loop-backedge` to the backward edge.
13. Run the complex control flow sample and make sure there is at least `if/goto`.
14. Add unknown/malformed test.
15. If simulation is supported, implement target discovery and function resolution of `simulation.py`.
16. Add simulator project for pure calculation functions, control flow, return values and parameter binding.
17. Add minimal data-only adapter hooks for language-specific behavior, do not add interpreters.
18. Add `ExternalEnvironment` test for external calls and test unhandled results.
19. Execute the real artifact through CLI and confirm that the query, args, and return values are correct.
20. Register the GUI and confirm that the resource and target can be found, and the trace is visible after Run.
21. Check that the adapter and host do not have paths that execute frontend bytecode.
22. Let’s see if we need core to enhance the advanced structure.

## 28. Completion criteria

A frontend is completed within the target support range, and at least satisfies:

- decoder can stably parse target files.
- Each decodable instruction generates a step.
- `SourceRef` offset is correct.
- The effect table covers all known opcodes.
- unknown opcode is diagnostic.
- Control flow target is correct.
- Conditional jumps are not incorrectly linearized.
- Complex control flow outputs at least low-level `if/goto`.
- No misleading success status.
- GUI can register, identify, decompile, and display CFG.
- Test coverage decoder, effects, control hints, integration.

Impersonation support is optional, and not supporting impersonation does not disqualify frontend from decompilation capabilities. if
The frontend declares that it supports simulation and must also meet:

- `simulation_adapter` provides data-only target lookup and runtime facts.
- All simulation targets can resolve the function of the current lifted `ModuleIR`.
- The simulator executes generic IR, and the frontend does not execute bytecode and does not maintain the simulator frame/stack.
- Tests cover target discovery, ambiguous queries, parameters, return values, and runtime facts of the target language.
- Tests cover at least one control flow scenario and one external environment scenario.
- Unresolved calls, unsupported IR, exceptions, limit violations and cancellations all have structured results.
- CLI/GUI only consumes the public simulator API and does not implement language-specific search and execution logic.
- `simulator_projects` with source code, generated artifacts and repeatable validation of expectations.

If these are met, but you still cannot output advanced `while/for/switch`, this is usually not a frontend defect, but the core's current structural capability boundary. Frontend cannot bypass core in order to display more beautifully.

## 29. Choose your VM modeling path

Different VMs have different implementation entrances, but in the end they all submit the same thin IR.

First determine which category the VM belongs to, and then choose a modeling strategy.

| VM types | Common characteristics | frontend modeling methods | Control flow focus |
|---|---|---|---|
| Pure stack machine | opcode takes value from operand stack | `Push`, `Pop`, `Binary`, `CallStackArgs` | Conditions on the stack, target on the stack, `branch_stack_width` |
| Register VM | opcode explicitly reads and writes registers/slots | Use `LoadLocal`/`StoreLocal` or register to name locals | branch condition mostly comes from register expressions |
| Accumulator VM | Implicit accumulator | Map accumulator to stable local, such as `acc` | Update `acc` for each operation |
| Three address code VM | `dst = op src1 src2` | `LoadLocal` + `Binary` + `StoreLocal`, or directly `AssignValue` | target is usually immediate/relative |
| typed stack VM | Stack values have types | effect retains expressions, metadata can retain types | merge point type consistency is important |
| native-like bytecode | with address, jump table, indirect jump | conservative target recovery, unknown indirect jump partial | don't guess computed jump |
| AST-ish bytecode | opcode is close to the syntax node | Still submit thin facts, do not construct AST | Let core restore the structure uniformly |

If the VM is not a stack machine, do not force the stack machine example. The goal is to express equivalent facts, not to emulate the opcode names in the document.

## 30. API Contract Quick Check

This section lists the fields of commonly used objects in a centralized manner to facilitate comparison when writing code.

### FrontendPlugin

| Members | Type | Required | Description |
|---|---|---:|---|
| `id` | `str` | yes | stable frontend id |
| `display_name` | `str` | Yes | GUI display name |
| `supported_inputs` | `tuple[str, ...]` | Recommended | Display and selection aids |
| `version_support` | `FrontendVersionSupport` | Recommended | Support version notes |
| `can_load(data, filename)` | method | Yes | Quickly determine whether the input may belong to the frontend |
| `decode(data, filename)` | method | Yes | Return `FrontendModule` or throw `FrontendDecodeError` |
| `lift(module)` | method | yes | returns `ModuleIR` |

`can_load()` should not throw ordinary parsing errors. In the case of "it may be in this format but the content is damaged", you can return `True` and then use `decode()` to give the precise error.

### FrontendModule

| Field | Type | Description |
|---|---|---|
| `frontend_id` | `str` | Must equal plugin id |
| `payload` | `object` | frontend private decoder model |
| `metadata` | `dict` | provenance, version, diagnostics, statistics |

`payload` will not be interpreted by core. Only `lift()` of the same frontend can read it.

### VMBytecodeStep

| Field | Type | Required | Description |
|---|---|---:|---|
| `opcode` | `str` | yes | stable opcode name |
| `source` | `SourceRef` | is | the original source |
| `decoded` | `VMDecodedInstruction | None` | Recommended | GUI/CLI demonstration |
| `raw` | `str` | Recommended | Raw disassembly text |
| `effects` | `tuple[Effect, ...] | None` | Yes | thin stack/value facts |
| `hints` | `tuple[VMHint, ...]` | Recommended | Control flow/call/aggregation facts |

Three states of `effects`:

| Writing | Meaning | Typical uses |
|---|---|---|
| `()` | Explicit and unsemantic | `NOP`, padding |
| `(UnknownOpcode(...),)` | Instruction bounds are known but semantics are unknown | Keep raw context |
| `None` | This opcode cannot currently be expressed safely | Let core partial/unsupported |

Don't use `()` to mean "not yet implemented".

### VMHint

| Field | Type | Description |
|---|---|---|
| `kind` | `str` | hint type |
| `source` | `SourceRef` | hint source |
| `target` | `int | None` | bytecode target offset |
| `value` | `object | None` | case value, region dict, shape information |
| `label` | `str` | Display label |
| `detail` | `str | None` | Neutral details, such as `target-if-true` |
| `flow` | `str | None` | `conditional`, `unconditional`, `multiway` |

Common field combinations:

| kind | required | optional | description |
|---|---|---|---|
| `branch-target` | `target` | `flow`, `detail`, `label` | Forward or normal jump |
| `loop-backedge` | `target` | `flow`, `detail`, `label` | Backedge |
| `case-target` | `target`, `value` | `label` | switch case |
| `default-target` | `target` | `label` | switch default |
| `fallthrough` | `target` | `label` | explicit fallthrough fact |
| `materialized-condition` | None | `detail`, `flow` | Condition has been materialized on stack/register |
| `exception-region` | `value` | `label` | try/protected range |
| `call-shape` | `value` | `label` | Call parameters/return shape |
| `aggregate-shape` | `value` | `label` | array/map/object shape |

### VMStatefulCallbacks

| callback | input | return | description |
|---|---|---|---|
| `initial_locals` | None | `dict[str, Expr]` | Function entry locals |
| `lift_linear` | `start, end, locals, stack` | `VMLinearState | None` | Interpret linear slice |
| `branch_condition` | `branch, stack_slice` | `Expr | None` | Construct condition from consumption stack slice |
| `branch_stack_width` | `instruction` | `int` | The number of stack values consumed by branch in CFG semantics |

`branch_stack_width` is not an "opcode operand number". It is the number of core values that should be removed from the stack state on the branch edge.

If the branch target is immediate and not on the stack, width usually only contains condition. If both condition and target are on the stack, width is usually 2.

### SimulationAdapter

`SimulationAdapter` is an optional frontend capability and is not part of the core lifting API.

| Members | Input | Return | Description |
|---|---|---|---|
| `frontend_id` | None | `str` | Must equal plugin id |
| `resolve_function` | `query`, `decoded_module`, `lifted_module` | `ResolvedFunction` or `NotHandled` | frontend-specific target resolution |
| `list_simulation_targets` | `decoded_module`, `lifted_module` | `tuple[SimulationTargetCandidate, ...]` or `NotHandled` | GUI/CLI target discovery |

Optional runtime facts use the same data-only adapter, but not the execution entry point:

| hook | function |
|---|---|
| `resolve_global` | Resolve global name as `ResolvedFunction` or `IntrinsicCall` |
| `resolve_call` | Resolve dynamic call request |
| `resolve_indirect_call` | Resolve controlled indirect calls |
| `truthy` | Language-specific truth and false values |
| `binary_op` / `unary_op` | Language-specific operations |
| `get_attr` / `set_attr` | Member access |
| `get_item` / `set_item` | Index access |
| `iterate` | Iterator value view |
| `set_captured` | Capture variable updates |

The above hooks should all return `NotHandled` by default. The return value must be validated generic runtime
value, `ResolvedFunction`, `IntrinsicCall` or `NotHandled`. Cannot return callable,
frame, stack, module, decoder model or other execution object.

### ExternalEnvironment

`ExternalEnvironment` is injected by CLI, GUI or embedding host, not by frontend
Automatically created:

| Type | Function |
|---|---|
| `ExternalCallRequest` | Data request for function name, parameters, keyword parameters, caller and source |
| `ExternalCallResult` | A structured result that was returned, raised, or not handled |
| `NotHandled` | The host is not responsible for the call |environment does not accept `ModuleIR`, `FunctionIR`, frame, stack, adapter or runner.
The loading of `runtime.py` belongs to the host-support package and is a trusted code execution, not
simulator sandbox.

## 31. Effect cookbook

The following are selection guidelines for common effects.

| Target semantics | Recommended effects | Stack input | Stack output | Remarks |
|---|---|---|---|---|
| Pressure constant | `Push(Const(...))` | 0 | 1 | The constant value must be a stable value |
| Pop-up value | `Pop(count=n)` | n | 0 | Do not use for conditional jump and early consumption |
| Copy stack value | `Copy`/`DuplicateTop` | Visual effect | Visual effect | Used for dup class opcode |
| Swap order | `Swap(depth=n)` | n | n | Common in non-default operand order |
| Binary operation | `Binary(op=...)` | 2 | 1 | Confirm the left/right order first |
| Compare | `Binary(op=\"==\")` or `Compare` | 2 | 1 | Output conditional expression |
| Read local | `LoadLocal(name=...)` | 0 | 1 | Register mappable as local |
| write local | `StoreLocal(name=...)` | 1 | 0 | use stable local name |
| call | `CallStackArgs`/`Invoke` | argc | returns | runtime API readable callee |
| Return to the top of the stack | `ReturnTop` | 1 | Terminate | Function return value |
| Return without value | `ReturnVoid` | 0 | Terminate | halt/void return |
| Unknown | `UnknownOpcode` | Undecided | unsupported | Reserved raw |

Prioritize semantic precision when choosing an effect. When it cannot be expressed accurately, do not write an approximate effect of "it looks like it can run".

## 32. Register VM modeling template

The register VM doesn't need to disguise everything as an operand stack.

Registers can be mapped as core local:
```python
def reg_name(index: int) -> str:
    return f"r{index}"
```
Three address operations:
```text
ADD dst, left, right
```
It can be modeled as:
```python
return (
    LoadLocal(source=src, name=reg_name(left)),
    LoadLocal(source=src, name=reg_name(right)),
    Binary(source=src, op="+", semantics="static"),
    StoreLocal(source=src, name=reg_name(dst)),
)
```
Register conditional jump:
```text
JUMP_IF_ZERO r3, target
```
If target is immediate, the effect does not need to keep target on the stack.

There are two safe ways to write.

The first one: temporarily push the register condition to the core stack in the effect of the branch instruction, and then let `branch_stack_width` consume 1 condition value. This fits the current core's stateful branch callback model:
```python
def effects_for_jump_if_zero(ins, src):
    return (LoadLocal(source=src, name=reg_name(ins.condition_reg)),)


def branch_condition(branch, stack):
    condition = stack[-1]
    return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))


branch_stack_width=lambda ins: 1 if ins.opcode == "JUMP_IF_ZERO" else 0
```
This way of writing is not an "early pop condition". It just materializes the register condition into an expression visible to the branch callback; what is actually removed from the stack state is `branch_stack_width`.

Submit target hint at the same time:
```python
VMHint(
    kind="branch-target",
    source=src,
    target=ins.target,
    flow="conditional",
    detail="target-if-true",
)
```
This type of VM usually does not need `materialized-condition`, because the condition is not the value that the preorder opcode has left on the operand stack, but the branch instruction itself reading the register.

Second: If subsequent core extensions support branch condition generation directly from decoded operand, callback can be used to read the register operand in `branch.decoded.operands` and construct `LoadLocal`/`BinaryOp`. The first one is preferred in the current document template because it only relies on existing effects/callbacks.

## 33. Binary decoder cookbook

The binary VM decoder must first clearly distinguish the container and instruction stream.

Suggested order:

1. Check magic/header.
2. Read version.
3. Determine endianness.
4. Parse section table or code offset.
5. Parse constant pool.
6. Parse the function table or entry point.
7. Parse the instruction stream.
8. Parse debug/local/symbol information.
9. Verify whether the branch target falls on the instruction boundary.
10. Save raw bytes or disassembled text.

Fixed width instruction:
```python
offset = code_start
while offset < code_end:
    opcode = data[offset]
    operand = int.from_bytes(data[offset + 1:offset + 4], byteorder)
    instructions.append(MyInstruction(offset=offset, opcode=opcode_name(opcode), size=4, operands=(operand,)))
    offset += 4
```
Variable length instruction:
```python
offset = code_start
while offset < code_end:
    opcode = data[offset]
    size, operands = decode_operands(data, offset, opcode)
    if size <= 0 or offset + size > code_end:
        raise FrontendDecodeError(f"truncated instruction at {offset:#x}")
    instructions.append(MyInstruction(offset=offset, opcode=opcode_name(opcode), size=size, operands=operands))
    offset += size
```

Relative branch target：

```python
target = instruction.offset + instruction.size + signed_delta
```

Absolute branch target：

```python
target = code_base + absolute_offset
```
Regardless of the target, the final value submitted to `VMHint.target` must use the same coordinate system as `SourceRef.offset`.

## 34. Target recovery strategy

There are six common Target sources.

| Source | Strategy | On Failure |
|---|---|---|
| immediate absolute | direct analysis | invalid target diagnostic |
| immediate relative | `offset + size + delta` | invalid target diagnostic |
| constant pool label | table lookup analysis | unknown label unsupported |
| stack constant | local constant propagation | don’t guess if you can’t calculate |
| register constant | data flow/fixpoint | partial if not calculated |
| jump table | `case-target/default-target` | Missing items are partial |

Simple linear constant propagation can only cover the case where there is no join or the join does not affect the target.

When encountering branch joins, loops, or register targets, conservative data flow should be used:
```text
each program point stores an abstract state
constant values are int
unknown values are Unknown
merging different constants produces Unknown
worklist until a fixpoint
submit only targets known to be a single int and present in valid_offsets
```
Don't submit the "most likely" target. A false target is worse than an unknown target because it produces misleading CFG.

## 35. Switch and jump table

Switch-like opcode should not pretend to be a series of ordinary conditional jumps.

If the VM explicitly provides multicast distribution:
```text
SWITCH selector, default, [(case0, target0), (case1, target1)]
```
submit:
```python
hints = (
    VMHint(kind="default-target", source=src, target=default_target, flow="multiway"),
    VMHint(kind="case-target", source=src, value=case0, target=target0, flow="multiway"),
    VMHint(kind="case-target", source=src, value=case1, target=target1, flow="multiway"),
)
```
`branch_stack_width` should override the selector and any on-stack target. If the selector comes from a register or immediate, don't count it as being pushed onto the stack.

Jump table common mistakes:

- Forget the default target.
- Mix case value and case index.
- table entry is relative offset but submitted as absolute.
- Guess unresolved computed jump as switch.

## 36. Exception region

Exception tables are control flow facts and should be expressed using hints.

Typical information:
```text
protected_start
protected_end
handler_target
exception_type
stack_depth
binding_name
```
Can be submitted:
```python
VMHint(
    kind="exception-region",
    source=src,
    value={
        "start": protected_start,
        "end": protected_end,
        "target": handler_target,
        "type": exception_type,
    },
)
```
Rules:

- `start/end/target` uses the same offset coordinate system.
- `end` is the boundary defined by the VM format, usually the end of the half-open range.
- The handler target must fall within the instruction boundary.
- exception type can be a neutral string or a constant identifier.
- Don't put try/catch AST nodes into the frontend.

If the VM has complex semantics such as finally, filter, fault, resume, etc., and existing hints cannot express them, priority should be given to supplementing VM-neutral fact or making core partial.

## 37. Function discovery and calling convention

Many frontend difficulties lie in function discovery, not in a single opcode.

Function sources may be:

- Explicit function tables.
- debug/symbol table.
- Entry point + call target recursively discovered.
- section metadata.
- Fixed offset.
- Flat programs wrapped into `main`.

Rules:

- Each function generates `VMFunctionSpec` independently.
- A function being unsupported should not block other functions.
- Use a stable synthetic name when the function name is missing, for example `sub_0040`.
- `params` only puts certain parameters.
- `local_names` only puts locals confirmed by VM/debug information.
- When the call target is uncertain, the call can be conservatively expressed as an indirect call.

The calling convention needs to be clear:

| Question | Example |
|---|---|
| Where the parameters come from | stack, register, locals, argument area |
| Where to put the return value | stack, register, memory |
| whether call clears the stack | caller-clean, callee-clean |
| Is it possible to throw an exception | exception edge |
| Is there closure/upvalue | captured locals |

If the calling convention is unclear, don't generate an incorrect parameter list for the sake of aesthetics. Give priority to retaining low-level calling forms.

### 37.1 Simulation target discovery and query conventions

Function discovery also determines whether simulation is available. The decompilation phase can use synthetic names or conservative
indirect call; the simulation phase must be able to uniquely map the target query selected by the user to
A function in the current lifted module.

Documented in the README and tests for each frontend:

| Project | Issues that must be clarified |
|---|---|
| target label | What is the stable name displayed to the user by the GUI/CLI |
| query | What is the data-only flag received by frontend |
| Uniqueness | How to disambiguate overloading, nested function with the same name, and anonymous functions |
| Parameters | Whether the parameter name is reliable and whether the keyword parameter is supported |
| receiver/context | How to express instance method, closure and upvalue in data-only context |
| External calls | Which calls are resolved by the adapter and which are handed over to the environment |

Recommended strategy:

- query can be a string name when the name is globally unique.
- When a method with the same name exists, the query should include owner and descriptor/signature.
- Anonymous functions should use stable source offset, function index or frontend-defined id,
  Python object identity cannot be used.
- instance receiver, closure context or member selection can only be used as
  The data of `ResolvedFunction.context` must not be callable or frontend executor.
- Return `NotHandled` when the adapter cannot be resolved reliably; do not select the first match.

`list_simulation_targets()` only lists the uniquely resolvable entry targets in the current artifact.
It should not make every nested helper, synthetic bridge, or function that cannot satisfy the argument convention a convenience
are exposed to users. When exposed, the label must indicate its stable identity.

## 38. Unsupported decision matrix

| situation | frontend behavior | result |
|---|---|---|
| The input is not in this format | `can_load=False` | Leave it to other frontend |
| It looks like this format but the header is damaged | `decode()` throws `FrontendDecodeError` | User sees decode error |
| The opcode boundary cannot be determined | `decode()` throws `FrontendDecodeError` | Prevent misaligned parsing |
| opcode known but not implemented effect | `UnknownOpcode` or `effects=None` | partial/unsupported |
| branch target is invalid | Submit diagnostic, do not guess target | partial/unsupported |
| branch target cannot be calculated | do not submit target hint | partial/unsupported or linear fragment |
| Insufficient stack depth | Let core diagnostic | partial/unsupported |
| control flow is too complex | correct effects/hints + stateful callbacks | `if/goto` fallback |
| core can be safely structured | correct facts | `if/while/switch` |

There are two dangers to avoid during development: false success:

- Output `status ok`, but complex control flow is linearized.
- The high-level structure is output, but the condition polarity or target is wrong.

Both are more difficult to troubleshoot than explicit partial.

## 39. Minimum runnable external plug-in list

New authors should make a tiny VM first instead of moving directly to a full VM.

Minimal functionality:

- a constant opcode.
- A binary operation opcode.
- An output or return opcode.
- An unconditional jump example.
- A conditional jump example.

Document list:
```text
tiny-vm-plugin/
├── unidecompiler-plugin.toml
├── tiny_vm_frontend/
│   ├── __init__.py
│   ├── model.py
│   ├── decoder.py
│   ├── plugin.py
│   └── lifter.py
└── tests/
    └── test_integration.py
```
If tiny VM also supports simulation, add:
```text
tiny-vm-plugin/
├── tiny_vm_frontend/
│   └── simulation.py
└── tests/
    ├── test_integration.py
    ├── test_simulation.py
    └── test_simulation_environment.py
```
Acceptance order:

1. `python -m py_compile tiny_vm_frontend/*.py`
2. The API registration directory is successful.
3. Linear sample output expression.
4. Conditional jump sample output `if` or `if/goto`.
5. The backward jump sample CFG has back edges.
6. The GUI can recognize the file and does not display it as a resource.
7. GUI CFG view does not crash.
8. If the declaration supports impersonation, target discovery can list unique functions.
9. The simulator can execute at least one purely computational function and retain the return value.
10. Unresolved external calls can be handled when environment is provided, and fail explicitly when not provided.
11. The adapter has no frontend bytecode interpreter or executable callback.

Document fragments can be copied directly, but must be replaced:

- frontend id.
- opcode name.
- offset coordinate system.
- operand decoding.
- Stack order.
- branch target source.
- runtime call name.
- simulation query format.
- Runtime facts for the simulation adapter.
- The function name and return value protocol that the external environment needs to handle.

### 39.1 Complete minimal plug-in example

Below is a closed tiny VM external plugin skeleton. It supports:

- `CONST n`
- `ADD`
- `RETURN`
- `JUMP target`
- `JUMP_IF_ZERO target`
- `NOP`

This example is the immediate target + stack condition model. It is not a clone of an off-the-shelf VM, just a replicable minimal closed loop.

To be more precise: it only applies to toy VMs where the "jump target is immediate and the condition value comes from the stack". If your VM uses register conditions, relative targets or jump tables, please replace the corresponding parts instead of copying them directly.
```text
tiny_vm_plugin/
├── unidecompiler-plugin.toml
└── tiny_vm_frontend/
    ├── __init__.py
    ├── model.py
    ├── decoder.py
    ├── plugin.py
    └── lifter.py
```

`unidecompiler-plugin.toml`：

```toml
[frontend]
id = "tiny-stack-vm"
module = "tiny_vm_frontend.plugin:TinyStackVmFrontendPlugin"
```

`tiny_vm_frontend/model.py`：

```python
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TinyInstruction:
    offset: int
    opcode: str
    size: int
    operands: tuple[int, ...] = ()
    raw: str = ""
    line: int | None = None


@dataclass(frozen=True)
class TinyFunction:
    name: str
    offset: int
    instructions: tuple[TinyInstruction, ...]
    params: tuple[str, ...] = ()
    local_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class TinyProgram:
    filename: str | None
    version: int
    functions: tuple[TinyFunction, ...]
    diagnostics: tuple[str, ...] = ()
```

`tiny_vm_frontend/decoder.py`：

```python
from __future__ import annotations

from .model import TinyFunction, TinyInstruction, TinyProgram

MAGIC = b"TVM1"

OP_CONST = 0x01
OP_ADD = 0x02
OP_RETURN = 0x03
OP_JUMP = 0x04
OP_JUMP_IF_ZERO = 0x05
OP_NOP = 0x00


def looks_like_tiny_vm(data: bytes, filename: str | None = None) -> bool:
    return data.startswith(MAGIC) or (filename is not None and filename.endswith(".tvm"))


def decode_tiny_vm(data: bytes, filename: str | None = None) -> TinyProgram:
    if len(data) < 6 or not data.startswith(MAGIC):
        raise ValueError("missing TVM magic")
    version = data[4]
    function_count = data[5]
    offset = 6
    functions: list[TinyFunction] = []

    for _ in range(function_count):
        if offset >= len(data):
            raise ValueError("truncated function table")
        name_len = data[offset]
        offset += 1
        if offset + name_len > len(data):
            raise ValueError("truncated function name")
        name = data[offset:offset + name_len].decode("ascii", errors="strict")
        offset += name_len
        if offset + 2 > len(data):
            raise ValueError("truncated instruction count")
        ins_count = int.from_bytes(data[offset:offset + 2], "little")
        offset += 2

        instructions: list[TinyInstruction] = []
        for _ in range(ins_count):
            if offset >= len(data):
                raise ValueError("truncated instruction stream")
            ins_offset = offset
            opcode = data[offset]
            offset += 1
            if opcode in {OP_CONST, OP_JUMP, OP_JUMP_IF_ZERO}:
                if offset >= len(data):
                    raise ValueError(f"truncated operand at {ins_offset:#x}")
                operand = data[offset]
                offset += 1
                instructions.append(
                    TinyInstruction(
                        offset=ins_offset,
                        opcode={OP_CONST: "CONST", OP_JUMP: "JUMP", OP_JUMP_IF_ZERO: "JUMP_IF_ZERO"}[opcode],
                        size=2,
                        operands=(operand,),
                        raw=f"{ins_offset:04x}: ...",
                    )
                )
            elif opcode == OP_ADD:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="ADD", size=1, raw=f"{ins_offset:04x}: ..."))
            elif opcode == OP_RETURN:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="RETURN", size=1, raw=f"{ins_offset:04x}: ..."))
            elif opcode == OP_NOP:
                instructions.append(TinyInstruction(offset=ins_offset, opcode="NOP", size=1, raw=f"{ins_offset:04x}: ..."))
            else:
                instructions.append(TinyInstruction(offset=ins_offset, opcode=f"OP_{opcode:02X}", size=1, raw=f"{ins_offset:04x}: ..."))

        functions.append(
            TinyFunction(
                name=name,
                offset=instructions[0].offset if instructions else offset,
                instructions=tuple(instructions),
            )
        )

    return TinyProgram(filename=filename, version=version, functions=tuple(functions))
```
A minimal input sample:
```text
54 56 4d 31 01 01 04 6d 61 69 6e 04 00 01 02 01 03 02 03
```
The meaning of this string of bytes is:

- `TVM1` magic
- version = `1`
- function_count = `1`
- function name = `main`
- instruction_count = `4`
- instructions = `CONST 2`, `CONST 3`, `ADD`, `RETURN`

The expected output of this sample only needs to be understood as "linear calculation and return", for example:
```text
function main() {
    return 2 + 3
}
```
If the backend further performs constant folding, it may also be displayed as `return 5`. The concern here is whether the control flow and stack semantics are correct, not whether the final text retains the intermediate constant form.

`tiny_vm_frontend/plugin.py`:
```python
from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendVersionSupport

from .decoder import decode_tiny_vm, looks_like_tiny_vm
from .lifter import lift_program


class TinyStackVmFrontendPlugin:
    id = "tiny-stack-vm"
    display_name = "Tiny Stack VM"
    supported_inputs = (".tvm",)
    version_support = FrontendVersionSupport(
        family="tiny-stack-vm",
        versions=("1",),
        parser="tiny-stack-vm decoder 1",
        status="experimental",
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_tiny_vm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        try:
            program = decode_tiny_vm(data, filename)
        except ValueError as error:
            raise FrontendDecodeError(str(error)) from error
        return FrontendModule(
            frontend_id=self.id,
            payload=program,
            metadata={
                "filename": filename,
                "format": self.id,
                "version": program.version,
                "diagnostics": program.diagnostics,
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(module.frontend_id)
        return lift_program(module.payload, module.metadata)
```

`tiny_vm_frontend/lifter.py`：

```python
from __future__ import annotations

from unidecompiler.core.effects import Binary, Push, ReturnTop, UnknownOpcode
from unidecompiler.core.ir import BinaryOp, Const, SourceRef
from unidecompiler.core.vm_bytecode import VMBytecodeStep
from unidecompiler.core.vm_effect_table import VMEffectTable
from unidecompiler.core.vm_function import VMFunctionSpec, lift_steps, lift_vm_step_function, recover_vm_function
from unidecompiler.core.vm_hints import VMHint
from unidecompiler.core.vm_module import assemble_vm_module
from unidecompiler.core.vm_operands import VMDecodedInstruction, VMOperand
from unidecompiler.core.vm_region import VMLinearState, VMRegionOpcodeClasses, VMStatefulCallbacks, build_hint_region_profile

from .model import TinyFunction, TinyInstruction, TinyProgram

FRONTEND_ID = "tiny-stack-vm"
CONTROL = frozenset({"JUMP", "JUMP_IF_ZERO"})
CONDITIONAL = frozenset({"JUMP_IF_ZERO"})


EFFECTS = VMEffectTable(
    opcode_attr="opcode",
    ignored=frozenset({"NOP"}),
    exact={
        "CONST": lambda _ctx, ins, src: (Push(source=src, value=Const(source=src, value=int(ins.operands[0]))),),
        "ADD": lambda _ctx, _ins, src: (Binary(source=src, op="+", semantics="static"),),
        "RETURN": lambda _ctx, _ins, src: (ReturnTop(source=src),),
    },
    fallback=lambda _ctx, ins, src: (UnknownOpcode(source=src, opcode=ins.opcode, raw=ins.raw),),
)

REGION_CLASSES = VMRegionOpcodeClasses(
    noise=frozenset({"NOP"}),
    control=CONTROL,
    jumps=CONTROL,
    forward_jumps=CONTROL,
    backward_jumps=CONTROL,
    conditional_jumps=CONDITIONAL,
)


def raw_window(instructions: tuple[TinyInstruction, ...], index: int) -> tuple[str, ...]:
    start = max(0, index - 2)
    end = min(len(instructions), index + 3)
    return tuple(ins.raw for ins in instructions[start:end])


def immediate_targets(function: TinyFunction) -> dict[int, int]:
    valid_offsets = {ins.offset for ins in function.instructions}
    targets: dict[int, int] = {}
    for ins in function.instructions:
        if ins.opcode in CONTROL:
            target = int(ins.operands[0])
            if target in valid_offsets:
                targets[ins.offset] = target
    return targets


def branch_stack_width(instruction) -> int:
    if instruction.opcode == "JUMP_IF_ZERO":
        return 1
    return 0


def branch_condition(branch, stack):
    if branch.opcode != "JUMP_IF_ZERO" or not stack:
        return None
    condition = stack[-1]
    return BinaryOp(source=condition.source, op="==", left=condition, right=Const(value=0, source=condition.source))


def make_stateful_callbacks(program: TinyProgram, function: TinyFunction):
    def lift_linear(start, end, locals_, stack):
        steps = tuple(make_step(program, ins) for ins in function.instructions[start:end])
        result = lift_steps(steps, initial_locals=locals_, initial_stack=stack)
        if result.stopped_at is not None and result.state.terminator is None:
            return None
        return VMLinearState(
            locals=result.state.locals,
            stack=tuple(result.state.stack),
            statements=tuple(result.state.statements),
            terminator=result.state.terminator,
            stopped_at=(start + steps.index(result.stopped_at)) if result.stopped_at is not None else None,
        )

    return VMStatefulCallbacks(
        initial_locals=lambda: {},
        lift_linear=lift_linear,
        branch_condition=branch_condition,
        branch_stack_width=branch_stack_width,
    )
```
The `stopped_at` here just records which step the linear interpretation stops at, so that the core can continue to restore the control flow; it is not the signal itself of "reporting an error on failure".
```python
def make_step(program: TinyProgram, instruction: TinyInstruction, targets: dict[int, int] | None = None) -> VMBytecodeStep:
    source = SourceRef(frontend=FRONTEND_ID, offset=instruction.offset)
    operands = tuple(
        VMOperand(role="target" if instruction.opcode in CONTROL else "immediate", value=value, text=str(value))
        for value in instruction.operands
    )
    decoded = VMDecodedInstruction(opcode=instruction.opcode, source=source, operands=operands, raw=instruction.raw)
    hints: tuple[VMHint, ...] = ()
    if instruction.opcode in CONTROL and targets is not None and instruction.offset in targets:
        target = targets[instruction.offset]
        hints = (VMHint(kind="branch-target", source=source, target=target, flow="conditional" if instruction.opcode in CONDITIONAL else "unconditional"),)
        if instruction.opcode in CONDITIONAL:
            hints += (VMHint(kind="materialized-condition", source=source, detail="stack", flow="conditional"),)
    return VMBytecodeStep(opcode=instruction.opcode, source=source, decoded=decoded, raw=instruction.raw, effects=EFFECTS.effects_for(program, instruction, source), hints=hints)


def lift_function(function: TinyFunction, program: TinyProgram):
    targets = immediate_targets(function)
    steps = tuple(make_step(program, ins, targets) for ins in function.instructions)
    profile = build_hint_region_profile(steps, frontend=FRONTEND_ID, opcode_classes=REGION_CLASSES, raw_window=lambda index: raw_window(function.instructions, index))
    spec = VMFunctionSpec(name=function.name, params=function.params, frontend=FRONTEND_ID, instruction_count=len(steps), local_names=function.local_names)
    return recover_vm_function(
        spec,
        lambda: lift_vm_step_function(
            spec,
            steps,
            profile=profile,
            stateful_callbacks=make_stateful_callbacks(program, function),
            raw_window=lambda index: raw_window(function.instructions, index),
        ),
        raw=tuple(ins.raw for ins in function.instructions),
    )


def lift_program(program: TinyProgram, metadata):
    return assemble_vm_module(
        name=program.filename or "<tiny-stack-vm-program>",
        source_language=FRONTEND_ID,
        metadata={"frontend": metadata, "bytecode_format": FRONTEND_ID},
        functions=tuple(lift_function(function, program) for function in program.functions),
    )
```
The purpose of this appendix is not to teach you how to write Tiny VM, but to give you a minimal closed loop: decoder, plugin, effect table, control hints, stateful callbacks, and module assembly are all there. When you actually write your own frontend, you only replace the model and opcode semantics.

### 39.2 Add simulation to the minimal plug-in

The tiny VM in the previous section already has a stable function name `main`, definite parameter/return conventions and executable
generic IR, so simulation can be added without adding any new VM interpreter.

First add a line to the plugin class:
```python
from .simulation import TinyStackVmSimulationAdapter


class TinyStackVmFrontendPlugin:
    id = "tiny-stack-vm"
    # Keep the remaining attributes and methods unchanged。
    simulation_adapter = TinyStackVmSimulationAdapter
```
Added `tiny_vm_frontend/simulation.py`:
```python
"""Target lookup only; generic IR execution belongs to the shared simulator."""

from __future__ import annotations

class TinyStackVmSimulationAdapter:
    frontend_id = "tiny-stack-vm"

    def resolve_function(self, query, decoded_module, lifted_module):
        # Lazy import keeps normal decompilation independent of simulator install.
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str):
            return NotHandled
        matches = tuple(
            function
            for function in self._walk(lifted_module.functions)
            if function.name == query
        )
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], identifier=query)

    def list_simulation_targets(self, decoded_module, lifted_module):
        from unidecompiler_simulator import SimulationTargetCandidate

        functions = tuple(self._walk(lifted_module.functions))
        name_counts: dict[str, int] = {}
        for function in functions:
            name_counts[function.name] = name_counts.get(function.name, 0) + 1
        return tuple(
            SimulationTargetCandidate(function.name, function.name)
            for function in functions
            if name_counts[function.name] == 1
        )

    @staticmethod
    def _walk(functions):
        for function in functions:
            yield function
            yield from TinyStackVmSimulationAdapter._walk(function.nested_functions)
```
Added `tests/test_simulation.py`. It only uses the public registry/simulator API, so it can both
Verify the external directory plug-in registration, and also verify that the function returned by the adapter indeed belongs to the current `ModuleIR`:
```python
from __future__ import annotations

from pathlib import Path
import unittest

from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine, SimulationStatus


PLUGIN_DIRECTORY = Path(__file__).resolve().parents[1]
SAMPLE = bytes.fromhex(
    "54 56 4d 31 01 01 04 6d 61 69 6e 04 00 01 02 01 03 02 03"
)


class TinyVmSimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        registry = FrontendRegistry.discover()
        registry.register_directory(PLUGIN_DIRECTORY)
        self.simulator = SimulationEngine.from_registry(registry)

    def test_lists_and_executes_main(self) -> None:
        listing = self.simulator.list_artifact_targets(
            SAMPLE,
            "add.tvm",
            frontend_id="tiny-stack-vm",
        )
        self.assertEqual(listing.frontend_id, "tiny-stack-vm")
        self.assertIsNone(listing.diagnostic)
        self.assertEqual(tuple(target.label for target in listing.targets), ("main",))

        result = self.simulator.simulate_artifact(
            SAMPLE,
            "add.tvm",
            listing.targets[0].query,
            frontend_id="tiny-stack-vm",
        )
        self.assertIs(result.status, SimulationStatus.COMPLETED)
        self.assertEqual(result.values, (5,))
        self.assertIsNone(result.exception)
        self.assertIsNone(result.diagnostic)
        self.assertGreater(result.steps, 0)

    def test_rejects_unknown_query(self) -> None:
        result = self.simulator.simulate_artifact(
            SAMPLE,
            "add.tvm",
            "missing",
            frontend_id="tiny-stack-vm",
        )
        self.assertIs(result.status, SimulationStatus.INVALID_REQUEST)
        self.assertIsNotNone(result.diagnostic)


if __name__ == "__main__":
    unittest.main()
```
For VMs with external calls, write an additional environment test. The following example assumes that your generic
IR generates `Call(Global("write_text"), ..., returns=0)`; the function name must be replaced with yours
frontend actual generated name:
```python
from unidecompiler_simulator import (
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
    SimulationStatus,
)


class RecordingEnvironment:
    def __init__(self) -> None:
        self.calls = []

    def call(self, request):
        self.calls.append((request.name, request.args))
        if request.name != "write_text":
            return NotHandled
        return ExternalCallResult(
            ExternalCallStatus.RETURNED,
            values=(),
            stdout=str(request.args[0]),
        )


environment = RecordingEnvironment()
result = simulator.simulate_artifact(
    artifact_data,
    "writes.tvm",
    "main",
    frontend_id="tiny-stack-vm",
    environment=environment,
)
assert result.status is SimulationStatus.COMPLETED
assert environment.calls == [("write_text", ("hello",))]
assert any(event.stdout == "hello" for event in result.events)
```
When making external calls with mutable state (buffer, heap, file descriptor), do not
replace VM semantics with a local list inside `RecordingEnvironment`. Put the state in
a controlled runtime object and test allocation, reads, writes, out-of-bounds access,
input exhaustion, and reset-per-run behavior according to the template in Section 20.1.4.3.

Final minimum acceptance order:
```bash
python -m py_compile tiny_vm_frontend/*.py
python -m unittest discover -v -s tests
```
After passing, go to the GUI to register the plug-in directory, open the sample, and confirm on the Simulation page:

1. Frontend displays `tiny-stack-vm`;
2. `main` appears in the Target drop-down box;
3. After clicking Run, the status is `completed` and the returned value is `5`;
4. If the runtime has stdout, the corresponding text will appear in the Output column;
5. The result of clicking Run again does not depend on the previous Run.

## 40. When does core need to be changed?

Frontend cannot bypass core, but some VMs do require core extensions.

You should consider changing core:

- Multiple VMs require the same new effect.
- Existing hints cannot express certain kinds of general control flow facts.
- low-level CFG preserves semantics, but core cannot safely structure common shapes.
- The backend already has structure nodes, but the core has no recovery pass.

Situations where frontend should not be changed:

- Handwriting AST to make a sample appear as `while`.
- Remove complex opcodes in order to skip unsupported.
- Lose control edge for GUI to look good.
- Write business exceptions so that the current input is readable.

If core cannot be changed, the correct bottom line is:
```text
retain the complete decoded instruction
submit every effect/hint that can be determined
emit partial or low-level if/goto
do not emit misleading linear pseudocode
```
