"""Optional .NET method lookup for the decoupled generic IR simulator."""

from __future__ import annotations


class DotNetSimulationAdapter:
    frontend_id = "dotnet-cli"

    def resolve_function(self, query, decoded_module, lifted_module):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str):
            return NotHandled
        method_name = query.rsplit(".", 1)[-1]
        context = self._function_context(lifted_module.functions)
        matches = context.get(method_name, ())
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], context=context, identifier=query)

    def resolve_global(self, name, context):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(context, dict):
            return NotHandled
        matches = context.get(name.rsplit(".", 1)[-1], ())
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], context=context, identifier=name)

    def list_simulation_targets(self, decoded_module, lifted_module):
        from unidecompiler_simulator import SimulationTargetCandidate

        context = self._function_context(lifted_module.functions)
        return tuple(
            SimulationTargetCandidate(name, name)
            for name, matches in context.items()
            if len(matches) == 1
        )

    def get_attr(self, obj, attr, context):
        from unidecompiler_simulator import NotHandled

        if attr == "length" and isinstance(obj, (list, tuple, str, bytes)):
            return len(obj)
        return NotHandled

    @staticmethod
    def _function_context(functions):
        context = {}
        for function in functions:
            context.setdefault(function.name, []).append(function)
        return {name: tuple(matches) for name, matches in context.items()}
