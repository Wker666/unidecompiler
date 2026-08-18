from __future__ import annotations

from unidecompiler.plugins import FrontendVersionSupport


LUA_VERSION_SUPPORT = FrontendVersionSupport(
    family="Lua bytecode",
    versions=("5.4", "5.1 header/resource fallback"),
    parser="internal Lua 5.4 chunk parser; header-only fallback for older chunks",
    status="Lua 5.4 instruction submission; older chunks are detected but not lifted",
    notes=(
        "Adding Lua 5.1 support should add a Lua 5.1 decoder/opcode table in this frontend.",
        "The frontend must still submit thin VM steps only.",
    ),
)
