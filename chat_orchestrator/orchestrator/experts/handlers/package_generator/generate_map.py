"""Generate site map step handler for Light Preliminary Package.

This handler generates a site map image from pd_site_submissions table data,
showing boundaries, poles, cables, and buildings.
"""

import base64
import json
import os
from typing import Any, Dict, Optional

import psycopg

from orchestrator.experts.handlers.package_generator.site_geo_source import load_site_row_data
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.layout import generate_layout
from shared.mapping import generate_site_map
from shared.mapping.data_reader import extract_site_boundary
from shared.utils.drive_upload import upload_step_output
from shared.utils.grid_matcher import find_best_grid_match
from shared.utils.logging import get_logger
from shared.utils.option_parsing import normalize_numeric_input

LOGGER = get_logger(__name__)


def _merge_layout_into_row_data(
    row_data: Dict[str, Any], layout_result: Dict[str, Any]
) -> Dict[str, Any]:
    """Overlay auto-generated layout data onto the database row.

    Called when generate_distribution_layout produced results and the site
    had no pre-existing layout data. Merges poles, cables, buildings, and
    meta into the row_data dict so generate_distribution_map renders the layout.
    """
    for field in ("poles_geo_flat", "distribution_geo_flat", "buildings_geo_flat", "meta_geo_flat"):
        if field in layout_result and layout_result[field]:
            row_data[field] = layout_result[field]
    return row_data


def _row_has_layout_data(row_data: Dict[str, Any]) -> bool:
    """Check whether the DB row already has distribution layout data."""
    for field in ("distribution_geo_flat", "poles_geo_flat"):
        value = row_data.get(field)
        if value is None:
            continue
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                continue
        if isinstance(value, dict) and len(value.get("features", [])) > 0:
            return True
    return False


def _run_layout_engine(row_data: Dict[str, Any], site_name: str = "") -> Optional[Dict[str, Any]]:
    """Run the distribution layout engine for a site that has no pre-existing layout.

    Extracts boundary and buildings from row_data, calls generate_layout(),
    and returns the layout result dict (or None on failure).
    """
    outline_geom = row_data.get("outline_geom")
    buildings_geojson = row_data.get("buildings_geo_flat")
    if not outline_geom or not buildings_geojson:
        LOGGER.warning("Cannot run layout engine: missing outline_geom or buildings_geo_flat")
        return None

    # Parse buildings GeoJSON
    if isinstance(buildings_geojson, str):
        try:
            buildings_geojson = json.loads(buildings_geojson)
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Cannot parse buildings_geo_flat as JSON")
            return None

    # Extract boundary polygon from WKB
    try:
        boundary_obj = extract_site_boundary(outline_geom)
        if boundary_obj is None:
            LOGGER.warning("Could not extract boundary polygon")
            return None
        boundary_polygon = boundary_obj.polygon
    except Exception as e:
        LOGGER.warning(f"Error extracting boundary: {e}")
        return None

    # Run the layout engine
    try:
        LOGGER.info("Running distribution layout engine (no pre-existing layout data)")
        result = generate_layout(
            boundary=boundary_polygon,
            buildings_geojson=buildings_geojson,
            spacing_m=45.0,
            max_drop_distance_m=40.0,
            target_coverage=90.0,
            site_name=site_name,
        )
        if result:
            meta = result.get("meta_geo_flat", {})
            LOGGER.info(
                f"Layout engine produced: {meta.get('pole_count', 0)} poles, "
                f"{meta.get('coverage_percentage', 0):.1f}% coverage, "
                f"{meta.get('backbone_cable_length_m', 0):.0f}m backbone + "
                f"{meta.get('drop_cable_length_m', 0):.0f}m drops"
            )
        return result
    except Exception as e:
        LOGGER.exception(f"Layout engine failed: {e}")
        return None


