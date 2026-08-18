from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, TypeVar

from unidecompiler.core.effects import Effect
from unidecompiler.core.ir import SourceRef


InstructionT = TypeVar("InstructionT")
ContextT = TypeVar("ContextT")

EffectFactory = Callable[[ContextT, InstructionT, SourceRef], tuple[Effect, ...] | None]
EffectPredicate = Callable[[str, InstructionT], bool]


@dataclass(frozen=True)
class VMEffectRule(Generic[ContextT, InstructionT]):
    """Declarative row for opcode families that are not a single exact mnemonic."""

    matches: EffectPredicate[InstructionT]
    factory: EffectFactory[ContextT, InstructionT]


@dataclass(frozen=True)
class VMEffectTable(Generic[ContextT, InstructionT]):
    """Table-driven current-instruction mapping into VM-neutral effects."""

    opcode_attr: str
    ignored: frozenset[str] = frozenset()
    exact: dict[str, EffectFactory[ContextT, InstructionT]] | None = None
    rules: tuple[VMEffectRule[ContextT, InstructionT], ...] = ()
    fallback: EffectFactory[ContextT, InstructionT] | None = None

    def opcode_for(self, instruction: InstructionT) -> str:
        return str(getattr(instruction, self.opcode_attr))

    def effects_for(
        self,
        context: ContextT,
        instruction: InstructionT,
        source: SourceRef,
    ) -> tuple[Effect, ...] | None:
        opcode = self.opcode_for(instruction)
        if opcode in self.ignored:
            return ()
        factory = (self.exact or {}).get(opcode)
        if factory is not None:
            effects = factory(context, instruction, source)
            if effects is not None:
                return effects
        for rule in self.rules:
            if rule.matches(opcode, instruction):
                effects = rule.factory(context, instruction, source)
                if effects is not None:
                    return effects
        if self.fallback is not None:
            return self.fallback(context, instruction, source)
        return None
