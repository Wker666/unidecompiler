# unidecompiler-all

`unidecompiler-all` is the complete installation meta-package for
unidecompiler. It installs the core, command-line host, PySide6 GUI, and all
published frontend plugins for Python bytecode, JVM class files, Lua bytecode,
.NET CLI assemblies, and WebAssembly modules.

It contains no decompiler implementation of its own. Install it when you want
the complete workbench rather than selecting individual frontend packages:

```sh
python -m pip install unidecompiler-all
```

After installation, run `unidecompiler --help` for the CLI or
`unidecompiler-gui` for the desktop workbench.
