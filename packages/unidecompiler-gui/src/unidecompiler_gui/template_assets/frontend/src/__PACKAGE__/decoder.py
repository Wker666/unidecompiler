from unidecompiler.plugins import FrontendDecodeError


def looks_like_input(data: bytes, filename: str | None = None) -> bool:
    """Recognize __VM_NAME__ input without executing external programs."""
    return bool(filename and filename.lower().endswith(__SUFFIXES__))


def decode_input(data: bytes, filename: str | None = None):
    """Return a stable frontend-private decoded model."""
    raise FrontendDecodeError("__VM_NAME__ decoding has not been implemented")
