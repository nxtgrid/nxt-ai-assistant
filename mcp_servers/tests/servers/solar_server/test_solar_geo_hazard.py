import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
import rasterio.errors

# solar_mcp_server now imports shared_code.tool_registry, which needs
# mcp_servers/ itself on sys.path (mirrors dev.sh and how server_registry
# loads modules in production) — not just the repo root this file already
# gets from being under mcp_servers/tests/.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
_MCP_ROOT = os.path.join(_REPO_ROOT, "mcp_servers")
if _MCP_ROOT not in sys.path:
    sys.path.insert(0, _MCP_ROOT)

from mcp_servers.servers.solar_server import solar_mcp_server as sms


def _mock_src(sample_value=1.0, nodata=-9999.0):
    """Build a MagicMock standing in for a rasterio dataset context manager."""
    src = MagicMock()
    src.nodata = nodata
    src.sample.return_value = iter([[sample_value]])
    src.__enter__.return_value = src
    src.__exit__.return_value = False
    return src


def _rasterio_open_router(routes):
    """routes: dict[url] -> MagicMock src or an Exception instance to raise."""

    def _open(url, *a, **k):
        route = routes.get(url)
        if isinstance(route, Exception):
            raise route
        if route is not None:
            return route
        raise AssertionError(f"Unexpected rasterio.open call for {url}")

    return _open


class TestQueryFlood:
    def test_all_layers_succeed(self):
        routes = {
            sms._FLOOD_RIVERINE_RP1000_URL: _mock_src(2.0),
            sms._FLOOD_COASTAL_RP1000_URL: _mock_src(0.5),
            sms._FLOOD_RIVERINE_RP100_URL: _mock_src(0.3),
        }
        for url in sms._FLOOD_RCP85_2050_RP100_MODELS:
            routes[url] = _mock_src(1.0)

        with patch.object(sms.rasterio, "open", side_effect=_rasterio_open_router(routes)):
            result = sms._query_flood(9.39, 9.31)

        assert result["flood_warnings"] == []
        assert result["flood_worst_case_depth_m"] == 2.0
        assert result["flood_riverine_rp1000_m"] == 2.0
        assert result["flood_coastal_rp1000_m"] == 0.5
        assert result["flood_rp100_historical_m"] == 0.3
        assert result["flood_rp100_rcp85_2050_median_m"] == 1.0
        assert result["flood_rp100_rcp85_2050_max_m"] == 1.0

    def test_one_layer_404s_others_still_populate(self):
        """Regression test for the WRI Aqueduct S3 outage: one dead layer must not
        blank out the layers that are still reachable."""
        routes = {
            sms._FLOOD_RIVERINE_RP1000_URL: rasterio.errors.RasterioIOError(
                "HTTP response code: 404"
            ),
            sms._FLOOD_COASTAL_RP1000_URL: _mock_src(0.5),
            sms._FLOOD_RIVERINE_RP100_URL: _mock_src(0.3),
        }
        for url in sms._FLOOD_RCP85_2050_RP100_MODELS:
            routes[url] = _mock_src(1.0)

        with patch.object(sms.rasterio, "open", side_effect=_rasterio_open_router(routes)):
            result = sms._query_flood(9.39, 9.31)

        assert result["flood_riverine_rp1000_m"] is None
        assert result["flood_coastal_rp1000_m"] == 0.5
        # worst case falls back to whichever component is available
        assert result["flood_worst_case_depth_m"] == 0.5
        assert any("Riverine RP1000" in w for w in result["flood_warnings"])

    def test_all_flood_sources_down_returns_all_none_with_reasons(self):
        err = rasterio.errors.RasterioIOError("HTTP response code: 404")
        routes = {
            sms._FLOOD_RIVERINE_RP1000_URL: err,
            sms._FLOOD_COASTAL_RP1000_URL: err,
            sms._FLOOD_RIVERINE_RP100_URL: err,
        }
        for url in sms._FLOOD_RCP85_2050_RP100_MODELS:
            routes[url] = err

        with patch.object(sms.rasterio, "open", side_effect=_rasterio_open_router(routes)):
            result = sms._query_flood(9.39, 9.31)

        assert result["flood_worst_case_depth_m"] is None
        assert result["flood_rp100_rcp85_2050_median_m"] is None
        assert len(result["flood_warnings"]) >= 4  # 3 primary layers + ensemble

    def test_partial_rcp85_ensemble_reports_count(self):
        routes = {
            sms._FLOOD_RIVERINE_RP1000_URL: _mock_src(1.0),
            sms._FLOOD_COASTAL_RP1000_URL: _mock_src(1.0),
            sms._FLOOD_RIVERINE_RP100_URL: _mock_src(1.0),
        }
        models = sms._FLOOD_RCP85_2050_RP100_MODELS
        routes[models[0]] = rasterio.errors.RasterioIOError("404")
        for url in models[1:]:
            routes[url] = _mock_src(1.0)

        with patch.object(sms.rasterio, "open", side_effect=_rasterio_open_router(routes)):
            result = sms._query_flood(9.39, 9.31)

        assert result["flood_rp100_rcp85_2050_median_m"] is not None
        assert any("partial (4/5" in w for w in result["flood_warnings"])


