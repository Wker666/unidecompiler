"""Optional Lua runtime facts for the decoupled generic IR simulator."""

from __future__ import annotations


class LuaSimulationAdapter:
    """Resolve Lua names and values without executing Lua bytecode."""

    frontend_id = "lua"

    def resolve_function(self, query, decoded_module, lifted_module):
        from unidecompiler_simulator import NotHandled, ResolvedFunction

        if not isinstance(query, str):
            return NotHandled
        matches = [
            function
            for function in self._walk(lifted_module.functions)
            if function.name == query
        ]
        if len(matches) != 1:
            return NotHandled
        return ResolvedFunction(matches[0], identifier=query)

    def resolve_global(self, name, context):
        from unidecompiler_simulator import IntrinsicCall, NotHandled

        if name == "vm_forloop_continues":
            return IntrinsicCall("range_continues")
        return NotHandled

    def list_simulation_targets(self, decoded_module, lifted_module):
        from unidecompiler_simulator import SimulationTargetCandidate

        functions = tuple(self._walk(lifted_module.functions))
        names = {function.name for function in functions}
        return tuple(
            SimulationTargetCandidate(function.name, function.name)
            for function in functions
            if sum(candidate.name == function.name for candidate in functions) == 1
            and function.name in names
        )

    def unary_op(self, op, value, context):
        from unidecompiler_simulator import NotHandled, TableValue

        if op != "#":
            return NotHandled
        if isinstance(value, TableValue):
            return len(value.array_items)
        if isinstance(value, (list, str, tuple)):
            return len(value)
        return NotHandled

    def get_item(self, obj, key, context):
        from unidecompiler_simulator import NotHandled

        if not isinstance(key, int):
            return NotHandled
        if isinstance(obj, list):
            return obj[key - 1]
        return NotHandled

    def set_item(self, obj, key, value, context):
        from unidecompiler_simulator import NotHandled

        if not isinstance(key, int):
            return NotHandled
        if isinstance(obj, list):
            obj[key - 1] = value
            return None
        return NotHandled

    @staticmethod
    def _walk(functions):
        for function in functions:
            yield function
            yield from LuaSimulationAdapter._walk(function.nested_functions)
