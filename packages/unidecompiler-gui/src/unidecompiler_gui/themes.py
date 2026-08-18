"""QSS-backed visual themes for the workbench."""
from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path


@dataclass(frozen=True)
class Theme:
    id: str
    display_name: str
    stylesheet: str
    colors: dict[str, str]


_COLORS = {
    "dark": {
        "syntax_keyword": "#79c0ff", "syntax_literal": "#d2a8ff", "syntax_number": "#ffa657",
        "syntax_function": "#d2a8ff", "syntax_comment": "#8b949e", "syntax_string": "#a5d6ff",
        "line_number_background": "#161b22", "line_number_foreground": "#8b949e",
        "status_ok": "#56d364", "status_warning": "#e3b341", "status_error": "#ff7b72", "status_info": "#58a6ff",
    },
    "light": {
        "syntax_keyword": "#005cc5", "syntax_literal": "#6f42c1", "syntax_number": "#b75501",
        "syntax_function": "#6f42c1", "syntax_comment": "#6a737d", "syntax_string": "#22863a",
        "line_number_background": "#f6f8fa", "line_number_foreground": "#6a737d",
        "status_ok": "#147d3d", "status_warning": "#8a4b08", "status_error": "#9b1c1c", "status_info": "#075985",
    },
}


def builtin_themes() -> tuple[Theme, ...]:
    root = files("unidecompiler_gui").joinpath("themes")
    return tuple(
        Theme(theme_id, theme_id.title(), root.joinpath(f"{theme_id}.qss").read_text(encoding="utf-8"), colors)
        for theme_id, colors in _COLORS.items()
    )


def external_theme(path: Path, fallback: Theme) -> Theme:
    """Load a user stylesheet while retaining semantic colors from *fallback*."""
    return Theme(path.stem, path.stem, path.read_text(encoding="utf-8"), fallback.colors)
