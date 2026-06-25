import asyncio
from unittest.mock import patch

from orchestrator.experts.handlers.package_generator import generate_map as gm


class _Ctx:
    def __init__(self):
        self._state = {"geo_source": "community", "site_name": "Commville"}
        self.user_input = ""
        self.accumulated_results = {}

    def get_state(self, k, d=None):
        return self._state.get(k, d)

    def get_input(self, k, d=None):
        return self._state.get(k, d)

    def get_previous_result(self, s):
        return None

    async def send_progress_to_user(self, *a, **k):
        return True


def test_map_community_route_skips_pd_site_submissions_lookup():
    community_row = {
        "id": None,
        "site_name": "Commville",
        "outline_geom": b"x",
        "buildings_geo_flat": {"features": [{"a": 1}]},
        "poles_geo_flat": {"features": []},
        "distribution_geo_flat": {"features": []},
        "meta_geo_flat": {},
        "site_details": '{"state": "Edo"}',
    }

    async def fake_load(ctx, db_config):
        return dict(community_row)

    with (
        patch.object(gm, "load_site_row_data", side_effect=fake_load),
        patch.object(
            gm, "_get_db_config", return_value={"host": "h", "user": "u", "password": "p"}
        ),
        patch.object(gm, "_lookup_site_by_name") as lookup_name,
        patch.object(
            gm,
            "_run_layout_engine",
            return_value={"poles_geo_flat": {"features": [1]}, "meta_geo_flat": {}},
        ),
        patch.object(
            gm, "generate_site_map", return_value={"success": True, "metadata": {}, "image": "AAAA"}
        ),
        patch.object(gm, "upload_step_output", return_value={}),
    ):
        result = asyncio.run(gm.generate_distribution_map(_Ctx()))

    lookup_name.assert_not_called()  # community route must not hit pd_site_submissions
    assert result.error is None
