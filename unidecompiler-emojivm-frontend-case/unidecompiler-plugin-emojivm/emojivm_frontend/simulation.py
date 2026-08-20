"""Simulation target lookup for EmojiVM generic IR."""

from __future__ import annotations

from unidecompiler.core.ir import FunctionIR, ModuleIR
from unidecompiler.plugins import FrontendModule


class EmojiVMSimulationAdapter:
    """Expose EmojiVM generic-IR targets to the shared simulator.

    This adapter deliberately does not execute EmojiVM instructions.  It only
    maps the frontend-owned ``"main"`` query to the generic-IR function.
    VM-specific I/O and memory calls remain explicit external/unsupported
    operations in the simulator.
    """

    frontend_id = "emojivm"

    def resolve_function(
        self,
        query: object,
        decoded_module: FrontendModule,
        lifted_module: ModuleIR,
    ):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str) or query != "main":
            return NotHandled
        matches = tuple(
            function
            for function in self._walk(lifted_module.functions)
            if function.name == "main"
        )
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], identifier="main")

    def list_simulation_targets(
        self,
        decoded_module: FrontendModule,
        lifted_module: ModuleIR,
    ):
        from unidecompiler_simulator import NotHandled, SimulationTargetCandidate

        if self.resolve_function("main", decoded_module, lifted_module) is NotHandled:
            return ()
        return (SimulationTargetCandidate("main", "main"),)

    @staticmethod
    def _walk(functions: tuple[FunctionIR, ...]):
        for function in functions:
            yield function
            yield from EmojiVMSimulationAdapter._walk(function.nested_functions)