def _enrich_statistics(
    statistics: Dict[str, Any], layout_meta: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """Enrich map statistics with richer metrics from the layout engine.

    The layout engine's meta_geo_flat has backbone/drop cable breakdown,
    coverage percentage, average span length, etc. Merge these into the
    statistics dict that flows through to populate_cells.
    """
    if not layout_meta:
        return statistics

    statistics["backbone_cable_length_m"] = layout_meta.get("backbone_cable_length_m", 0)
    statistics["drop_cable_length_m"] = layout_meta.get("drop_cable_length_m", 0)
    statistics["backbone_cable_count"] = layout_meta.get("backbone_cable_count", 0)
    statistics["drop_cable_count"] = layout_meta.get("drop_cable_count", 0)
    statistics["coverage_percentage"] = layout_meta.get("coverage_percentage", 0)
    statistics["average_span_length_m"] = layout_meta.get("average_span_length_m", 0)
    statistics["max_drop_cable_length_m"] = layout_meta.get("max_drop_cable_length_m", 0)

    # Override cable_length_m with accurate layout engine total
    total_cable = layout_meta.get("distribution_line_total_length")
    if total_cable:
        statistics["cable_length_m"] = total_cable

    return statistics


def _render_power_heatmap(
    distribution_geojson: dict,
    buildings_geojson: Optional[dict] = None,
) -> Optional[bytes]:
    """Render backbone cable load as a power heatmap image.

    Backbone cables are colored by power_kw using a heat colormap.
    Returns PNG bytes, or None if no power_kw data is present in the GeoJSON.
    """
    import io

    import matplotlib
    import matplotlib.cm as cm
    import matplotlib.colors as mcolors

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import shape

    if isinstance(distribution_geojson, str):
        try:
            distribution_geojson = json.loads(distribution_geojson)
        except (json.JSONDecodeError, TypeError):
            return None

    features = distribution_geojson.get("features", [])
    backbone = [
        f
        for f in features
        if f.get("properties", {}).get("cable_type") == "backbone"
        and "power_kw" in f.get("properties", {})
    ]

    if not backbone:
        return None

    power_values = [f["properties"]["power_kw"] for f in backbone]
    max_kw = max(power_values)
    if max_kw <= 0:
        return None

    fig, ax = plt.subplots(figsize=(10, 10), facecolor="#1a1a2e")
    ax.set_facecolor("#1a1a2e")
    ax.set_aspect("equal")
    ax.axis("off")

    cmap = cm.get_cmap("hot_r")
    norm = mcolors.Normalize(vmin=0, vmax=max_kw)

    # Buildings for context (small grey dots)
    if buildings_geojson:
        if isinstance(buildings_geojson, str):
            try:
                buildings_geojson = json.loads(buildings_geojson)
            except (json.JSONDecodeError, TypeError):
                buildings_geojson = None
    if buildings_geojson:
        for f in buildings_geojson.get("features", []):
            try:
                geom = shape(f["geometry"])
                cx, cy = geom.centroid.x, geom.centroid.y
                color = "#3a5f8a" if f.get("properties", {}).get("connected") else "#4a4a6a"
                ax.plot(cx, cy, ".", color=color, markersize=2, alpha=0.6)
            except Exception:
                continue

    # Drop cables (thin, muted)
    for f in features:
        if f.get("properties", {}).get("cable_type") == "drop":
            try:
                geom = shape(f["geometry"])
                xs, ys = geom.xy
                ax.plot(xs, ys, color="#3a3a5a", linewidth=0.6, alpha=0.5)
            except Exception:
                continue

    # Backbone cables colored by load
    for f in backbone:
        try:
            geom = shape(f["geometry"])
            power_kw = f["properties"]["power_kw"]
            xs, ys = geom.xy
            ax.plot(xs, ys, color=cmap(norm(power_kw)), linewidth=2.5)
        except Exception:
            continue

    sm = cm.ScalarMappable(cmap=cmap, norm=norm)
    sm.set_array([])
    cbar = plt.colorbar(sm, ax=ax, fraction=0.03, pad=0.02)
    cbar.set_label("Load (kW)", color="white", fontsize=11)
    cbar.ax.yaxis.set_tick_params(color="white")
    plt.setp(cbar.ax.yaxis.get_ticklabels(), color="white")
    ax.set_title("Distribution Power Load", color="white", fontsize=13, pad=8)

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="#1a1a2e")
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def _get_db_config() -> Dict[str, Any]:
    """Get database configuration from environment variables."""
    return {
        "host": os.getenv("AUTH_DB_HOST"),
        "port": int(os.getenv("AUTH_DB_PORT", "5432")),
        "user": os.getenv("AUTH_DB_USER"),
        "password": os.getenv("AUTH_DB_PASSWORD"),
        "dbname": os.getenv("AUTH_DB_NAME", "postgres"),
    }


