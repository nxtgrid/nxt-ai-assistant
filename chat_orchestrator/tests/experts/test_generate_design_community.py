import asyncio
from unittest.mock import AsyncMock

from orchestrator.experts.handlers.package_generator import generate_design as gd


class _Ctx:
    def __init__(self):
        self._state = {"geo_source": "community", "site_name": "Commville"}
        self.effective_email = "staff@example.com"
        self.accumulated_results = {
            "generate_distribution_map": {"statistics": {"served_buildings": 50}}
        }
        self.mcp_executor = AsyncMock()
        self.mcp_executor.call_tool.return_value = (
            '{"success": true, "design": {"Id": "d1", "Name": "n"}, '
            '"cost_summary": {}, "energy_specs": {"total_kwp": 30, "total_kwh": 60}}'
        )

    def get_state(self, k, d=None):
        return self._state.get(k, d)

    def get_parameter_value(self, k, d=None):
        return self._state.get(k, d)

    def get_previous_result(self, s):
        return self.accumulated_results.get(s)

    def get_input(self, k, d=None):
        return self._state.get(k, d)

    async def send_progress_to_user(self, *a, **k):
        return True


def test_design_runs_for_community_without_site_id():
    result = asyncio.run(gd.generate_powerplant_design(_Ctx()))
    assert result.error is None
    assert result.state_updates["design_generated"] is True


def test_design_forwards_optional_user_parameters():
    """User/LLM-supplied design parameters must reach the design_and_bom call."""
    ctx = _Ctx()
    ctx._state.update(
        {
            "inverter_type": "Quattro 10kVA",
            "wp_per_conn_override": 850,
            "force_3phase": True,
            "regulation_constraint": "None",
            "initial_3phase_connections": 5,
        }
    )
    result = asyncio.run(gd.generate_powerplant_design(ctx))
    assert result.error is None
    args = ctx.mcp_executor.call_tool.call_args[0][1]
    assert args["inverter_type"] == "Quattro 10kVA"
    assert args["wp_per_conn_override"] == 850
    assert args["force_3phase"] is True
    assert args["regulation_constraint"] == "None"
    assert args["initial_3phase_connections"] == 5
    assert args["created_by"] == "staff@example.com"


def test_design_omits_unsupplied_optional_parameters():
    """Without user input the engine's own defaults must apply (args omitted)."""
    ctx = _Ctx()
    asyncio.run(gd.generate_powerplant_design(ctx))
    args = ctx.mcp_executor.call_tool.call_args[0][1]
    assert "inverter_type" not in args
    assert "wp_per_conn_override" not in args
    assert "regulation_constraint" not in args
