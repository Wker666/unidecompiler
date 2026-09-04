# __DISPLAY_NAME__

__DESCRIPTION__

## Requested Feature

__USER_REQUIREMENTS__

## Development

Read `AGENTS.md` before implementation, then follow `docs/NEW_VM_FRONTEND.md`.
This project must submit VM-neutral thin IR to `unidecompiler`; core owns CFG,
structure recovery, AST, and pseudocode. Simulation support is optional and
must remain data-only at the frontend boundary.

After lifting, core runs a semantics-preserving recovery fixed point before
the final AST is emitted. It may refine structured `FunctionIR` and retry CFG
structuring, but it retains low-level CFG/goto when equivalence cannot be
proved. Backends do not perform recovery. See `docs/NEW_VM_FRONTEND.md` for
the complete frontend, version-support, provenance, and simulation contract.

CFG recovery is edge-aware: parallel edges keep their concrete identity, and
Phi or fallthrough-jump cleanup is safe only when core proves predecessor-edge,
exception-state, data-flow, and evaluation-order equivalence. Frontend code
must not inspect or simplify CFGs; submit all decoded control-flow facts and
let the shared core reducer decide whether a structured replacement is safe.

Effects must preserve more than stack depth. Use the neutral core effects for
local/global/member/item writes and deletes, stack copies, container kinds, and
fixed-width numeric operations. Record signedness, bit width, and wrap/trap
overflow policy when the VM defines them. Shared shift/rotate aliases such as
`shl`, `shr`, `rol`, and `ror` are core semantics; do not implement them in the
frontend or a simulation adapter. Add behavioral tests for aliasing,
pre-mutation values, numeric edge cases, and every submitted effect.

Calls may carry a descriptive core `CallEffectSummary` for reads, writes,
return arity, and raise/suspend/mutation facts. It is metadata, never an
executor. Unknown calls conservatively form mutation barriers for deferred
stack values. Core's pass manager owns fixed-point budgets and repeated-state
diagnostics; a frontend must not implement its own CFG recovery loop.
