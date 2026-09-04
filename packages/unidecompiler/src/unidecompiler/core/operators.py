"""VM-neutral numeric operator normalization.

Frontends submit the spelling they decode.  Normalization belongs to core so
that shifts, rotates, and bitwise aliases have one semantics-preserving
definition for the simulator and any future analysis consumer.
"""

from __future__ import annotations


NUMERIC_OPERATOR_ALIASES: dict[str, str] = {
    "shl": "<<", "sll": "<<", "lsl": "<<", "sal": "<<", "lshift": "<<",
    # ``shr`` is retained as the historical arithmetic spelling.  Explicit
    # logical-right aliases use a distinct canonical operator so signedness
    # is not guessed by the simulator.
    "shr": ">>", "srl": ">>>", "lsr": ">>>", "lshr": ">>>", "rshift": ">>",
    "sar": ">>", "sra": ">>", "asr": ">>", "ashr": ">>",
    "ushr": ">>>", "urshift": ">>>",
    "band": "&", "bitand": "&", "bor": "|", "bitor": "|",
    "bxor": "^", "bitxor": "^", "xor": "^",
    "rotl": "rol", "rotr": "ror", "rotate_left": "rol", "rotate_right": "ror",
}


def normalize_numeric_operator(op: str) -> str:
    """Return the canonical spelling without guessing unknown operators."""

    return NUMERIC_OPERATOR_ALIASES.get(op, op)


CANONICAL_NUMERIC_OPERATORS = frozenset({
    "+", "-", "*", "/", "//", "%", "**", "==", "!=", "<", "<=", ">", ">=",
    "&", "|", "^", "<<", ">>", ">>>", "rol", "ror",
})
