---
name: vm-frontend-development
description: Build or adapt a thin unidecompiler VM frontend from an interpreter/runtime, a real bytecode sample, and its caller or entry-point information.
---

# VM Frontend Development

Use this skill when a user wants to add support for a VM or packed bytecode format to
unidecompiler. The user should not need to write an opcode manual or design the
frontend architecture. Ask for, or locate, these three inputs:

- VM interpreter/runtime source: the dispatch loop, opcode handlers, stack/register
  operations, calls, returns, exceptions, and closure construction.
- At least one real bytecode sample.
- The real caller and entry information: the code that invokes the VM, the initial
  cursor/base, arguments, and any pre-execution setup.

If one of these is missing, inspect the workspace for it before asking the user.
Do not infer an entry solely from a likely-looking integer or opcode.

When this skill is used from a generated project, first read the project's
`AGENTS.md`, `analysis_inputs/manifest.json`, and `docs/AI_CONTEXT.md` (when AI
guidance is enabled). When working inside the monorepo, also read the
repository-level `AGENTS.md`; a standalone generated project normally has no
separate core `AGENTS.md`.
The copied interpreter and bytecode are untrusted,
static reference data: do not execute, import, or send them to a remote service.
The manifest records project-relative paths, sizes, and SHA-256 hashes; never
restore or record the original machine paths.

## Boundaries

The frontend is an adapter, not a second decompiler or VM implementation.

- Frontend code may parse the format, decode instructions, map operands, emit
  effects, and submit neutral `VMHint` facts.
- Core owns CFG construction, stack recovery, exception edges, loops, AST, and
  pseudocode rendering.
- Do not construct `FunctionIR`, `BasicBlock`, CFG, AST, or source structures in
  the frontend.
- Do not implement a program counter, execution stack, VM interpreter, or
  frontend-specific simulator.
- Keep hints and effects VM-neutral. Do not add sample-specific offsets or
  business-logic rules to core.
- Preserve explicit unsupported diagnostics when a fact cannot be proved; never
  delete a hint, fabricate an empty effect, or reinterpret an exception as an
  ordinary branch just to improve a status count.
- Treat CFG edges as concrete facts. Parallel edges may share source, target,
  and kind but remain distinct; never deduplicate them or infer a Phi/jump
  rewrite from block IDs or equal-looking text.
- CFG structuring and refinement belong to core. Submit branch, loop,
  exception, and handler hints and rely on core's shared edge-aware analysis;
  do not add frontend graph algorithms or source-structure recovery.
- __SIMULATION_GUIDANCE__
- Classify every semantic statement as `proven`, `inferred`, or `unresolved` and
  cite file-relative lines and public bytecode offsets. Treat the user-supplied
  entry as unverified until the caller/runtime evidence confirms it. Conflicts
  are blockers, not invitations to guess.

## Workflow

### 1. Read the local contract

Read the project `AGENTS.md` and the current `docs/NEW_VM_FRONTEND.md`. When
working inside the monorepo, also read the repository-level/core `AGENTS.md`.
Inspect the public APIs used by existing frontends:

```text
VMBytecodeStep / VMDecodedInstruction / VMOperand
VMHint
effects.py
lift_vm_step_function()
VMRegionProfile and VMStatefulCallbacks
```

Check the working tree before editing and preserve unrelated user changes.

### 2. Extract the VM execution model

From the runtime and caller, write down a small fact sheet before coding:

- How the opcode is fetched (`pc`, `pc++`, `a[++cursor]`, or another rule).
- Whether the sample is a flat stream, packed table, or has runtime mutation.
- Instruction arity and variable-length operand rules.
- Normal fallthrough and every branch-target formula.
- Function and closure entry formulas.
- Value stack, locals/registers, call arguments, and return behavior.
- Exception-handler stack behavior, including nested `ACTIVE` and `PROTECTED`
  contexts if the VM has them.
- Which runtime setup must be represented as decoded data rather than replayed.

Use public VM offsets consistently for `SourceRef.offset` and `VMHint.target`.
Do not compare internal instruction indexes with frontend offsets.

### 3. Scaffold the frontend

