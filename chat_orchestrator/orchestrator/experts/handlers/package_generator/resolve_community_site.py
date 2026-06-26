"""Route B entry step for the LPP workflow: GPS anchor -> community boundary -> footprints.

Inputs (packet inputs / state):
    latitude (str, required), longitude (str, required)
    community_name (str, optional — overrides the GRID3-derived name)
    surveyed_buildings_geojson (optional — overrides fetched footprints downstream)

State written:
    geo_source = "community"   (tells load_site_row_data which route to use)
    site_name, community_state
    footprint_count            (single source of truth for planning numbers)
    grid3_building_count       (kept only for visibility / urban gate context)
    footprint_source, community_boundary_drive_id, community_buildings_drive_id
"""

from __future__ import annotations

import asyncio
import json
import math

from shapely.geometry import shape

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.layout.building_footprints import fetch_building_footprints
from shared.layout.community_detector import detect_communities
from shared.layout.settlement_datasets import (
    SettlementDataNotConfigured,
    SettlementDataUnavailable,
    resolve_dataset_for_anchor,
)
from shared.utils.drive_upload import upload_step_output
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("resolve_community_site")
async def resolve_community_site(context: StepContext) -> StepResult:
    # Idempotency: already resolved
    if context.get_state("geo_source") == "community" and context.get_state("footprint_count"):
        return StepResult(
            data={},
            state_updates={},
            progress_message="Community site already resolved.",
        )

    lat_str = str(context.get_input("latitude") or "")
    lon_str = str(context.get_input("longitude") or "")
    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except (TypeError, ValueError):
        return StepResult.failure(
            "An anchor GPS location is required. Provide latitude and longitude."
        )
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return StepResult.failure("Latitude and longitude must be finite numbers.")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return StepResult.failure("Coordinates out of range.")

    requested_name = (context.get_input("community_name") or "").strip()
    anchor_name = requested_name or f"anchor_{lat:.5f}_{lon:.5f}".replace(".", "_").replace(
        "-", "m"
    )

    # Resolve which country dataset covers this anchor (reverse-geocode + manifest).
    try:
        dataset = await asyncio.to_thread(resolve_dataset_for_anchor, lat, lon)
    except SettlementDataNotConfigured:
        return StepResult.failure("Community detection data is not available.")
    except SettlementDataUnavailable as e:
        return StepResult.failure(e.user_message())
    except Exception as e:
        LOGGER.exception(f"Dataset resolution failed: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    await context.send_progress_to_user(
        f"Detecting community boundary at ({lat_str}, {lon_str}) in {dataset.country_name}..."
    )

    try:
        results = await asyncio.to_thread(
            detect_communities,
            [{"lat": lat, "lon": lon, "name": anchor_name}],
            dataset.path,
            layer=dataset.layer,
            building_count_col=dataset.building_count_col,
            source_label=f"GRID3_{dataset.iso3}" if dataset.iso3 else None,
            skip_map=True,  # map_b64 is unused in the LPP workflow; skip satellite tile download
        )
    except FileNotFoundError:
        return StepResult.failure("Community detection data is not available.")
    except Exception as e:
        LOGGER.exception(f"Community detection failed: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    community = results[0]
    if community.error or not community.boundary:
        return StepResult.failure(
            f"Could not detect a usable community boundary at ({lat_str}, {lon_str})."
        )

    # community.community_name is "Unknown" only when OSM yields nothing usable;
    # don't let that literal become the site label — fall back to the anchor id.
    detected_name = community.community_name if community.community_name != "Unknown" else ""
    site_name = requested_name or detected_name or anchor_name
    grid3_estimate = int(community.building_count or 0)

    await context.send_progress_to_user(
        f"Fetching building footprints for {site_name} (GRID3 estimate ~{grid3_estimate})..."
    )

    boundary_polygon = shape(community.boundary["geometry"])
    try:
        footprints = await asyncio.to_thread(
            fetch_building_footprints, boundary_polygon, grid3_estimate
        )
    except Exception as e:
        LOGGER.exception(f"Footprint fetch failed for {site_name}: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    if footprints.count == 0:
        return StepResult.failure(f"No building footprints found within the {site_name} boundary.")

    # Persist artifacts to Drive (never the blobs into packet_state — JSONB timeout risk).
    boundary_bytes = json.dumps(community.boundary).encode("utf-8")
    buildings_bytes = json.dumps(footprints.buildings_geojson).encode("utf-8")
    drive_ids = await upload_step_output(
        site_folder_id=context.get_state("site_folder_id"),
        subfolder_name="Community Detection",
        site_name=site_name,
        files=[
            (boundary_bytes, "application/json", "community_boundary"),
            (buildings_bytes, "application/json", "community_buildings"),
        ],
    )

    LOGGER.info(
        f"resolve_community_site: {site_name} — footprints={footprints.count} "
        f"({footprints.source}), GRID3 estimate={grid3_estimate}"
    )

    return StepResult(
        # Full dicts in data for same-execution get_previous_result() in load_site_row_data.
        data={
            "boundary": community.boundary,
            "buildings_geojson": footprints.buildings_geojson,
            "footprint_count": footprints.count,
            "footprint_source": footprints.source,
            "grid3_building_count": grid3_estimate,
            "footprint_notes": footprints.notes,
        },
        state_updates={
            "geo_source": "community",
            "site_name": site_name,
            "community_state": (community.boundary.get("properties") or {}).get("state"),
            "footprint_count": footprints.count,
            "footprint_source": footprints.source,
            "grid3_building_count": grid3_estimate,
            "community_boundary_drive_id": drive_ids.get("community_boundary", ""),
            "community_buildings_drive_id": drive_ids.get("community_buildings", ""),
        },
        progress_message=(
            f"{site_name}: {footprints.count} footprints ({footprints.source}); "
            f"GRID3 estimate {grid3_estimate}"
        ),
    )
