"""Simulation command hosted by the unidecompiler CLI package."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from unidecompiler.plugin_registry import FrontendRegistry
from unidecompiler_simulator import SimulationEngine, SimulationLimits
from unidecompiler_simulation_host_python import PythonFileEnvironment


def main(argv: list[str] | None = None, *, registry: FrontendRegistry | None = None) -> int:
    parser = argparse.ArgumentParser(prog="unidecompiler simulate")
    parser.add_argument("input")
    parser.add_argument("--function", required=True, help="frontend-owned function query")
    parser.add_argument("--args", default="[]", help="JSON array of function arguments")
    parser.add_argument("--frontend")
    parser.add_argument("--max-steps", type=int, default=100_000)
    parser.add_argument("--max-call-depth", type=int, default=128)
    parser.add_argument("--environment", type=Path, help="trusted Python file providing unresolved functions")
    parser.add_argument(
        "--show-host-output",
        action="store_true",
        help="write captured environment stdout and stderr to stderr",
    )
    args = parser.parse_args(argv)
    try:
        values = json.loads(args.args)
        if not isinstance(values, list):
            raise ValueError("--args must be a JSON array")
        environment = PythonFileEnvironment.load(args.environment) if args.environment else None
        engine = SimulationEngine.from_registry(registry or FrontendRegistry.discover())
        result = engine.simulate_path(
            Path(args.input),
            args.function,
            frontend_id=args.frontend,
            args=tuple(values),
            environment=environment,
            limits=SimulationLimits(args.max_steps, args.max_call_depth),
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print(f"invalid simulation request: {error}", file=sys.stderr)
        return 2
    if args.show_host_output:
        for event in result.events:
            if event.kind != "external-call":
                continue
            if event.stdout:
                print(event.stdout, file=sys.stderr, end="")
            if event.stderr:
                print(event.stderr, file=sys.stderr, end="")
    print(json.dumps(_result_payload(result), default=repr, sort_keys=True))
    return 0 if result.status.value == "completed" else 1


def _result_payload(result):
    return {
        "status": result.status.value,
        "values": result.values,
        "exception": result.exception,
        "locals": result.locals,
        "steps": result.steps,
        "diagnostic": result.diagnostic,
        "trace_truncated": result.trace_truncated,
        "events": [
            {
                "function": event.function,
                "function_index": event.function_index,
                "block": event.block,
                "kind": event.kind,
                "detail": event.detail,
                "args": event.args,
                "values": event.values,
                "exception": event.exception,
                "stdout": event.stdout,
                "stderr": event.stderr,
                "source": event.source,
            }
            for event in result.events
        ],
    }
