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
- `packages/unidecompiler-plugin-*/`: independently installable frontend packages.
- `packages/unidecompiler-all/`: complete-installation meta-package.
- `opcode_projects/source/<project>`: source stress projects.
- `opcode_projects/generate/<project>`: generated stress project outputs.
- `docs/`: supporting design notes.

The stress corpus is local working data and is scanned by path rather than
imported as a Python test package.

## Installation And Use

The `unidecompiler-all` meta-package will provide the complete CLI, GUI, and
all frontend plugins with one command once it is published:

```sh
python -m pip install unidecompiler-all
```

Until that package is available on PyPI, install the components you need
directly as shown below.

Install a published command-line setup with the formats you need:

```sh
python -m pip install unidecompiler-cli \
  unidecompiler-plugin-python-pyc \
  unidecompiler-plugin-jvm-class
```

Run `unidecompiler --help` to see CLI options. Plugins are discovered through
Python entry points, so installing another frontend adds its input formats
without changing the core or host application.

For the desktop workbench, install the GUI and all bundled frontend packages:

```sh
python -m pip install 'unidecompiler-gui[all-formats]'
unidecompiler-gui
```

The GUI is read-only and uses only the public `DecompilerEngine` facade.

## Development

Repository cloning is required only for development. Create and activate a
Python 3.11+ virtual environment, then install the workspace packages in
editable mode:

```sh
.venv/bin/python -m pip install build -e packages/unidecompiler \
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

Unit tests are only the first verification layer. After unit tests pass, compare
real source code under `opcode_projects/source` with decompiled output from
`opcode_projects/generate`.
