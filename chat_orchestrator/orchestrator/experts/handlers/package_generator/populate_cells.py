"""Populate cells step handler for Light Preliminary Package.

This handler populates the "Main Input" sheet with site data by:
1. Collecting all available values from previous workflow steps
2. Reading key labels from column A of the sheet
3. Looking up explicit mappings from the Google Doc's ## Cell Mapping section
4. Writing values to column B and data keys to column C (grey, for reference)

Only explicitly mapped labels are populated. Labels not in the Cell Mapping
section are skipped to prevent incorrect data from being written.
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from googleapiclient.discovery import build

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.utils.apps_script_client import replace_sheet_image
from shared.utils.google_auth import get_sheets_credentials, get_sheets_write_credentials
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Default sheet name for data input
DEFAULT_SHEET_NAME = "Main Input"

# Default columns to check for keys (can have multiple for multi-column layouts)
DEFAULT_KEY_COLUMNS = ["A"]

# Maps editable_ parameter names to their corresponding workflow data keys.
# Module-level to avoid rebuilding on every call.
EDITABLE_PARAM_MAPPINGS = {
    "editable_total_buildings": "computed.total_buildings",
    "editable_served_building_count": "meta.served_building_count",
    "editable_total_kwp": "energy.total_kwp",
    "editable_total_kwh": "energy.total_kwh",
}


def _column_letter_to_index(letter: str) -> int:
    """Convert column letter to 0-based index (A=0, B=1, etc.)."""
    letter = letter.upper()
    result = 0
    for char in letter:
        result = result * 26 + (ord(char) - ord("A") + 1)
    return result - 1


def _index_to_column_letter(index: int) -> str:
    """Convert 0-based index to column letter (0=A, 1=B, etc.)."""
    result = ""
    index += 1  # Convert to 1-based
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _parse_cell_mapping(raw_sections: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Parse cell mapping from expert raw sections.

    Looks for 'cell_mapping' key and extracts key -> source pairs.
    These serve as explicit overrides for LLM-based matching.

    Special values:
        - "SKIP", "NONE", "IGNORE", "-" = Don't map this label (exclude from LLM)

    Supports two formats:

    Arrow format (original):
        Site Name -> site.site_name
        Total kWp -> energy.total_kwp

    Markdown table format (from Google Docs):
        | **Required Input** | **Available Data Field** | **Notes** |
        | --- | --- | --- |
        | **Poles** | meta.pole_count | Direct match. |
        | **Community Name** | site.site_name | Direct match. |

    Keys are stored with original casing for display, but matching is
    done case-insensitively by storing a lowercase lookup version.

    Args:
        raw_sections: Dict of raw section content from expert config

    Returns:
        Dict mapping lowercase key labels to source_path (or SKIP sentinel)
    """
    if not raw_sections:
        return {}

    # Get cell mapping section content (key is lowercase with underscores)
    section = raw_sections.get("cell_mapping", "")
    if not section:
        LOGGER.debug("No 'cell_mapping' section found in expert config")
        return {}

    mapping = {}

    for line in section.split("\n"):
        line = line.strip()
        if not line or line.startswith("<!--") or line.startswith("#"):
            continue

        # Format 1: Arrow format — "Key Label -> source.field"
        if " -> " in line:
            key, source = line.split(" -> ", 1)
            key = key.strip()
            mapping[key.lower()] = source.strip()
            continue

        # Format 2: Markdown table — "| Key | source.field | notes |"
        if line.startswith("|") and line.count("|") >= 3:
            cells = [c.strip() for c in line.split("|")]
            # Split produces empty strings at start/end: ['', 'Key', 'source', 'notes', '']
            cells = [c for c in cells if c]
            if len(cells) < 2:
                continue

            key = cells[0]
            source = cells[1]

            # Strip markdown bold markers (**text**) from key only.
            # Don't strip from source — * may be a multiplication operator.
            key = re.sub(r"\*+", "", key).strip()
            source = re.sub(r"^\*\*|\*\*$", "", source).strip()

            # Skip header row and separator row (---|---|---)
            if not key or not source or set(key) <= {"-", " "} or set(source) <= {"-", " "}:
                continue
            # Skip header-like rows (common headers)
            if key.lower() in ("required input", "key", "label", "field", "column"):
                continue

            # Extract just the data field path from source (ignore formulas/notes)
            # e.g., "energy.total_kwp*computed.total_buildings/meta.served_building_count"
            # → take only the first dotted path (before any operator)
            source_match = re.match(r"^([\w.]+)", source)
            if source_match:
                mapping[key.lower()] = source_match.group(1)

    LOGGER.info(f"Parsed {len(mapping)} explicit cell mappings from expert config")
    return mapping


