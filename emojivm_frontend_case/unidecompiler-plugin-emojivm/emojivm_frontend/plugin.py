from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendModule, FrontendVersionSupport

from .decoder import decode_emojivm, looks_like_emojivm
from .lifter import lift_program
from .simulation import EmojiVMSimulationAdapter


class EmojiVMFrontendPlugin:
    id = "emojivm"
    display_name = "EmojiVM source"
    supported_inputs = (".evm",)
    simulation_adapter = EmojiVMSimulationAdapter
    version_support = FrontendVersionSupport(
        family="EmojiVM",
        versions=("1",),
        parser="text/UTF-8",
        status="experimental",
        notes=("Supports the instruction and digit tables documented in EMOJIVM.md.",),
    )

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_emojivm(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        program = decode_emojivm(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=program,
            metadata={
                "filename": filename,
                "format": "emojivm",
                "version": "1",
                "endianness": None,
                "debug_info_present": False,
                "emojivm": {
                    "instruction_count": len(program.instructions),
                    "source_codepoints": len(program.source),
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(f"EmojiVM frontend cannot lift module from {module.frontend_id!r}")
        return lift_program(module.payload, module.metadata)
