"""Symbolic execution command hosted by the unidecompiler CLI package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import z3

from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_symbolic import (
    SymbolicEngine,
    SymbolicInput,
    SymbolicLimits,
    SymbolicSort,
    SymbolicStatus,
)


def main(argv: list[str] | None = None, *, registry: FrontendRegistry | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unidecompiler symbolic")
    parser.add_argument("input")
    parser.add_argument("--function", required=True, help="frontend-owned function query")
    parser.add_argument("--frontend")
    parser.add_argument("--symbolic", default="{}", help="JSON object of symbolic input specifications")
    parser.add_argument("--concrete", default="{}", help="JSON object of concrete parameter values")
    parser.add_argument("--max-paths", type=int, default=256)
    parser.add_argument("--max-steps", type=int, default=100_000)
    parser.add_argument("--max-loop-unroll", type=int, default=32)
    parser.add_argument("--max-call-depth", type=int, default=32)
    parser.add_argument("--solver-timeout-ms", type=int, default=5_000)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    args = parser.parse_args(argv)
    try:
        symbolic_inputs = _symbolic_inputs(args.symbolic)
        concrete_args = _object(args.concrete, "--concrete")
        limits = SymbolicLimits(
            args.max_paths, args.max_steps, args.max_loop_unroll,
            args.max_call_depth, args.solver_timeout_ms,
        )
        limits.validate()
        engine = SymbolicEngine.from_registry(registry or FrontendRegistry.discover())
        result = engine.explore_path(
            Path(args.input), args.function, frontend_id=args.frontend,
            symbolic_inputs=symbolic_inputs, concrete_args=concrete_args, limits=limits,
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid symbolic request: {error}", file=sys.stderr)
        return 2
    if args.format == "text":
        print(_text_result(result))
    else:
        print(json.dumps(_result_payload(result), ensure_ascii=False, sort_keys=True, allow_nan=False))
    return _exit_code(result.status)


def _object(value: str, option: str) -> dict[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
        raise ValueError(f"{option} must be a JSON object with string keys")
    return parsed


def _symbolic_inputs(value: str) -> tuple[SymbolicInput, ...]:
    result: list[SymbolicInput] = []
    for name, spec in _object(value, "--symbolic").items():
        if not isinstance(spec, dict):
            raise ValueError("each --symbolic input must be an object")
        unknown = set(spec) - {"sort", "bit_width"}
        if unknown:
            raise ValueError(f"unknown symbolic input fields for {name!r}: {', '.join(sorted(unknown))}")
        try:
            sort = SymbolicSort(spec.get("sort", SymbolicSort.INT.value))
        except ValueError as error:
            raise ValueError(f"invalid symbolic sort for {name!r}") from error
        width = spec.get("bit_width")
        if width is not None and (not isinstance(width, int) or isinstance(width, bool)):
            raise ValueError(f"bit_width for {name!r} must be an integer")
        item = SymbolicInput(name, sort, width)
        item.validate()
        result.append(item)
    return tuple(result)


def _value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if z3.is_expr(value):
        return str(value)
    if isinstance(value, tuple):
        return [_value(item) for item in value]
    if isinstance(value, list):
        return [_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _value(item) for key, item in value.items()}
    return repr(value)


def _result_payload(result) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": result.status.value,
        "explored_paths": result.explored_paths,
        "pruned_paths": result.pruned_paths,
        "diagnostic": result.diagnostic,
        "paths": [
            {
                "path_id": path.path_id,
                "status": path.status.value,
                "constraints": [str(item) for item in path.constraints],
                "model": None if path.model is None else {key: _value(value) for key, value in path.model.items()},
                "returns": [_value(value) for value in path.returns],
                "raised": _value(path.raised),
                "blocks": list(path.blocks),
                "edges": list(path.edges),
                "steps": path.steps,
                "diagnostic": path.diagnostic,
            }
            for path in result.paths
        ],
    }


def _text_result(result) -> str:
    lines = [
        f"{result.status.value}: {result.explored_paths} explored, {result.pruned_paths} pruned",
    ]
    if result.diagnostic:
        lines.append(result.diagnostic)
    for path in result.paths:
        outcome = path.diagnostic or repr(path.raised if path.raised is not None else path.returns)
        lines.append(f"[{path.path_id}] {path.status.value}; {path.steps} steps; {outcome}")
        if path.constraints:
            lines.append("  constraints: " + "; ".join(str(item) for item in path.constraints))
        if path.model:
            lines.append("  model: " + json.dumps({key: _value(value) for key, value in path.model.items()}, sort_keys=True))
    return "\n".join(lines)


def _exit_code(status: SymbolicStatus) -> int:
    if status is SymbolicStatus.COMPLETED:
        return 0
    if status is SymbolicStatus.INVALID_REQUEST:
        return 2
    return 1