def _parse_key_columns(raw_sections: Optional[Dict[str, str]]) -> List[str]:
    """Parse input data key columns from expert raw sections.

    Looks for 'input_data_key_column' key in raw_sections dict.
    Supports single column (e.g., "B") or multiple columns (e.g., "A, D").

    Args:
        raw_sections: Dict of raw section content from expert config

    Returns:
        List of column letters (e.g., ["B"] or ["A", "D"]), or empty list if not found
    """
    if not raw_sections:
        return []

    # Key is lowercase with underscores (from _split_into_subsections)
    section = raw_sections.get("input_data_key_column", "")
    if not section:
        # Also try singular/plural variants
        section = raw_sections.get("input_data_key_columns", "")
    if not section:
        LOGGER.debug("No 'input_data_key_column' section found in expert config")
        return []

    # Parse the column value - could be "B" or "A, D" or "A,D"
    # Take the first non-empty, non-comment line
    for line in section.split("\n"):
        line = line.strip()
        if not line or line.startswith("<!--") or line.startswith("#"):
            continue

        # Parse column letters (comma or space separated)
        columns = [c.strip().upper() for c in re.split(r"[,\s]+", line) if c.strip()]
        if columns:
            LOGGER.info(f"Parsed key columns from expert config: {columns}")
            return columns

    return []


