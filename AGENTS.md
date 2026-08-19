# Agent Development Rules

This file defines mandatory development rules for `unidecompiler`.

The project is built around one hard architectural rule: VM frontends are thin
submitters, and the core owns recovery.

## Architecture Contract

The decompiler is split into three layers:

1. VM frontends parse bytecode formats and submit neutral thin IR.
2. Core lifts thin VM steps, effects, hints, regions, CFG-like control flow, and
   recoverable structures.
3. Backends render the recovered generic IR into pseudocode.

The frontend pipeline is:

1. Decode the VM bytecode with the frontend's format decoder.
2. Convert each decoded instruction into a `VMBytecodeStep`.
3. Attach neutral operands, opcode classes, hints, and effect-table results.
4. Submit the complete step stream through `lift_vm_step_function`.
5. Let core produce full, partial, or unsupported generic IR.
6. Treat any unsupported result in an intended coverage path as a defect to
   eliminate, not an acceptable development endpoint.

The full recovery pipeline is:

```txt
VM bytecode -> thin IR -> generic IR -> SSA/analysis -> AST -> pseudocode
```

Useful source metadata should flow through the pipeline when available,
including source filenames, bytecode versions, constants, debug tables,
instruction offsets, line info, local variable info, upvalue or member names,
and frontend diagnostics. This metadata is provenance and analysis context; it
must not become a reason for core to depend on frontend-private decoded models.
Metadata is pass-through context only. It must not express program logic,
control flow, recovery decisions, or source-language semantics.

Here, `unsupported` means the core could not safely recover the current stack
shape, control-flow shape, or IR combination, so it must emit an explicit
fallback instead of guessing.
This fallback exists only as a safety valve. During active development, any
`unsupported` produced for a supported or intended-to-be-supported shape must be
treated as a bug and removed before the change is considered done.
An `unsupported` result must include enough bytecode context to analyze the
failure: the relevant instruction window, raw opcode text when available,
decoded operands, branch targets or region hints, and the reason recovery
stopped.
If even that fallback cannot be expressed safely, the core should raise an
explicit error rather than emitting misleading pseudocode.

Python `.pyc`, JVM `.class`, Lua chunks, .NET CLI assemblies, and WebAssembly
modules follow this model. JVM class reading uses the `jawa` library and must
not shell out to `javap`. .NET/C# assembly reading uses the `dnfile` library and
must not shell out to disassembly tools. WASM reading uses library validation
and instruction decoding (`wasmtime` + `wasm`) before submitting thin operators.

The stress corpus is organized as `opcode_projects/source/<project>` and
`opcode_projects/generate/<project>`. Generated and source stress projects are
local working data; they are scanned by path rather than imported as a Python
test package.

## Simulation Architecture Contract

Simulation is a separate, optional consumer of recovered generic IR. Its
architectural boundary is as strict as the frontend/core boundary: preserving
this decoupling is mandatory.

The simulation dependency direction is:

```txt
frontend -> core generic IR <- simulator <- CLI / GUI / other application hosts
```

`core` must not import, depend on, or know about the simulator. The simulator
may depend on public generic IR, but it must not execute frontend bytecode,
decoded frontend models, VM opcodes, opcode effect tables, or thin IR effects.
It owns generic-IR frames, calls, control flow, limits, exceptions,
cancellation, and execution tracing. It must not branch on a frontend ID or
contain language-specific execution behavior.

A frontend may optionally expose a `simulation_adapter`. Unsupported simulation
is valid. A frontend that opts in may only:

- enumerate presentation-safe, data-only target queries;
- resolve a frontend-owned query to a `FunctionIR` belonging to the current
  lifted `ModuleIR`, including frontend-specific ambiguity handling; and
- supply narrow, data-only runtime facts when generic IR cannot express them.

Simulation adapters must not execute or interpret functions or instructions,
maintain frames or stacks, recover control flow, inspect simulator internals, or
return executable callbacks. The simulator validates that a resolved function
belongs to the current lifted module and retains sole ownership of execution.
CLI and GUI treat frontend target queries as opaque data; they must not infer
language-specific function names, overload resolution, class/member lookup, or
dynamic call targets.

Unresolved named calls may be delegated only through the data-only
`ExternalEnvironment` protocol. An environment receives an
`ExternalCallRequest` and returns an `ExternalCallResult`; it must never
receive generic IR, frames, adapters, or execution control. Its inputs and
outputs must be validated generic runtime values. A runtime file such as
`runtime.py` is explicitly selected, trusted host code: it is not sandboxed and
its loading belongs in a host-support package, never in core, the simulator, or
a frontend.

