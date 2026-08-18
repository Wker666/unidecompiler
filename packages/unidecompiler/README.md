# unidecompiler

`unidecompiler` is the frontend-neutral core for universal bytecode
decompilation. It owns thin-IR lifting, recovery, generic IR, diagnostics, AST
generation, and the stable `DecompilerEngine` facade used by CLI, GUI, and
frontend plugin packages.

Frontend plugins decode VM-specific formats and submit neutral bytecode facts;
they do not perform source-structure recovery.

## Install

Install the core library directly from PyPI. Cloning this repository is not
required for normal use:

```sh
python -m pip install unidecompiler
```

To decompile an artifact, also install a host such as `unidecompiler-cli` or
`unidecompiler-gui` and the frontend plugin for the bytecode format you need.
