# VM Analysis Worksheet

This document is generated from user-selected, local reference files. It is an
evidence worksheet, not an implementation and not an execution trace.

## Inputs

| Role | Project-relative path | Original filename |
| --- | --- | --- |
| Interpreter source | `analysis_inputs/interpreter/__INTERPRETER_FILE__` | `__INTERPRETER_FILE__` |
| Bytecode sample | `analysis_inputs/samples/__BYTECODE_FILE__` | `__BYTECODE_FILE__` |

The authoritative hashes, sizes, and source-independent provenance are in
`analysis_inputs/manifest.json`. Never add the original absolute paths.

## Entry Supplied By User

- Kind: `__ENTRY_KIND__`
- Value: `__ENTRY_VALUE__`
- Context: __ENTRY_CONTEXT__
- Verification state: `user-specified-unverified`
- Simulation scope: __SIMULATION_GUIDANCE__

## Required Evidence Pass

Before implementing, inspect the interpreter dispatch loop and the caller that
selects the entry. Record file-relative line numbers and bytecode offsets for:

1. Fetch and cursor/PC advancement.
2. Instruction width and operand decoding.
3. Fallthrough and every branch-target formula.
4. Function, closure, and captured-argument entry rules.
5. Value stack, locals/registers, calls, returns, and exceptions.
6. Runtime setup that must be represented as decoded metadata.

Classify each statement as `proven`, `inferred`, or `unresolved`; cite the
evidence for `proven` and `inferred` statements. Do not turn an unresolved fact
into an effect, hint, or core special case.

## Implementation Deliverable

Implement a deterministic decoder and a thin frontend in `src/__PACKAGE__/`.
Every decodable instruction must become a `VMBytecodeStep` with neutral
operands, effects, raw text, provenance, and VM-neutral hints. Core owns CFG,
stack recovery, exception edges, loops, AST, and pseudocode. The frontend must
not execute the VM or construct source structures.

Add focused tests for proven semantics and a real-sample verification. Keep
unsupported diagnostics explicit and contextual when semantics cannot be
proved. Verify that generated output has no accidental absolute paths or
credentials.

__SIMULATION_DELIVERABLE__
