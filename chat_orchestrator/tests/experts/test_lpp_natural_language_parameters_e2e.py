import asyncio
from unittest.mock import AsyncMock

from orchestrator.experts.handlers.package_generator import generate_design as gd
from orchestrator.graphs.nodes.expert_handler import _build_lpp_packet_inputs


class _DesignCtx:
    def __init__(self):
        self.packet_inputs = {
            "site_name": "Pankshin",
            "technology_family": "deye",
            "initial_residential_connections": 120,
            "initial_business_connections": 35,
            "wp_per_conn_override": 850,
        }
        self.packet_state = {"geo_source": "community"}
        self.packet_id = "lpp-packet-1"
        self.effective_email = "staff@example.com"
        self.sent_messages = []
        self.accumulated_results = {
            "generate_distribution_map": {"statistics": {"served_buildings": 155}}
        }
        self.mcp_executor = AsyncMock()
        self.mcp_executor.call_tool.return_value = (
            '{"success": true, "design": {"Id": "d1", "Name": "n"}, '
            '"cost_summary": {}, "energy_specs": {"total_kwp": 30, "total_kwh": 60}}'
        )

    def get_state(self, key, default=None):
        return self.packet_state.get(key, default)

    def get_parameter_value(self, key, default=None):
        if key in self.packet_inputs:
            return self.packet_inputs[key]
        return self.packet_state.get(key, default)

    def get_previous_result(self, step_name):
        return self.accumulated_results.get(step_name)

    def get_input(self, key, default=None):
        return self.packet_inputs.get(key, default)

    async def send_progress_to_user(self, message, *args, **kwargs):
        self.sent_messages.append(message)
        return True


def test_exact_deye_not_victron_request_builds_deye_packet_inputs():
    request = (
        "Can you create an LPP for the site located at "
        "9.3947551,9.3176320 using Deye technology not Victron?"
    )
    inputs = _build_lpp_packet_inputs(
        packet_type="light_preliminary_package",
        effective_request="/lpp 9.3947551,9.3176320",
        expert_command="/lpp 9.3947551,9.3176320",
        key_entity="9.3947551,9.3176320",
        args="9.3947551,9.3176320",
        raw_request=request,
    )

    assert inputs["latitude"] == "9.3947551"
    assert inputs["longitude"] == "9.3176320"
    assert inputs["technology_family"] == "deye"


def test_natural_language_parameters_reach_design_args():
    ctx = _DesignCtx()

    result = asyncio.run(gd.generate_powerplant_design(ctx))

    assert result.error is None
    args = ctx.mcp_executor.call_tool.call_args[0][1]
    assert args["technology_family"] == "deye"
    assert args["initial_residential_connections"] == 120
    assert args["initial_business_connections"] == 35
    assert args["wp_per_conn_override"] == 850
    assert "DEYE" in ctx.sent_messages[0].upper()