Every simulation outcome must be represented by a structured
`SimulationResult`. Completed execution, raised values, unsupported IR,
unhandled external calls, invalid requests, limits, and cancellation must never
be silently converted into success or guessed behavior. Trace limits may
truncate recorded events, but must not alter execution semantics.

## GUI Plugin Architecture Contract

GUI plugins are trusted, optional application extensions. They are not VM
frontends, simulation adapters, backends, or core extensions. The dependency
direction is strictly one-way:

```txt
GUI plugin -> unidecompiler-gui-sdk -> GUI plugin host -> public GUI/core/simulator APIs
```

The SDK remains a small, versioned, GUI-toolkit-neutral data contract. Plugins
receive immutable document/function/AST/reference/selection/job snapshots and
may register commands or declarative panels, request navigation, subscribe to
host events, and submit asynchronous simulation jobs using an opaque,
frontend-owned query. They must never receive decoded artifacts, frontends,
`ModuleIR`, `FunctionIR`, simulation adapters, runners, frames, stacks, Qt
widgets, or the Workbench.

Plugins are read-only and cannot modify input data, IR, AST, pseudocode,
frontend registrations, or simulation execution. The GUI host owns Qt,
threading, document revisions, navigation, and job lifecycle. Plugin panels
are data-only state, not plugin-provided widgets. Installation is GUI-host
infrastructure separate from the VM frontend registry. Plugins are trusted
in-process Python, are not sandboxed, and their dependencies are checked but
never installed automatically. Core, simulator, frontends, and backends must
never import the GUI SDK, plugin host, installer, or user plugins.

## Frontend Version Metadata

Each frontend owns a small version-support declaration that says which VM
versions or bytecode families it currently accepts.

This metadata lives with the frontend implementation, not in the core. The CLI
and registry only read and display it.

That means:

- adding Lua 5.1/5.4 support is a Lua frontend change;
- adding a newer JVM classfile range is a JVM frontend change;
- unsupported versions should be reported as unsupported or resource input,
  not handled by cross-language logic in core.

## Thin IR Contract

Thin IR is the adapter between any VM frontend and the generic core. It is not a
language AST.

Frontends may submit:

- `VMBytecodeStep`: opcode, source, decoded operands, raw text, effects, hints.
- `VMDecodedInstruction` and `VMOperand`: neutral instruction facts.
- `VMHint`: branch targets, loop backedges, and other VM-neutral facts.
- `Effect` values from effect tables: stack/local/member/value actions.
- `VMRegionOpcodeClasses`: local opcode classification mapped into generic
  categories.
- Low-level callbacks only when core needs to evaluate a linear VM slice.

Thin IR may be extended when a new VM exposes a repeated, cross-VM concept.
Allowed additions must describe common bytecode semantics, not a source-language
construct or one frontend's private recovery trick.
If the current thin IR cannot express a VM behavior cleanly, prefer adding a
new neutral thin IR fact, effect, hint, or operand concept over introducing
complex adaptation logic elsewhere.

## Hard Rules

These rules are mandatory.

- Frontends must not recover AST/source structures.
- Frontends must not build `if`, loop, match, block, CFG, or region structures.
- Frontends must not call structure constructors such as `vm_if`, `vm_while`,
  `vm_foreach`, or direct IR block/function assembly APIs.
- Frontends must not use `VMLiftTable` or `VMLiftRule` to choose recovery paths.
- Frontends must not reject complex shapes before submission just because a
  local linear lifter cannot handle them.
- Frontends must not inspect or depend on backend/core private recovery details.
- Frontends must not add special cases for one business corpus, one fixture, or
  one language feature.
- Core must not import or depend on simulator packages, simulation adapters, or
  host runtime implementations.
- The simulator must execute only recovered generic IR. It must not interpret
  frontend bytecode, thin IR, opcode tables, or language-specific semantics.
- Simulation adapters are optional and data-only. They must not expose function
  execution, instruction stepping, evaluation, frame/stack management, control
  flow recovery, or executable callback behavior.
- GUI and CLI must consume simulator APIs and opaque frontend queries only; they
  must not implement frontend-specific target lookup or simulation semantics.
- Runtime-file loading and other executable host integrations must remain
  outside core, simulator, and frontend packages. They are trusted host code,
  not a simulator sandbox.
- External environments must use the data-only environment protocol and must not
  receive IR, frames, adapters, or execution control.
- Metadata must only be passed through as provenance, diagnostics, and analysis
  context. It must not encode program logic or recovery behavior.
- Frontends must parse or represent every opcode they can decode and submit it
  to core as thin IR, effects, hints, or explicit unsupported context.
- If a VM needs a special adaptation, keep it inside that frontend only as
  decoding, operand mapping, opcode classification, hints, or thin effects.
- If the behavior has cross-VM meaning, add a neutral thin IR effect/hint/fact
  and recover it in core.
