"""Fetch geo hazard step handler for Light Preliminary Package.

Fetches worst-case flood depth (WRI Aqueduct RP1000) and terrain elevation
(Copernicus DEM GLO-30) for each power plant site candidate.

Flood depth is expressed in metres above ground — no DEM subtraction needed.
Boundary terrain stats (min/max elevation range) are computed using each
candidate's polygon boundary from the site selection step.

Results are stored per candidate (geo_hazard_per_candidate list) and as
top-level fields for the rank-1 (best) candidate, for backwards-compatible
consumption by populate_cells and dump_values.
"""

import json
from typing import Any, Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_FLOOD_FIELDS = [
    "flood_worst_case_depth_m",
    "flood_riverine_rp1000_m",
    "flood_coastal_rp1000_m",
    "flood_rp100_historical_m",
    "flood_rp100_rcp85_2050_median_m",
    "flood_rp100_rcp85_2050_max_m",
    "site_elevation_m",
    "boundary_min_elevation_m",
    "boundary_max_elevation_m",
    "boundary_elevation_range_m",
]


def _candidate_summary(candidate: Dict[str, Any]) -> str:
    rank = candidate.get("rank", "?")
    lat = candidate.get("lat", "?")
    lon = candidate.get("lon", "?")
    flood = candidate.get("flood_worst_case_depth_m", "?")
    elev = candidate.get("site_elevation_m", "?")
    return f"Rank {rank} ({lat:.4f},{lon:.4f}): flood={flood}m elev={elev}m"


@register_step("fetch_geo_hazard")
async def fetch_geo_hazard(context: StepContext) -> StepResult:
    """Fetch flood depth and terrain elevation for each power plant site candidate.

    Uses site_candidates from generate_distribution_layout (each with lat/lon and
    polygon boundary). Calls solar_get_site_geo_hazard for each candidate so that
    boundary elevation stats are computed against the actual candidate plot, not the
    community center.

    Falls back to the community center (from generate_distribution_map) if no
    candidates are available (e.g. layout step was skipped).

    Requires:
    - generate_distribution_layout must have run first (provides site_candidates)
    - generate_distribution_map provides fallback coordinates if needed
    """
    # Idempotency guard — restore full per-candidate list on re-entry
    if context.get_state("geo_hazard_fetched"):
        LOGGER.info("fetch_geo_hazard: already done, skipping")
        per_candidate = context.get_state("geo_hazard_per_candidate") or []
        best = per_candidate[0] if per_candidate else {}
        return StepResult(
            data={
                "geo_hazard_per_candidate": per_candidate,
                **{f: best.get(f) for f in _FLOOD_FIELDS},
            },
            state_updates={},
            progress_message="Geo hazard data already fetched.",
        )

    # Resolve site candidates — prefer layout result, fall back to state
    layout_result = context.get_previous_result("generate_distribution_layout")
    site_candidates: List[Dict[str, Any]] = (
        (layout_result or {}).get("site_candidates") or context.get_state("site_candidates") or []
    )

    # Fallback: no candidates — use community center from map step
    if not site_candidates:
        LOGGER.info("No site_candidates available — falling back to community center")
        map_result = context.get_previous_result("generate_distribution_map")
        if not map_result:
            return StepResult.failure(
                "No site candidates or map data available — run generate_distribution_layout first"
            )
        center = map_result.get("center", {})
        lat = center.get("lat")
        lon = center.get("lon")
        if lat is None or lon is None:
            return StepResult.failure("No coordinates available from map generation")
        site_candidates = [{"rank": 1, "lat": lat, "lon": lon}]

    LOGGER.info(f"Fetching geo hazard data for {len(site_candidates)} site candidate(s)")

    await context.send_progress_to_user(
        f"Fetching flood and terrain data for {len(site_candidates)} "
        f"power plant site candidate(s)..."
    )

    geo_hazard_per_candidate: List[Dict[str, Any]] = []

    for candidate in site_candidates:
        lat = candidate.get("lat")
        lon = candidate.get("lon")
        rank = candidate.get("rank", 1)

        if lat is None or lon is None:
            LOGGER.warning(f"Candidate rank={rank} has no coordinates — skipping")
            continue

        tool_args: Dict[str, Any] = {"latitude": lat, "longitude": lon}
        polygon = candidate.get("polygon")
        if polygon:
            tool_args["power_plant_boundary_geojson"] = json.dumps(polygon)

        try:
            result_str = await context.mcp_executor.call_tool(
                "solar_get_site_geo_hazard",
                tool_args,
            )

            if isinstance(result_str, str) and result_str.startswith("Error:"):
                LOGGER.warning(f"Geo hazard tool error for candidate rank={rank}: {result_str}")
                continue

            result: Dict[str, Any] = (
                json.loads(result_str) if isinstance(result_str, str) else result_str
            )
        except json.JSONDecodeError as e:
            LOGGER.error(f"Failed to parse geo hazard response for candidate rank={rank}: {e}")
            continue
        except Exception as e:
            LOGGER.error(f"Geo hazard call failed for candidate rank={rank}: {e}")
            continue

        entry: Dict[str, Any] = {"rank": rank, "lat": lat, "lon": lon}
        for field in _FLOOD_FIELDS:
            entry[field] = result.get(field)
        geo_hazard_per_candidate.append(entry)

        LOGGER.info(f"Geo hazard fetched: {_candidate_summary(entry)}")

    if not geo_hazard_per_candidate:
        return StepResult.failure(
            sanitize_error_for_user(
                "Geo hazard lookup failed for all site candidates", "geo_hazard"
            )
        )

    # Rank-1 candidate values as top-level fields for downstream consumers
    best = geo_hazard_per_candidate[0]

    summaries = " | ".join(_candidate_summary(c) for c in geo_hazard_per_candidate)
    progress = f"Geo hazard ({len(geo_hazard_per_candidate)} candidates): {summaries}"

    return StepResult(
        data={
            "geo_hazard_per_candidate": geo_hazard_per_candidate,
            **{f: best.get(f) for f in _FLOOD_FIELDS},
        },
        state_updates={
            "geo_hazard_fetched": True,
            "geo_hazard_per_candidate": geo_hazard_per_candidate,
            # Rank-1 values also in state for single-field consumers
            "flood_worst_case_depth_m": best.get("flood_worst_case_depth_m", 0.0),
            "flood_riverine_rp1000_m": best.get("flood_riverine_rp1000_m", 0.0),
            "flood_coastal_rp1000_m": best.get("flood_coastal_rp1000_m", 0.0),
            "flood_rp100_historical_m": best.get("flood_rp100_historical_m", 0.0),
            "flood_rp100_rcp85_2050_median_m": best.get("flood_rp100_rcp85_2050_median_m", 0.0),
            "flood_rp100_rcp85_2050_max_m": best.get("flood_rp100_rcp85_2050_max_m", 0.0),
            "site_elevation_m": best.get("site_elevation_m"),
        },
        progress_message=progress,
    )
