"""Optional data-only simulation adapter for __DISPLAY_NAME__."""
from unidecompiler_simulator import NotHandled


class SimulationAdapter:
    frontend_id = "__PROJECT_ID__"

    def list_simulation_targets(self, decoded_module, lifted_module):
        return NotHandled

    def resolve_function(self, query, decoded_module, lifted_module):
        return NotHandled