def _lookup_site_by_name(site_name: str, db_config: Dict[str, Any]) -> Dict[str, Any]:
    """Look up site(s) from pd_site_submissions by site_name with fuzzy matching.

    Args:
        site_name: Site name to search for (can be partial or misspelled)
        db_config: Database connection configuration

    Returns:
        Dict with:
            - found: bool
            - site_id: int (if single match)
            - site_name: str (actual name from db)
            - multiple: bool (if multiple matches)
            - options: list of {id, site_name, created_at} (if multiple matches)
            - fuzzy_match: bool (True if fuzzy matching was used)
    """
    conninfo = (
        f"host={db_config['host']} "
        f"port={db_config.get('port', 5432)} "
        f"dbname={db_config.get('dbname', 'postgres')} "
        f"user={db_config['user']} "
        f"password={db_config['password']}"
    )
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            # First try exact case-insensitive match (exclude deleted sites)
            cur.execute(
                """SELECT id, site_name, created_at
                   FROM pd_site_submissions
                   WHERE LOWER(site_name) = LOWER(%s)
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC""",
                (site_name,),
            )
            rows = cur.fetchall()

            if rows:
                # Exact match found
                if len(rows) == 1:
                    return {
                        "found": True,
                        "site_id": rows[0][0],
                        "site_name": rows[0][1],
                        "multiple": False,
                        "options": [],
                        "fuzzy_match": False,
                    }

                # Multiple exact matches - return options
                options = [
                    {
                        "id": r[0],
                        "site_name": r[1],
                        "created_at": r[2].isoformat() if r[2] else None,
                    }
                    for r in rows
                ]
                return {
                    "found": True,
                    "site_id": None,
                    "site_name": None,
                    "multiple": True,
                    "options": options,
                    "fuzzy_match": False,
                }

            # No exact match - try fuzzy matching
            LOGGER.info(f"No exact match for '{site_name}', trying fuzzy matching")

            # Fetch all site names for fuzzy matching (exclude deleted sites)
            cur.execute(
                """SELECT DISTINCT site_name FROM pd_site_submissions
                   WHERE site_name IS NOT NULL AND deleted_at IS NULL"""
            )
            all_names = [r[0] for r in cur.fetchall()]

            if not all_names:
                return {
                    "found": False,
                    "site_id": None,
                    "multiple": False,
                    "options": [],
                    "fuzzy_match": False,
                }

            # Use fuzzy matching
            matched_name, was_fuzzy, score = find_best_grid_match(site_name, all_names)

            if not matched_name:
                LOGGER.info(f"No fuzzy match found for '{site_name}'")
                return {
                    "found": False,
                    "site_id": None,
                    "multiple": False,
                    "options": [],
                    "fuzzy_match": False,
                }

            LOGGER.info(f"Fuzzy matched '{site_name}' -> '{matched_name}' (score: {score}%)")

            # Look up the matched site(s) (exclude deleted sites)
            cur.execute(
                """SELECT id, site_name, created_at
                   FROM pd_site_submissions
                   WHERE LOWER(site_name) = LOWER(%s)
                     AND deleted_at IS NULL
                   ORDER BY created_at DESC""",
                (matched_name,),
            )
            rows = cur.fetchall()

            if not rows:
                return {
                    "found": False,
                    "site_id": None,
                    "multiple": False,
                    "options": [],
                    "fuzzy_match": True,
                }

            if len(rows) == 1:
                return {
                    "found": True,
                    "site_id": rows[0][0],
                    "site_name": rows[0][1],
                    "multiple": False,
                    "options": [],
                    "fuzzy_match": True,
                    "fuzzy_score": score,
                }

            # Multiple matches for the fuzzy-matched name
            options = [
                {
                    "id": r[0],
                    "site_name": r[1],
                    "created_at": r[2].isoformat() if r[2] else None,
                }
                for r in rows
            ]
            return {
                "found": True,
                "site_id": None,
                "site_name": None,
                "multiple": True,
                "options": options,
                "fuzzy_match": True,
                "fuzzy_score": score,
            }


