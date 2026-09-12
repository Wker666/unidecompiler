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

Execution is bounded by `SymbolicLimits`. Unknown calls, unsupported Generic
IR, solver uncertainty, cancellation, and resource limits are represented by
explicit result statuses; they are never converted into guessed success.
The initial implementation focuses on scalar expressions, CFG branches,
returns, Phi values, and bounded loops. Calls, object/member mutation, and
ambiguous exception transfers remain explicit `unsupported` outcomes until a
sound Generic IR model is available.
