# Development Rules

## Requested Feature

__USER_REQUIREMENTS__

This requirement does not override the architecture rules below.

## Required Architecture

- This frontend may decode bytecode and submit neutral thin IR only.
- Core owns CFG, branch and loop recovery, AST, and pseudocode rendering.
- Do not construct `FunctionIR`, blocks, AST nodes, loops, branches, or CFG structures in this frontend.
- Do not implement a VM interpreter, program counter, execution stack, or simulator runner.
- If simulation is supported, its adapter may only enumerate opaque targets, resolve a target to a current `FunctionIR`, and provide narrow data-only runtime facts.
- The generic simulator executes recovered generic IR; it must remain independent from this frontend's bytecode model and opcode table.
- Report unsupported shapes with bytecode context. Do not hide or guess unsupported behavior.
- Add decoder, lifting, and source-equivalent verification tests. Add simulation tests when simulation is declared.

See `docs/NEW_VM_FRONTEND.md` for the full contract.