- If existing thin IR cannot express the behavior cleanly, extend thin IR as far
  as needed with neutral concepts instead of adding complex frontend adapters,
  backend exceptions, or core-side special-case glue.
- Backends must stay separable from core analysis. Do not hard-code pseudocode
  printer policy into generic IR or frontend recovery.
- Low-level CFG/goto structuring is forbidden in AST rendering, pseudocode
  backends, and frontend code. CFG pattern matching, loop recovery, branch
  recovery, and goto elimination must live only in VM-neutral core structuring
  passes.
- Rendering layers may only consume already-structured nodes. They must not
  inspect CFG edges, infer loops or branches, eliminate gotos, or make recovery
  decisions.
- Any structuring pass registry must remain VM-neutral, deterministic, and
  semantics-preserving. It must not become a frontend-specific rule escape
  hatch, a corpus-specific recovery table, or a way to bypass thin IR/core
  ownership.
- A structuring pass may replace low-level CFG/goto output only when it can
  preserve exactly the same code logic and has focused tests for the recovered
  shape.
- Core must preserve semantics where it can. When it cannot, it should degrade
  to partial or unsupported generic IR with raw context instead of moving logic
  back into a frontend.
- During active development, unsupported results are prohibited on the target
  coverage set; they must be driven to zero for the scenarios under development
  before the work is considered complete.
- Unsupported output must be analyzable. It must print the bytecode context that
  caused recovery to stop, including nearby instructions, raw opcode text when
  available, decoded operands, branch or region hints, and a concise unsupported
  reason.
- If core cannot even express a conservative unsupported fallback for the
  current shape, it should fail loudly with an explicit error.
- When recovery is within range, prefer normal structured output. If a proposed
  change would break the decompiler or distort semantics, core may fall back to
  low-level `goto`/CFG form as a safe floor. This fallback is explicitly
  allowed, but only as a preservation path: it must keep the code logic exact,
  emit explicit CFG edges and jumps, and must never become a reason to push
  structure recovery back into a frontend.
- `unsupported` and low-level `goto`/CFG output are future-proofing fallbacks,
  not acceptable development endpoints. During active development, all known
  `unsupported` cases must be resolved in core or represented by a shared thin
  IR concept. If replacing a `goto`/CFG fallback with structured output is
  low-cost, do it, but only when the resulting pseudocode preserves exactly the
  same code logic.

## Adding A New VM

A new VM frontend should only need to:

1. Decode its bytecode format.
2. Define opcode-to-effect table entries.
3. Define operand roles and raw instruction text.
4. Classify control opcodes into neutral region categories.
5. Emit branch/loop hints when targets are available.
6. Submit all instructions as `VMBytecodeStep` through `lift_vm_step_function`.

If the new VM cannot be recovered correctly after that, fix or extend the core
recovery layer, or add a shared thin IR concept. Do not solve it by adding
source-structure recovery to the frontend.

If a frontend elects to support simulation, it must additionally:

1. Enumerate unambiguous, data-only simulation targets.
2. Resolve every accepted query to a `FunctionIR` in the current lifted module.
3. Keep all lookup and runtime facts inside an optional, non-executing
   `simulation_adapter`.
4. Add focused tests for target discovery, query ambiguity, arguments and
   return values, and any frontend-specific runtime facts.

## Verification Guardrails

Unit tests are only the first verification layer. A change is not correct just
because the unit suite passes. After unit tests pass, compare real source code
under `opcode_projects/source` with decompiled output from
`opcode_projects/generate`; the change is accepted only when the recovered code
has the same logic, or when unsupported shapes are reported explicitly instead
of producing misleading pseudocode.

The test suite includes frontend-decoupling checks that enforce this design:

- VM frontends submit thin bytecode steps to core.
- VM frontends use effect tables for opcode submission.
- VM frontends do not register lift rules.
- VM frontends do not construct blocks, functions, or source structures directly.
- Core VM layers remain frontend-neutral.
- Simulator execution remains generic-IR-only and core has no simulator
  dependency.
- Simulation adapters remain optional, data-only, and cannot execute frontend
  instructions or return executable callbacks.
- Every simulation-enabled frontend is covered by `simulator_projects` with
  generated artifacts and source-equivalent execution checks. Coverage must
  include control flow, container/value operations, returns, and an external
  environment call or an explicit unhandled-call result.
- CLI and GUI integration tests consume only simulator results, preserve target
  selection, and expose completion, failure, cancellation, and trace-truncation
  outcomes visibly.
- GUI plugin tests cover SDK isolation, manifest validation, safe archive
  extraction, and enabled/disabled lifecycle behavior.

Run:

```sh
.venv/bin/python -m pytest -q
```
