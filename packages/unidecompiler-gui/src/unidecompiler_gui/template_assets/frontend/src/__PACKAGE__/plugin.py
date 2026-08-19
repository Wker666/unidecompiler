from unidecompiler.plugins import FrontendModule

from .decoder import decode_input, looks_like_input
from .lifter import lift_module
from .support import VERSION_SUPPORT


class Frontend:
    id = "__PROJECT_ID__"
    display_name = "__DISPLAY_NAME__"
    supported_inputs = __SUFFIXES__
    version_support = VERSION_SUPPORT

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_input(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        return FrontendModule(self.id, decode_input(data, filename), {"filename": filename})

    def lift(self, module: FrontendModule):
        if module.frontend_id != self.id:
            raise TypeError(f"cannot lift module from {module.frontend_id!r}")
        return lift_module(module)
