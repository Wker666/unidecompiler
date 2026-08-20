# unidecompiler

A small universal bytecode decompiler experiment.

The project is built around one hard architectural rule: VM frontends are thin
submitters, and the core owns recovery.

## Purpose

unidecompiler is designed for authorized analysis of VM-SDK and bytecode
virtualization protection. Its goal is to provide one frontend-neutral recovery
pipeline for proprietary or custom virtual machines, including SDK-protected
applications where bytecode is the only practical analysis surface.

The Python, JVM, Lua, .NET CLI, and WebAssembly frontends in this repository
are reference implementations and regression coverage. They demonstrate the
thin-IR contract; they are not the boundary of the project. A new VM frontend
should decode its format and submit neutral bytecode facts while the core owns
control-flow recovery, AST construction, diagnostics, and rendering.

Use the project only for software you own or are authorized to analyze.

## Install

For normal use, install released packages with `pip`. You do not need to clone
this repository or build from source. Clone the repository only when developing,
testing, or contributing to unidecompiler.

For development rules and agent instructions, see `AGENTS.md`.

At a high level, the pipeline is:

```txt
VM bytecode -> thin IR -> generic IR -> SSA/analysis -> AST -> pseudocode
```

## Package Architecture

`unidecompiler` is an embeddable core library. It has no command-line entry
point, no bytecode format parser, and no dependency on a concrete frontend.
The repository is a Python package workspace: each distributable component
lives under `packages/` and can be installed independently.

- `unidecompiler`: generic IR, lifting, analysis, structuring, and backends.
- `unidecompiler-cli`: optional command-line host.
- `unidecompiler-gui`: read-only PySide6 workbench.
- `unidecompiler-gui-sdk`: stable, Qt-neutral API for trusted GUI plugins.
- `unidecompiler-simulator`: optional bounded executor for recovered generic IR.
- `unidecompiler-simulation-host-python`: trusted Python runtime host for
  applications that provide unresolved functions.
- `unidecompiler-plugin-*`: independently installable frontend adapters.
- `unidecompiler-all`: complete-installation meta-package.

The CLI and other hosts discover installed adapters through the
`unidecompiler.frontends` Python entry-point group. An embedding application
can instead create a `FrontendRegistry` from an explicit plugin collection.

## Current Architecture

The decompiler is split into three layers:

1. External VM frontend plugins parse bytecode formats and submit neutral thin IR.
2. Core lifts thin VM steps, effects, hints, regions, CFG-like control flow, and
   recoverable structures.
3. Backends render the recovered generic IR into pseudocode.

The current frontend pipeline is:

1. Decode the VM bytecode with the frontend's format decoder.
2. Convert each decoded instruction into a `VMBytecodeStep`.
3. Attach neutral operands, opcode classes, hints, and effect-table results.
4. Submit the complete step stream through `lift_vm_step_function`.
5. Let core produce full, partial, or unsupported generic IR.

### Simulation Architecture

Simulation is an optional consumer of recovered generic IR. It is deliberately
decoupled from both core recovery and frontend bytecode execution:

```txt
frontend -> core generic IR <- simulator <- CLI / GUI / embedding host
```

The core does not depend on the simulator, and the simulator does not execute
frontend bytecode, VM opcodes, effect tables, or frontend-private decoded
models. A frontend may optionally provide a data-only simulation adapter for
function lookup and runtime facts. Language-specific lookup, such as Lua names
or JVM class/method names, remains owned by that frontend.

The simulator returns structured results for completion, return values,
exceptions, unsupported operations, limits, cancellation, and execution trace.
See `packages/unidecompiler-simulator/README.md` for the public library API and
frontend query formats.

Supported frontend families follow this model:

- Python `.pyc`
- JVM `.class`
- Lua chunks
- .NET CLI assemblies
- WebAssembly modules

## Repository Layout

- `packages/unidecompiler/`: embeddable core package, using a standard `src/` layout.
- `packages/unidecompiler-cli/`: optional CLI host package.
- `packages/unidecompiler-gui/`: read-only desktop workbench package.
- `packages/unidecompiler-gui-sdk/`: versioned data contracts for GUI plugins.
- `packages/unidecompiler-simulator/`: bounded generic IR execution library.
- `packages/unidecompiler-simulation-host-python/`: trusted Python runtime host shared by applications.
- `packages/unidecompiler-plugin-*/`: independently installable frontend packages.
- `packages/unidecompiler-all/`: complete-installation meta-package.
- `emojivm_frontend_case/`: complete custom-VM frontend and simulator example.
- `unidecompiler-gui-test-plugin/`: Qt-free GUI SDK plugin example.
- `opcode_projects/source/<project>`: source stress projects.
- `opcode_projects/generate/<project>`: generated stress project outputs.
- `simulator_projects/`: source fixtures and expected results for generic-IR
  simulation.
- `docs/`: supporting design notes.

The stress corpora are local working data and are scanned by path rather than
imported as Python test packages. `opcode_projects` validates decompiler
recovery; `simulator_projects` validates execution of recovered IR and the
frontend adapter boundary.

## Installation And Use

The `unidecompiler-all` meta-package provides the complete CLI, GUI, GUI plugin
SDK, and all frontend plugins with one command:

```sh
python -m pip install unidecompiler-all
```

Install a published command-line setup with the formats you need:

```sh
python -m pip install unidecompiler-cli \
  unidecompiler-plugin-python-pyc \
  unidecompiler-plugin-jvm-class
```

