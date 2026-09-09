"""GUI compatibility exports for the shared host-neutral writer."""
from unidecompiler_export import (
    PseudocodeMetadataExport,
    VscodeMetadataExportError,
    build_vscode_metadata,
    export_pseudocode_documents,
    export_pseudocode_documents_with_vscode_metadata,
    write_pseudocode,
    write_pseudocode_with_vscode_metadata,
    write_vscode_metadata,
)

__all__ = (
    "PseudocodeMetadataExport",
    "VscodeMetadataExportError",
    "build_vscode_metadata",
    "export_pseudocode_documents",
    "export_pseudocode_documents_with_vscode_metadata",
    "write_pseudocode",
    "write_pseudocode_with_vscode_metadata",
    "write_vscode_metadata",
)
