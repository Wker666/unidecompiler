# unidecompiler-plugin-wasm

Frontend plugin for WebAssembly `.wasm` modules. It validates and decodes
modules with `wasm` and `wasmtime`, then submits neutral thin IR to
`unidecompiler`.

Install with:

```sh
python -m pip install unidecompiler-plugin-wasm
```

The plugin is discovered automatically by compatible CLI and GUI hosts.
