from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ObjectValue:
    """A host-independent object used by the default in-memory runtime."""

    type_name: str = "object"
    fields: dict[str, Any] = field(default_factory=dict)


@dataclass
class TableValue:
    """A table retaining array and keyed fields without host-language policy."""

    array_items: list[Any] = field(default_factory=list)
    fields: dict[Any, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SliceValue:
    start: Any = None
    stop: Any = None
    step: Any = None


def validate_runtime_value(value: Any, *, _seen: set[int] | None = None) -> None:
    """Reject host objects and executable values at the simulator boundary."""

    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return
    if isinstance(value, SliceValue):
        for item in (value.start, value.stop, value.step):
            if item is not None:
                validate_runtime_value(item, _seen=_seen)
        return
    if _seen is None:
        _seen = set()
    identity = id(value)
    if identity in _seen:
        raise TypeError("cyclic runtime values are not supported")
    if isinstance(value, ObjectValue):
        if not isinstance(value.type_name, str):
            raise TypeError("object type_name must be a string")
        _seen.add(identity)
        for field_value in value.fields.values():
            validate_runtime_value(field_value, _seen=_seen)
        _seen.remove(identity)
        return
    if isinstance(value, TableValue):
        _seen.add(identity)
        for item in value.array_items:
            validate_runtime_value(item, _seen=_seen)
        for key, item in value.fields.items():
            validate_runtime_value(key, _seen=_seen)
            validate_runtime_value(item, _seen=_seen)
        _seen.remove(identity)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        _seen.add(identity)
        for item in value:
            validate_runtime_value(item, _seen=_seen)
        _seen.remove(identity)
        return
    if isinstance(value, dict):
        _seen.add(identity)
        for key, item in value.items():
            validate_runtime_value(key, _seen=_seen)
            validate_runtime_value(item, _seen=_seen)
        _seen.remove(identity)
        return
    raise TypeError(f"unsupported host runtime value: {type(value).__name__}")


def snapshot_value(value: Any, *, _seen: set[int] | None = None) -> Any:
    if _seen is None:
        _seen = set()
    if isinstance(value, (ObjectValue, TableValue, dict, list, tuple, set, frozenset)):
        identity = id(value)
        if identity in _seen:
            return "<cycle>"
        _seen.add(identity)
    else:
        identity = None
    if isinstance(value, ObjectValue):
        result = ObjectValue(value.type_name, {key: snapshot_value(item, _seen=_seen) for key, item in value.fields.items()})
        _seen.remove(identity)
        return result
    if isinstance(value, SliceValue):
        return value
    if isinstance(value, TableValue):
        result = TableValue(
            [snapshot_value(item, _seen=_seen) for item in value.array_items],
            {snapshot_value(key, _seen=_seen): snapshot_value(item, _seen=_seen) for key, item in value.fields.items()},
        )
        _seen.remove(identity)
        return result
    if isinstance(value, dict):
        result = {key: snapshot_value(item, _seen=_seen) for key, item in value.items()}
        _seen.remove(identity)
        return result
    if isinstance(value, list):
        result = [snapshot_value(item, _seen=_seen) for item in value]
        _seen.remove(identity)
        return result
    if isinstance(value, tuple):
        result = tuple(snapshot_value(item, _seen=_seen) for item in value)
        _seen.remove(identity)
        return result
    if isinstance(value, set):
        result = {snapshot_value(item, _seen=_seen) for item in value}
        _seen.remove(identity)
        return result
    if isinstance(value, frozenset):
        result = frozenset(snapshot_value(item, _seen=_seen) for item in value)
        _seen.remove(identity)
        return result
    return value
