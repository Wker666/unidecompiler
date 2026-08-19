from __PACKAGE__.simulation import SimulationAdapter


def test_simulation_adapter_is_data_only_until_implemented() -> None:
    assert SimulationAdapter.frontend_id == "__PROJECT_ID__"
