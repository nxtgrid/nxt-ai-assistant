import asyncio
import importlib
from unittest.mock import AsyncMock, patch

# NOTE: importlib.import_module (not `from package_generator import generate_qgis_project`
# or `import package_generator.generate_qgis_project as gq`) is required here:
# package_generator/__init__.py does
# `from .generate_qgis_project import generate_qgis_project`, which rebinds the
# `generate_qgis_project` attribute on the `package_generator` package to the *function*,
# shadowing the submodule of the same name. Both of the more natural import forms resolve
# `generate_qgis_project` via attribute access on the already-imported package and get the
# function, not the module. Going straight to sys.modules via importlib sidesteps that.
gq = importlib.import_module(
    "orchestrator.experts.handlers.package_generator.generate_qgis_project"
)


class _Ctx:
    def __init__(self):
        self._state = {
            "site_folder_id": "folder-1",
            "site_name": "TestSite",
        }
        self.packet_state = self._state
        self._layout_result = {
            "poles_geo_flat": {},
            "distribution_geo_flat": {},
            "site_boundary_wgs84": None,
        }

    def get_state(self, k, d=None):
        return self._state.get(k, d)

    def get_previous_result(self, step_name):
        if step_name == "generate_distribution_layout":
            return self._layout_result
        return None

    def get_input(self, k, d=None):
        return self._state.get(k, d)

    def get_parameter_value(self, k, d=None):
        return self._state.get(k, d)

    async def send_progress_to_user(self, *a, **k):
        return True


def test_qgis_project_state_updates_include_drive_ids_for_both_uploads():
    """upload_step_output's return values must land in state_updates as
    *_drive_id keys (not discarded) so the workflow executor's artifact sweep
    can attach them to the design's artifact history."""
    ctx = _Ctx()

    async def fake_upload(*, files, **kwargs):
        label = files[0][2]
        if label == "distribution_design_draft":
            return {"distribution_design_draft": "drive-qgs-id"}
        return {"distribution_network": "drive-gpkg-id"}

    with (
        patch(
            "shared.layout.annotations.place_lightning_arrestors",
            return_value=[1, 2],
        ),
        patch(
            "shared.layout.annotations.place_power_jumpers",
            return_value=[1, 2, 3],
        ),
        patch(
            "shared.layout.qgis_export.build_qgis_project",
            return_value=(b"qgs-bytes", b"gpkg-bytes"),
        ),
        patch(
            "shared.utils.drive_upload.upload_step_output",
            new=AsyncMock(side_effect=fake_upload),
        ),
    ):
        result = asyncio.run(gq.generate_qgis_project(ctx))

    assert result.error is None
    assert result.state_updates["qgis_project_uploaded"] is True
    assert result.state_updates["distribution_design_draft_drive_id"] == "drive-qgs-id"
    assert result.state_updates["distribution_network_drive_id"] == "drive-gpkg-id"
    assert result.data["lightning_arrestor_count"] == 2
    assert result.data["power_jumper_count"] == 3


def test_qgis_project_idempotency_guard_leaves_state_updates_empty():
    """Resume-after-success path (qgis_project_uploaded already True) must
    remain untouched: no drive-id keys need to be re-added since nothing ran."""
    ctx = _Ctx()
    ctx._state["qgis_project_uploaded"] = True

    result = asyncio.run(gq.generate_qgis_project(ctx))

    assert result.error is None
    assert result.state_updates == {}
    assert result.data == {"qgis_project_uploaded": True}


def test_qgis_project_omits_drive_id_when_upload_returns_no_id():
    """If upload_step_output returns a dict without the expected label (e.g.
    upload failed non-fatally), the corresponding *_drive_id key must be
    omitted rather than set to a falsy/missing value."""
    ctx = _Ctx()

    async def fake_upload_missing(*, files, **kwargs):
        return {}

    with (
        patch(
            "shared.layout.annotations.place_lightning_arrestors",
            return_value=[],
        ),
        patch(
            "shared.layout.annotations.place_power_jumpers",
            return_value=[],
        ),
        patch(
            "shared.layout.qgis_export.build_qgis_project",
            return_value=(b"qgs-bytes", b"gpkg-bytes"),
        ),
        patch(
            "shared.utils.drive_upload.upload_step_output",
            new=AsyncMock(side_effect=fake_upload_missing),
        ),
    ):
        result = asyncio.run(gq.generate_qgis_project(ctx))

    assert result.error is None
    assert "distribution_design_draft_drive_id" not in result.state_updates
    assert "distribution_network_drive_id" not in result.state_updates
    assert result.state_updates["qgis_project_uploaded"] is True
