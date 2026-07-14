"""Convergence layer: produce a pd_site_submissions-shaped `row_data` dict for
either LPP route (submission lookup OR community/GPS anchor).

Downstream handlers (generate_distribution_layout, generate_distribution_map)
consume `row_data` and never need to know which route produced it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from shapely import wkb
from shapely.geometry import shape

from shared.mapping.data_reader import _ensure_dict, fetch_site_pipeline_row
from shared.utils.drive_upload import download_drive_file
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def community_boundary_to_row_data(
    boundary_feature: dict[str, Any],
    buildings_geojson: dict[str, Any],
    site_name: str,
    site_state: Optional[str] = None,
    site_id: Any = None,
) -> dict[str, Any]:
    """Build a row_data dict (pd_site_submissions shape) from a community boundary.

    Empty poles/distribution/meta force a fresh layout in the downstream engine.
    `outline_geom` is WKB so the existing extract_site_boundary() works unchanged.
    """
    geom = shape(boundary_feature["geometry"])
    outline_wkb = wkb.dumps(geom)
    site_details = {"state": site_state, "source": "community"}
    return {
        "id": site_id,
        "site_name": site_name,
        "outline_geom": outline_wkb,
        "buildings_geo_flat": buildings_geojson,
        "distribution_geo_flat": {"features": []},
        "poles_geo_flat": {"features": []},
        "meta_geo_flat": {},
        "site_details": json.dumps(site_details),
    }


def _resolve_surveyed_buildings(context) -> Optional[dict[str, Any]]:
    """Return surveyed-buildings GeoJSON if supplied as an optional input/state value.

    Accepts a dict, a JSON string, or a list of features. Returns None when absent.
    """
    raw = context.get_input("surveyed_buildings_geojson") or context.get_state(
        "surveyed_buildings_geojson"
    )
    if not raw:
        return None
    fc = _ensure_dict(raw, default={"features": []})
    if fc.get("features"):
        LOGGER.info(f"Using surveyed buildings override: {len(fc['features'])} features")
        return fc
    return None


async def load_site_row_data(context, db_config: dict[str, Any]) -> dict[str, Any]:
    """Return a pd_site_submissions-shaped row_data dict for the active route.

    Route is selected by state['geo_source']:
      - "community": build from resolve_community_site results/state (no DB).
      - otherwise:   fetch the submission row by site_id.

    A surveyed-buildings override, when present, replaces buildings_geo_flat
    on either route.

    On the community route, boundary/buildings resolve in two tiers:
      1. Same-execution: context.get_previous_result("resolve_community_site")
         (populated only when resolve_community_site ran earlier in this same
         workflow execution).
      2. Cross-execution: download the boundary/buildings GeoJSON from Drive
         using the community_boundary_drive_id / community_buildings_drive_id
         IDs resolve_community_site persists to packet_state (the case
         run_single_step needs — resolve_community_site completed in a prior
         execution, so get_previous_result() is empty here).
    If neither tier yields a boundary, a ValueError is raised (callers may
    treat this as a genuine "site not resolved yet" failure).
    """
    surveyed = _resolve_surveyed_buildings(context)

    if context.get_state("geo_source") == "community":
        prev = context.get_previous_result("resolve_community_site") or {}

        boundary_feature = prev.get("boundary")
        if not boundary_feature:
            boundary_drive_id = context.get_state("community_boundary_drive_id")
            if boundary_drive_id:
                boundary_bytes = await download_drive_file(boundary_drive_id)
                boundary_feature = json.loads(boundary_bytes)

        buildings = surveyed or prev.get("buildings_geojson")
        if not buildings:
            buildings_drive_id = context.get_state("community_buildings_drive_id")
            if buildings_drive_id:
                buildings_bytes = await download_drive_file(buildings_drive_id)
                buildings = json.loads(buildings_bytes)

        if not boundary_feature:
            raise ValueError("Community route: no boundary available from resolve_community_site")
        row = community_boundary_to_row_data(
            boundary_feature,
            buildings or {"type": "FeatureCollection", "features": []},
            site_name=context.get_state("site_name") or "Community",
            site_state=context.get_state("community_state"),
        )
        return row

    # Submission route
    site_id = context.get_input("site_id") or context.get_state("site_id")
    row = await fetch_site_pipeline_row(site_id=int(site_id), db_config=db_config)
    if surveyed is not None:
        row["buildings_geo_flat"] = surveyed
    return row
