"""Tests for distribution layout step handler and generate_map.py merge logic."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
    _has_existing_layout,
)
from shared.mapping.data_reader import _ensure_dict

# --- _merge_layout_into_row_data tests ---


class TestMergeLayoutIntoRowData:
    """Test the merge function that overlays layout data onto generate_map's row_data."""

    def _import_merge(self):
        # Import here to avoid module-level import issues
        from orchestrator.experts.handlers.package_generator.generate_map import (
            _merge_layout_into_row_data,
        )

        return _merge_layout_into_row_data

    def test_merges_all_fields(self):
        merge = self._import_merge()
        row_data = {
            "id": 1,
            "site_name": "Test Site",
            "outline_geom": b"fake_wkb",
            "buildings_geo_flat": {"features": []},
            "poles_geo_flat": None,
            "distribution_geo_flat": None,
            "meta_geo_flat": None,
        }
        layout_result = {
            "poles_geo_flat": {
                "type": "FeatureCollection",
                "features": [{"geometry": {"type": "Point"}}],
            },
            "distribution_geo_flat": {
                "type": "FeatureCollection",
                "features": [{"geometry": {"type": "LineString"}}],
            },
            "buildings_geo_flat": {
                "type": "FeatureCollection",
                "features": [{"properties": {"connected": True}}],
            },
            "meta_geo_flat": {"pole_count": 10, "coverage_percentage": 85.0},
        }
        merged = merge(row_data, layout_result)

        assert merged["poles_geo_flat"] == layout_result["poles_geo_flat"]
        assert merged["distribution_geo_flat"] == layout_result["distribution_geo_flat"]
        assert merged["buildings_geo_flat"] == layout_result["buildings_geo_flat"]
        assert merged["meta_geo_flat"] == layout_result["meta_geo_flat"]
        # Unrelated fields preserved
        assert merged["id"] == 1
        assert merged["site_name"] == "Test Site"

    def test_skips_empty_layout_fields(self):
        merge = self._import_merge()
        row_data = {
            "poles_geo_flat": {"features": [{"original": True}]},
            "distribution_geo_flat": None,
            "buildings_geo_flat": None,
            "meta_geo_flat": None,
        }
        # Layout result with empty/None fields should not overwrite
        layout_result = {
            "poles_geo_flat": None,
            "distribution_geo_flat": {},
            "buildings_geo_flat": {"type": "FeatureCollection", "features": []},
            "meta_geo_flat": {"pole_count": 5},
        }
        merged = merge(row_data, layout_result)

        # None and empty dict should NOT overwrite
        assert merged["poles_geo_flat"] == {"features": [{"original": True}]}
        assert merged["distribution_geo_flat"] is None  # {} is falsy
        # Non-empty fields should overwrite
        assert merged["meta_geo_flat"] == {"pole_count": 5}

    def test_empty_layout_result_preserves_row(self):
        merge = self._import_merge()
        row_data = {"poles_geo_flat": "original", "meta_geo_flat": "original"}
        merged = merge(row_data, {})
        assert merged["poles_geo_flat"] == "original"
        assert merged["meta_geo_flat"] == "original"


# --- _ensure_dict tests (canonical from shared.mapping.data_reader) ---


class TestEnsureDict:
    def test_none_returns_default(self):
        result = _ensure_dict(None, default={"features": []})
        assert result == {"features": []}

    def test_dict_passes_through(self):
        d = {"key": "value"}
        assert _ensure_dict(d) is d

    def test_json_string_parsed(self):
        d = {"features": [1, 2, 3]}
        assert _ensure_dict(json.dumps(d)) == d

    def test_invalid_json_returns_default(self):
        result = _ensure_dict("not json{", default={"features": []})
        assert result == {"features": []}

    def test_non_dict_json_returns_default(self):
        result = _ensure_dict(json.dumps([1, 2, 3]), default={"features": []})
        assert result == {"features": []}


# --- _has_existing_layout tests ---


class TestHasExistingLayoutHandler:
    def test_no_poles_key(self):
        assert _has_existing_layout({}) is False

    def test_none_poles(self):
        assert _has_existing_layout({"poles_geo_flat": None}) is False

    def test_empty_feature_collection(self):
        assert _has_existing_layout({"poles_geo_flat": {"features": []}}) is False

    def test_populated_feature_collection(self):
        data = {
            "poles_geo_flat": {
                "type": "FeatureCollection",
                "features": [
                    {"geometry": {"type": "Point", "coordinates": [10, 20]}, "properties": {}}
                ],
            }
        }
        assert _has_existing_layout(data) is True

    def test_json_string_with_features(self):
        data = {
            "poles_geo_flat": json.dumps(
                {"features": [{"geometry": {"type": "Point", "coordinates": [10, 20]}}]}
            )
        }
        assert _has_existing_layout(data) is True


# --- Step handler async tests ---


