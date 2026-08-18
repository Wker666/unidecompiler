from __future__ import annotations

import argparse
import base64
from dataclasses import fields, is_dataclass
import json
import math
import sys
from typing import Any
from pathlib import Path

from unidecompiler.backends.pseudocode import GenericPseudocodeBackend
from unidecompiler.core.astify import module_to_ast
from unidecompiler.plugin_registry import FrontendRegistry, FrontendSelectionError
from unidecompiler.plugins import FrontendDecodeError
from unidecompiler.input_sources import expand_input_path


def main(argv: list[str] | None = None, *, registry: FrontendRegistry | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="unidecompiler",
        description="Decompile bytecode into generic pseudocode.",
    )
    parser.add_argument("input", nargs="?", help="input bytecode file")
    parser.add_argument(
        "--frontend",
        help="explicit frontend plugin id, e.g. lua",
        default=None,
    )
    parser.add_argument(
        "--versions",
        action="store_true",
        help="print supported frontend version matrix",
    )
    parser.add_argument(
        "--format",
        choices=("pseudocode", "ast-json"),
        default="pseudocode",
        help="output format (default: pseudocode)",
    )
    args = parser.parse_args(argv)

    registry = registry or FrontendRegistry.discover()
    if args.versions:
        for plugin, support in registry.version_support():
            versions = ", ".join(support.versions)
            print(
                f"{plugin.id}: {support.family} | {versions} | "
                f"{support.status} | parser: {support.parser}"
            )
        return 0

    if args.input is None:
        parser.error("input is required unless --versions is used")
    input_path = Path(args.input)
    artifacts = expand_input_path(input_path)
    if not artifacts:
        raise SystemExit(f"no input files found in {input_path}")

    backend = GenericPseudocodeBackend() if args.format == "pseudocode" else None
    ast_modules: list[dict[str, Any]] = []
    processed = 0
    for artifact in artifacts:
        try:
            frontend = registry.select(
                artifact.data, artifact.display_path, explicit_id=args.frontend
            )
        except FrontendSelectionError:
            print(f"resource: {artifact.display_path}", file=sys.stderr)
            continue
        try:
            decoded = frontend.decode(artifact.data, artifact.display_path)
        except FrontendDecodeError:
            print(f"resource: {artifact.display_path}", file=sys.stderr)
            continue
        try:
            module = frontend.lift(decoded)
            if args.format == "ast-json":
                ast_modules.append(_json_value(module_to_ast(module)))
            else:
                assert backend is not None
                emitted = backend.emit(module)
                print(emitted.text)
        except Exception as error:
            print(
                f"error: {artifact.display_path}: {type(error).__name__}: {error}",
                file=sys.stderr,
            )
            continue
        processed += 1

    if processed == 0:
        raise SystemExit(f"no supported input files found in {input_path}")
    if args.format == "ast-json":
        print(
            json.dumps(
                {"schema_version": 1, "modules": ast_modules},
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
        )
    return 0


def _json_value(value: Any) -> Any:
    """Convert the generic AST dataclasses into a JSON-compatible tree."""
    if is_dataclass(value):
        return {
            "node_type": type(value).__name__,
            **{field.name: _json_value(getattr(value, field.name)) for field in fields(value)},
        }
    if isinstance(value, bytes | bytearray | memoryview):
        return {
            "value_type": "bytes",
            "base64": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, complex):
        return {"value_type": "complex", "real": value.real, "imag": value.imag}
    if isinstance(value, float) and not math.isfinite(value):
        return {"value_type": "float", "value": repr(value)}
    if isinstance(value, tuple | list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        if all(isinstance(key, str) for key in value):
            return {key: _json_value(item) for key, item in value.items()}
        return {
            "value_type": "map",
            "entries": [[_json_value(key), _json_value(item)] for key, item in value.items()],
        }
    if isinstance(value, set | frozenset):
        return {"value_type": "set", "items": [_json_value(item) for item in sorted(value, key=repr)]}
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"AST JSON cannot encode {type(value).__name__}")


if __name__ == "__main__":
    raise SystemExit(main())
