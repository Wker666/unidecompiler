"""Load trusted Python runtime files without coupling to a specific UI host."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
from types import ModuleType

from unidecompiler_simulator import (
    ExternalCallRequest,
    ExternalCallResult,
    ExternalCallStatus,
    NotHandled,
)


class PythonFileEnvironment:
    """A fresh module-backed environment for a trusted user Python file."""

    def __init__(self, module: ModuleType) -> None:
        self._module = module

    @classmethod
    def load(cls, path: Path | str) -> "PythonFileEnvironment":
        runtime_path = Path(path)
        if not runtime_path.is_file():
            raise ValueError(f"environment file does not exist: {runtime_path}")
        module_name = (
            f"unidecompiler_environment_{runtime_path.stem}_{abs(hash(runtime_path.resolve()))}"
        )
        module = ModuleType(module_name)
        module.__file__ = str(runtime_path)
        source = runtime_path.read_text(encoding="utf-8")
        exec(compile(source, str(runtime_path), "exec"), module.__dict__)
        return cls(module)

    def call(self, request: ExternalCallRequest):
        function = getattr(self._module, request.name, None)
        if function is None:
            return NotHandled
        if not callable(function):
            return ExternalCallResult(
                ExternalCallStatus.RAISED,
                exception=f"environment member {request.name!r} is not callable",
            )
        stdout = io.StringIO()
        stderr = io.StringIO()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                value = function(*request.args, **dict(request.keywords))
        except Exception as error:
            return ExternalCallResult(
                ExternalCallStatus.RAISED,
                exception=f"{type(error).__name__}: {error}",
                stdout=stdout.getvalue(),
                stderr=stderr.getvalue(),
            )
        return ExternalCallResult(
            ExternalCallStatus.RETURNED,
            values=(value,),
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )
