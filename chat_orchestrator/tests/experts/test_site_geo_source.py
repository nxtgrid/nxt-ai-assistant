import asyncio
from types import SimpleNamespace  # noqa: F401
from unittest.mock import patch

from shapely import wkb
from shapely.geometry import shape

from orchestrator.experts.handlers.package_generator import site_geo_source as sgs
from orchestrator.experts.handlers.package_generator.site_geo_source import (
    community_boundary_to_row_data,
)


def test_community_boundary_to_row_data_shape():
    boundary_feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[4.58, 6.81], [4.60, 6.81], [4.60, 6.83], [4.58, 6.81]]],
        },
        "properties": {},
    }
    buildings = {"type": "FeatureCollection", "features": [{"a": 1}, {"a": 2}]}

    row = community_boundary_to_row_data(
        boundary_feature, buildings, site_name="Testville", site_state="Edo"
    )

    assert row["site_name"] == "Testville"
    assert row["buildings_geo_flat"] == buildings
    assert row["poles_geo_flat"] == {"features": []}  # forces fresh layout
    # outline_geom round-trips back to the original polygon
    geom = wkb.loads(row["outline_geom"])
    assert geom.equals(shape(boundary_feature["geometry"]))
    assert '"state": "Edo"' in row["site_details"]


class _Ctx:
    """Minimal StepContext stand-in for the resolver."""

    def __init__(self, state=None, inputs=None, results=None):
        self._state = state or {}
        self._inputs = inputs or {}
        self._results = results or {}

    def get_state(self, k, default=None):
        return self._state.get(k, default)

    def get_input(self, k, default=None):
        return self._inputs.get(k, default if default is not None else self._state.get(k))

    def get_previous_result(self, step):
        return self._results.get(step)


def test_load_submission_route_calls_db():
    ctx = _Ctx(state={"site_id": 42, "site_name": "RealSite"})
    fake_row = {
        "id": 42,
        "site_name": "RealSite",
        "outline_geom": b"x",
        "buildings_geo_flat": {"features": [{"a": 1}]},
    }

    async def fake_fetch(site_id, db_config=None):
        assert site_id == 42
        return dict(fake_row)

    with patch.object(sgs, "fetch_site_pipeline_row", side_effect=fake_fetch):
        row = asyncio.run(sgs.load_site_row_data(ctx, {"host": "h", "user": "u", "password": "p"}))
    assert row["site_name"] == "RealSite"
    assert row["buildings_geo_flat"]["features"] == [{"a": 1}]


def test_load_community_route_uses_previous_result():
    boundary_feature = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[4.58, 6.81], [4.60, 6.81], [4.60, 6.83], [4.58, 6.81]]],
        },
        "properties": {},
    }
    buildings = {"type": "FeatureCollection", "features": [{"a": 1}, {"a": 2}, {"a": 3}]}
    ctx = _Ctx(
        state={"geo_source": "community", "site_name": "Commville", "community_state": "Edo"},
        results={
            "resolve_community_site": {"boundary": boundary_feature, "buildings_geojson": buildings}
        },
    )
    row = asyncio.run(sgs.load_site_row_data(ctx, {"host": "h"}))
    assert row["site_name"] == "Commville"
    assert len(row["buildings_geo_flat"]["features"]) == 3


def test_surveyed_buildings_override_wins():
    surveyed = {"type": "FeatureCollection", "features": [{"s": 1}]}
    ctx = _Ctx(
        state={
            "site_id": 42,
            "site_name": "RealSite",
            "surveyed_buildings_geojson": surveyed,
        }
    )
    fake_row = {
        "id": 42,
        "site_name": "RealSite",
        "outline_geom": b"x",
        "buildings_geo_flat": {"features": [{"a": 1}, {"a": 2}]},
    }

    async def fake_fetch(site_id, db_config=None):
        return dict(fake_row)

    with patch.object(sgs, "fetch_site_pipeline_row", side_effect=fake_fetch):
        row = asyncio.run(sgs.load_site_row_data(ctx, {"host": "h"}))
    assert row["buildings_geo_flat"] == surveyed  # surveyed overrides DB footprints