class TestQueryTerrain:
    def test_dem_tile_unavailable(self):
        with patch.object(
            sms.rasterio,
            "open",
            side_effect=rasterio.errors.RasterioIOError("HTTP response code: 404"),
        ):
            result = sms._query_terrain(9.39, 9.31, None)

        assert result["site_elevation_m"] is None
        assert any("no DEM tile covers" in w for w in result["terrain_warnings"])

    def test_dem_tile_succeeds_no_boundary(self):
        with patch.object(sms.rasterio, "open", return_value=_mock_src(250.0)):
            result = sms._query_terrain(9.39, 9.31, None)

        assert result["site_elevation_m"] == 250.0
        assert result["boundary_min_elevation_m"] is None
        assert result["terrain_warnings"] == []

    def test_boundary_mask_failure_keeps_point_elevation(self):
        boundary = {"type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}
        with (
            patch.object(sms.rasterio, "open", return_value=_mock_src(250.0)),
            patch.object(
                sms, "rasterio_mask", side_effect=ValueError("Input shapes do not overlap raster")
            ),
        ):
            result = sms._query_terrain(9.39, 9.31, boundary)

        assert result["site_elevation_m"] == 250.0
        assert result["boundary_min_elevation_m"] is None
        assert any("Boundary terrain stats unavailable" in w for w in result["terrain_warnings"])


class TestHandleGetSiteGeoHazard:
    @pytest.mark.asyncio
    async def test_both_sources_succeed_no_warnings(self):
        with (
            patch.object(
                sms,
                "_query_flood",
                return_value={
                    "flood_worst_case_depth_m": 1.0,
                    "flood_riverine_rp1000_m": 1.0,
                    "flood_coastal_rp1000_m": 0.2,
                    "flood_rp100_historical_m": 0.1,
                    "flood_rp100_rcp85_2050_median_m": 0.3,
                    "flood_rp100_rcp85_2050_max_m": 0.5,
                    "flood_warnings": [],
                },
            ),
            patch.object(
                sms,
                "_query_terrain",
                return_value={
                    "site_elevation_m": 250.0,
                    "boundary_min_elevation_m": None,
                    "boundary_max_elevation_m": None,
                    "boundary_elevation_range_m": None,
                    "terrain_warnings": [],
                },
            ),
        ):
            response = await sms._handle_get_site_geo_hazard({"latitude": 9.39, "longitude": 9.31})

        payload = json.loads(response[0].text)
        assert "data_warnings" not in payload
        assert payload["flood_worst_case_depth_m"] == 1.0
        assert payload["site_elevation_m"] == 250.0

    @pytest.mark.asyncio
    async def test_flood_down_terrain_up_degrades_gracefully(self):
        """Mirrors the real-world WRI outage: flood data is a total loss but terrain
        still succeeds — the tool must return the terrain data, not a hard error."""
        with (
            patch.object(
                sms,
                "_query_flood",
                return_value={
                    "flood_worst_case_depth_m": None,
                    "flood_riverine_rp1000_m": None,
                    "flood_coastal_rp1000_m": None,
                    "flood_rp100_historical_m": None,
                    "flood_rp100_rcp85_2050_median_m": None,
                    "flood_rp100_rcp85_2050_max_m": None,
                    "flood_warnings": [
                        "Riverine RP1000 flood depth unavailable (data source unreachable)"
                    ],
                },
            ),
            patch.object(
                sms,
                "_query_terrain",
                return_value={
                    "site_elevation_m": 250.0,
                    "boundary_min_elevation_m": None,
                    "boundary_max_elevation_m": None,
                    "boundary_elevation_range_m": None,
                    "terrain_warnings": [],
                },
            ),
        ):
            response = await sms._handle_get_site_geo_hazard({"latitude": 9.39, "longitude": 9.31})

        assert not response[0].text.startswith("Error:")
        payload = json.loads(response[0].text)
        assert payload["site_elevation_m"] == 250.0
        assert payload["flood_worst_case_depth_m"] is None
        assert any("Riverine RP1000" in w for w in payload["data_warnings"])

    @pytest.mark.asyncio
    async def test_total_loss_returns_error(self):
        empty_flood = {
            "flood_worst_case_depth_m": None,
            "flood_riverine_rp1000_m": None,
            "flood_coastal_rp1000_m": None,
            "flood_rp100_historical_m": None,
            "flood_rp100_rcp85_2050_median_m": None,
            "flood_rp100_rcp85_2050_max_m": None,
            "flood_warnings": ["Riverine RP1000 flood depth unavailable (data source unreachable)"],
        }
        empty_terrain = {
            "site_elevation_m": None,
            "boundary_min_elevation_m": None,
            "boundary_max_elevation_m": None,
            "boundary_elevation_range_m": None,
            "terrain_warnings": [
                "Terrain elevation unavailable (no DEM tile covers this location)"
            ],
        }
        with (
            patch.object(sms, "_query_flood", return_value=empty_flood),
            patch.object(sms, "_query_terrain", return_value=empty_terrain),
        ):
            response = await sms._handle_get_site_geo_hazard({"latitude": 9.39, "longitude": 9.31})

        assert response[0].text.startswith("Error:")
        assert "unavailable" in response[0].text

    @pytest.mark.asyncio
    async def test_unexpected_internal_error_in_one_source_does_not_kill_the_other(self):
        with (
            patch.object(sms, "_query_flood", side_effect=RuntimeError("boom")),
            patch.object(
                sms,
                "_query_terrain",
                return_value={
                    "site_elevation_m": 250.0,
                    "boundary_min_elevation_m": None,
                    "boundary_max_elevation_m": None,
                    "boundary_elevation_range_m": None,
                    "terrain_warnings": [],
                },
            ),
        ):
            response = await sms._handle_get_site_geo_hazard({"latitude": 9.39, "longitude": 9.31})

        assert not response[0].text.startswith("Error:")
        payload = json.loads(response[0].text)
        assert payload["site_elevation_m"] == 250.0
        assert payload["flood_worst_case_depth_m"] is None
        assert any("internal error" in w for w in payload["data_warnings"])
