import asyncio
import sys
from unittest.mock import patch

# Use sys.modules to get the actual module object (the package __init__ overwrites the
# package attribute with the function of the same name, so `import ... as rcs` returns
# the function instead of the module).
import orchestrator.experts.handlers.package_generator.resolve_community_site  # noqa: F401

rcs = sys.modules["orchestrator.experts.handlers.package_generator.resolve_community_site"]


class _Ctx:
    def __init__(self, inputs):
        self._inputs = inputs
        self._state = {}
        self.accumulated_results = {}

    def get_input(self, k, default=None):
        return self._inputs.get(k, default)

    def get_state(self, k, default=None):
        return self._state.get(k, default)

    async def send_progress_to_user(self, *a, **k):
        return True


def _community(boundary_feat):
    return type(
        "C",
        (),
        {
            "anchor_name": "anchor1",
            "community_name": "Commville",
            "building_count": 100,
            "block_count": 12,
            "boundary": boundary_feat,
            "map_b64": "",
            "error": "",
        },
    )()


def test_resolve_community_site_sets_state_and_footprints():
    boundary_feat = {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[4.58, 6.81], [4.60, 6.81], [4.60, 6.83], [4.58, 6.81]]],
        },
        "properties": {},
    }
    ctx = _Ctx({"latitude": "6.82", "longitude": "4.59", "community_name": "Commville"})
    footprints = type(
        "FR",
        (),
        {
            "buildings_geojson": {
                "type": "FeatureCollection",
                "features": [{"i": i} for i in range(87)],
            },
            "source": "microsoft",
            "ms_count": 87,
            "google_count": 0,
            "grid3_estimate": 100,
            "count": 87,
            "notes": ["Microsoft footprints: 87"],
        },
    )()

    dataset = type(
        "DatasetRef",
        (),
        {
            "path": "/tmp/x.gpkg",
            "layer": "main_GRID3_NGA_settlement_extents_v4_0",
            "building_count_col": "building_count",
            "country_name": "Nigeria",
            "iso3": "NGA",
        },
    )()

    with (
        patch.object(rcs, "resolve_dataset_for_anchor", return_value=dataset),
        patch.object(rcs, "detect_communities", return_value=[_community(boundary_feat)]),
        patch.object(rcs, "fetch_building_footprints", return_value=footprints),
        patch.object(rcs, "upload_step_output", return_value={}),
    ):
        result = asyncio.run(rcs.resolve_community_site(ctx))

    assert result.error is None
    assert result.state_updates["geo_source"] == "community"
    assert result.state_updates["site_name"] == "Commville"
    assert result.state_updates["footprint_count"] == 87
    assert result.state_updates["grid3_building_count"] == 100
    assert result.data["buildings_geojson"]["features"]


def test_resolve_community_site_requires_coordinates():
    ctx = _Ctx({"community_name": "NoCoords"})
    result = asyncio.run(rcs.resolve_community_site(ctx))
    assert result.error is not None


def test_resolve_sites_skips_for_community_route():
    from orchestrator.experts.handlers.package_generator.resolve_sites import resolve_sites

    class _CommCtx:
        def __init__(self):
            self._state = {"geo_source": "community", "site_name": "Commville"}

        def get_input(self, k, d=None):
            return self._state.get(k, d)

        def get_state(self, k, d=None):
            return self._state.get(k, d)

        @property
        def effective_org_id(self):
            return 2

        async def send_progress_to_user(self, *a, **k):
            return True

    result = asyncio.run(resolve_sites(_CommCtx()))
    assert result.error is None
    assert result.data.get("sites_to_process") in (None, [])