class TestGenerateDistributionLayoutStep:
    @pytest.fixture
    def mock_context(self):
        ctx = MagicMock()
        ctx.get_input = MagicMock(side_effect=lambda k: {"site_id": 42, "site_name": "Test"}.get(k))
        ctx.get_state = MagicMock(return_value=None)
        ctx.get_parameter_value = MagicMock(return_value=None)
        ctx.send_progress_to_user = AsyncMock()
        return ctx

    @pytest.mark.asyncio
    async def test_skips_when_no_site_id(self):
        ctx = MagicMock()
        ctx.get_input = MagicMock(return_value=None)
        ctx.get_state = MagicMock(return_value=None)

        from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
            generate_distribution_layout,
        )

        result = await generate_distribution_layout(ctx)
        assert result.data.get("skipped") is True
        assert result.data.get("skip_reason") == "no_site_id"

    @pytest.mark.asyncio
    async def test_skips_when_no_db_host(self, mock_context):
        from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
            generate_distribution_layout,
        )

        with patch.dict("os.environ", {}, clear=True):
            result = await generate_distribution_layout(mock_context)
        assert result.data.get("skipped") is True
        assert result.data.get("skip_reason") == "no_db_config"

    @pytest.mark.asyncio
    async def test_skips_when_site_has_layout(self, mock_context):
        from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
            generate_distribution_layout,
        )

        fake_row = {
            "id": 42,
            "site_name": "Test",
            "outline_geom": None,
            "buildings_geo_flat": None,
            "poles_geo_flat": {
                "features": [{"geometry": {"type": "Point", "coordinates": [1, 2]}}]
            },
            "meta_geo_flat": None,
        }

        with patch.dict("os.environ", {"AUTH_DB_HOST": "localhost"}, clear=False):
            with patch(
                "orchestrator.experts.handlers.package_generator.generate_distribution_layout.load_site_row_data",
                new_callable=AsyncMock,
                return_value=fake_row,
            ):
                result = await generate_distribution_layout(mock_context)

        assert result.data.get("skipped") is True
        assert result.data.get("skip_reason") == "existing_layout"
        assert "existing" in (result.progress_message or "").lower()

    @pytest.mark.asyncio
    async def test_returns_layout_on_success(self, mock_context):
        from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
            generate_distribution_layout,
        )

        fake_row = {
            "id": 42,
            "site_name": "Test",
            "outline_geom": None,
            "buildings_geo_flat": {
                "features": [
                    {
                        "geometry": {
                            "type": "Polygon",
                            "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                        }
                    }
                ]
            },
            "poles_geo_flat": None,
            "meta_geo_flat": None,
        }

        fake_layout = {
            "poles_geo_flat": {
                "type": "FeatureCollection",
                "features": [{"geometry": {"type": "Point"}}],
            },
            "distribution_geo_flat": {"type": "FeatureCollection", "features": []},
            "buildings_geo_flat": {"type": "FeatureCollection", "features": []},
            "meta_geo_flat": {
                "pole_count": 5,
                "coverage_percentage": 92.0,
                "backbone_cable_length_m": 200.0,
                "drop_cable_length_m": 80.0,
            },
        }

        mock_boundary = MagicMock()
        mock_boundary.polygon = MagicMock()

        with patch.dict("os.environ", {"AUTH_DB_HOST": "localhost"}, clear=False):
            with (
                patch(
                    "orchestrator.experts.handlers.package_generator.generate_distribution_layout.load_site_row_data",
                    new_callable=AsyncMock,
                    return_value=fake_row,
                ),
                patch(
                    "shared.mapping.data_reader.extract_site_boundary",
                    return_value=mock_boundary,
                ),
                patch(
                    "shared.layout.generate_layout",
                    return_value=fake_layout,
                ),
            ):
                result = await generate_distribution_layout(mock_context)

        assert result.data == fake_layout
        assert result.state_updates["layout_generated"] is True
        assert result.state_updates["layout_coverage_pct"] == 92.0

    @pytest.mark.asyncio
    async def test_layout_community_route_does_not_require_site_id(self, monkeypatch):
        monkeypatch.setenv("AUTH_DB_HOST", "h")

        from orchestrator.experts.handlers.package_generator.generate_distribution_layout import (
            generate_distribution_layout,
        )

        community_row = {
            "id": None,
            "site_name": "Commville",
            "outline_geom": b"x",
            "buildings_geo_flat": {"features": [{"a": 1}, {"a": 2}]},
            "poles_geo_flat": {"features": []},
        }

        async def fake_load(ctx, db_config):
            return dict(community_row)

        class _Boundary:
            polygon = None

        ctx = MagicMock()
        ctx.get_input = MagicMock(return_value=None)
        ctx.get_state = MagicMock(
            side_effect=lambda k, *a: "community" if k == "geo_source" else None
        )
        ctx.get_parameter_value = MagicMock(return_value=None)
        ctx.send_progress_to_user = AsyncMock()

        with (
            patch(
                "orchestrator.experts.handlers.package_generator.generate_distribution_layout.load_site_row_data",
                side_effect=fake_load,
            ),
            patch(
                "shared.mapping.data_reader.extract_site_boundary",
                return_value=_Boundary(),
            ),
            patch(
                "shared.layout.generate_layout",
                return_value={
                    "poles_geo_flat": {"features": [1]},
                    "meta_geo_flat": {"coverage_percentage": 95, "pole_count": 3},
                },
            ),
        ):
            result = await generate_distribution_layout(ctx)

        # Did not bail with skip_reason="no_site_id"
        assert result.data.get("skip_reason") != "no_site_id"
