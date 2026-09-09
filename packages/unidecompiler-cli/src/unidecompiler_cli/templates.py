"""CLI host for exporting frontend and GUI plugin starter projects."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from unidecompiler_export.templates import (
    TemplateExportError,
    TemplateRequest,
    derive_project_names,
    export_template,
)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="unidecompiler template",
        description="Export a VM frontend or GUI plugin starter project.",
    )
    parser.add_argument(
        "--interactive", "-i", action="store_true",
        help="collect template settings through an interactive prompt",
    )
    parser.add_argument("kind", choices=("frontend", "gui_plugin"), nargs="?")
    parser.add_argument("project_name", nargs="?")
    parser.add_argument("--output-directory", "-o", type=Path)
    parser.add_argument("--author")
    parser.add_argument("--description")
    parser.add_argument("--requirements")
    parser.add_argument("--vm-name", default=None)
    parser.add_argument("--suffix", dest="suffixes", action="append", default=[])
    parser.add_argument("--version", dest="versions", action="append", default=[])
    parser.add_argument(
        "--simulation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the optional data-only simulator adapter (frontend templates only; default: disabled)",
    )
    parser.add_argument(
        "--ai-guidance",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="include the AI development kit and copied analysis inputs (frontend templates only; default: disabled)",
    )
    parser.add_argument(
        "--interpreter-source",
        type=Path,
        help="VM interpreter source required by --ai-guidance",
    )
    parser.add_argument(
        "--bytecode-sample",
        type=Path,
        help="bytecode sample required by --ai-guidance",
    )
    parser.add_argument(
        "--entry-kind",
        choices=("symbol", "offset", "exported_function"),
        default="",
        help="VM entry coordinate kind required by --ai-guidance",
    )
    parser.add_argument(
        "--entry-value",
        default="",
        help="VM entry symbol, offset, or exported function required by --ai-guidance",
    )
    parser.add_argument(
        "--entry-context",
        default="",
        help="optional initial cursor/base, arguments, and setup facts for AI guidance",
    )
    args = parser.parse_args(argv)
    if args.interactive:
        try:
            _collect_interactive(args)
        except (EOFError, KeyboardInterrupt):
            print("template generation cancelled", file=sys.stderr)
            return 130
    else:
        required_options = (
            "kind",
            "project_name",
            "output_directory",
            "author",
            "description",
            "requirements",
        )
        missing = [option for option in required_options if getattr(args, option) in (None, "")]
        if missing:
            parser.error("the following arguments are required in non-interactive mode: " + ", ".join(missing))

    # BooleanOptionalAction uses None above only to let the interactive wizard
    # apply its documented defaults.  The exporter itself receives real bools.
    args.simulation = bool(args.simulation)
    args.ai_guidance = bool(args.ai_guidance)

    try:
        display_name, project_id, package_name = derive_project_names(args.project_name)
        request = TemplateRequest(
            kind=args.kind,
            project_id=project_id,
            package_name=package_name,
            display_name=display_name,
            author=args.author,
            description=args.description,
            requirements=args.requirements,
            output_directory=args.output_directory,
            vm_name=args.vm_name or display_name,
            suffixes=tuple(args.suffixes),
            versions=tuple(args.versions),
            include_simulation=args.simulation,
            include_ai_guidance=args.ai_guidance,
            interpreter_source=args.interpreter_source,
            bytecode_sample=args.bytecode_sample,
            entry_kind=args.entry_kind,
            entry_value=args.entry_value,
            entry_context=args.entry_context,
        )
        destination = export_template(request)
    except (TemplateExportError, OSError) as error:
        parser.error(str(error))
    print(destination)
    return 0


def _collect_interactive(args: argparse.Namespace) -> None:
    """Fill an argparse namespace from a small, dependency-free wizard."""
    args.kind = args.kind or _ask_choice("Template kind", ("frontend", "gui_plugin"), "frontend")
    args.project_name = args.project_name or _ask_required("Project name")
    args.output_directory = args.output_directory or Path(_ask_required("Output directory"))
    args.author = args.author or _ask_required("Author")
    args.description = args.description or _ask_required("Description")
    args.requirements = args.requirements or _ask_required("Requested feature / requirements")

    if args.kind == "frontend":
        args.vm_name = args.vm_name or _ask("VM display name", args.project_name)
        if not args.suffixes:
            args.suffixes = _ask_csv("Bytecode suffixes", ".vm")
        if not args.versions:
            args.versions = _ask_csv("Bytecode versions", "1")
        if args.simulation is None:
            args.simulation = _ask_yes_no("Include the optional simulator adapter", False)
        if args.ai_guidance is None:
            args.ai_guidance = _ask_yes_no("Include the AI development kit", False)
        if args.ai_guidance:
            args.interpreter_source = args.interpreter_source or Path(
                _ask_required("VM interpreter source file")
            )
            args.bytecode_sample = args.bytecode_sample or Path(
                _ask_required("Bytecode sample file")
            )
            args.entry_kind = args.entry_kind or _ask_choice(
                "VM entry kind", ("symbol", "offset", "exported_function"), "symbol"
            )
            args.entry_value = args.entry_value or _ask_required("VM entry value")
            args.entry_context = args.entry_context or _ask(
                "Entry context (optional)", ""
            )
def _ask(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = _read_prompt(f"{label}{suffix}: ").strip()
    return value or (default or "")


def _ask_required(label: str) -> str:
    while True:
        value = _ask(label)
        if value:
            return value
        print(f"{label} is required.", file=sys.stderr)


def _ask_choice(label: str, choices: tuple[str, ...], default: str) -> str:
    choices_text = "/".join(choices)
    while True:
        value = _ask(f"{label} ({choices_text})", default)
        if value in choices:
            return value
        print(f"Choose one of: {choices_text}", file=sys.stderr)


def _ask_csv(label: str, default: str) -> list[str]:
    while True:
        values = [item.strip() for item in _ask(label, default).split(",") if item.strip()]
        if values:
            return values
        print(f"{label} must contain at least one value.", file=sys.stderr)


def _ask_yes_no(label: str, default: bool) -> bool:
    default_text = "Y/n" if default else "y/N"
    while True:
        value = _read_prompt(f"{label} ({default_text}): ").strip().lower()
        if not value:
            return default
        if value in {"y", "yes"}:
            return True
        if value in {"n", "no"}:
            return False
        print("Please answer yes or no.", file=sys.stderr)


def _read_prompt(prompt: str) -> str:
    print(prompt, end="", file=sys.stderr, flush=True)
    value = sys.stdin.readline()
    if value == "":
        raise EOFError
    return value.rstrip("\r\n")
