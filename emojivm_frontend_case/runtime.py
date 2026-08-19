"""Trusted host runtime for EmojiVM generic-IR simulation.

Choose this file in the GUI Simulation panel's Runtime field.  It provides
the named external calls emitted by the EmojiVM frontend; the shared simulator
still owns control flow and generic-IR execution.

For GUI runs, optionally set EMOJIVM_RUNTIME_STDIN before launching the GUI.
Its UTF-8 bytes are supplied to the first read_buffer() call.  Command-line
runs without that variable consume standard input instead.
"""

from __future__ import annotations

import builtins
import os
import sys


_MAX_BUFFERS = 10
_MAX_BUFFER_SIZE = 1500
_buffers: list[bytearray | None] = [None] * _MAX_BUFFERS
_configured_input: bytes | None = None


def _buffer_index(index: int) -> int:
    if not isinstance(index, int) or isinstance(index, bool):
        raise TypeError("buffer index must be an integer")
    if not 0 <= index < _MAX_BUFFERS:
        raise IndexError(f"buffer index out of range: {index}")
    return index


def _buffer(index: int) -> bytearray:
    checked_index = _buffer_index(index)
    buffer = _buffers[checked_index]
    if buffer is None:
        raise ValueError(f"buffer {checked_index} is not allocated")
    return buffer


def _offset(buffer: bytearray, offset: int) -> int:
    if not isinstance(offset, int) or isinstance(offset, bool):
        raise TypeError("buffer offset must be an integer")
    if not 0 <= offset < len(buffer):
        raise IndexError(f"buffer offset out of range: {offset}")
    return offset


def alloc(size: int) -> None:
    """Allocate the first free EmojiVM buffer slot."""

    if not isinstance(size, int) or isinstance(size, bool):
        raise TypeError("allocation size must be an integer")
    if not 0 <= size <= _MAX_BUFFER_SIZE:
        raise ValueError(f"allocation size must be between 0 and {_MAX_BUFFER_SIZE}")
    try:
        index = _buffers.index(None)
    except ValueError as error:
        raise MemoryError("no free EmojiVM buffer slots") from error
    _buffers[index] = bytearray(size)


def free(index: int) -> None:
    """Release an allocated EmojiVM buffer."""

    checked_index = _buffer_index(index)
    _buffer(checked_index)
    _buffers[checked_index] = None


def load_byte(index: int, offset: int) -> int:
    """Return mem[index][offset] using the frontend's generic-IR argument order."""

    buffer = _buffer(index)
    return buffer[_offset(buffer, offset)]


def store_byte(index: int, offset: int, value: int) -> None:
    """Store the low byte of value at mem[index][offset]."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("stored value must be an integer")
    buffer = _buffer(index)
    buffer[_offset(buffer, offset)] = value & 0xFF


def _input_bytes(limit: int) -> bytes:
    global _configured_input

    if _configured_input is None:
        configured = os.environ.get("EMOJIVM_RUNTIME_STDIN")
        if configured is not None:
            _configured_input = configured.encode("utf-8")
        elif sys.stdin.isatty():
            _configured_input = b""
        else:
            _configured_input = sys.stdin.buffer.read()
    data, _configured_input = _configured_input[:limit], _configured_input[limit:]
    return data


def read_buffer(index: int) -> None:
    """Read available configured input into an allocated buffer."""

    buffer = _buffer(index)
    data = _input_bytes(len(buffer))
    buffer[: len(data)] = data


def write_buffer(index: int) -> None:
    """Write bytes through the first NUL terminator, matching EmojiVM WRITE."""

    buffer = _buffer(index)
    terminator = buffer.find(0)
    data = buffer if terminator < 0 else buffer[:terminator]
    # PythonFileEnvironment redirects stdout to StringIO to capture simulator
    # events, so use text output rather than sys.stdout.buffer.
    sys.stdout.write(bytes(data).decode("utf-8", errors="replace"))
    sys.stdout.flush()


def puts_until_zero(value: int) -> None:
    """Fallback for the frontend's current single-value PUTS generic IR."""

    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError("PUTS value must be an integer")
    if value:
        sys.stdout.write(chr(value & 0xFF))
        sys.stdout.flush()


def print(value: int) -> None:
    """Emit EmojiVM PRINT output as decimal text."""

    builtins.print(value, end="")
