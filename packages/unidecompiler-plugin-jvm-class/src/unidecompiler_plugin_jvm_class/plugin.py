from __future__ import annotations

from unidecompiler.core.ir import FunctionIR, ModuleIR, SourceRef
from unidecompiler.plugins import FrontendModule
from unidecompiler_plugin_jvm_class.classfile import (
    ClassFileDecoder,
    PreferredClassFileDecoder,
    looks_like_class,
)
from unidecompiler_plugin_jvm_class.lifter import lift_java_class
from unidecompiler_plugin_jvm_class.simulation import JavaClassSimulationAdapter
from unidecompiler_plugin_jvm_class.support import JVM_CLASS_VERSION_SUPPORT


class JavaClassFrontendPlugin:
    id = "jvm-class"
    display_name = "JVM class file"
    supported_inputs = (".class",)
    version_support = JVM_CLASS_VERSION_SUPPORT
    simulation_adapter = JavaClassSimulationAdapter

    def __init__(self, decoder: ClassFileDecoder | None = None) -> None:
        self.decoder = decoder or PreferredClassFileDecoder()

    def can_load(self, data: bytes, filename: str | None = None) -> bool:
        return looks_like_class(data) and self.decoder.can_decode(data, filename)

    def decode(self, data: bytes, filename: str | None = None) -> FrontendModule:
        class_file = self.decoder.decode(data, filename)
        return FrontendModule(
            frontend_id=self.id,
            payload=class_file,
            metadata={
                "filename": filename,
                "format": "class",
                "version": f"{class_file.major_version}.{class_file.minor_version}",
                "endianness": "big",
                "debug_info_present": False,
                "diagnostics": [],
                "jvm": {
                    "decoder": class_file.decoder_id or self.decoder.id,
                    "class_name": class_file.class_name,
                    "major_version": class_file.major_version,
                    "minor_version": class_file.minor_version,
                },
            },
        )

    def lift(self, module: FrontendModule) -> ModuleIR:
        if module.frontend_id != self.id:
            raise TypeError(
                f"JVM class frontend cannot lift module from {module.frontend_id!r}"
            )

        return lift_java_class(module.payload, module.metadata)
