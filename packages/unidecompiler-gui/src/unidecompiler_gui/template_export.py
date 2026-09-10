"""GUI compatibility facade for the host-side template exporter."""
from unidecompiler_export.templates import (
    TemplateExportError,
    TemplateRequest,
    build_ai_goal_prompt,
    derive_project_names,
    export_template,
)

__all__ = (
    "TemplateExportError",
    "TemplateRequest",
    "build_ai_goal_prompt",
    "derive_project_names",
    "export_template",
)
