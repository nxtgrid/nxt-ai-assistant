import asyncio
import base64
import time
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


def _community_row_with_layout():
    return {
        "id": None,
        "site_name": "Commville",
        "outline_geom": b"x",
        "buildings_geo_flat": {"features": [{"a": 1}]},
        "poles_geo_flat": {"features": []},
        "distribution_geo_flat": {"features": []},
        "meta_geo_flat": {},
        "site_details": '{"state": "Edo"}',
    }


def test_site_options_map_skips_duplicate_upload_when_layout_step_already_uploaded():
    """Regression test for Phase B Issue 1 (duplicate/interleaved site_options artifact
    history): when generate_distribution_layout already uploaded the site_options_map and
    stashed site_options_drive_id in packet_state, generate_distribution_map must not
    upload the same image again or re-emit the drive_id in its own state_updates -- doing
    so would make the workflow executor's artifact sweep record a second, redundant
    version for what is conceptually a single "site_options" artifact.
    """
    community_row = _community_row_with_layout()
    site_options_b64 = base64.b64encode(b"fake site options png bytes").decode()

    async def fake_load(ctx, db_config):
        return dict(community_row)

    upload_calls = []

    async def fake_upload(*, files, **kwargs):
        label = files[0][2]
        upload_calls.append(label)
        return {label: f"drive-{label}"}

    ctx = _Ctx()
    ctx._state["site_options_drive_id"] = "existing-drive-id"

    with (
        patch.object(gm, "load_site_row_data", side_effect=fake_load),
        patch.object(
            gm, "_get_db_config", return_value={"host": "h", "user": "u", "password": "p"}
        ),
        patch.object(gm, "_lookup_site_by_name"),
        patch.object(
            gm,
            "_run_layout_engine",
            return_value={
                "poles_geo_flat": {"features": [1]},
                "meta_geo_flat": {},
                "site_options_map_b64": site_options_b64,
            },
        ),
        patch.object(
            gm, "generate_site_map", return_value={"success": True, "metadata": {}, "image": "AAAA"}
        ),
        patch.object(gm, "upload_step_output", side_effect=fake_upload),
    ):
        result = asyncio.run(gm.generate_distribution_map(ctx))

    assert result.error is None
    assert "site_options_map" not in upload_calls  # no duplicate Drive upload
    assert "site_options_drive_id" not in result.state_updates  # no duplicate sweep entry
    # Still exposed in `data` for same-execution downstream consumers.
    assert result.data["site_options_drive_id"] == "existing-drive-id"


def test_site_options_map_uploads_when_not_already_present():
    """Companion to the skip-duplicate regression test: when no prior step has uploaded
    a site_options_map yet, generate_distribution_map must still perform the upload and
    record the new drive_id in state_updates."""
    community_row = _community_row_with_layout()
    site_options_b64 = base64.b64encode(b"fake site options png bytes").decode()

    async def fake_load(ctx, db_config):
        return dict(community_row)

    upload_calls = []

    async def fake_upload(*, files, **kwargs):
        label = files[0][2]
        upload_calls.append(label)
        return {label: f"drive-{label}"}

    ctx = _Ctx()  # no site_options_drive_id preset

    with (
        patch.object(gm, "load_site_row_data", side_effect=fake_load),
        patch.object(
            gm, "_get_db_config", return_value={"host": "h", "user": "u", "password": "p"}
        ),
        patch.object(gm, "_lookup_site_by_name"),
        patch.object(
            gm,
            "_run_layout_engine",
            return_value={
                "poles_geo_flat": {"features": [1]},
                "meta_geo_flat": {},
                "site_options_map_b64": site_options_b64,
            },
        ),
        patch.object(
            gm, "generate_site_map", return_value={"success": True, "metadata": {}, "image": "AAAA"}
        ),
        patch.object(gm, "upload_step_output", side_effect=fake_upload),
    ):
        result = asyncio.run(gm.generate_distribution_map(ctx))

    assert result.error is None
    assert "site_options_map" in upload_calls  # upload happens when nothing exists yet
    assert result.state_updates["site_options_drive_id"] == "drive-site_options_map"


def test_distribution_and_site_options_uploads_run_concurrently():
    community_row = _community_row_with_layout()
    site_options_b64 = base64.b64encode(b"fake site options png bytes").decode()

    async def fake_load(ctx, db_config):
        return dict(community_row)

    upload_calls = []

    async def fake_upload(*, files, **kwargs):
        label = files[0][2]
        upload_calls.append(label)
        await asyncio.sleep(0.2)
        return {label: f"drive-{label}"}

    ctx = _Ctx()

    started = time.monotonic()
    with (
        patch.object(gm, "load_site_row_data", side_effect=fake_load),
        patch.object(
            gm, "_get_db_config", return_value={"host": "h", "user": "u", "password": "p"}
        ),
        patch.object(gm, "_lookup_site_by_name"),
        patch.object(
            gm,
            "_run_layout_engine",
            return_value={
                "poles_geo_flat": {"features": [1]},
                "meta_geo_flat": {},
                "site_options_map_b64": site_options_b64,
            },
        ),
        patch.object(
            gm, "generate_site_map", return_value={"success": True, "metadata": {}, "image": "AAAA"}
        ),
        patch.object(gm, "upload_step_output", side_effect=fake_upload),
    ):
        result = asyncio.run(gm.generate_distribution_map(ctx))
    elapsed = time.monotonic() - started

    assert result.error is None
    assert set(upload_calls) == {"distribution_map", "site_options_map"}
    assert elapsed < 0.35


def test_fallback_layout_generation_does_not_block_event_loop():
    community_row = _community_row_with_layout()

    def slow_layout(row_data, site_name=""):
        time.sleep(0.15)
        return {"poles_geo_flat": {"features": [1]}, "meta_geo_flat": {}}

    async def fake_upload(*, files, **kwargs):
        label = files[0][2]
        return {label: f"drive-{label}"}

    async def run_render():
        ctx = _Ctx()
        heartbeat = asyncio.create_task(asyncio.sleep(0.05))
        try:
            result = await gm._render_map_from_row_data(
                ctx, dict(community_row), site_id=None, site_name="Commville"
            )
            return result, heartbeat.done()
        finally:
            if not heartbeat.done():
                heartbeat.cancel()

    with (
        patch.object(gm, "_run_layout_engine", side_effect=slow_layout),
        patch.object(
            gm, "generate_site_map", return_value={"success": True, "metadata": {}, "image": "AAAA"}
        ),
        patch.object(gm, "upload_step_output", side_effect=fake_upload),
    ):
        result, heartbeat_completed_during_layout = asyncio.run(run_render())

    assert result.error is None
    assert heartbeat_completed_during_layout is True


def test_power_heatmap_closes_partial_figure_when_colormap_fails(monkeypatch):
    import matplotlib.cm as cm
    import matplotlib.pyplot as plt

    plt.close("all")

    distribution_geojson = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "LineString",
                    "coordinates": [(9.0, 9.0), (9.001, 9.001)],
                },
                "properties": {"cable_type": "backbone", "power_kw": 1.0},
            }
        ],
    }

    def fail_get_cmap(*args, **kwargs):
        raise RuntimeError("colormap failed")

    monkeypatch.setattr(cm, "get_cmap", fail_get_cmap, raising=False)

    assert gm._render_power_heatmap(distribution_geojson) is None
    assert plt.get_fignums() == []
