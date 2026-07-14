"""Generate QGIS project file step handler for Light Preliminary Package.

Converts the distribution layout output into a .qgs + .gpkg pair with all
reference layers present. Runs after generate_distribution_layout (and
optionally after generate_site_layout).

Phase 2: Also auto-places lightning arrestors and power jumpers per operator
standards using network-distance algorithms on the backbone graph.

The .qgs and .gpkg are uploaded to Google Drive — bytes are NOT stored in state.
"""

import asyncio
from typing import Any

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step(
    "generate_qgis_project",
    contract=StepContract(
        description=(
            "Converts the distribution layout output into a .qgs + .gpkg project pair, "
            "auto-placing lightning arrestors and power jumpers on the backbone graph."
        ),
        consumes_state=(),
        # qgis_project_uploaded: idempotency guard. site_name: `get_input(...)
        # or get_state(...) or "Unknown"`. editable_number_of_phases /
        # editable_max_drop_distance_m: `get_parameter_value(...) or
        # <default>` (each also has a ParamSpec default below). site_folder_id:
        # passed to upload_step_output(), which documents "skip if None".
        optional_consumes_state=(
            "qgis_project_uploaded",
            "site_name",
            "editable_number_of_phases",
            "editable_max_drop_distance_m",
            "site_folder_id",
        ),
        produces_state=(
            "qgis_project_uploaded",
            "distribution_design_draft_drive_id",
            "distribution_network_drive_id",
        ),
        consumes_results=("generate_distribution_layout",),
        params=(
            ParamSpec(
                name="editable_number_of_phases",
                description="Number of electrical phases for the distribution design.",
                default="1",
            ),
            ParamSpec(
                name="editable_max_drop_distance_m",
                param_type="number",
                description="Maximum drop-cable distance in metres from pole to building.",
                default=40.0,
            ),
        ),
        guard_keys=("qgis_project_uploaded",),
        side_effects=(
            "Auto-places lightning arrestors/power jumpers using network-distance "
            "algorithms; builds and uploads .qgs + .gpkg files to Google Drive."
        ),
    ),
)
async def generate_qgis_project(context: StepContext) -> StepResult:
    """Generate QGIS project file from layout and optional site plan data."""
    # Idempotency guard: QGIS project already uploaded (handles recovery re-entry)
    if context.get_state("qgis_project_uploaded"):
        LOGGER.info("generate_qgis_project: already done, skipping")
        return StepResult(
            data={"qgis_project_uploaded": True},
            state_updates={},
            progress_message="QGIS project already uploaded.",
        )

    # Prefer current-execution step result; fall back to persisted state for resume
    layout_result = context.get_previous_result("generate_distribution_layout") or {}
    if layout_result.get("skipped"):
        layout_result = {}
    if not layout_result:
        return StepResult(
            data={"skipped": True, "skip_reason": "no_layout_data"},
            progress_message="No layout data — skipping QGIS export.",
        )

    site_name = context.get_input("site_name") or context.get_state("site_name") or "Unknown"
    number_of_phases = context.get_parameter_value("editable_number_of_phases") or "1"
    max_drop_m = float(context.get_parameter_value("editable_max_drop_distance_m") or 40.0)

    # Get boundary from layout result metadata
    site_boundary_wgs84 = layout_result.get("site_boundary_wgs84")

    await context.send_progress_to_user(f"Generating QGIS project file for {site_name}...")

    from shared.layout.annotations import place_lightning_arrestors, place_power_jumpers
    from shared.layout.qgis_export import build_qgis_project

    try:
        poles_geo = layout_result.get("poles_geo_flat", {})
        dist_geo = layout_result.get("distribution_geo_flat", {})

        # Phase 2: Auto-place lightning arrestors and power jumpers (parallel)
        arrestors_gdf, jumpers_gdf = await asyncio.gather(
            asyncio.to_thread(
                place_lightning_arrestors,
                poles_geojson=poles_geo,
                distribution_geojson=dist_geo,
            ),
            asyncio.to_thread(
                place_power_jumpers,
                poles_geojson=poles_geo,
                distribution_geojson=dist_geo,
            ),
        )

        qgs_bytes, gpkg_bytes = await asyncio.to_thread(
            build_qgis_project,
            layout_result=layout_result,
            site_name=site_name,
            number_of_phases=number_of_phases,
            max_drop_distance_m=max_drop_m,
            site_boundary_wgs84=site_boundary_wgs84,
            arrestors_gdf=arrestors_gdf,
            jumpers_gdf=jumpers_gdf,
        )
    except Exception as exc:
        LOGGER.exception("QGIS project generation failed: %s", exc)
        from shared.utils.error_messages import sanitize_error_for_user

        return StepResult.failure(sanitize_error_for_user(str(exc)))

    # Upload both .qgs and .gpkg to Google Drive (non-fatal).
    # Both must live in the same folder for the project to find its data.
    from shared.utils.drive_upload import upload_step_output

    site_folder_id = context.get_state("site_folder_id")
    qgs_ids, gpkg_ids = await asyncio.gather(
        upload_step_output(
            site_folder_id=site_folder_id,
            subfolder_name="Distribution Design",
            site_name=site_name,
            files=[(qgs_bytes, "application/xml", "distribution_design_draft")],
            explicit_extension="qgs",
        ),
        upload_step_output(
            site_folder_id=site_folder_id,
            subfolder_name="Distribution Design",
            site_name=site_name,
            files=[(gpkg_bytes, "application/geopackage+sqlite3", "distribution_network")],
            explicit_extension="gpkg",
        ),
    )

    arrestor_count = len(arrestors_gdf) if arrestors_gdf is not None else 0
    jumper_count = len(jumpers_gdf) if jumpers_gdf is not None else 0

    # Store Drive file IDs (not blobs) in state_updates so the workflow
    # executor's artifact sweep (shared/grid_design/artifact_log.py) can
    # attach these uploads to the design's artifact history.
    state_updates: dict[str, Any] = {"qgis_project_uploaded": True}
    if qgs_ids.get("distribution_design_draft"):
        state_updates["distribution_design_draft_drive_id"] = qgs_ids["distribution_design_draft"]
    if gpkg_ids.get("distribution_network"):
        state_updates["distribution_network_drive_id"] = gpkg_ids["distribution_network"]

    return StepResult(
        data={
            "qgis_project_uploaded": True,
            "lightning_arrestor_count": arrestor_count,
            "power_jumper_count": jumper_count,
        },
        state_updates=state_updates,
        progress_message=(
            f"QGIS project uploaded for {site_name} "
            f"({arrestor_count} arrestors, {jumper_count} jumpers)."
        ),
    )
