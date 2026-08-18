from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendModule
from unidecompiler_plugin_dotnet_cli.assembly import (
    DnfileAssemblyDecoder,
    DotNetAssemblyDecoder,
    looks_like_dotnet,
)
from unidecompiler_plugin_dotnet_cli.lifter import lift_dotnet_assembly
from unidecompiler_plugin_dotnet_cli.simulation import DotNetSimulationAdapter
from unidecompiler_plugin_dotnet_cli.support import DOTNET_CLI_VERSION_SUPPORT


class DotNetFrontendPlugin:
    id = "dotnet-cli"
    display_name = ".NET CLI assembly"
    supported_inputs = (".dll", ".exe")
    version_support = DOTNET_CLI_VERSION_SUPPORT
    simulation_adapter = DotNetSimulationAdapter

    def __init__(self, decoder: DotNetAssemblyDecoder | None = None) -> None:
        self.decoder = decoder or DnfileAssemblyDecoder()

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_dotnet(data) and self.decoder.can_decode(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        assembly = self.decoder.decode(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=assembly,
            metadata={
                "filename": filename,
                "format": "cli-assembly",
                "version": None,
                "endianness": "little",
                "debug_info_present": False,
                "diagnostics": [],
                "dotnet": {
                    "decoder": assembly.decoder_id or self.decoder.id,
                    "assembly_name": assembly.name,
                    "method_count": len(assembly.methods),
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(
                f".NET frontend cannot lift module from {module.frontend_id!r}"
            )

        return lift_dotnet_assembly(module.payload, module.metadata)
