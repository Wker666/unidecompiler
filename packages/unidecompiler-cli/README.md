# unidecompiler-cli

`unidecompiler-cli` is the command-line host for installed `unidecompiler`
frontend plugins. It discovers plugins through the `unidecompiler.frontends`
entry-point group and uses the public `DecompilerEngine` facade.

Install the CLI and one or more frontend packages:

```sh
python -m pip install unidecompiler-cli unidecompiler-plugin-python-pyc
```

Run `unidecompiler --help` for command-line usage.
