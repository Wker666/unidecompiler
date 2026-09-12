# Agent Development Rules

## Requested Feature

__USER_REQUIREMENTS__

This requirement does not override the architecture contract below.

If simulation is enabled, the adapter must remain independent of frontend
bytecode execution and must resolve only generic functions in the lifted module.

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

The full recovery pipeline is a semantics-preserving core fixed point:

```txt
VM bytecode -> thin IR -> generic IR / low-level CFG
                              |
                              v
                    CFG structuring (if/while/branch)
                              |
                              v
                    structured FunctionIR refinement
                              |
                +-------------+-------------+
                |                           |
             changed                     stable
                |                           |
                +--> CFG analysis/structuring ↺
                                            |
                                            v
                                  final AST -> pseudocode
```

The refinement loop is owned by core. It may simplify structured `FunctionIR`
expressions and statements, then re-run generic CFG analysis and structuring
when a verified rewrite changes the recoverable shape. It repeats until no
safe rewrite remains. The final `FunctionDecl` AST is emitted only after this
fixed point; backends render it and never perform CFG recovery. This is a
structured-`FunctionIR` refinement boundary, not an implicit or lossy
`FunctionDecl`-to-CFG conversion.

Core CFG facts are edge-aware and tied to an immutable CFG snapshot. Concrete
parallel edges retain deterministic identities and ordinals; incoming and
outgoing edge queries must not collapse them by source/target/kind. Shared
analysis snapshots expose dominators, postdominators, dominance frontiers,
grouped loop facts, and irreducible-entry edges to VM-neutral structuring
passes. A rewrite such as Phi cleanup or fallthrough-jump removal is valid only
when exact predecessor edges, exception state, and data-flow equivalence are
proved. When those facts are unavailable, retain the low-level CFG/goto form.

The generic `Phi` value retains its logical incoming labels and may additionally
carry one concrete CFG `edge_id` per incoming value. Parallel predecessor edges
must use these edge identities; they must never be collapsed into a dictionary
keyed only by block ID. CFG lookup maps are read-only snapshots and preserve the
declared block identity sequence for duplicate-ID diagnostics.

The shared CFG validator also checks entry and target existence, contiguous
parallel-edge ordinals, exception-edge provenance, and malformed edge fields.
Accepted core rewrites record auditable `recovery_proofs` metadata containing
the rule, concrete block/edge IDs, CFG snapshot key, and raw context; rejected
candidates record `recovery_rejections`. These records are diagnostics only and
must not be consumed by a frontend or backend as control-flow instructions.
Region reduction has a deterministic fixed-point budget and repeated-state
diagnostic. It always returns the last verified function when a candidate is
rejected, a state repeats, or the budget is exhausted.

Useful source metadata should flow through the pipeline when available,
including source filenames, bytecode versions, constants, debug tables,
instruction offsets, line info, local variable info, upvalue or member names,
and frontend diagnostics. This metadata is provenance and analysis context; it
must not become a reason for core to depend on frontend-private decoded models.
Metadata is pass-through context only. It must not express program logic,
control flow, recovery decisions, or source-language semantics.

When the decoder can prove an instruction's exact position in the complete
input artifact, it may attach a `ByteRange(start, size)` to the public decoded
instruction. `SourceRef.offset` remains the VM control-flow coordinate; a
`ByteRange` is only absolute, read-only provenance for Structure/Hex views.
Never derive a range from a VM PC, RVA, instruction index, or a byte search.
When the conversion is not provable, leave the range unset.

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

Host-only concerns stay outside the core package. `unidecompiler.progress`
provides optional immutable progress events; it is observational and must not
affect recovery. The separate `unidecompiler-export` package owns pseudocode
file export and starter-project generation for VM frontends and GUI plugins.
Neither capability may be implemented in a frontend, backend, CFG pass, or
`DecompilerEngine` recovery decision.

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

## Symbolic Execution Contract

Symbolic execution is an optional consumer of recovered generic IR. Its
dependency direction is:

```txt
frontend -> core generic IR <- simulator / symbolic <- CLI / GUI / hosts
```

`unidecompiler-symbolic` may reuse the simulator's public artifact-preparation
boundary (`prepare_artifact_target`) to obtain a module and resolve a
frontend-owned opaque query, but it owns path states, constraints, solver
interaction, limits, and diagnostics. It must execute only `ModuleIR` and
`FunctionIR`; it must never read frontend bytecode, decoded models, VM opcodes,
effect tables, thin IR, simulator frames, or executable adapter callbacks.

Frontend simulation adapters remain optional and data-only. They may enumerate
targets, resolve a query to a `FunctionIR` in the current lifted module, and
provide narrow runtime facts. They must not implement symbolic evaluation,
path exploration, VM interpretation, or language-specific execution. CLI and
GUI pass queries through as opaque data and only render public symbolic
results.

`SymbolicResult` must make every outcome explicit: completed paths, raised
values, unsupported IR or runtime facts, invalid requests, path/step/loop/call
depth limits, solver timeouts, and cancellation. A bounded exploration limit
or solver uncertainty is not success and must not be guessed away. Trace limits
may truncate diagnostics but must not change execution semantics. Current
support is intended for scalar values, branches, Phi/multiway control flow,
and bounded loops; unsupported containers, calls, or exception transfers must
remain contextual unsupported results until a sound generic-IR model exists.

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

## Generic Value Semantics Contract

Thin effects and generic IR must preserve observable value semantics, not only
stack depth. This contract applies to every frontend and to the simulator:

- A value with side effects is evaluated once. Stack copies, duplicates,
  unpacking, argument collection, and store-at-depth operations must preserve
  aliases without repeating the original expression.
- A value read before a local, global, captured variable, indirect reference,
  member, or item is mutated must retain its old value. Core owns any temporary
  materialization needed to enforce that ordering.
