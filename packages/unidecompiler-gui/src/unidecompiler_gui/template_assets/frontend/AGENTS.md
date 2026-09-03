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
- Core reaches a semantics-preserving recovery fixed point before AST emission;
  backends only render the result and never recover CFG or eliminate gotos.
- Keep exception edges and handler state intact when core cannot safely structure
  the normal CFG; low-level CFG/goto is the preservation floor.
- Core CFG edges are concrete and edge-aware, including deterministic identities
  for parallel edges. Do not deduplicate edges or remove Phi values/jumps by
  comparing block IDs or textual values alone.
- Reuse core's shared CFG analysis and reducer. Do not add frontend-specific
  CFG recovery, region matchers, or graph algorithms.
- Preserve observable values, not only stack height. Copies, duplicates,
  unpacking, calls, and stores must evaluate side-effecting inputs once and
  retain values read before a later mutation.
- Use neutral core effects for local/global/captured/member/item stores and
  deletes. Never replace a decoded mutation with an operand drop.
- Preserve tuple/list kind and all numeric semantics (`numeric_domain`,
  `bit_width`, and wrap/trap overflow policy). Core owns generic shift, rotate,
  and bitwise execution, including aliases such as `shl`, `shr`, `rol`, and
  `ror`; this frontend and its simulation adapter must not execute them.
- Test every submitted effect behaviorally, including aliasing, mutation
  barriers, numeric limits, and explicit unsupported outcomes.

See `docs/NEW_VM_FRONTEND.md` for the full contract.

In a generated project, if `docs/AI_CONTEXT.md` exists, it is AI-only project
context generated from user-selected reference artifacts. Read it before
implementing; do not treat its unverified entry or inferred facts as established
semantics.
