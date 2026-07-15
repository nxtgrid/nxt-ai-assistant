"""Generate site layout step handler for Light Preliminary Package.

Produces a to-scale power plant site layout diagram with solar arrays,
energy systems, lightning arresters, and infrastructure. Outputs Draw.io
XML (editable vector) and PNG (base64 for Telegram/state).

Runs after generate_distribution_map to use boundary and coordinate data.
"""

import asyncio
import math
import re

from shapely.geometry import Polygon

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step(
    "generate_site_layout",
    contract=StepContract(
        description=(
            "Generates a to-scale power plant site layout (Draw.io XML + PNG) with "
            "solar arrays, energy systems, and lightning arresters."
        ),
        # site_id: `if not site_id and not is_community: return
        # StepResult.failure(...)` -- a real hard failure (not a graceful
        # skip) on the non-community route. editable_total_kwp: ParamSpec
        # required=True below; `if target_kwp <= 0: return
        # StepResult.failure(...)`.
        consumes_state=("site_id", "editable_total_kwp"),
        # site_layout_png_drive_id: idempotency guard. site_name: `get_input
        # or get_state`, always guarded by `site_name or f"Site {site_id}"` at
        # every use site. geo_source: branch selector for the community-route
        # carve-out on site_id, defaulting to the (primary) non-community
        # check. editable_site_type / editable_panel_config: each has an
        # explicit `if not X: X = <kWp/site-type-based default>` fallback.
        # site_candidates: `context.get_state(...) or []`, with a further
        # legitimate fallback to a synthetic plant polygon when empty.
        # site_folder_id: passed to upload_step_output(), "skip if None".
        optional_consumes_state=(
            "site_layout_png_drive_id",
            "site_name",
            "geo_source",
            "technology_family",
            "editable_site_type",
            "editable_panel_config",
            "site_candidates",
            "site_folder_id",
        ),
        produces_state=(
            "site_layout_png_drive_id",
            "site_layout_drawio_drive_id",
            "editable_panel_config",
            "editable_site_type",
            "earth_pit_count",
            "avg_pv_combiner_distance_m",
            "feeder_pillar_distance_m",
        ),
        consumes_results=("generate_distribution_map",),
        params=(
            ParamSpec(
                name="editable_total_kwp",
                param_type="number",
                description="Target total kWp the site layout must accommodate.",
                required=True,
            ),
            ParamSpec(
                name="editable_site_type",
                description="Site type ('ess' or 'victron'); defaults by target kWp.",
            ),
            ParamSpec(
                name="technology_family",
                description=(
                    "Power plant technology family/architecture ('deye' for ESS layout, "
                    "'victron' for container layout)."
                ),
                synonyms=("technology type", "design type", "vendor", "equipment family", "deye", "victron"),
            ),
            ParamSpec(
                name="editable_panel_config",
                description="Panel series/parallel configuration (e.g. '20S2P').",
            ),
        ),
        guard_keys=("site_layout_png_drive_id",),
        side_effects=(
            "Runs geometry-packing plus PNG/Draw.io rendering (up to 120s); uploads "
            "the site layout PNG and Draw.io XML to Google Drive."
        ),
    ),
)
async def generate_site_layout(context: StepContext) -> StepResult:
    """Generate to-scale power plant site layout (Draw.io + PNG)."""
    # Idempotency guard: layout already uploaded to Drive (handles recovery re-entry)
    if context.get_state("site_layout_png_drive_id"):
        drive_id = context.get_state("site_layout_png_drive_id")
        LOGGER.info(f"generate_site_layout: already done (drive_id={drive_id}), skipping")
        return StepResult(
            data={"site_layout_png_drive_id": drive_id},
            state_updates={},
            progress_message="Site layout already generated.",
        )

    site_name = context.get_input("site_name") or context.get_state("site_name")
    # Strip HTML tags from site_name — it appears in Draw.io XML labels which may
    # be rendered as HTML in the diagrams.net web UI.
    if site_name:
        site_name = re.sub(r"<[^>]+>", "", site_name)[:100]
    site_id = context.get_input("site_id") or context.get_state("site_id")

    # Community route (Route B) is GPS-anchored and has no DB site_id — it works
    # from site_name + the map center + plant-site candidates. site_id is only
    # ever a display fallback for a missing site_name below, so don't block the
    # community route on it (mirrors the guard in generate_distribution_layout).
    is_community = context.get_state("geo_source") == "community"
    if not site_id and not is_community:
        return StepResult.failure("No site ID available — run generate_distribution_map first")

    # Target kWp
    target_kwp = float(context.get_parameter_value("editable_total_kwp") or 0)
    if target_kwp <= 0:
        return StepResult.failure("Target kWp must be greater than 0")

    # Site type: user parameter with kWp-based fallback
    site_type = context.get_parameter_value("editable_site_type")
    if not site_type:
        technology_family = (
            context.get_parameter_value("technology_family")
            or context.get_state("technology_family")
            or ""
        )
        if str(technology_family).lower() == "deye" or target_kwp >= 100:
            site_type = "ess"
        else:
            site_type = "victron"

    # Panel config: user parameter with site-type fallback
    panel_config = context.get_parameter_value("editable_panel_config")
    if not panel_config:
        panel_config = "20S2P" if site_type == "ess" else "5S2P"

    from shared.site_layout import parse_panel_config

    try:
        series, parallel = parse_panel_config(panel_config)
    except ValueError as e:
        return StepResult.failure(str(e))

    # Coordinates from generate_distribution_map
    map_result = context.get_previous_result("generate_distribution_map")
    center = map_result.get("center", {}) if map_result else {}
    center_lat = center.get("lat")  # None if absent — explicit bounds check below
    center_lon = center.get("lon")  # None if absent
    latitude = center_lat or 0  # For sun angle calculations (equatorial fallback)

    layout_label = (
        "DEYE/ESS"
        if site_type == "ess" and str(technology_family).lower() == "deye"
        else site_type.upper()
    )
    await context.send_progress_to_user(
        f"Generating {layout_label} site layout for "
        f"{site_name or f'Site {site_id}'} ({panel_config})..."
    )

    # Use the plant site polygon from site selection if available.
    # site_candidates[0]["polygon"] is the first PlantSite polygon in WGS84 GeoJSON,
    # serialised by shared/layout/pipeline.py. This is the actual plant site footprint
    # (e.g. 40m x 60m), NOT the community boundary.
    plant_polygon = None
    stored_utm_crs: str | None = None
    site_candidates = context.get_state("site_candidates") or []
    if site_candidates:
        polygon_geom = site_candidates[0].get("polygon")
        if polygon_geom:
            from shapely.geometry import Polygon as ShapelyPolygon
            from shapely.geometry import shape

            _candidate = shape(polygon_geom)
            _minx, _miny, _maxx, _maxy = _candidate.bounds
            if (
                not isinstance(_candidate, ShapelyPolygon)
                or _candidate.is_empty
                or not _candidate.is_valid
                or not (-180 <= _minx <= _maxx <= 180 and -90 <= _miny <= _maxy <= 90)
            ):
                LOGGER.warning(
                    "site_candidates[0].polygon failed validation (type=%s, valid=%s, "
                    "bounds=%s) — falling back to synthetic polygon",
                    type(_candidate).__name__,
                    _candidate.is_valid,
                    (_minx, _miny, _maxx, _maxy),
                )
            else:
                plant_polygon = _candidate
                stored_utm_crs = site_candidates[0].get("utm_crs")
                LOGGER.info("Using PlantSite polygon from site_candidates for site layout boundary")

    if plant_polygon is None:
        # No site candidates found (no roads, all areas excluded, etc.).
        # Create a synthetic plant site polygon sized for target_kwp, centered on
        # the map center. This is always preferable to using the community boundary
        # (outline_geom), which would cause the geometry engine to time out trying
        # to pack solar arrays across an entire community.
        if (
            center_lat is not None
            and center_lon is not None
            and -90 < center_lat < 90
            and -180 < center_lon < 180
        ):
            plant_polygon, stored_utm_crs = _make_synthetic_plant_polygon(
                lat=center_lat, lon=center_lon, target_kwp=target_kwp
            )
            if plant_polygon is not None:
                LOGGER.warning(
                    f"No site candidates for {site_name} — using synthetic plant polygon "
                    f"({target_kwp:.0f} kWp) centered at ({center_lat:.4f}, {center_lon:.4f})"
                )
        if plant_polygon is None:
            LOGGER.warning(
                f"Cannot create plant polygon for {site_name}: center coordinates "
                f"unavailable or invalid (lat={center_lat!r}, lon={center_lon!r})"
            )
            return StepResult.failure(
                f"No plant site found for {site_name or f'Site {site_id}'}. "
                "Could not determine a valid location for the power plant."
            )

    # Project boundary from WGS84 degrees to UTM meters.
    # The geometry engine uses meter-based constants (box sizes, fence setbacks).
    # Without projection, buffer/packing operations produce degenerate results
    # and matplotlib rendering hangs on degree-scale coordinates.
    # Use the stored utm_crs from site selection (exact zone) when available to
    # avoid zone re-estimation errors at 6° UTM zone boundaries.
    boundary = _project_boundary_to_utm(plant_polygon, utm_crs=stored_utm_crs)
    if boundary is None:
        return StepResult.failure("Could not project site boundary to meters.")

    # Extract gate position from site candidates (produced by site identification)
    gate_pos = None
    if site_candidates:
        top_candidate = site_candidates[0]
        gate_lat = top_candidate.get("gate_lat")
        gate_lon = top_candidate.get("gate_lon")
        if gate_lat is not None and gate_lon is not None:
            gate_pos = _gate_to_site_local(gate_lat, gate_lon, boundary)
            if gate_pos:
                LOGGER.info(f"Using gate from site identification: {gate_pos}")

    # Run geometry + rendering in thread (CPU-bound) with timeout
    try:
        layout, drawio_xml, png_b64 = await asyncio.wait_for(
            asyncio.to_thread(
                _generate_and_render,
                boundary=boundary,
                series_count=series,
                parallel_count=parallel,
                target_kwp=target_kwp,
                site_type=site_type,
                latitude=latitude,
                site_name=site_name or f"Site {site_id}",
                gate_pos=gate_pos,
            ),
            timeout=120,
        )
    except asyncio.TimeoutError:
        LOGGER.error(f"Site layout generation timed out after 120s for {site_name}")
        return StepResult.failure(
            f"Site layout generation timed out for {site_name or f'Site {site_id}'}. "
            "The site boundary may be too complex."
        )
    except Exception as e:
        LOGGER.exception(f"Site layout generation failed: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))

    # Warn if target not met
    if layout.achieved_kwp < target_kwp * 0.95:
        await context.send_progress_to_user(
            f"Note: achieved {layout.achieved_kwp:.1f} kWp "
            f"(target was {target_kwp:.1f} kWp) — site boundary may be too small."
        )

    progress = (
        f"{layout_label} site layout generated for {site_name or f'Site {site_id}'}:\n"
        f"  {layout.total_modules} modules, {layout.achieved_kwp:.1f} kWp\n"
        f"  {len(layout.arrays)} arrays ({panel_config}), "
        f"{len(layout.lightning_positions)} lightning arresters, "
        f"{len(layout.earth_pit_positions)} earth pits"
    )
    await context.send_progress_to_user(progress)

    # Extract cable route distances for AppSheet design update
    dc_lengths = [r.length_m for r in layout.cable_routes if r.cable_type == "dc"]
    ac_routes = [r for r in layout.cable_routes if r.cable_type == "ac"]

    avg_pv_combiner_distance = sum(dc_lengths) / len(dc_lengths) if dc_lengths else 25.0
    feeder_pillar_distance = ac_routes[0].length_m if ac_routes else 7.0

    LOGGER.info(
        f"Cable distances for {site_name}: "
        f"avg PV combiner={avg_pv_combiner_distance:.1f}m, "
        f"feeder pillar={feeder_pillar_distance:.1f}m"
    )

    # Upload site layout PNG + Draw.io XML to Drive (non-fatal, async)
    # Store Drive file IDs instead of base64/XML blobs to avoid packet_state bloat
    import base64

    from shared.utils.drive_upload import upload_step_output

    drive_ids = await upload_step_output(
        site_folder_id=context.get_state("site_folder_id"),
        subfolder_name="Engineering Documents",
        site_name=site_name or f"Site_{site_id}",
        files=[
            (base64.b64decode(png_b64), "image/png", "site_layout"),
            (drawio_xml.encode("utf-8"), "application/xml", "site_layout_drawio"),
        ],
    )

    return StepResult(
        data={
            "module_count": layout.total_modules,
            "achieved_kwp": layout.achieved_kwp,
            "array_count": len(layout.arrays),
            "arrester_count": len(layout.lightning_positions),
            "earth_pit_count": len(layout.earth_pit_positions),
            "avg_pv_combiner_distance_m": round(avg_pv_combiner_distance, 1),
            "feeder_pillar_distance_m": round(feeder_pillar_distance, 1),
        },
        state_updates={
            "site_layout_png_drive_id": drive_ids.get("site_layout", ""),
            "site_layout_drawio_drive_id": drive_ids.get("site_layout_drawio", ""),
            "editable_panel_config": panel_config,
            "editable_site_type": site_type,
            "earth_pit_count": len(layout.earth_pit_positions),
            "avg_pv_combiner_distance_m": round(avg_pv_combiner_distance, 1),
            "feeder_pillar_distance_m": round(feeder_pillar_distance, 1),
        },
        progress_message=(
            f"Layout: {layout.total_modules} modules, "
            f"{layout.achieved_kwp:.1f} kWp, "
            f"{len(layout.lightning_positions)} lightning arresters, "
            f"{len(layout.earth_pit_positions)} earth pits\n"
            f"Cable distances: PV combiner avg {avg_pv_combiner_distance:.1f}m, "
            f"feeder pillar {feeder_pillar_distance:.1f}m"
        ),
    )


def _make_synthetic_plant_polygon(
    lat: float, lon: float, target_kwp: float
) -> tuple[Polygon, str] | tuple[None, None]:
    """Create a UTM plant site polygon centered at lat/lon, sized for target_kwp.

    Returns (polygon_utm, utm_crs_str), or (None, None) on failure.
    The polygon is already in UTM so _project_boundary_to_utm passes it through unchanged.

    Sizing: target_kwp × 15.5 sqm/kWp × 1.5 buffer (fencing + internal setbacks),
    minimum 30 kWp equivalent so the polygon is never degenerate.
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        LOGGER.warning(f"Invalid coordinates for synthetic polygon: lat={lat}, lon={lon}")
        return None, None

    try:
        import geopandas as gpd
        from shapely.geometry import Point
        from shapely.geometry import box as shapely_box

        required_area = max(target_kwp, 30.0) * 15.5 * 1.5  # sqm with buffer
        side_m = math.sqrt(required_area)

        center_gdf = gpd.GeoDataFrame(geometry=[Point(lon, lat)], crs="EPSG:4326")
        utm_crs = str(center_gdf.estimate_utm_crs())
        center_utm = center_gdf.to_crs(utm_crs).geometry.iloc[0]

        cx, cy = center_utm.x, center_utm.y
        polygon_utm = shapely_box(
            cx - side_m / 2, cy - side_m / 2, cx + side_m / 2, cy + side_m / 2
        )
        return polygon_utm, utm_crs
    except Exception as e:
        LOGGER.exception(f"Failed to create synthetic plant polygon: {e}")
        return None, None


def _project_boundary_to_utm(boundary, utm_crs: str | None = None):
    """Project a WGS84 boundary polygon to UTM meters.

    The geometry engine expects meter-based coordinates for buffer operations,
    box packing, and infrastructure placement. WGS84 degree coordinates cause
    degenerate geometry and matplotlib rendering hangs.

    Args:
        boundary: WGS84 polygon to project.
        utm_crs: Optional EPSG string (e.g. "EPSG:32632") stored at site selection
            time. When provided, avoids re-estimating the UTM zone, which can
            produce a wrong zone for sites near 6° zone boundaries.

    Returns the projected polygon, or None on failure.
    """
    try:
        import geopandas as gpd

        minx, miny, maxx, maxy = boundary.bounds

        # Already in projected CRS (coordinates > 1000 = likely meters)
        if minx > 1000 or miny > 1000:
            return boundary

        gdf = gpd.GeoDataFrame(geometry=[boundary], crs="EPSG:4326")
        if utm_crs is None:
            # estimate_utm_crs uses pyproj's authoritative zone database (handles
            # Norway/Svalbard special zones) — more robust than manual formula.
            utm_crs = str(gdf.estimate_utm_crs())
        projected = gdf.to_crs(utm_crs).geometry.iloc[0]

        LOGGER.info(
            f"Projected boundary from WGS84 to {utm_crs}: "
            f"({maxx - minx:.6f}° x {maxy - miny:.6f}°) → "
            f"({projected.bounds[2] - projected.bounds[0]:.0f}m x "
            f"{projected.bounds[3] - projected.bounds[1]:.0f}m)"
        )
        return projected
    except Exception as e:
        LOGGER.exception(f"Failed to project boundary to UTM: {e}")
        return None


def _gate_to_site_local(gate_lat, gate_lon, boundary):
    """Convert WGS84 gate position to site-local coordinates matching the boundary.

    The site layout boundary is in site-local meters (from extract_site_boundary).
    We need to project the gate lat/lon into the same coordinate system.
    """
    try:
        import geopandas as gpd
        from shapely.geometry import Point

        gate_wgs84 = Point(gate_lon, gate_lat)

        # The boundary from extract_site_boundary is already in a projected CRS.
        # We need to find that CRS and project the gate point into it.
        # extract_site_boundary returns a polygon — check if it has raw coords
        # by comparing with boundary bounds (site-local coords are typically
        # in the hundreds of thousands range for UTM)
        bminx, bminy, bmaxx, bmaxy = boundary.bounds

        # If bounds are in UTM range (> 1000), project gate to UTM
        if bminx > 1000 or bminy > 1000:
            gate_gdf = gpd.GeoDataFrame(geometry=[gate_wgs84], crs="EPSG:4326")
            utm_crs = str(gate_gdf.estimate_utm_crs())
            gate_utm = gate_gdf.to_crs(utm_crs).geometry.iloc[0]
            return (gate_utm.x, gate_utm.y)
        else:
            # Boundary is in WGS84 degrees
            return (gate_lon, gate_lat)
    except Exception:
        LOGGER.debug("Failed to convert gate position to site-local", exc_info=True)
        return None


def _generate_and_render(
    boundary,
    series_count,
    parallel_count,
    target_kwp,
    site_type,
    latitude,
    site_name,
    gate_pos=None,
) -> tuple:
    """Run geometry computation + both renderers in a single thread."""
    from shared.site_layout import generate_site_layout
    from shared.site_layout.drawio_renderer import render_drawio
    from shared.site_layout.png_renderer import render_png

    layout = generate_site_layout(
        boundary=boundary,
        panels_per_box=series_count * parallel_count,
        target_kwp=target_kwp,
        site_type=site_type,
        latitude=latitude,
        site_name=site_name,
        gate_pos=gate_pos,
    )
    drawio_xml = render_drawio(layout)
    png_b64 = render_png(layout)
    return layout, drawio_xml, png_b64
