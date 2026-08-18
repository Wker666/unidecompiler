from __future__ import annotations

from unidecompiler.plugins import FrontendVersionSupport


DOTNET_CLI_VERSION_SUPPORT = FrontendVersionSupport(
    family=".NET CLI assembly",
    versions=("ECMA-335 CLI metadata accepted by dnfile",),
    parser="dnfile PE/.NET metadata library",
    status="thin IL step submission",
)