- Writes and deletes use neutral store/delete effects and generic IR targets for
  locals, globals, captured values, attributes, and items. A frontend must not
  approximate a decoded mutation by dropping operands.
- Container identity is semantic. `BuildArray(kind="tuple")` must remain a
  tuple through generic IR, AST, rendering, and simulation rather than becoming
  a list.
- Numeric operations carry their operator, static/dynamic semantics, numeric
  domain, bit width, and overflow policy. Core transformations must preserve all
  of those fields. Shared shift, rotate, and bitwise spellings belong in a
  VM-neutral operator normalization layer; a frontend must not implement `ror`,
  `rol`, `shl`, `shr`, or equivalent execution itself. The shared registry
  canonicalizes `shl` to `<<`, arithmetic `shr`/`sar` to `>>`, logical-right
  aliases to `>>>`, and rotate aliases to `rol`/`ror`; unknown spellings remain
  unchanged and must not be guessed.
- Calls may carry a descriptive `CallEffectSummary` with reads, writes, return
  arity, and possible raise/suspend/mutation behavior. It is analysis metadata
  only and never an executor. Unknown calls conservatively form a mutation
  barrier for deferred stack values.
- A Phi has one incoming value per concrete predecessor label. Duplicate labels
  are invalid and must be rejected rather than collapsed through a dictionary.
  Multiple Phi assignments at block entry use parallel-copy semantics.
- When a call shape, exception value, numeric domain, container kind, or write
  target cannot be represented safely, emit analyzable unsupported context or
  fail explicitly. Do not manufacture a default value or guessed operation.

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
- The symbolic executor must explore only recovered generic IR. It must not
  interpret frontend bytecode, thin IR, opcode tables, simulator frames, or
  language-specific semantics.
- Simulation adapters are optional and data-only. They must not expose function
  execution, instruction stepping, evaluation, frame/stack management, control
  flow recovery, or executable callback behavior.
- GUI and CLI must consume simulator/symbolic APIs and opaque frontend queries
  only; they must not implement frontend-specific target lookup, simulation
  semantics, solver behavior, or path exploration.
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
- CFG rewrites must use concrete edge identity and preserve parallel edges,
  exception edges, handler state, and observable ordering; never remove a Phi
  or jump by comparing block IDs or textual values alone.
- Reuse the shared VM-neutral CFG analysis and region-reducer seams. Do not
  introduce frontend-specific CFG matchers or duplicate graph algorithms in a
  backend or frontend.
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

## Documentation and Release Hygiene

`Readme.md`, this file, and `docs/NEW_VM_FRONTEND.md` are the repository's
canonical architecture and onboarding documents. Changes to frontend-facing
contracts must update all three. The generated frontend template's copied
`README.md`, `AGENTS.md`, and `docs/NEW_VM_FRONTEND.md` are maintained by
`unidecompiler-export` and must stay synchronized with the applicable core
contract. GUI-plugin template guidance must likewise remain synchronized with
the GUI SDK boundary.

The GUI's pseudocode export is host functionality, not frontend recovery:
single-result export writes one selected result, while all-result export writes
each open result with pseudocode to a user-selected directory. Export names
must use sanitized basenames, avoid overwriting existing files, and never
encode absolute source paths or private metadata.

The CLI exposes the same host export capability. `-o/--output` writes one
successful result, while `--output-dir` writes one collision-safe file per
successful artifact. `unidecompiler template frontend ...` and
`unidecompiler template gui_plugin ...` generate starter projects through
`unidecompiler-export`; template generation is atomic and never overwrites an
existing project directory. Frontend templates may explicitly opt into the
data-only simulator adapter (`--simulation`) and the AI analysis kit
(`--ai-guidance`); both are disabled by default, and AI inputs require explicit
paths and entry facts.
The CLI also provides `unidecompiler symbolic`; the GUI exposes a Symbolic tab.
Both consume the public symbolic API and must not implement path exploration or
frontend-specific target lookup.
The CLI's `--interactive`/`-i` template wizard is only an alternative way to
collect these export settings; it must call the same exporter and must not
contain decoding or recovery logic.

Before a release, run the full test suite and `git diff --check`, build every
package with both wheel and sdist (including `unidecompiler-all`), and run
`twine check` on every artifact. Inspect archives for credentials, machine
paths, private source, test corpora, and untracked build output. Build
directories and `dist/` files stay ignored and are not committed. GitHub push
and PyPI upload are explicit maintainer actions and must never be automated by
an agent without a direct request.

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

If a frontend advertises symbolic execution, it must additionally:

1. Reuse the data-only target discovery and resolution contract; do not add a
   symbolic interpreter to the frontend.
2. Ensure advertised targets lift to generic IR expressions, statements,
   terminators, and parameter metadata that the symbolic package can validate.
3. Add focused tests for branch feasibility, models/returns, unsupported IR,
   and every configured exploration limit.

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
- CLI and GUI symbolic integration tests consume only `SymbolicResult`, preserve
  opaque target selection, and expose path constraints, models, returns/raises,
  unsupported results, limits, solver timeouts, and cancellation visibly.
- GUI plugin tests cover SDK isolation, manifest validation, safe archive
  extraction, and enabled/disabled lifecycle behavior.
- Every public thin effect and every generic IR expression, statement, and
  terminator family must have executable coverage. Coverage should be measured
  by runtime instrumentation or an equivalent behavior check, not by scanning
  class names in source.
- Stack/value effects require differential or model-based tests for ordering,
  aliasing, underflow, and mutation barriers. Numeric tests must cover canonical
  operators and aliases, widths, signedness, overflow policies, and zero/limit
  cases.

Run:

```sh
.venv/bin/python -m pytest -q
```
