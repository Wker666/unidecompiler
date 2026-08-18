# unidecompiler-cli

`unidecompiler-cli` is the command-line host for installed `unidecompiler`
frontend plugins. It discovers plugins through the `unidecompiler.frontends`
entry-point group and uses the public `DecompilerEngine` facade.

Install the CLI and one or more frontend packages:

```sh
python -m pip install unidecompiler-cli unidecompiler-plugin-python-pyc
```

Run `unidecompiler --help` for command-line usage.

The optional simulator command is hosted here, while execution remains in the
separate simulator library:

```sh
unidecompiler simulate sample.bytecode --function 'Example.run' --args '[1, 2]'
```

For trusted programs that require functions outside the lifted module, pass a
Python environment file. Top-level functions are matched by name, while their
stdout and stderr are returned as structured simulation events:

```sh
unidecompiler simulate sample.pyc --function main --environment runtime.py
```