def _collect_all_available_values(context: StepContext) -> Dict[str, Any]:
    """Collect all available values from previous workflow steps and state.

    Aggregates data from:
    - site.*: Basic site info from state
    - location.*: Coordinates from generate_distribution_map
    - meta.*: Statistics from generate_distribution_map
    - computed.*: Computed values from statistics
    - design.*: Design info from generate_powerplant_design
    - bom.*: Cost summary from generate_powerplant_design
    - energy.*: Energy specs from generate_powerplant_design and fetch_solar_potential
    - solar.*: Solar irradiation data from fetch_solar_potential

    Args:
        context: Step execution context

    Returns:
        Dict with prefixed keys mapping to values
    """
    all_values: Dict[str, Any] = {}

    # 1. Site data from state and generate_distribution_map
    map_result = context.get_previous_result("generate_distribution_map") or {}
    statistics = map_result.get("statistics", {})
    center = map_result.get("center", {})
    site_state = map_result.get("site_state")

    # Format GPS coordinates to 6 decimal places
    lat = center.get("lat")
    lon = center.get("lon")
    gps_combined = None
    if lat is not None and lon is not None:
        try:
            gps_combined = f"{float(lat):.6f}, {float(lon):.6f}"
        except (TypeError, ValueError):
            pass

    all_values.update(
        {
            "site.site_name": context.get_state("site_name"),
            "site.site_id": context.get_state("site_id"),
            "site.state": site_state,
            "location.lat": f"{float(lat):.6f}" if lat is not None else None,
            "location.lon": f"{float(lon):.6f}" if lon is not None else None,
            "location.gps": gps_combined,  # Combined "lat, lon" format
            "meta.pole_count": statistics.get("poles"),
            "meta.served_building_count": statistics.get("served_buildings"),
            "meta.unserved_building_count": statistics.get("unserved_buildings"),
            "meta.coverage_percentage": statistics.get("coverage_percentage"),
            "meta.backbone_cable_length_m": statistics.get("backbone_cable_length_m"),
            "meta.drop_cable_length_m": statistics.get("drop_cable_length_m"),
            "meta.backbone_cable_count": statistics.get("backbone_cable_count"),
            "meta.drop_cable_count": statistics.get("drop_cable_count"),
            "meta.average_span_length_m": statistics.get("average_span_length_m"),
            "meta.max_drop_cable_length_m": statistics.get("max_drop_cable_length_m"),
            "computed.total_buildings": statistics.get("total_buildings"),
            "computed.cable_length_m": statistics.get("cable_length_m"),
        }
    )

    # 2. BOM/Design data from generate_site_bom (preferred) and generate_powerplant_design
    design_result = context.get_previous_result("generate_powerplant_design") or {}
    bom_step_result = context.get_previous_result("generate_site_bom") or {}
    cost_summary = bom_step_result.get("cost_summary") or design_result.get("cost_summary", {})
    energy_specs = bom_step_result.get("energy_specs") or design_result.get("energy_specs", {})

    # Calculate Wp per connection (total_kwp * 1000 / served_buildings)
    total_kwp = energy_specs.get("total_kwp")
    served_buildings = statistics.get("served_buildings", 0)
    wp_per_conn = None
    if total_kwp and served_buildings and served_buildings > 0:
        try:
            wp_per_conn = round(float(total_kwp) * 1000 / served_buildings, 1)
        except (TypeError, ValueError):
            pass

    all_values.update(
        {
            "design.design_id": design_result.get("design_id"),
            "design.design_name": design_result.get("design_name"),
            "bom.total_cost": cost_summary.get("total_cost"),
            "bom.main_energy_asset_cost": cost_summary.get("main_energy_asset_cost"),
            "bom.metering_cost": cost_summary.get("metering_cost"),
            "bom.bos_cost": cost_summary.get("bos_cost"),
            "bom.item_count": design_result.get("bom_item_count"),
            "energy.total_kwp": total_kwp,
            "energy.total_kwh": energy_specs.get("total_kwh"),
            "energy.total_kva": energy_specs.get("total_kva"),
            "energy.Wp_per_conn": wp_per_conn,
            "energy.num_subsystems": energy_specs.get("num_subsystems"),
            "energy.num_inverters": energy_specs.get("num_inverters"),
            "energy.num_batteries": energy_specs.get("num_batteries"),
            "energy.num_panels": energy_specs.get("num_panels"),
        }
    )

    # 3. Solar potential data from fetch_solar_potential
    solar_result = context.get_previous_result("fetch_solar_potential") or {}
    all_values.update(
        {
            "energy.gsa_daily_potential_kwhperkwp": solar_result.get("daily_kwh_per_kwp"),
            "energy.gsa_yearly_potential_kwhperkwp": solar_result.get("yearly_kwh_per_kwp"),
            "solar.optimal_tilt_deg": solar_result.get("optimal_tilt_deg"),
            "solar.ghi_kwh_m2": solar_result.get("ghi_kwh_m2"),
            "solar.gti_kwh_m2": solar_result.get("gti_kwh_m2"),
            "solar.dni_kwh_m2": solar_result.get("dni_kwh_m2"),
            "solar.avg_temp_c": solar_result.get("avg_temp_c"),
            "solar.elevation_m": solar_result.get("elevation_m"),
        }
    )

    # Filter out None values but keep 0s
    return {k: v for k, v in all_values.items() if v is not None}


def _fetch_sheet_keys_multi_column(
    document_id: str,
    sheet_name: str,
    key_columns: List[str],
) -> List[Dict[str, Any]]:
    """Fetch key labels from specified columns of a sheet.

    Args:
        document_id: Google Sheets ID
        sheet_name: Name of the worksheet
        key_columns: List of column letters to check for keys (e.g., ["A", "D"])

    Returns:
        List of dicts with row, key_column, label, value_column, ref_column
    """
    creds = get_sheets_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    all_keys: List[Dict[str, Any]] = []

    for key_col in key_columns:
        key_col = key_col.upper()
        range_name = f"'{sheet_name}'!{key_col}:{key_col}"

        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=document_id,
                range=range_name,
            )
            .execute()
        )

        values = result.get("values", [])

        # Calculate adjacent columns for value and data key reference
        key_col_idx = _column_letter_to_index(key_col)
        value_col = _index_to_column_letter(key_col_idx + 1)
        ref_col = _index_to_column_letter(key_col_idx + 2)

        for row_idx, row in enumerate(values, start=1):
            label = row[0] if row else ""
            if label:  # Only include non-empty labels
                all_keys.append(
                    {
                        "row": row_idx,
                        "key_column": key_col,
                        "label": label,
                        "value_column": value_col,
                        "ref_column": ref_col,
                    }
                )

    return all_keys