def _lookup_site_by_id(site_id: int, db_config: Dict[str, Any]) -> Optional[str]:
    """Look up site name by ID.

    Args:
        site_id: Site submission ID
        db_config: Database connection configuration

    Returns:
        Site name or None if not found
    """
    conninfo = (
        f"host={db_config['host']} "
        f"port={db_config.get('port', 5432)} "
        f"dbname={db_config.get('dbname', 'postgres')} "
        f"user={db_config['user']} "
        f"password={db_config['password']}"
    )
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT site_name FROM pd_site_submissions
                   WHERE id = %s AND deleted_at IS NULL""",
                (site_id,),
            )
            row = cur.fetchone()
            return str(row[0]) if row else None


@register_step("generate_distribution_map")
async def generate_distribution_map(context: StepContext) -> StepResult:
    """Generate a site map image from pd_site_submissions data.

    Looks up site by ID or name, handles multiple matches by pausing
    for user selection, then generates map with boundaries, poles,
    cables, and buildings.

    Args:
        context: Step execution context with packet inputs

    Returns:
        StepResult with map_image_b64 and statistics, or user prompt if
        multiple sites match the name
    """
    # Idempotency guard: map already generated (handles recovery re-entry)
    if context.get_state("map_generated"):
        LOGGER.info("generate_distribution_map: already done, skipping")
        return StepResult(
            data={
                "map_generated": True,
                "map_image_b64": None,  # Not stored in state (too large)
                "statistics": {},
                "site_id": context.get_state("site_id"),
                "site_name": context.get_state("site_name"),
                "center": {},
            },
            state_updates={},
            progress_message="Distribution map already generated.",
        )

    # --- Community (Route B): boundary + footprints already resolved upstream ---
    if context.get_state("geo_source") == "community":
        site_name = context.get_state("site_name") or "Community"
        db_config = _get_db_config()
        await context.send_progress_to_user(
            f"Generating site map for {site_name}...\nThis may take a moment."
        )
        try:
            row_data = await load_site_row_data(context, db_config)
        except Exception as e:
            LOGGER.exception(f"Error resolving community geo: {e}")
            return StepResult.failure("Could not assemble community site data.")
        return await _render_map_from_row_data(context, row_data, site_id=None, site_name=site_name)

    site_id = context.get_input("site_id")
    site_name = context.get_input("site_name")
    selected_site_id = context.get_state("selected_site_id")

    # Debug: Log entry state
    LOGGER.info(
        f"generate_distribution_map entry: site_id={site_id}, site_name={site_name}, "
        f"selected_site_id={selected_site_id}, user_input='{context.user_input}', "
        f"awaiting_site_selection={context.get_state('awaiting_site_selection')}"
    )

    # Check if we're resuming after asking user to select from multiple sites
    if context.get_state("awaiting_site_selection") and context.user_input:
        user_response = context.user_input.strip()

        # Check for cancel commands first
        if user_response.lower() in ["cancel", "skip", "abort", "quit", "exit", "stop", "no"]:
            LOGGER.info("User cancelled site selection")
            return StepResult(
                skip_remaining=True,
                progress_message="Package generation cancelled.",
            )

        site_options = context.get_state("site_options") or []

        LOGGER.info(
            f"Processing user site selection: '{user_response}' from {len(site_options)} options"
        )

        # Try to match user input to a site option
        # User might enter "1", "2", "1." (option number) or "206" (site ID)
        matched_site = None

        # Normalize emoji numbers to plain digits
        normalized_response = normalize_numeric_input(user_response)

        # First try as a 1-based option number
        if normalized_response.isdigit():
            option_num = int(normalized_response)

            # Check if it's a valid option number (1-based)
            if 1 <= option_num <= len(site_options):
                matched_site = site_options[option_num - 1]
                LOGGER.info(f"User selected option {option_num}: site ID {matched_site['id']}")
            else:
                # Try matching as a direct site ID
                for opt in site_options:
                    if opt["id"] == option_num:
                        matched_site = opt
                        LOGGER.info(f"User selected site by ID: {matched_site['id']}")
                        break

        if matched_site:
            selected_site_id = matched_site["id"]
            # Clear the awaiting state since we got a selection
            # (state_updates will be applied after this step completes)
        else:
            # Couldn't parse the selection - show options again
            options_text = "\n".join(
                [
                    f"  {i + 1}. ID {opt['id']} - {opt['site_name']} "
                    f"(submitted {opt['created_at'][:10] if opt['created_at'] else 'unknown'})"
                    for i, opt in enumerate(site_options)
                ]
            )
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    f"I didn't understand '{user_response}'. "
                    f"Please enter a number (1-{len(site_options)}) or site ID:\n"
                    f"{options_text}"
                ),
            )

    # Check if user already selected a site from multiple options
    if selected_site_id:
        site_id = selected_site_id

    # Get database config
    db_config = _get_db_config()

    if not db_config.get("host"):
        return StepResult.failure("Database not configured (AUTH_DB_HOST not set)")

    # Handle case where no site is specified - ask the user
    if not site_id and not site_name:
        # Check if we're resuming after asking for site name
        if context.get_state("awaiting_site_name") and context.user_input:
            user_response = context.user_input.strip()

            # Check for cancel commands first
            if user_response.lower() in ["cancel", "skip", "abort", "quit", "exit", "stop", "no"]:
                LOGGER.info("User cancelled site name input")
                return StepResult(
                    skip_remaining=True,
                    progress_message="Package generation cancelled.",
                )

            site_name = user_response
            LOGGER.info(f"User provided site name: '{site_name}'")
            # Continue with lookup below
        else:
            LOGGER.info("No site specified, asking user for site name")
            return StepResult(
                state_updates={"awaiting_site_name": True},
                needs_user_input=True,
                user_prompt="Please provide the site name for the LPP package:",
            )

    # Resolve site_id if only site_name provided
    if not site_id and site_name:
        LOGGER.info(f"Looking up site by name: {site_name}")

        try:
            lookup = _lookup_site_by_name(site_name, db_config)
        except Exception as e:
            LOGGER.exception(f"Database error looking up site: {e}")
            return StepResult.failure(f"Database error: {str(e)}")

        if not lookup["found"]:
            # Site not found - ask for a different name instead of failing
            return StepResult(
                state_updates={"awaiting_site_name": True},
                needs_user_input=True,
                user_prompt=(
                    f"Site '{site_name}' not found. Please check the spelling and try again:\n"
                    "(Enter the site name as it appears in the site submissions)"
                ),
            )

        if lookup["multiple"]:
            # Multiple matches - return options and pause for user input
            options = lookup["options"]
            options_text = "\n".join(
                [
                    f"  {i + 1}. ID {opt['id']} - {opt['site_name']} "
                    f"(submitted {opt['created_at'][:10] if opt['created_at'] else 'unknown'})"
                    for i, opt in enumerate(options)
                ]
            )

            LOGGER.info(
                f"Multiple site submissions found for '{site_name}': {[o['id'] for o in options]}"
            )

            return StepResult(
                data={"site_options": options},
                state_updates={
                    "awaiting_site_selection": True,
                    "awaiting_site_name": False,
                    "site_options": options,
                },
                needs_user_input=True,
                user_prompt=(
                    f"Multiple submissions found for '{site_name}':\n"
                    f"{options_text}\n\n"
                    "Which one should I use? (enter number or ID)"
                ),
            )

        site_id = lookup["site_id"]
        actual_site_name = lookup["site_name"]  # Use actual name from database
        was_fuzzy = lookup.get("fuzzy_match", False)
        if was_fuzzy:
            LOGGER.info(
                f"Using fuzzy-matched site name: '{actual_site_name}' (input was '{site_name}')"
            )
        site_name = actual_site_name

    # Final validation
    if not site_id:
        return StepResult.failure("No site specified. Provide site_id or site_name.")

    # If we only had site_id, look up the name
    if not site_name:
        try:
            site_name = _lookup_site_by_id(site_id, db_config)
        except Exception as e:
            LOGGER.warning(f"Could not look up site name for ID {site_id}: {e}")
            site_name = f"Site {site_id}"

    LOGGER.info(f"Generating map for site {site_id}: {site_name}")

    # Notify user before the heavy lifting (DB fetch + layout engine + map render)
    await context.send_progress_to_user(
        f"Generating site map for {site_name}...\nThis may take a moment."
    )

    # Fetch site data from database using psycopg
    try:
        conninfo = (
            f"host={db_config['host']} "
            f"port={db_config.get('port', 5432)} "
            f"dbname={db_config.get('dbname', 'postgres')} "
            f"user={db_config['user']} "
            f"password={db_config['password']}"
        )
        with psycopg.connect(conninfo) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT
                        id, site_name,
                        outline_geom,
                        buildings_geo_flat,
                        distribution_geo_flat,
                        poles_geo_flat,
                        meta_geo_flat,
                        site_details
                    FROM pd_site_submissions
                    WHERE id = %s AND deleted_at IS NULL""",
                    (site_id,),
                )
                row = cur.fetchone()

        if not row:
            return StepResult.failure(f"Site ID {site_id} not found in database")

        columns = [
            "id",
            "site_name",
            "outline_geom",
            "buildings_geo_flat",
            "distribution_geo_flat",
            "poles_geo_flat",
            "meta_geo_flat",
            "site_details",
        ]
        row_data = dict(zip(columns, row))

        # Debug: Log types and sizes of JSON fields
        for field in ["buildings_geo_flat", "distribution_geo_flat", "poles_geo_flat"]:
            value = row_data.get(field)
            if value is None:
                LOGGER.warning(f"{field} is NULL for site {site_id}")
            else:
                value_type = type(value).__name__
                # Try to get feature count
                feature_count: int | str = "unknown"
                if isinstance(value, dict):
                    feature_count = len(value.get("features", []))
                elif isinstance(value, str):
                    try:
                        import json

                        parsed = json.loads(value)
                        feature_count = len(parsed.get("features", []))
                    except Exception:
                        pass
                LOGGER.info(
                    f"{field} for site {site_id}: type={value_type}, features={feature_count}"
                )

    except Exception as e:
        LOGGER.exception(f"Error fetching site data: {e}")
        return StepResult.failure(f"Database error: {str(e)}")

    return await _render_map_from_row_data(context, row_data, site_id=site_id, site_name=site_name)


