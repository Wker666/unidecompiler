"""Optional Python function lookup for the decoupled generic IR simulator."""

from __future__ import annotations


class PythonPycSimulationAdapter:
    frontend_id = "python-pyc"

    def resolve_function(self, query, decoded_module, lifted_module):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str):
            return NotHandled
        context = self._function_context(lifted_module.functions)
        matches = context.get(query, ())
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], context=context, identifier=query)

    def resolve_global(self, name, context):
        from unidecompiler_simulator import IntrinsicCall, NotHandled, ResolvedFunction

        if name in {"len", "range", "iter_has_next", "iter_next"}:
            return IntrinsicCall(name)

        if not isinstance(context, dict):
            return NotHandled
        matches = context.get(name, ())
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

    @classmethod
    def _function_context(cls, functions):
        context = {}
        for function in cls._walk(functions):
            context.setdefault(function.name, []).append(function)
        return {name: tuple(matches) for name, matches in context.items()}

    @staticmethod
    def _walk(functions):
        for function in functions:
            yield function
            yield from PythonPycSimulationAdapter._walk(function.nested_functions)