def _get_sheet_id(service, document_id: str, sheet_name: str) -> Optional[int]:
    """Get the sheet ID for a named sheet.

    Args:
        service: Google Sheets API service
        document_id: Google Sheets document ID
        sheet_name: Name of the worksheet

    Returns:
        Sheet ID (integer) or None if not found
    """
    try:
        spreadsheet = service.spreadsheets().get(spreadsheetId=document_id).execute()
        for sheet in spreadsheet.get("sheets", []):
            props = sheet.get("properties", {})
            if props.get("title") == sheet_name:
                sheet_id = props.get("sheetId")
                return int(sheet_id) if sheet_id is not None else None
    except Exception as e:
        LOGGER.warning(f"Could not get sheet ID for '{sheet_name}': {e}")
    return None


def _write_mapped_values(
    document_id: str,
    sheet_name: str,
    write_operations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Write values and data keys to adjacent columns (grey text for refs).

    Each write operation specifies its own value_column and ref_column,
    supporting multi-column layouts where keys may be in different columns.

    Args:
        document_id: Google Sheets ID
        sheet_name: Name of the worksheet
        write_operations: List of dicts with row, value, data_key, value_column, ref_column

    Returns:
        Dict with success status and optional error
    """
    if not write_operations:
        return {"success": True, "cells_written": 0}

    creds = get_sheets_write_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Build batch update for values and data keys (each op has its own columns)
    data = []
    for op in write_operations:
        row = op["row"]
        value = op["value"]
        data_key = op.get("data_key", "")
        value_col = op.get("value_column", "B")
        ref_col = op.get("ref_column", "C")

        # Write value to value_column and data key to ref_column
        data.append(
            {
                "range": f"'{sheet_name}'!{value_col}{row}:{ref_col}{row}",
                "values": [[value, data_key]],
            }
        )

    body = {
        "valueInputOption": "USER_ENTERED",
        "data": data,
    }

    try:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=document_id,
            body=body,
        ).execute()

        # Format ref columns as light grey (group by ref_column for efficiency)
        sheet_id = _get_sheet_id(service, document_id, sheet_name)
        if sheet_id is not None:
            # Group operations by ref_column
            ops_by_ref_col: Dict[str, List[int]] = {}
            for op in write_operations:
                ref_col = op.get("ref_column", "C")
                if ref_col not in ops_by_ref_col:
                    ops_by_ref_col[ref_col] = []
                ops_by_ref_col[ref_col].append(op["row"])

            # Create format requests for each ref column
            format_requests = []
            for ref_col, rows in ops_by_ref_col.items():
                ref_col_idx = _column_letter_to_index(ref_col)
                min_row = min(rows)
                max_row = max(rows)

                format_requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": min_row - 1,  # 0-indexed
                                "endRowIndex": max_row,  # exclusive
                                "startColumnIndex": ref_col_idx,
                                "endColumnIndex": ref_col_idx + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {
                                        "foregroundColor": {
                                            "red": 0.6,
                                            "green": 0.6,
                                            "blue": 0.6,
                                        }
                                    }
                                }
                            },
                            "fields": "userEnteredFormat.textFormat.foregroundColor",
                        }
                    }
                )

            if format_requests:
                try:
                    service.spreadsheets().batchUpdate(
                        spreadsheetId=document_id,
                        body={"requests": format_requests},
                    ).execute()
                    LOGGER.debug(f"Applied grey formatting to {len(format_requests)} ref column(s)")
                except Exception as e:
                    # Non-fatal - formatting is nice-to-have
                    LOGGER.warning(f"Could not apply grey formatting: {e}")

        return {"success": True, "cells_written": len(write_operations)}

    except Exception as e:
        LOGGER.exception(f"Error writing to sheet: {e}")
        return {"success": False, "error": str(e)}


@register_step(
    "populate_lpp_cells",
    contract=StepContract(
        description=(
            "Populates the Main Input sheet with site/design/energy data via explicit "
            "Cell Mapping (from the expert config), replaces the map image in the "
            "'Proposed Budget' sheet, and creates the Full BOM tab."
        ),
        # document_id is a hard requirement: `if not document_id: return
        # StepResult.failure(...)`.
        consumes_state=("document_id",),
        # cells_populated: idempotency guard. site_name/site_id: only dumped
        # as reference values via _collect_all_available_values(), filtered
        # out if None. map_image_drive_id: `if not map_image_b64: drive_id =
        # get_state(...); if drive_id: <download>` -- falls through to "no
        # image available" logging, not a failure, when absent. The four
        # editable_* overrides: `override_value =
        # get_parameter_value(editable_key); if override_value is not None:
        # <apply>` -- skipped entirely when absent.
        optional_consumes_state=(
            "cells_populated",
            "site_name",
            "site_id",
            "map_image_drive_id",
            "editable_total_buildings",
            "editable_served_building_count",
            "editable_total_kwp",
            "editable_total_kwh",
        ),
        produces_state=("cells_populated", "map_image_replaced", "bom_tab_populated"),
        consumes_results=(
            "generate_distribution_map",
            "generate_powerplant_design",
            "generate_site_bom",
            "fetch_solar_potential",
        ),
        params=(
            ParamSpec(
                name="editable_total_buildings",
                param_type="integer",
                description="User-confirmed total building count override (maps to computed.total_buildings).",
            ),
            ParamSpec(
                name="editable_served_building_count",
                param_type="integer",
                description="User-confirmed served-building count override (maps to meta.served_building_count).",
            ),
            ParamSpec(
                name="editable_total_kwp",
                param_type="number",
                description="User-confirmed total kWp override (maps to energy.total_kwp).",
            ),
            ParamSpec(
                name="editable_total_kwh",
                param_type="number",
                description="User-confirmed total kWh override (maps to energy.total_kwh).",
            ),
        ),
        guard_keys=("cells_populated",),
        side_effects=(
            "Writes to the Main Input sheet and replaces the map image in the "
            "'Proposed Budget' sheet via the Sheets/Apps Script API; creates the Full "
            "BOM tab (calls create_bom_sheet from populate_bom_tab.py)."
        ),
    ),
)
async def populate_lpp_cells(context: StepContext) -> StepResult:
    """Populate the sheet with site data using LLM-based matching.

    Reads keys from specified columns (default: A), uses LLM to match them
    to data keys, writes values to the adjacent column and data key references
    to the column after that (grey text).

    Supports multi-column layouts where keys may be in different columns
    (e.g., A and D for a two-column form layout).

    Explicit mappings from the `## Cell Mapping` section take precedence
    over LLM-based matching.

    Inputs:
    - sheet_name: Sheet to populate (default: "Main Input")
    - key_columns: Columns to check for keys (default: ["A"])

    Requires:
    - document_id in state (from copy_lpp_template)
    - Data from previous steps (generate_distribution_map, generate_powerplant_design)

    Args:
        context: Step execution context with packet inputs

    Returns:
        StepResult with cells_populated count and mapping details
    """
    # Idempotency guard: cells already populated (handles recovery re-entry)
    if context.get_state("cells_populated"):
        LOGGER.info("populate_lpp_cells: already done, skipping")
        return StepResult(
            data={"cells_populated": True},
            state_updates={},
            progress_message="Spreadsheet cells already populated.",
        )

    await context.send_progress_to_user("Populating spreadsheet cells...")

    document_id = context.get_state("document_id")

    if not document_id:
        return StepResult.failure("No document_id in state - run copy_lpp_template first")

    # Get sheet name from input or use default
    sheet_name = context.get_input("sheet_name") or DEFAULT_SHEET_NAME

    # Get expert raw sections for parsing additional settings (cell mapping, key columns)
    expert_raw_sections = context.accumulated_results.get("expert_raw_sections", {})

    # Determine key columns - priority: input > expert config > default
    key_columns_input = context.get_input("key_columns")
    if key_columns_input:
        # From step input (highest priority)
        if isinstance(key_columns_input, str):
            key_columns = [c.strip().upper() for c in key_columns_input.split(",")]
        else:
            key_columns = [c.strip().upper() for c in key_columns_input]
    else:
        # Try expert config (### Input Data Key Column section)
        key_columns = _parse_key_columns(expert_raw_sections)
        if not key_columns:
            # Fall back to default
            key_columns = DEFAULT_KEY_COLUMNS

    LOGGER.info(
        f"Populating cells in {sheet_name} for doc {document_id}, key columns: {key_columns}"
    )

    # 1. Collect all available values from workflow results
    all_values = _collect_all_available_values(context)

    # Apply user overrides from editable parameters (confirmation flow)
    for editable_key, data_key in EDITABLE_PARAM_MAPPINGS.items():
        override_value = context.get_parameter_value(editable_key)
        if override_value is not None:
            LOGGER.info(f"Applying user override: {data_key} = {override_value}")
            all_values[data_key] = override_value

    if not all_values:
        return StepResult.failure("No data available from previous workflow steps")

    # 2. Get sheet keys from specified columns
    try:
        key_entries = await asyncio.to_thread(
            _fetch_sheet_keys_multi_column, document_id, sheet_name, key_columns
        )
    except Exception as e:
        LOGGER.exception(f"Error fetching sheet keys: {e}")
        return StepResult.failure(f"Error reading sheet: {str(e)}")

    if not key_entries:
        LOGGER.warning(f"No keys found in columns {key_columns} of {sheet_name}")
        return StepResult(
            data={"cells_populated": 0, "message": f"No keys found in columns {key_columns}"},
            state_updates={"cells_populated": True},
            progress_message="No cells to populate",
        )

    # 3. Parse explicit mappings from expert config (overrides)
    # explicit_mapping uses lowercase keys for case-insensitive matching
    explicit_mapping = _parse_cell_mapping(expert_raw_sections)

    # 4. Build write operations using ONLY explicit mappings
    # Labels not in the Cell Mapping section are skipped entirely
    write_operations: List[Dict[str, Any]] = []
    matched_keys = []
    unmatched_keys = []
    debug_writes: List[Dict[str, Any]] = []  # For detailed output

    for entry in key_entries:
        label = entry["label"]
        # Only use explicit mappings (case-insensitive via lowercase key)
        explicit_data_key = explicit_mapping.get(label.lower())
        data_key = explicit_data_key if explicit_data_key in all_values else None

        if data_key and data_key in all_values:
            value = all_values[data_key]
            write_op = {
                "row": entry["row"],
                "value": value,
                "data_key": data_key,
                "value_column": entry["value_column"],
                "ref_column": entry["ref_column"],
            }
            write_operations.append(write_op)
            debug_writes.append(
                {
                    "cell": f"{entry['key_column']}{entry['row']}",
                    "label": label,
                    "data_key": data_key,
                    "value": value,
                    "value_cell": f"{entry['value_column']}{entry['row']}",
                    "ref_cell": f"{entry['ref_column']}{entry['row']}",
                }
            )
            matched_keys.append(f"{label} ({entry['key_column']}{entry['row']})")
        elif data_key:
            # Mapping exists but data not available
            unmatched_keys.append(f"{label} (key: {data_key} not in data)")
        else:
            # No mapping found
            unmatched_keys.append(f"{label} (no match)")

    LOGGER.info(
        f"Prepared {len(write_operations)} cell updates, {len(unmatched_keys)} unmapped labels"
    )

    # 6. Write to sheet (adjacent column = value, next column = data key in grey)
    if not write_operations:
        return StepResult(
            data={
                "cells_populated": 0,
                "matched_keys": matched_keys,
                "unmatched_keys": unmatched_keys,
                "mapping": explicit_mapping,
            },
            state_updates={"cells_populated": True},
            progress_message="No matching cells found to populate",
        )

    try:
        result = await asyncio.to_thread(
            _write_mapped_values, document_id, sheet_name, write_operations
        )
    except Exception as e:
        LOGGER.exception(f"Error writing sheet values: {e}")
        return StepResult.failure(f"Error writing to sheet: {str(e)}")

    if not result["success"]:
        return StepResult.failure(f"Failed to write values: {result.get('error', 'Unknown error')}")

    cells_written = result.get("cells_written", len(write_operations))
    LOGGER.info(f"Successfully populated {cells_written} cells in {sheet_name}")

    # 9. Replace map image in "Proposed Budget" sheet
    # The map image from generate_distribution_map needs to be inserted into the budget sheet
    map_image_replaced = False
    map_image_error = None

    # Get map image: prefer step result, then Drive download via stored file ID
    map_result_img = context.get_previous_result("generate_distribution_map") or {}
    map_image_b64 = map_result_img.get("map_image_b64")
    if not map_image_b64:
        drive_id = context.get_state("map_image_drive_id")
        if drive_id:
            try:
                from shared.utils.drive_upload import download_drive_file

                img_bytes = await download_drive_file(drive_id)
                import base64 as b64mod

                map_image_b64 = b64mod.b64encode(img_bytes).decode()
                LOGGER.info("Retrieved map image from Drive for sheet insertion")
            except Exception as e:
                LOGGER.warning(f"Failed to download map image from Drive: {e}")

    if map_image_b64:
        LOGGER.info("Replacing map image in 'Proposed Budget' sheet")
        try:
            # Use convenience function which handles image resizing for Sheets' 1M pixel limit
            image_result = await replace_sheet_image(
                sheet_id=document_id,
                worksheet_name="Proposed Budget",
                image_base64=map_image_b64,
                min_height_px=100,  # Match images >= 100px height
            )

            if image_result.success:
                map_image_replaced = True
                image_data = image_result.data or {}
                mode = image_data.get("mode", "unknown")
                LOGGER.info(
                    f"Map image {mode} in 'Proposed Budget': "
                    f"{image_data.get('inserted_width')}x{image_data.get('inserted_height')} -> "
                    f"{image_data.get('final_width')}x{image_data.get('final_height')}"
                )
            else:
                map_image_error = image_result.error_message
                LOGGER.warning(f"Failed to replace map image: {map_image_error}")
        except Exception as e:
            map_image_error = str(e)
            LOGGER.exception(f"Error replacing map image: {e}")
    else:
        LOGGER.warning("No map image available to insert into 'Proposed Budget' sheet")

    # 10. Create Full BOM tab with items grouped by Component Type
    bom_tab_populated = False
    bom_tab_error = None
    bom_item_count = 0

    # BOM items come from generate_site_bom (preferred) or generate_powerplant_design (fallback)
    bom_result_src = context.get_previous_result("generate_site_bom") or {}
    bom_items = bom_result_src.get("bom_items", [])
    if not bom_items:
        LOGGER.warning(
            "generate_site_bom result empty, falling back to generate_powerplant_design BOM"
        )
        design_result_bom = context.get_previous_result("generate_powerplant_design") or {}
        bom_items = design_result_bom.get("bom_items", [])
    if bom_items:
        try:
            from orchestrator.experts.handlers.package_generator.populate_bom_tab import (
                create_bom_sheet,
            )

            bom_result = await asyncio.to_thread(create_bom_sheet, document_id, bom_items)
            bom_tab_populated = bom_result.get("success", False)
            bom_item_count = bom_result.get("bom_item_count", len(bom_items))
            if not bom_tab_populated:
                bom_tab_error = bom_result.get("error")
        except Exception as e:
            bom_tab_error = str(e)
            LOGGER.exception(f"Error creating BOM tab: {e}")
    else:
        LOGGER.info("No BOM items available - skipping BOM tab creation")

    return StepResult(
        data={
            "cells_populated": cells_written,
            "matched_keys": matched_keys,
            "unmatched_keys": unmatched_keys,
            "mapping": explicit_mapping,
            "key_columns": key_columns,
            "debug_writes": debug_writes,  # Detailed write operations for debugging
            "map_image_replaced": map_image_replaced,
            "map_image_error": map_image_error,
            "bom_tab_populated": bom_tab_populated,
            "bom_tab_error": bom_tab_error,
            "bom_item_count": bom_item_count,
        },
        state_updates={
            "cells_populated": True,
            "map_image_replaced": map_image_replaced,
            "bom_tab_populated": bom_tab_populated,
        },
        progress_message=f"Populated {cells_written} cells in {sheet_name}"
        + (", map image updated" if map_image_replaced else "")
        + (f", BOM tab with {bom_item_count} items" if bom_tab_populated else ""),
    )
