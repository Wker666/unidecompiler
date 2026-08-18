"""Optional WebAssembly function lookup for the decoupled generic IR simulator."""

from __future__ import annotations


class WasmSimulationAdapter:
    frontend_id = "wasm"

    def resolve_function(self, query, decoded_module, lifted_module):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str):
            return NotHandled
        context = self._function_context(lifted_module.functions)
        matches = context.get(query, ()) or context.get(query.removeprefix("$"), ())
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], context=context, identifier=query)

    def resolve_global(self, name, context):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(context, dict):
            return NotHandled
        matches = context.get(name, ())
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], context=context, identifier=name)

    def list_simulation_targets(self, decoded_module, lifted_module):
        from unidecompiler_simulator import SimulationTargetCandidate

        context = self._function_context(lifted_module.functions)
        candidates = []
        seen = set()
        for name, matches in context.items():
            if len(matches) != 1 or id(matches[0]) in seen:
                continue
            seen.add(id(matches[0]))
            candidates.append(SimulationTargetCandidate(name, name))
        return tuple(candidates)

    @staticmethod
    def _function_context(functions):
        context = {}
        for index, function in enumerate(functions):
            context.setdefault(function.name, []).append(function)
            context.setdefault(f"$func{index}", []).append(function)
        return {name: tuple(matches) for name, matches in context.items()}
