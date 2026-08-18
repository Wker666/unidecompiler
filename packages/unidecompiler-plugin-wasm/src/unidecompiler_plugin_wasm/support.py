from __future__ import annotations

from unidecompiler.plugins import FrontendVersionSupport


WASM_VERSION_SUPPORT = FrontendVersionSupport(
    family="WebAssembly module",
    versions=("binary v1",),
    parser="wasmtime validation + wasm library instruction decode",
    status="thin wasm operator submission",
)
