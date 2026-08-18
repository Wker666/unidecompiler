from __future__ import annotations

from unidecompiler.plugins import FrontendVersionSupport


JVM_CLASS_VERSION_SUPPORT = FrontendVersionSupport(
    family="JVM classfile",
    versions=("45.3-52.0",),
    parser="jawa classfile library / header fallback",
    status="JVM 8-era classfile decode + thin opcode submission",
    notes=(
        "Java 8 uses classfile major version 52.",
        "Extending newer Java versions should update this frontend's version metadata and tests.",
    ),
)
