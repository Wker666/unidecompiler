from __future__ import annotations

"""Compatibility import for the library-owned input expansion API."""
from unidecompiler.input_sources import ARCHIVE_SUFFIXES, InputArtifact, expand_input_path, iter_input_path

__all__ = ("ARCHIVE_SUFFIXES", "InputArtifact", "expand_input_path", "iter_input_path")
