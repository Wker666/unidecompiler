"""VM-neutral bytecode decompilation library.

Use :class:`unidecompiler.plugin_registry.FrontendRegistry` to supply or
discover frontend plugins.  Command-line hosting lives in ``unidecompiler-cli``.
"""

from unidecompiler.engine import (
    BytecodeInstruction,
    BytecodeStructure,
    DecompileResult,
    DecompilerEngine,
    FunctionResult,
    PseudocodeDocument,
    PseudocodeRange,
    StructureNode,
)
from unidecompiler.provenance import ByteRange
from unidecompiler.analysis import BytecodeControlFlowInstruction, BrowseEntry, BrowseIndex, ControlFlowBlock, ControlFlowEdge, FunctionControlFlow, Reference, Symbol, SymbolIndex
from unidecompiler.plugins import FrontendDecodeError, FrontendModule, FrontendPlugin, FrontendVersionSupport
from unidecompiler.plugin_registry import FrontendRegistrationError

__all__ = (
    "ByteRange", "BytecodeInstruction", "BytecodeStructure", "DecompileResult", "DecompilerEngine", "FrontendDecodeError",
    "FrontendModule", "FrontendPlugin", "FrontendVersionSupport", "FrontendRegistrationError", "FunctionResult",
    "PseudocodeDocument", "PseudocodeRange", "StructureNode",
    "BytecodeControlFlowInstruction", "BrowseEntry", "BrowseIndex", "ControlFlowBlock", "ControlFlowEdge", "FunctionControlFlow", "Reference", "Symbol", "SymbolIndex",
)
