"""Tests for generate_site_layout step handler."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from shapely.geometry import Polygon

from orchestrator.experts.handlers.package_generator.generate_site_layout import (
    generate_site_layout,
)


def _make_fake_cable_route(cable_type, length_m):
    """Create a mock CableRoute."""
    route = MagicMock()
    route.cable_type = cable_type
    route.length_m = length_m
    return route


class TestGenerateSiteLayoutStep:
    @pytest.fixture
    def mock_context(self):
        ctx = MagicMock()
        ctx.get_input = MagicMock(
            side_effect=lambda k: {"site_id": 42, "site_name": "TestSite"}.get(k)
        )
        ctx.get_state = MagicMock(return_value=None)
        ctx.get_parameter_value = MagicMock(
            side_effect=lambda k: {
                "editable_total_kwp": "50",
                "editable_site_type": "victron",
                "editable_panel_config": "5S2P",
            }.get(k)
        )
        ctx.get_previous_result = MagicMock(return_value={"center": {"lat": 9.0, "lon": 2.0}})
        ctx.send_progress_to_user = AsyncMock()
        return ctx

    @pytest.mark.asyncio
    async def test_skips_when_no_site_id(self):
        ctx = MagicMock()
        ctx.get_input = MagicMock(return_value=None)
        ctx.get_state = MagicMock(return_value=None)
        result = await generate_site_layout(ctx)
        assert result.error is not None
        assert "site ID" in result.error

    @pytest.mark.asyncio
    async def test_fails_when_no_kwp(self, mock_context):
        mock_context.get_parameter_value = MagicMock(
            side_effect=lambda k: {
                "editable_total_kwp": "0",
                "editable_site_type": "victron",
                "editable_panel_config": "5S2P",
            }.get(k)
        )
        result = await generate_site_layout(mock_context)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_fails_on_invalid_panel_config(self, mock_context):
        mock_context.get_parameter_value = MagicMock(
            side_effect=lambda k: {
                "editable_total_kwp": "50",
                "editable_site_type": "victron",
                "editable_panel_config": "INVALID",
            }.get(k)
        )
        result = await generate_site_layout(mock_context)
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_returns_layout_on_success(self, mock_context):
        """generate_site_layout uses site_candidates / synthetic polygon — no DB access."""
        mock_boundary_polygon = Polygon([(0, 0), (46, 0), (46, 43), (0, 43)])

        fake_layout = MagicMock()
        fake_layout.total_modules = 20
        fake_layout.achieved_kwp = 45.5
        fake_layout.target_kwp = 50.0
        fake_layout.arrays = [MagicMock()] * 3
        fake_layout.lightning_positions = [(10, 10), (30, 30)]
        fake_layout.earth_pit_positions = [(5, 5)]
        fake_layout.cable_routes = [
            _make_fake_cable_route("dc", 12.5),
            _make_fake_cable_route("dc", 15.3),
            _make_fake_cable_route("ac", 8.2),
        ]

        with (
            patch(
                "orchestrator.experts.handlers.package_generator.generate_site_layout._make_synthetic_plant_polygon",
                return_value=(mock_boundary_polygon, "EPSG:32631"),
            ),
            patch(
                "orchestrator.experts.handlers.package_generator.generate_site_layout._project_boundary_to_utm",
                return_value=mock_boundary_polygon,
            ),
            patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
            patch("shared.utils.drive_upload.upload_step_output", new_callable=AsyncMock),
        ):
            mock_thread.return_value = (fake_layout, "<xml>drawio</xml>", "cG5nX2RhdGE=")
            result = await generate_site_layout(mock_context)

        assert result.data["module_count"] == 20
        assert result.data["achieved_kwp"] == 45.5
        assert result.data["avg_pv_combiner_distance_m"] == 13.9  # avg(12.5, 15.3)
        assert result.data["feeder_pillar_distance_m"] == 8.2
        assert result.state_updates["editable_panel_config"] == "5S2P"
        assert result.state_updates["editable_site_type"] == "victron"
        assert result.state_updates["avg_pv_combiner_distance_m"] == 13.9
        assert result.state_updates["feeder_pillar_distance_m"] == 8.2

    @pytest.mark.asyncio
    async def test_site_type_fallback_ess(self, mock_context):
        """When no site_type specified and kwp >= 100, defaults to ESS."""
        mock_context.get_parameter_value = MagicMock(
            side_effect=lambda k: {
                "editable_total_kwp": "150",
                "editable_site_type": None,
                "editable_panel_config": None,
            }.get(k)
        )

        mock_boundary = MagicMock()
        mock_boundary.polygon = Polygon([(0, 0), (60, 0), (60, 50), (0, 50)])

        fake_layout = MagicMock()
        fake_layout.total_modules = 40
        fake_layout.achieved_kwp = 140.0
        fake_layout.target_kwp = 150.0
        fake_layout.arrays = [MagicMock()] * 5
        fake_layout.lightning_positions = [(10, 10)]
        fake_layout.cable_routes = [
            _make_fake_cable_route("dc", 20.0),
            _make_fake_cable_route("ac", 10.0),
        ]

        with (
            patch(
                "orchestrator.experts.handlers.package_generator.generate_site_layout._make_synthetic_plant_polygon",
                return_value=(mock_boundary.polygon, "EPSG:32631"),
            ),
            patch(
                "orchestrator.experts.handlers.package_generator.generate_site_layout._project_boundary_to_utm",
                return_value=mock_boundary.polygon,
            ),
            patch("asyncio.to_thread", new_callable=AsyncMock) as mock_thread,
            patch(
                "shared.utils.drive_upload.upload_step_output",
                new_callable=AsyncMock,
            ),
        ):
            mock_thread.return_value = (fake_layout, "<xml/>", "cG5nX2RhdGE=")
            result = await generate_site_layout(mock_context)

        assert result.state_updates["editable_site_type"] == "ess"
        assert result.state_updates["editable_panel_config"] == "20S2P"