Use the repository's existing plugin template. The normal shape is:

```text
my-vm/
├── AGENTS.md
├── README.md
├── pyproject.toml
├── unidecompiler-plugin.toml
├── src/my_vm/
│   ├── __init__.py
│   ├── model.py
│   ├── decoder.py
│   ├── lifter.py
│   ├── plugin.py
│   └── support.py
└── tests/
    ├── test_decoder.py
    ├── test_lifter.py
    └── test_frontend.py
```

Keep decoder-private classes in `model.py`. `plugin.py` should only validate,
decode, and delegate lifting. Declare the entry point in `pyproject.toml` and
keep the external manifest consistent with it.

### 4. Implement decoding first

Build a deterministic decoder that records, for every executable instruction:

- public offset and size;
- opcode and decoded operands;
- raw text/provenance;
- branch, handler, and closure targets when the runtime proves them.

For packed tables, distinguish executable opcode cells from data cells and model
runtime table mutation as data or a separate neutral function when appropriate.
Never use address ordering as a substitute for control flow. Follow explicit
targets and establish function roots from the caller/runtime and closure rules.

### 5. Implement thin lifting

Map each decoded instruction to one `VMBytecodeStep` with neutral effects and
hints. Use effects for value/local/member/call/return behavior; use hints for
branch targets, loop backedges, exception regions, handler pushes/pops, and
exception-edge state. Do not build blocks or source structures.

For exceptions, submit facts per proven context. A shared instruction may be
reached both protected and unprotected, so a handler-scoped
`exception-edge-state` must not become a global assertion. Conditional handler
pops should be used when a clone may not contain the selected frame. Preserve
hint order when one instruction replaces an active frame and pushes a protected
frame.

### 6. Add tests before broad runs

Create small synthetic programs from the runtime semantics before relying on a
large sample. At minimum cover:

- sequential effects and returns;
- each branch-target formula;
- closure target and captured arguments;
- a shared potentially-throwing instruction on protected and unprotected paths;
- conditional handler pop with and without a matching frame;
- nested handler entry/pop/push behavior;
- malformed or unprovable facts producing explicit diagnostics.

Then add a real-sample integration test that checks function discovery, status,
rendered output, and important semantic markers.

__SIMULATION_DELIVERABLE__

### 7. Verify in layers

Run focused tests first, then the full frontend suite. Create an isolated
verification environment and install only the packages needed for import
checks. Never execute the copied interpreter or bytecode during verification.
Verify the imported paths, not only package metadata:

```bash
python -m venv .venv
./.venv/bin/python -m pip install unidecompiler-all
python -m pip install --no-deps -e ./my-vm
python -m pip check
python -m pytest -q my-vm/tests
python my-vm/scripts/audit_sample.py path/to/sample.vm
unidecompiler --versions
unidecompiler path/to/sample.vm --frontend my-vm > artifacts/my-vm-final.txt
```

Inspect the audit and CLI output for:

- all intended functions recovered as `ok`;
- no accidental `unsupported` or `partial` text in the target coverage set;
- no missing executable/effect/control offsets or closure targets;
- deterministic output and preserved semantic markers.

For generated projects, keep the analysis inputs and documentation local. Do
not install this skill globally, do not execute user-provided runtime code, and
do not place credentials, absolute paths, or hidden model reasoning in source,
reports, tests, or commits. If AI assistance is unavailable, continue with
ordinary static frontend development and leave unresolved facts explicit.

Also verify both discovery paths when applicable:

```python
from pathlib import Path
from unidecompiler.plugin_registry import FrontendRegistry

installed = FrontendRegistry.discover()
external = FrontendRegistry()
external.register_directory(Path("my-vm"))
```

## Stopping conditions

Stop and report the blocker instead of changing core or guessing when:

- the runtime/caller does not establish a trustworthy entry or target formula;
- the published core lacks a required public neutral fact or has incorrect
  contextual exception semantics;
- the sample depends on missing generated/decryption/runtime state;
- a target remains unsupported because its semantics cannot be proved.

Report the exact missing input, API, or diagnostic and the command used to verify
it. Leave all changes uncommitted unless the user explicitly requests otherwise.