async def _render_map_from_row_data(
    context: StepContext, row_data: Dict[str, Any], site_id: Any, site_name: Optional[str]
) -> StepResult:
    """Shared render path for both submission and community routes.

    Takes an already-resolved row_data (pd_site_submissions-shaped), runs the
    layout engine (or reuses an upstream result), renders the map, uploads to Drive,
    and returns a StepResult with all the usual outputs.
    """
    # --- Distribution layout: generate or use existing ---
    layout_meta = None
    use_existing = context.get_state("use_site_submission_layout") or False

    # First check if a previous workflow step already produced layout
    layout_result = context.get_previous_result("generate_distribution_layout")
    if not layout_result or layout_result.get("skipped"):
        layout_result = context.get_state("layout_result")

    if layout_result and layout_result.get("poles_geo_flat"):
        # A previous step explicitly provided layout — always use it
        LOGGER.info("Using layout from previous workflow step")
    elif use_existing and _row_has_layout_data(row_data):
        # User opted into the site submission layout — skip generation
        LOGGER.info("Using existing layout from site submission (use_site_submission_layout=True)")
        layout_result = None
    else:
        # Default: generate a fresh layout regardless of what the DB has
        layout_result = _run_layout_engine(row_data, site_name=site_name or "")

    # Capture site options image from layout result (if not already in state
    # from a previous generate_distribution_layout step)
    site_options_map_b64 = context.get_state("site_options_map_b64")
    if not site_options_map_b64 and layout_result:
        site_options_map_b64 = layout_result.get("site_options_map_b64")

    # Capture site_candidates from layout result when they aren't already in state.
    # generate_distribution_layout populates state.site_candidates on full runs, but
    # falls back to empty for idempotency/QGIS-fast-path cases.  In those cases
    # generate_distribution_map runs _run_layout_engine() which finds candidates and
    # renders the site options map — but those candidates were never persisted.
    # Persist them here so generate_site_layout can use the correct polygon instead
    # of falling back to the synthetic NWES square.
    site_candidates_from_layout: list = context.get_state("site_candidates") or []
    if not site_candidates_from_layout and layout_result:
        site_candidates_from_layout = layout_result.get("site_candidates", [])

    # Merge generated layout into row_data (overrides DB data)
    if layout_result and layout_result.get("poles_geo_flat"):
        LOGGER.info(f"Merging auto-generated layout into site {site_id} row data")
        row_data = _merge_layout_into_row_data(row_data, layout_result)
        layout_meta = layout_result.get("meta_geo_flat")

    # Generate the site map
    try:
        result = generate_site_map(
            row_data=row_data,
            format="png",
            dpi=150,
            add_satellite=True,
        )
    except Exception as e:
        LOGGER.exception(f"Error generating site map: {e}")
        return StepResult.failure(f"Error generating map: {str(e)}")

    if not result.get("success"):
        error_msg = result.get("error", "Unknown error generating map")
        LOGGER.error(f"Map generation failed: {error_msg}")
        return StepResult.failure(f"Failed to generate map: {error_msg}")

    # Extract metadata and enrich with layout engine metrics
    metadata = result.get("metadata", {})
    statistics = metadata.get("statistics", {})
    statistics = _enrich_statistics(statistics, layout_meta)
    center = metadata.get("center", {})

    # Extract state from site_details (JSONB column)
    site_details = row_data.get("site_details")
    site_state = None
    if site_details:
        import json

        if isinstance(site_details, str):
            try:
                site_details = json.loads(site_details)
            except json.JSONDecodeError:
                pass
        if isinstance(site_details, dict):
            site_state = site_details.get("state")

    cable_length = statistics.get("cable_length_m")
    cable_info = f"{cable_length:,.0f}m cable" if cable_length else "no cable data"
    LOGGER.info(
        f"Generated map for {site_name} (ID {site_id}): "
        f"{statistics.get('total_buildings', 0)} buildings "
        f"({statistics.get('served_buildings', 0)} served, {statistics.get('unserved_buildings', 0)} unserved), "
        f"{statistics.get('poles', 0)} poles, "
        f"{cable_info}, "
        f"center=({center.get('lat', '?')}, {center.get('lon', '?')}), state={site_state}"
    )

    # Format statistics message for user
    stats_msg = (
        f"Site: {site_name}\n"
        f"Buildings: {statistics.get('total_buildings', 0)} "
        f"({statistics.get('served_buildings', 0)} served)\n"
        f"Poles: {statistics.get('poles', 0)}"
    )
    if cable_length:
        stats_msg += f"\nCable length: {cable_length:,.0f}m"

    # Upload distribution map to Drive (non-fatal, async)
    # Store Drive file IDs in state instead of base64 blobs to keep packet_state small.
    # Consumers use get_previous_result() for base64 in normal flow;
    # Drive IDs are the fallback for workflow resume after failure.
    drive_ids = await upload_step_output(
        site_folder_id=context.get_state("site_folder_id"),
        subfolder_name="Distribution Design",
        site_name=site_name,
        files=[(base64.b64decode(result["image"]), "image/png", "distribution_map")],
    )

    # Upload site options map separately if available
    site_options_drive_id = ""
    if site_options_map_b64:
        options_ids = await upload_step_output(
            site_folder_id=context.get_state("site_folder_id"),
            subfolder_name="Distribution Design",
            site_name=site_name,
            files=[(base64.b64decode(site_options_map_b64), "image/png", "site_options_map")],
        )
        site_options_drive_id = options_ids.get("site_options_map", "")

    # Render and upload power flow heatmap (non-fatal — only produced when power_kw is present)
    power_heatmap_drive_id = ""
    power_heatmap_b64 = None
    dist_geojson = row_data.get("distribution_geo_flat")
    if dist_geojson:
        try:
            import asyncio as _asyncio

            heatmap_bytes = await _asyncio.to_thread(
                _render_power_heatmap,
                dist_geojson,
                row_data.get("buildings_geo_flat"),
            )
            if heatmap_bytes:
                power_heatmap_b64 = base64.b64encode(heatmap_bytes).decode()
                heatmap_ids = await upload_step_output(
                    site_folder_id=context.get_state("site_folder_id"),
                    subfolder_name="Distribution Design",
                    site_name=site_name,
                    files=[(heatmap_bytes, "image/png", "power_heatmap")],
                )
                power_heatmap_drive_id = heatmap_ids.get("power_heatmap", "")
                LOGGER.info(f"Power heatmap uploaded: {power_heatmap_drive_id}")
        except Exception as e:
            LOGGER.warning(f"Power heatmap generation failed (non-fatal): {e}")

    return StepResult(
        data={
            "map_image_b64": result["image"],
            "map_image_data_uri": f"data:image/png;base64,{result['image']}",
            "statistics": statistics,
            "statistics_message": stats_msg,
            "site_id": site_id,
            "site_name": site_name,
            "bounds": metadata.get("bounds"),
            "center": center,
            "site_state": site_state,
            "power_heatmap_b64": power_heatmap_b64,
        },
        state_updates={
            "map_generated": True,
            "map_image_drive_id": drive_ids.get("distribution_map", ""),
            "site_options_drive_id": site_options_drive_id,
            "power_heatmap_drive_id": power_heatmap_drive_id,
            "site_id": site_id,
            "site_name": site_name,
            "awaiting_site_selection": False,
            "site_state": site_state,
            # Persist site candidates so generate_site_layout uses the correct polygon.
            # Only written when candidates came from _run_layout_engine (idempotency /
            # QGIS fast-path cases where generate_distribution_layout left state empty).
            "site_candidates": site_candidates_from_layout,
            # Editable parameters for confirmation flow
            "editable_total_buildings": statistics.get("total_buildings", 0),
            "editable_served_building_count": statistics.get("served_buildings", 0),
            # Optional target energy parameters (empty = let AppSheet calculate freely)
            "editable_total_kwp": "",
            "editable_total_kwh": "",
        },
        progress_message=f"Generated map for {site_name}",
    )
