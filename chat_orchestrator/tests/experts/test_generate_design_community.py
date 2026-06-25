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