Run `unidecompiler --help` to see CLI options. Plugins are discovered through
Python entry points, so installing another frontend adds its input formats
without changing the core or host application.

The optional simulator command executes a selected function from the recovered
generic IR. The frontend chooses how the function query is resolved:

```sh
unidecompiler simulate sample.pyc \
  --function bubble_sort \
  --args '[[5, 1, 4, 2, 8]]'
```

Calls made by the selected function that are not present in the lifted module
can be handled by an explicit runtime file:

```sh
unidecompiler simulate sample.pyc \
  --function bubble_sort \
  --args '[[5, 1, 4, 2, 8]]' \
  --environment runtime.py \
  --show-host-output
```

The runtime file is trusted host Python code selected by the user. It is not a
sandbox and is loaded by the application-host package, not by core, the
simulator, or a frontend. The simulator itself receives only data-only call
requests and validated runtime values.

For the desktop workbench, install the GUI and all bundled frontend packages:

```sh
python -m pip install 'unidecompiler-gui[all-formats]'
unidecompiler-gui
```

The GUI is read-only. Its decompiler workflow uses the public
`DecompilerEngine` facade, while its optional Simulation tab uses the separate
public simulator API. Select a recovered artifact to discover targets from the
registered frontend, enter a JSON argument array, optionally choose a trusted
`runtime.py`, and press Run to inspect the result and execution trace. The GUI
does not implement language-specific target lookup or simulation semantics.

The GUI also provides a read-only `Structure / Hex` view. When a frontend can
prove an instruction's exact absolute byte range in the opened artifact, the
core exposes that neutral `ByteRange` provenance and the GUI highlights the
corresponding bytes. Logical VM offsets are kept separate from artifact byte
offsets; when a range cannot be proven, the GUI deliberately does not guess.
This view never edits, re-encodes, or executes the original bytes.

The GUI plugin SDK is installed automatically with `unidecompiler-gui`. Plugin
authors can install it directly when developing against the public, Qt-neutral
API:

```sh
python -m pip install unidecompiler-gui-sdk
```

See `docs/GUI_PLUGIN_DEVELOPMENT.md` for the plugin manifest and API contract.

### GUI SDK Plugins

GUI SDK plugins are application-layer extensions, separate from VM frontend
plugins. They run as trusted in-process Python code and use only immutable
snapshots plus host requests. They cannot access Qt widgets, `ModuleIR`,
`FunctionIR`, decoded bytecode, frontend adapters, simulator frames, or stacks.

Create a plugin with a root `plugin.toml`:

```toml
[plugin]
id = "example.workspace-inspector"
name = "Workspace Inspector"
version = "1.0.0"
api = "1"
entry = "workspace_inspector:register"

[python]
requires = []
```

Install the SDK for development and install the plugin directory from the GUI:

```sh
python -m pip install unidecompiler-gui-sdk
```

Use `context.commands` and `context.panels` to register declarative commands
and read-only panels. Installation, update, enable/disable, and removal take
effect after restarting the GUI. Plugin dependencies are checked but never
installed automatically. The complete API and lifecycle are documented in
`docs/GUI_PLUGIN_DEVELOPMENT.md`.

### GUI Plugin Example

The repository includes `unidecompiler-gui-test-plugin/`, a complete trusted
GUI plugin that uses only `unidecompiler_gui_sdk`. Install that directory from
`Plugins -> Manage plugins -> Install local folder`, then restart the GUI. It
adds a read-only workspace panel and commands for refreshing document data and
discovering simulation targets. The example does not import Qt, core internals,
frontend decoders, or simulator implementation classes.

### Custom VM Example

`emojivm_frontend_case/` demonstrates how to add a custom VM without changing
the core. It includes a VM format note, a sample artifact, a reference runner,
a frontend plugin, and a trusted runtime environment:

```text
emojivm_frontend_case/
├── chal.evm
├── emojivm
├── runtime.py
└── unidecompiler-plugin-emojivm/
```

The frontend can be registered through the public registry API:

```python
from pathlib import Path
from unidecompiler import DecompilerEngine

case = Path("emojivm_frontend_case")
engine = DecompilerEngine.discover()
engine.register_frontend_directory(case / "unidecompiler-plugin-emojivm")
result = engine.decompile_bytes(
    (case / "chal.evm").read_bytes(),
    filename="chal.evm",
    frontend_id="emojivm",
)
print(result.status)
print(result.pseudocode.text if result.pseudocode else "<no pseudocode>")
```

For generic-IR simulation, use the case's `runtime.py` through
`PythonFileEnvironment`. The runtime is trusted host code and is not a
sandbox. The case README contains the full registration, simulation, and
reference-runner workflow. Its test files are local development material and
are excluded from delivery.

## Development

Repository cloning is required only for development. Create and activate a
Python 3.11+ virtual environment, then install the workspace packages in
editable mode:

```sh
.venv/bin/python -m pip install build -e packages/unidecompiler \
  -e packages/unidecompiler-gui-sdk \
  -e packages/unidecompiler-simulator \
  -e packages/unidecompiler-simulation-host-python \
  -e packages/unidecompiler-cli \
  -e packages/unidecompiler-gui \
  -e packages/unidecompiler-plugin-lua \
  -e packages/unidecompiler-plugin-python-pyc \
  -e packages/unidecompiler-plugin-jvm-class \
  -e packages/unidecompiler-plugin-dotnet-cli \
  -e packages/unidecompiler-plugin-wasm
```

Build the core package independently:

```sh
.venv/bin/python -m build packages/unidecompiler
```
