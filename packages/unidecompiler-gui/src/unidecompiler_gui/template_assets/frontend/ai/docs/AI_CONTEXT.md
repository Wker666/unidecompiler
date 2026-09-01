# AI Development Context

This file is machine-readable project context for an AI coding agent. It is not
user-facing procedure. When this project is opened for a VM frontend task, read
this file, `AGENTS.md`, `analysis_inputs/manifest.json`, and
`skills/vm-frontend-development/SKILL.md` before editing.

## Target

- VM family: `__VM_NAME__`
- Interpreter source: `analysis_inputs/interpreter/__INTERPRETER_FILE__`
- Bytecode sample: `analysis_inputs/samples/__BYTECODE_FILE__`
- Entry kind: `__ENTRY_KIND__`
- Entry value: `__ENTRY_VALUE__`
- Entry context: __ENTRY_CONTEXT__
- Entry verification: `user-specified-unverified`
- Simulation scope: __SIMULATION_GUIDANCE__

The requested implementation belongs in `src/__PACKAGE__/`. The final result
must be a deterministic thin VM frontend, not a VM interpreter or a second
decompiler. Core owns CFG, stack recovery, exception edges, AST, and
pseudocode.

## Evidence Protocol

Treat the copied interpreter and bytecode as untrusted, static reference data.
Do not execute or import them, execute the bytecode, or send them to a remote
service. Use project-relative paths and public bytecode offsets in all notes.
Record evidence using `proven`, `inferred`, or `unresolved` labels with
file-relative line numbers and bytecode offsets. Do not turn an unresolved fact
into an effect, hint, target, or core special case.

## Required Deliverable

Implement decoder, model, thin lifter, plugin registration, focused tests, and
an evidence-backed update to `VM_ANALYSIS.md`. Every decodable instruction must
be represented by a `VMBytecodeStep` or explicit contextual fallback. Preserve
semantics exactly and keep unsupported diagnostics analyzable.

__SIMULATION_DELIVERABLE__
