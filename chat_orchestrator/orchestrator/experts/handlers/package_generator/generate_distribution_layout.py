"""Generate distribution layout step handler for Light Preliminary Package.

This handler auto-generates a distribution network layout (poles, backbone
cables, drop cables) for all sites. The output is BoM-grade: pole counts and
cable lengths feed directly into the AppSheet design engine for Bill of
Materials and cost calculations. Also produces site_candidates for the
generate_site_layout step.
"""

import asyncio
import os

from orchestrator.experts.handlers.package_generator.generate_map import _get_db_config
from orchestrator.experts.handlers.package_generator.site_geo_source import load_site_row_data
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.mapping.data_reader import _ensure_dict
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default algorithm parameters (overridable via env vars from settings UI)
DEFAULT_POLE_SPACING_M = float(os.getenv("LAYOUT_POLE_SPACING_M", "45.0"))
DEFAULT_MAX_DROP_DISTANCE_M = float(os.getenv("LAYOUT_MAX_DROP_DISTANCE_M", "40.0"))
DEFAULT_TARGET_COVERAGE = float(os.getenv("LAYOUT_TARGET_COVERAGE_PCT", "90.0"))

# Retry configuration for Overpass API
OVERPASS_RETRY_DELAY_S = 5


def _has_existing_layout(row_data: dict) -> bool:
    """Return True if row_data already has a QGIS-generated distribution layout."""
    poles = _ensure_dict(row_data.get("poles_geo_flat"))
    return bool(poles.get("features"))


def _existing_layout_state(
    spacing_m: float,
    max_drop_m: float,
    target_cov: float,
    context,
    site_candidates: list | None = None,
) -> dict:
    """Return the state_updates dict for existing-layout fast-path returns.

    Centralises the editable parameters so both early-return paths (no boundary
    and full site-candidates run) stay in sync when new parameters are added.
    """
    return {
        "site_candidates": site_candidates if site_candidates is not None else [],
        "editable_pole_spacing_m": spacing_m,
        "editable_max_drop_distance_m": max_drop_m,
        "editable_target_coverage_pct": target_cov,
        "editable_number_of_phases": (
            context.get_parameter_value("editable_number_of_phases")
            or context.get_state("editable_number_of_phases")
            or "1"
        ),
    }


