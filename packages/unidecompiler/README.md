# unidecompiler

`unidecompiler` is the frontend-neutral core for universal bytecode
decompilation. It owns thin-IR lifting, recovery, generic IR, diagnostics, AST
generation, and the stable `DecompilerEngine` facade used by CLI, GUI, and
frontend plugin packages.

Frontend plugins decode VM-specific formats and submit neutral bytecode facts;
they do not perform source-structure recovery.

The core may also carry optional `ByteRange` provenance for exact, absolute
locations in the original input artifact. This is read-only presentation data
for hosts such as the GUI; it has no execution, control-flow, or recovery
semantics and is omitted whenever the decoder cannot prove the range.

Core recovery reaches a fixed point before the final AST is emitted:

```txt
thin IR -> generic IR / low-level CFG
                 |
                 v
          CFG structuring
                 |
                 v
  structured FunctionIR refinement
          |                 |
       changed            stable
          |                 |
          +--> CFG analysis/structuring ↺
                              |
                              v
                   final AST -> backend rendering
```

`recovery_refinement.py` owns the VM-neutral refinement loop. It accepts only
rewrites that preserve the verified CFG and safety invariants; otherwise the
existing low-level CFG/goto representation remains the preservation floor.
The loop operates on structured `FunctionIR` inside core. It is not a
frontend-specific pass, and backends do not infer or recover control flow.

CFG rewrites are edge-aware. Parallel edges retain deterministic concrete
identity, and shared CFG analysis snapshots provide graph facts for
structuring. Same-value Phi cleanup and explicit fallthrough-jump removal are
performed only when predecessor edges, exception state, data-flow values, and
evaluation order are proven equivalent. If that proof is unavailable, core
keeps the low-level CFG/goto form rather than guessing.

The generic effect and IR layers preserve single evaluation, stack aliases,
and values observed before later mutations. Writes and deletes have explicit
targets for locals, globals, captured values, attributes, and items. Numeric
operations retain domain, bit width, and wrapping or trapping overflow policy,
while container literals retain tuple/list identity. These facts survive SSA,
CFG rewriting, AST conversion, and backend rendering.

Calls may carry descriptive `CallEffectSummary` metadata for reads, writes,
return arity, and possible raise/suspend/mutation behavior. The core never
executes a summary. Unknown calls conservatively barrier deferred values, and
the shared pass manager records fixed-point budget or non-progress diagnostics
with available bytecode context.

## Install

Install the core library directly from PyPI. Cloning this repository is not
required for normal use:

```sh
python -m pip install unidecompiler
```

To decompile an artifact, also install a host such as `unidecompiler-cli` or
`unidecompiler-gui` and the frontend plugin for the bytecode format you need.
