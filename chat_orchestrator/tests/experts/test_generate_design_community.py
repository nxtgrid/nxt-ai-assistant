import asyncio
from unittest.mock import AsyncMock, patch

from orchestrator.experts.handlers.package_generator import generate_design as gd


class _Ctx:
    def __init__(self):
        self._state = {"geo_source": "community", "site_name": "Commville"}
        self.packet_state = self._state
        self.packet_id = "lpp-packet-1"
        self.effective_email = "staff@example.com"
        self.sent_messages = []
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
        self.sent_messages.append(a[0])
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


def test_design_forwards_technology_family():
    ctx = _Ctx()
    ctx._state["technology_family"] = "deye"
    result = asyncio.run(gd.generate_powerplant_design(ctx))
    assert result.error is None
    args = ctx.mcp_executor.call_tool.call_args[0][1]
    assert args["technology_family"] == "deye"


def test_design_progress_names_deye_family():
    ctx = _Ctx()
    ctx._state["technology_family"] = "deye"
    result = asyncio.run(gd.generate_powerplant_design(ctx))

    assert result.error is None
    assert "DEYE" in ctx.sent_messages[0].upper()


def test_design_omits_unsupplied_optional_parameters():
    """Without user input the engine's own defaults must apply (args omitted)."""
    ctx = _Ctx()
    asyncio.run(gd.generate_powerplant_design(ctx))
    args = ctx.mcp_executor.call_tool.call_args[0][1]
    assert "inverter_type" not in args
    assert "wp_per_conn_override" not in args
    assert "regulation_constraint" not in args


def test_design_sweeps_preexisting_drive_ids_into_new_design():
    """Drive IDs stashed by earlier steps (map, community, layout) must be
    attached to the design's artifact history once the design_id exists."""
    ctx = _Ctx()
    ctx._state.update(
        {
            "map_image_drive_id": "file-map",
            "community_boundary_drive_id": "file-boundary",
            "unrelated_key": "not-a-drive-id",
        }
    )
    with patch.object(gd, "sweep_state_for_artifacts") as mock_sweep:
        result = asyncio.run(gd.generate_powerplant_design(ctx))

    assert result.error is None
    mock_sweep.assert_called_once_with("d1", ctx.packet_state, packet_id=ctx.packet_id)


def test_design_sweep_uses_asyncio_to_thread():
    """Regression guard: sweep_state_for_artifacts does blocking supabase-py
    network I/O (shared/grid_design/db.py Repository.get/.update), so the
    call site must go through asyncio.to_thread rather than calling it
    inline from this async handler. Patching sweep_state_for_artifacts
    directly (as test_design_sweeps_preexisting_drive_ids_into_new_design
    does above) can't distinguish a blocking inline call from a
    thread-wrapped one -- both satisfy the same assert_called_once_with.
    This test patches asyncio.to_thread itself so a future edit that drops
    the thread-wrapping fails."""
    ctx = _Ctx()
    ctx._state.update(
        {
            "map_image_drive_id": "file-map",
            "community_boundary_drive_id": "file-boundary",
            "unrelated_key": "not-a-drive-id",
        }
    )
    with patch.object(gd.asyncio, "to_thread", new_callable=AsyncMock) as mock_to_thread:
        result = asyncio.run(gd.generate_powerplant_design(ctx))

    assert result.error is None
    mock_to_thread.assert_called_once_with(
        gd.sweep_state_for_artifacts, "d1", ctx.packet_state, packet_id=ctx.packet_id
    )
