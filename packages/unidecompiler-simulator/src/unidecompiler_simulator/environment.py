"""Data-only boundary for optional host-provided simulation functions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from unidecompiler_simulator.adapters import NotHandled, _NotHandled
from unidecompiler_simulator.values import validate_runtime_value


@dataclass(frozen=True)
class ExternalFunction:
    """A named host function, never an executable callback in simulator state."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("external function name must not be empty")


@dataclass(frozen=True)
class ExternalCallRequest:
    """Pure data passed from generic IR execution to a host environment."""

    name: str
    args: tuple[object, ...]
    keywords: tuple[tuple[str, object], ...] = ()
    caller: str = ""
    source: object | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("external call name must not be empty")
        if not all(isinstance(name, str) for name, _ in self.keywords):
            raise TypeError("external call keyword names must be strings")
        names = tuple(name for name, _ in self.keywords)
        if len(names) != len(set(names)):
            raise ValueError("external call keyword names must be unique")
        for value in self.args:
            validate_runtime_value(value)
        for _, value in self.keywords:
            validate_runtime_value(value)


class ExternalCallStatus(StrEnum):
    NOT_HANDLED = "not_handled"
    RETURNED = "returned"
    RAISED = "raised"


@dataclass(frozen=True)
class ExternalCallResult:
    """The validated data result of one host environment invocation."""

    status: ExternalCallStatus
    values: tuple[object, ...] = ()
    exception: object | None = None
    stdout: str = ""
    stderr: str = ""
    diagnostic: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ExternalCallStatus):
            raise TypeError("external call result status must be ExternalCallStatus")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("external call output must be text")
        if self.diagnostic is not None and not isinstance(self.diagnostic, str):
            raise TypeError("external call diagnostic must be text")
        if self.status is ExternalCallStatus.NOT_HANDLED:
            if self.values or self.exception is not None:
                raise ValueError("unhandled external calls cannot carry a result")
            return
        if self.status is ExternalCallStatus.RETURNED:
            if self.exception is not None:
                raise ValueError("returned external calls cannot carry an exception")
            for value in self.values:
                validate_runtime_value(value)
            return
        if self.status is ExternalCallStatus.RAISED:
            if self.values:
                raise ValueError("raised external calls cannot carry return values")
            validate_runtime_value(self.exception)


@runtime_checkable
class ExternalEnvironment(Protocol):
    """Optional host provider for named functions unresolved by generic IR."""

    def call(self, request: ExternalCallRequest) -> ExternalCallResult | _NotHandled: ...


def call_environment(
    environment: ExternalEnvironment | None,
    request: ExternalCallRequest,
) -> ExternalCallResult | _NotHandled:
    if environment is None:
        return NotHandled
    method = getattr(environment, "call", None)
    if not callable(method):
        raise TypeError("external environment must provide call(request)")
    result = method(request)
    if result is NotHandled:
        return result
    if not isinstance(result, ExternalCallResult):
        raise TypeError("external environment must return ExternalCallResult or NotHandled")
    return result
