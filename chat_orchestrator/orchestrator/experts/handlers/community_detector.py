"""Expert step handler: detect community boundary from GRID3 GeoPackage.

Wraps shared/layout/community_detector.py (synchronous) via asyncio.to_thread()
to avoid blocking the FastAPI event loop during geopandas/Nominatim work.

Register in Expert Instructions Google Doc:
    [function:detect_community_boundary] - Detect community boundary for an anchor GPS point

Workflow inputs:
    anchor_name (str): Tag for the anchor location (e.g. IHS tower ID)
    latitude (str): Anchor latitude
    longitude (str): Anchor longitude

State written:
    community_name (str): OSM-derived community name
    community_building_count (int): Total building count across cluster
    community_map_drive_id (str): Drive file ID for the map PNG
    community_boundary_drive_id (str): Drive file ID for the boundary GeoJSON

Env vars required:
    SETTLEMENT_DATA_DIR: Location holding country GeoPackages + manifest.json
        (legacy GRID3_GPKG_PATH single-file mode still supported). The anchor's
        country is reverse-geocoded and matched against the manifest.
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.drive_upload import upload_step_output
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_ANCHOR_NAME_RE = re.compile(r"^[\w.\-]{1,100}$")


@register_step("detect_community_boundary")
async def detect_community_boundary(context: StepContext) -> StepResult:
    anchor_name = context.get_input("anchor_name") or ""
    if not _ANCHOR_NAME_RE.match(anchor_name):
        return StepResult.failure(
            "Invalid anchor name. Use only letters, numbers, underscores, hyphens, and dots (max 100 chars)."
        )

    try:
        lat = float(context.get_input("latitude"))
        lon = float(context.get_input("longitude"))
    except (TypeError, ValueError):
        return StepResult.failure("Latitude and longitude must be numbers.")
    if not (math.isfinite(lat) and math.isfinite(lon)):
        return StepResult.failure("Latitude and longitude must be finite numbers.")
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return StepResult.failure(
            "Coordinates out of range: latitude -90 to 90, longitude -180 to 180."
        )

    # ALL blocking work (geopandas, Nominatim HTTP, matplotlib) runs in thread
    from shared.layout.community_detector import detect_communities
    from shared.layout.settlement_datasets import (
        SettlementDataNotConfigured,
        SettlementDataUnavailable,
        resolve_dataset_for_anchor,
    )

    # Resolve which country dataset covers this anchor (reverse-geocode + manifest).
    try:
        dataset = await asyncio.to_thread(resolve_dataset_for_anchor, lat, lon)
    except SettlementDataNotConfigured:
        return StepResult.failure(
            "Community detection data is not available. Please contact support."
        )
    except SettlementDataUnavailable as e:
        return StepResult.failure(e.user_message())
    except Exception as e:
        LOGGER.exception(f"Dataset resolution failed for {anchor_name}: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    await context.send_progress_to_user(
        f"Detecting community boundary for {anchor_name} in {dataset.country_name}..."
    )

    try:
        results = await asyncio.to_thread(
            detect_communities,
            [{"lat": lat, "lon": lon, "name": anchor_name}],
            dataset.path,
            layer=dataset.layer,
            building_count_col=dataset.building_count_col,
            source_label=f"GRID3_{dataset.iso3}" if dataset.iso3 else None,
        )
    except FileNotFoundError:
        LOGGER.exception(f"GeoPackage not found for {anchor_name}")
        return StepResult.failure(
            "Community detection data is not available. Please contact support."
        )
    except Exception as e:
        LOGGER.exception(f"Community detection failed for {anchor_name}: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    community = results[0]
    map_bytes = base64.b64decode(community.map_b64) if community.map_b64 else b""
    boundary_bytes = json.dumps(community.boundary, indent=2).encode("utf-8")

    # Upload boundary GeoJSON (always); map PNG only when rendering wasn't skipped
    # Store Drive IDs in state — never the raw blobs (Supabase JSONB timeout risk)
    files = [(boundary_bytes, "application/json", "community_boundary")]
    if map_bytes:
        files.insert(0, (map_bytes, "image/png", "community_map"))
    drive_ids = await upload_step_output(
        site_folder_id=context.get_state("site_folder_id"),
        subfolder_name="Community Detection",
        site_name=anchor_name,
        files=files,
    )

    return StepResult(
        data={
            "anchor_name": community.anchor_name,
            "community_name": community.community_name,
            "boundary": community.boundary,  # full dict for get_previous_result() in same run
            "building_count": community.building_count,
            "block_count": community.block_count,
            "map_b64": community.map_b64,
        },
        state_updates={
            "community_name": community.community_name,
            "community_building_count": community.building_count,
            # Drive IDs only — not the boundary dict or base64 blobs
            "community_map_drive_id": drive_ids.get("community_map", ""),
            "community_boundary_drive_id": drive_ids.get("community_boundary", ""),
        },
        progress_message=(
            f"Community: {community.community_name} "
            f"({community.building_count} buildings, {community.block_count} blocks)"
        ),
    )
