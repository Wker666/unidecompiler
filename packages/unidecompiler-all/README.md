# unidecompiler-all

`unidecompiler-all` is the complete installation meta-package for
unidecompiler. It installs the core, command-line host, PySide6 GUI, GUI plugin
SDK, and all published frontend plugins for Python bytecode, JVM class files,
Lua bytecode, .NET CLI assemblies, and WebAssembly modules.

It contains no decompiler implementation of its own. Install it when you want
the complete workbench rather than selecting individual frontend packages:

```sh
python -m pip install unidecompiler-all
```

After installation, run `unidecompiler --help` for the CLI or
`unidecompiler-gui` for the desktop workbench.

The complete installation also includes bounded symbolic execution through
the CLI and GUI. Explore a recovered function with:

```sh
unidecompiler symbolic sample.pyc --function choose \
  --symbolic '{"value":{"sort":"int"}}'
```

The GUI exposes the same operation in its **Symbolic** tab. Both hosts consume
only recovered generic IR and frontend-owned opaque target queries; limits,
unsupported operations, solver timeouts, and cancellations are reported as
structured outcomes.
