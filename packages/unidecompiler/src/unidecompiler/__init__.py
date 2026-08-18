"""VM-neutral bytecode decompilation library.

Use :class:`unidecompiler.plugin_registry.FrontendRegistry` to supply or
discover frontend plugins.  Command-line hosting lives in ``unidecompiler-cli``.
"""

from unidecompiler.engine import (
    BytecodeInstruction,
    DecompileResult,
    DecompilerEngine,
    FunctionResult,
    PseudocodeDocument,
    PseudocodeRange,
)
from unidecompiler.analysis import BytecodeControlFlowInstruction, BrowseEntry, BrowseIndex, ControlFlowBlock, ControlFlowEdge, FunctionControlFlow, Reference, Symbol, SymbolIndex
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendPlugin, FrontendVersionSupport
from unidecompiler.plugin_registry import FrontendRegistrationError

__all__ = (
    "BytecodeInstruction", "DecompileResult", "DecompilerEngine", "FrontendDecodeError",
    "FrontendModule", "FrontendPlugin", "FrontendVersionSupport", "FrontendRegistrationError", "FunctionResult",
    "PseudocodeDocument", "PseudocodeRange",
    "BytecodeControlFlowInstruction", "BrowseEntry", "BrowseIndex", "ControlFlowBlock", "ControlFlowEdge", "FunctionControlFlow", "Reference", "Symbol", "SymbolIndex",
)
