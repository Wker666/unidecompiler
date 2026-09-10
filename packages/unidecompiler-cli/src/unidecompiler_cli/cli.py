from __future__ import annotations

import argparse
import base64
from dataclasses import fields, is_dataclass
import json
import math
import sys
from typing import Any
from pathlib import Path

from unidecompiler.engine import DecompilerEngine
from unidecompiler_export import (
    VscodeMetadataExportError,
    export_pseudocode_documents,
    export_text_documents,
    write_pseudocode,
    write_pseudocode_with_vscode_metadata,
)
from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler.input_sources import expand_input_path
from unidecompiler.progress import ProgressEvent, ProgressReporter


def main(argv: list[str] | None = None, *, registry: FrontendRegistry | None = None) -> int:
    command_argv = sys.argv[1:] if argv is None else argv
    if command_argv and command_argv[0] in {"simulate", "template", "export-template"}:
        if command_argv[0] in {"template", "export-template"}:
            from unidecompiler_cli.templates import main as template_main

            return template_main(command_argv[1:])
        from unidecompiler_cli.simulation import main as simulate_main

        return simulate_main(command_argv[1:], registry=registry)
    parser = argparse.ArgumentParser(
        prog="unidecompiler",
        description="Decompile bytecode into generic pseudocode.",
        epilog=(
            "Commands:\n"
            "  template         export a VM frontend or GUI plugin starter project\n"
            "  export-template  alias for template\n"
            "  simulate         run a recovered generic-IR function\n\n"
            "Template examples:\n"
            "  unidecompiler template frontend MyVM -o ./my-vm --author NAME "
            "--description TEXT --requirements TEXT --suffix .vm --version 1\n"
            "  unidecompiler template --interactive\n\n"
            "Run 'unidecompiler template --help' for all template options."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    output_group = parser.add_mutually_exclusive_group()
    output_group.add_argument(
        "-o",
        "--output",
        type=Path,
        help="write one successful result to this file instead of stdout",
    )
    output_group.add_argument(
        "--output-dir",
        type=Path,
        help="write every successful result as a separate file in this directory",
    )
    parser.add_argument(
        "--vscode-metadata",
        type=Path,
        metavar="PATH",
        help="write an opt-in VS Code navigation sidecar; requires --output",
    )
    parser.add_argument(
        "--progress",
        nargs="?",
        const="auto",
        default="never",
        metavar="MODE",
        help="show a dynamic progress bar on stderr (auto, always, never; default: disabled)",
    )
    args = parser.parse_args(command_argv)
    if args.progress not in {"auto", "always", "never"}:
        # With an optional argument argparse otherwise treats the positional
        # input after a bare ``--progress`` as MODE.  Accept that natural
        # spelling while retaining the explicit ``--progress always`` form.
        if args.input is None:
            args.input = args.progress
            args.progress = "auto"
        else:
            parser.error("--progress MODE must be auto, always, or never")
    if args.vscode_metadata is not None:
        if args.output_dir is not None:
            parser.error("--vscode-metadata cannot be used with --output-dir")
        if args.format == "ast-json":
            parser.error("--vscode-metadata is available only with --format pseudocode")
        if args.output is None:
            parser.error("--vscode-metadata requires --output PATH")

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

    ast_modules: list[dict[str, Any]] = []
    successful_results = []
    processed = 0
    reporter: ProgressReporter | None = _CLIProgressReporter() if _progress_enabled(args.progress) else None
    engine = DecompilerEngine.from_registry(registry)
    results = engine.decompile_artifacts(
        artifacts,
        args.frontend,
        progress=reporter,
    )
    for result in results:
        if result.status == "resource":
            print(f"resource: {result.display_path}", file=sys.stderr)
            continue
        if result.status != "ok":
            detail = next(
                (diagnostic.message for diagnostic in result.diagnostics if diagnostic.severity == "error"),
                result.status,
            )
            print(f"error: {result.display_path}: {detail}", file=sys.stderr)
            continue
        if args.format == "ast-json":
            if result.ast is not None:
                ast_modules.append(_json_value(result.ast))
        else:
            if result.pseudocode is not None and args.output is None and args.output_dir is None:
                print(result.pseudocode.text)
        successful_results.append(result)
        processed += 1

    if processed == 0:
        raise SystemExit(f"no supported input files found in {input_path}")
    if args.output is not None:
        if len(successful_results) != 1:
            raise SystemExit("--output requires exactly one successful input artifact")
        destination = args.output.expanduser()
        destination.parent.mkdir(parents=True, exist_ok=True)
        result = successful_results[0]
        if args.format == "ast-json":
            destination.write_text(
                json.dumps(
                    {"schema_version": 1, "modules": [_json_value(result.ast)]},
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )
        else:
            assert result.pseudocode is not None
            try:
                if args.vscode_metadata is None:
                    write_pseudocode(result, destination)
                else:
                    write_pseudocode_with_vscode_metadata(
                        result,
                        destination,
                        args.vscode_metadata.expanduser(),
                    )
            except VscodeMetadataExportError as error:
                raise SystemExit(str(error)) from error
            except (OSError, ValueError) as error:
                raise SystemExit(f"could not export pseudocode to {destination}: {error}") from error
        print(f"exported: {destination}", file=sys.stderr)
        return 0

    if args.output_dir is not None:
        directory = args.output_dir.expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        if args.format == "ast-json":
            exported = export_text_documents(
                (
                    (
                        result.display_path,
                        json.dumps(
                            {"schema_version": 1, "modules": [_json_value(result.ast)]},
                            ensure_ascii=False,
                            sort_keys=True,
                            allow_nan=False,
                        )
                        + "\n",
                    )
                    for result in successful_results
                    if result.ast is not None
                ),
                directory,
                suffix=".ast.json",
            )
        else:
            exported = export_pseudocode_documents(successful_results, directory)
        print(f"exported {len(exported)} file(s) to: {directory.resolve()}", file=sys.stderr)
        return 0

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


def _progress_enabled(mode: str) -> bool:
    return mode == "always" or (mode == "auto" and sys.stderr.isatty())


class _CLIProgressReporter:
    """Render a two-level progress bar without writing to stdout.

    The reporter deliberately uses only carriage-return updates.  This keeps
    pseudocode/JSON on stdout pipe-safe and avoids making the CLI depend on a
    terminal UI library for a small host-side presentation concern.
    """

    _BAR_WIDTH = 24

    def __init__(self, stream=None) -> None:
        self._stream = sys.stderr if stream is None else stream
        self._last_length = 0
        self._finished = False

    def report(self, event: ProgressEvent) -> None:
        if self._finished:
            return
        line = self._format_line(event)
        padding = max(0, self._last_length - len(line))
        self._stream.write("\r" + line + (" " * padding))
        self._stream.flush()
        self._last_length = len(line)
        if event.phase == "complete" and event.status in {"completed", "failed", "cancelled"}:
            is_last = (
                event.batch_index is None
                or event.batch_total is None
                or event.batch_index >= event.batch_total
            )
            if is_last:
                self._stream.write("\n")
                self._stream.flush()
                self._finished = True

    def _format_line(self, event: ProgressEvent) -> str:
        batch_current, batch_total, batch_fraction = self._batch_values(event)
        file_fraction = self._file_fraction(event)
        batch = _bar(batch_fraction, self._BAR_WIDTH)
        current = _bar(file_fraction, self._BAR_WIDTH)
        batch_count = _count_text(batch_current, batch_total)
        file_count = _work_count_text(event)
        detail = event.item_label or event.message or event.phase
        detail = " ".join(detail.split())
        label = _truncate(event.artifact_label or "artifact", 64)
        detail_text = f"{event.phase}: {detail} ({label})"
        return f"Files [{batch}] {batch_count} | File [{current}] {file_count} | {detail_text}"

    @staticmethod
    def _batch_values(event: ProgressEvent) -> tuple[int | None, int | None, float | None]:
        if event.batch_index is None or event.batch_total is None:
            return None, None, None
        total = event.batch_total
        current = min(total, max(0, event.batch_index))
        return current, total, (current / total if total > 0 else None)

    @staticmethod
    def _file_fraction(event: ProgressEvent) -> float | None:
        if event.phase == "complete":
            return 1.0 if event.status in {"completed", "failed", "cancelled"} else 0.0
        if event.fraction is not None:
            return event.fraction
        if event.status == "completed":
            return 1.0
        if event.status == "started":
            return 0.0
        return None


def _bar(fraction: float | None, width: int) -> str:
    if fraction is None:
        return "?".ljust(width, "-")
    filled = min(width, max(0, round(fraction * width)))
    return "#" * filled + "-" * (width - filled)


def _count_text(current: int | None, total: int | None) -> str:
    if current is None or total is None:
        return "?/?"
    return f"{current}/{total}"


def _work_count_text(event: ProgressEvent) -> str:
    if event.completed is None or event.total is None:
        return "?/?"
    return f"{event.completed}/{event.total} {event.unit}"


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return value[:limit]
    return value[: limit - 1] + "…"


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
