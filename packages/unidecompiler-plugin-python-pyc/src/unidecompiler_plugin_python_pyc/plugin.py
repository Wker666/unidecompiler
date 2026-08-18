from __future__ import annotations

from unidecompiler.core.ir import ModuleIR
from unidecompiler.plugins import FrontendModule
from unidecompiler_plugin_python_pyc.lifter import lift_pyc_module
from unidecompiler_plugin_python_pyc.pyc import decode_pyc, looks_like_pyc
from unidecompiler_plugin_python_pyc.simulation import PythonPycSimulationAdapter
from unidecompiler_plugin_python_pyc.support import PYTHON_PYC_VERSION_SUPPORT


class PythonPycFrontendPlugin:
    id = "python-pyc"
    display_name = "Python bytecode"
    supported_inputs = (".pyc",)
    version_support = PYTHON_PYC_VERSION_SUPPORT
    simulation_adapter = PythonPycSimulationAdapter

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_pyc(data)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        module = decode_pyc(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=module,
            metadata={
                "filename": filename,
                "format": "pyc",
                "version": module.magic.hex(),
                "endianness": "little",
                "debug_info_present": True,
                "diagnostics": [],
                "python": {
                    "flags": module.flags,
                    "decoder": "stdlib-marshal-dis",
                    "root_name": module.code.name,
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(
                f"Python pyc frontend cannot lift module from {module.frontend_id!r}"
            )

        return lift_pyc_module(module.payload, module.metadata)
