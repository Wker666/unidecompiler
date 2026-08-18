from __future__ import annotations

import importlib.util
import sys

from unidecompiler.plugins import FrontendVersionSupport


PYTHON_PYC_VERSION_SUPPORT = FrontendVersionSupport(
    family="CPython bytecode",
    versions=(f"{sys.version_info.major}.{sys.version_info.minor}",),
    parser="stdlib marshal/dis",
    status="current interpreter magic decode + thin opcode submission",
    notes=(f"magic={importlib.util.MAGIC_NUMBER.hex()}",),
)