@register_step("generate_distribution_layout")
async def generate_distribution_layout(context: StepContext) -> StepResult:
    """Generate distribution layout for the site.

    This step runs before generate_distribution_map in the LPP workflow. It:
    1. Extracts road network from OpenStreetMap via OSMnx
    2. Places poles along roads (or building paths if no roads), connects buildings, optimizes backbone
    3. Returns layout GeoJSON for generate_distribution_map to merge into row_data

    For sites with an existing QGIS layout (poles_geo_flat has features), runs in
    site_candidates_only mode: performs site selection only (~5-15s) to populate
    site_candidates for generate_site_layout, without regenerating poles or cables.
    """
    # Idempotency guard: layout already generated (handles recovery re-entry)
    if context.get_state("layout_generated"):
        LOGGER.info("generate_distribution_layout: already done, skipping")
        return StepResult(
            data={
                "layout_generated": True,
                "site_candidates": context.get_state("site_candidates") or [],
                # Return enough for generate_map to know layout exists
                "skipped": False,
            },
            state_updates={},
            progress_message="Distribution layout already generated.",
        )

    is_community = context.get_state("geo_source") == "community"
    site_id = context.get_input("site_id") or context.get_state("site_id")
    site_name = context.get_input("site_name") or context.get_state("site_name")

    if not is_community and not site_id:
        LOGGER.info("No site_id available yet — skipping distribution layout")
        return StepResult(
            data={"skipped": True, "skip_reason": "no_site_id"},
            state_updates={"site_candidates": []},
            progress_message="Site not yet resolved — layout generation deferred.",
        )

    db_config = _get_db_config()
    if not is_community and not db_config.get("host"):
        LOGGER.warning("AUTH_DB_HOST not set — skipping distribution layout")
        return StepResult(
            data={"skipped": True, "skip_reason": "no_db_config"},
            state_updates={"site_candidates": []},
        )

    # Fetch geo data via the route-agnostic resolver (DB row OR community boundary+footprints)
    try:
        row_data = await load_site_row_data(context, db_config)
    except ValueError:
        LOGGER.warning(f"Site geo not found (site_id={site_id}, community={is_community})")
        return StepResult(
            data={"skipped": True, "skip_reason": "site_not_found"},
            state_updates={"site_candidates": []},
        )
    except Exception as e:
        LOGGER.exception(f"Error resolving site geo: {e}")
        return StepResult(
            data={"skipped": True, "skip_reason": "db_error"},
            state_updates={"site_candidates": []},
            progress_message="Could not fetch site data.",
        )

    has_existing = _has_existing_layout(row_data)

    # Read editable parameters (use get_parameter_value so user confirmation overrides take effect)
    spacing_m = context.get_parameter_value("editable_pole_spacing_m") or DEFAULT_POLE_SPACING_M
    max_drop_m = (
        context.get_parameter_value("editable_max_drop_distance_m") or DEFAULT_MAX_DROP_DISTANCE_M
    )
    target_cov = (
        context.get_parameter_value("editable_target_coverage_pct") or DEFAULT_TARGET_COVERAGE
    )

    # Use confirmed target kWp for site candidate sizing when available.
    # On a first run this is None (design runs later), so find_plant_sites falls
    # back to a building-count estimate. On reruns after parameter confirmation
    # the correct value is already in state.
    actual_kwp = context.get_parameter_value("editable_total_kwp") or context.get_state(
        "editable_total_kwp"
    )
    kwp_for_layout = float(actual_kwp) if actual_kwp else None

    # Parse boundary
    from shared.mapping.data_reader import extract_site_boundary

    try:
        boundary_obj = extract_site_boundary(row_data["outline_geom"])
        boundary_polygon = boundary_obj.polygon
    except Exception as e:
        if has_existing:
            # No boundary — return fast path with empty site_candidates
            LOGGER.warning(
                f"No boundary for site {site_id} with existing layout — site_candidates=[]: {e}"
            )
            return StepResult(
                data={"skipped": True, "skip_reason": "existing_layout"},
                state_updates=_existing_layout_state(spacing_m, max_drop_m, target_cov, context),
                progress_message=f"Using existing QGIS layout for {site_name or f'Site {site_id}'}.",
            )
        LOGGER.exception(f"Failed to parse boundary for site {site_id}: {e}")
        return StepResult(
            data={"skipped": True, "skip_reason": "no_boundary"},
            state_updates={"site_candidates": []},
            progress_message="Could not parse site boundary.",
        )

    buildings_geojson = _ensure_dict(row_data.get("buildings_geo_flat"), default={"features": []})
    building_count = len(buildings_geojson.get("features", []))

    from shared.layout import generate_layout

    # Single definition of generate_layout kwargs — avoids copy-paste divergence on retries.
    # Timeouts: 30s for site-candidates-only pass, 180s for full layout (OSMnx + KDTree + Dijkstra).
    async def _run_layout(site_candidates_only: bool = False) -> dict | None:
        timeout = 30 if site_candidates_only else 180
        return await asyncio.wait_for(
            asyncio.to_thread(
                generate_layout,
                boundary=boundary_polygon,
                buildings_geojson=buildings_geojson,
                spacing_m=spacing_m,
                max_drop_distance_m=max_drop_m,
                target_coverage=target_cov,
                kwp=kwp_for_layout,
                site_name=site_name or f"Site {site_id}",
                site_candidates_only=site_candidates_only,
            ),
            timeout=timeout,
        )

    # Fast path for QGIS sites: run site selection only, skip full pole/cable generation
    if has_existing:
        await context.send_progress_to_user(
            f"Site {site_name or f'Site {site_id}'} has an existing QGIS layout — "
            "extracting plant site candidates."
        )
        if building_count > 0:
            fast_result = await _run_layout(site_candidates_only=True)
            site_candidates = (fast_result or {}).get("site_candidates", [])
        else:
            site_candidates = []
        return StepResult(
            data={"skipped": True, "skip_reason": "existing_layout"},
            state_updates=_existing_layout_state(
                spacing_m, max_drop_m, target_cov, context, site_candidates
            ),
            progress_message=f"Using existing QGIS layout for {site_name or f'Site {site_id}'}.",
        )

    if building_count == 0:
        LOGGER.info(f"Site {site_id} has no buildings — skipping layout")
        return StepResult(
            data={"skipped": True, "skip_reason": "no_buildings"},
            state_updates={"site_candidates": []},
            progress_message="No buildings found for layout.",
        )

    # Send progress message before long OSMnx operation
    await context.send_progress_to_user(
        f"Generating distribution layout for {site_name or f'Site {site_id}'}...\n"
        f"Extracting road network from OpenStreetMap ({building_count} buildings)."
    )

    # Run the layout algorithm (blocking I/O + CPU — run in thread)
    try:
        layout_result = await _run_layout()
    except asyncio.TimeoutError:
        LOGGER.error(f"Distribution layout timed out after 180s for {site_name}")
        return StepResult.failure(
            f"Distribution layout timed out for {site_name or f'Site {site_id}'}. "
            "The road network may be too complex."
        )
    except Exception as e:
        err_name = type(e).__name__
        # Check if retryable (Overpass API transient errors)
        if any(
            kw in err_name for kw in ["ConnectionError", "TimeoutError", "HTTPError", "Timeout"]
        ) or any(kw in str(e) for kw in ["429", "504", "timeout"]):
            LOGGER.warning(
                f"Overpass API transient error, retrying in {OVERPASS_RETRY_DELAY_S}s: {e}"
            )
            await asyncio.sleep(OVERPASS_RETRY_DELAY_S)
            try:
                layout_result = await _run_layout()
            except asyncio.TimeoutError:
                LOGGER.error(f"Distribution layout retry timed out for {site_name}")
                return StepResult.failure(
                    f"Distribution layout timed out for {site_name or f'Site {site_id}'}."
                )
            except Exception as retry_err:
                LOGGER.exception(f"Overpass API retry failed: {retry_err}")
                return StepResult.failure(sanitize_error_for_user(str(retry_err)))
        else:
            LOGGER.exception(f"Layout generation failed: {e}")
            return StepResult.failure(sanitize_error_for_user(str(e)))

    if layout_result is None:
        await context.send_progress_to_user(
            f"No road data available in OpenStreetMap for {site_name or f'Site {site_id}'}. "
            "Map will be generated without distribution layout. "
            "Cable lengths and pole counts will not be available for BoM."
        )
        return StepResult(
            data={},
            progress_message="Road data unavailable — layout skipped.",
        )

    # Extract coverage for reporting
    meta = layout_result.get("meta_geo_flat", {})
    coverage_pct = meta.get("coverage_percentage", 0.0)
    pole_count = meta.get("pole_count", 0)
    backbone_m = meta.get("backbone_cable_length_m", 0.0)
    drop_m = meta.get("drop_cable_length_m", 0.0)

    # Warn if coverage is below target
    coverage_msg = f"{coverage_pct:.0f}% building coverage"
    if coverage_pct < target_cov:
        coverage_msg += (
            f" (target: {target_cov:.0f}%). "
            "Cable lengths and pole counts may be underestimated. "
            "Review before finalizing BoM."
        )

    progress = (
        f"Distribution layout generated for {site_name or f'Site {site_id}'}:\n"
        f"  {pole_count} poles, {backbone_m:,.0f}m backbone, "
        f"{drop_m:,.0f}m drop cables\n"
        f"  {coverage_msg}"
    )

    await context.send_progress_to_user(progress)

    # Upload site options map to Drive (non-fatal, async)
    # Store Drive file ID instead of base64 blob to avoid packet_state bloat
    site_options_drive_id = ""
    site_options_b64 = layout_result.get("site_options_map_b64")
    if site_options_b64:
        import base64

        from shared.utils.drive_upload import upload_step_output

        drive_ids = await upload_step_output(
            site_folder_id=context.get_state("site_folder_id"),
            subfolder_name=None,
            site_name=site_name or f"Site_{site_id}",
            files=[(base64.b64decode(site_options_b64), "image/png", "site_options_map")],
        )
        site_options_drive_id = drive_ids.get("site_options_map", "")

    return StepResult(
        data=layout_result,
        state_updates={
            "layout_generated": True,
            "layout_coverage_pct": coverage_pct,
            "site_options_drive_id": site_options_drive_id,
            "site_candidates": layout_result.get("site_candidates", []),
            "editable_pole_spacing_m": spacing_m,
            "editable_max_drop_distance_m": max_drop_m,
            "editable_target_coverage_pct": target_cov,
            "editable_number_of_phases": (
                context.get_parameter_value("editable_number_of_phases")
                or context.get_state("editable_number_of_phases")
                or "1"
            ),
        },
        progress_message=f"Layout: {pole_count} poles, {coverage_pct:.0f}% coverage",
    )
