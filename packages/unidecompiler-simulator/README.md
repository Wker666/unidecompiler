# unidecompiler-simulator

`unidecompiler-simulator` is a library for bounded execution of
`unidecompiler` generic IR. It depends on the decompiler core but the core does
not depend on, import, or know about this package.

The simulator owns frames, control flow, calls, limits, exceptions, and the
execution trace. A frontend may expose an optional `simulation_adapter`
attribute for frontend-specific function lookup and runtime facts. The adapter
may answer individual operations, but it must not execute functions, interpret
instructions, recover control flow, or return executable callbacks.

Generic IR execution is available without a frontend:

```python
from unidecompiler_simulator import SimulationEngine

result = SimulationEngine().simulate_function(module, function, args=(1, 2))
```

Artifact execution gives an adapter the decoded frontend-owned payload only for
function lookup and runtime hooks:

```python
result = SimulationEngine().simulate_artifact(
    data,
    "sample.bytecode",
    query={"class": "Example", "method": "run"},
)
```

The `unidecompiler-cli` package hosts the command-line interface and accepts a
frontend-owned function query and JSON arguments:

```sh
unidecompiler simulate sample.bytecode --function 'Example.run' --args '[1, 2]'
```

For the Lua bubble-sort fixture, compile the source before invoking the
simulator. The Lua frontend adapter supplies only Lua function lookup and
runtime value facts; the simulator still executes the lifted generic IR.

```sh
luac -o bubble_sort.luac simulator_projects/source/arithmetic.lua
unidecompiler simulate bubble_sort.luac --frontend lua --function bubble_sort --args '[[5, 1, 4, 2, 8]]'
```

The completed result contains `"values": [[1, 2, 4, 5, 8]]`.

Built-in frontend adapters accept the following frontend-owned function queries:

| Frontend | Query |
| --- | --- |
| Python `.pyc` | Unique function name, for example `arithmetic` |
| JVM `.class` | Unique method name or `Class.method`, for example `Sample.add` |
| .NET assembly | Unique method name or `Type.Method`, for example `Probe.Add` |
| WebAssembly | Function name or `$funcN`, for example `add` or `$func0` |
| Lua chunk | Unique function name, for example `bubble_sort` |

Ambiguous queries are rejected by the relevant frontend adapter. The simulator
does not choose among overloads or perform frontend-specific name recovery.

Execution is bounded and in-memory. Unknown operations, unsupported IR, and
unsafe runtime behavior stop with a structured result instead of guessing.

Applications may inject an `ExternalEnvironment` for unresolved named calls.
The environment receives only `ExternalCallRequest` data and returns an
`ExternalCallResult`; it never receives IR, frames, adapters, or execution
control. Python-file loading is intentionally owned by `unidecompiler-cli`,
not this library.

The simulator does not resolve `Global` expressions as function names. Any
frontend-specific function lookup or dynamic call target must be provided by
the optional adapter and must resolve back to a `FunctionIR` owned by the
current lifted module.

The simulator intentionally does not execute frontend bytecode, frontend
opcode tables, or core `Effect` objects directly. Core is responsible for
lifting VM-neutral effects into generic IR; this package executes that generic
IR. Keeping that boundary strict prevents a language-specific opcode switch
from leaking into the simulator and keeps every frontend replaceable.
