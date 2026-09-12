# unidecompiler-symbolic

Bounded symbolic execution for recovered `unidecompiler` generic IR. The
package depends on `unidecompiler-simulator` for the public Generic IR/runtime
boundary, but owns symbolic states, path exploration, constraints, models,
limits, and diagnostics. It never executes frontend bytecode or simulator
private frames.

```python
from unidecompiler_symbolic import SymbolicEngine, SymbolicInput

result = SymbolicEngine().explore_function(
    module,
    function,
    symbolic_inputs=(SymbolicInput("x"),),
)
for path in result.paths:
    print(path.status, path.model, path.returns, path.constraints)
```

For a decoded artifact, construct the engine from the host registry and pass a
frontend-owned opaque target query. Target selection is performed by the
frontend's data-only simulation adapter; the symbolic package receives only
the resulting `ModuleIR` and `FunctionIR`:

```python
result = SymbolicEngine.from_registry(registry).explore_artifact(
    data,
    "sample.pyc",
    query="choose",
    symbolic_inputs=(SymbolicInput("value"),),
)
```

Execution is bounded by `SymbolicLimits`. Unknown calls, unsupported Generic
IR, solver uncertainty, cancellation, and resource limits are represented by
explicit result statuses; they are never converted into guessed success.
The initial implementation focuses on scalar expressions, CFG branches,
returns, exact CFG-edge Phi values, multiway branches, and bounded loops. Calls, object/member mutation, and
ambiguous exception transfers remain explicit `unsupported` outcomes until a
sound Generic IR model is available.
