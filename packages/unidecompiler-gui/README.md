# unidecompiler-gui

`unidecompiler-gui` is a read-only PySide6 workbench for the public
`unidecompiler.DecompilerEngine` API. It accepts one artifact, a directory, or
a ZIP/JAR archive and shows pseudocode, AST, bytecode, and diagnostics together.

Install from PyPI; cloning this repository is not required for normal use.

Install the base GUI with its Qt dependency:

```sh
python -m pip install unidecompiler-gui
```

Install all separately published frontend plugins when needed:

```sh
python -m pip install 'unidecompiler-gui[all-formats]'
```

The GUI never imports frontend plugin packages directly. It discovers installed
plugins through `DecompilerEngine` and does not modify input artifacts or save
workspace state. Its optional Simulation tab uses the separate generic IR
simulator and can load a trusted Python runtime file for unresolved functions.
