"""Dump all available values to reference columns in LPP sheet.

This handler dumps ALL available mapped values to columns E/F of the Main Input
sheet. This creates a reference list of all available data that can be manually
matched later or used for debugging.

Column E: Value key/label (e.g., site.site_name, meta.pole_count, bom.total_cost)
Column F: Value
"""

from typing import Any, List, Tuple

from googleapiclient.discovery import build

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.google_auth import get_sheets_write_credentials
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@register_step("dump_lpp_values")
async def dump_lpp_values(context: StepContext) -> StepResult:
    """Dump all available mapped values to columns E/F of Main Input sheet.

    Column E: Key/label (e.g., "site.site_name")
    Column F: Value

    This creates a reference list for manual matching or later automation.

    Data sources:
    - site.*: pd_site_submissions direct fields
    - meta.*: meta_geo_flat JSON
    - computed.*: Calculated from GeoJSON
    - bom.*: BOM cost summary from generate_powerplant_design
    - design.*: Design parameters from AppSheet
    """
    await context.send_progress_to_user("Writing reference values to spreadsheet...")

    document_id = context.get_state("document_id")
    if not document_id:
        return StepResult.failure("No document_id in state")

    # Collect all available values from various sources
    all_values: List[Tuple[str, Any]] = []

    # 1. Site data from generate_distribution_map
    map_result = context.get_previous_result("generate_distribution_map") or {}
    statistics = map_result.get("statistics", {})
    center = map_result.get("center", {})
    site_state = map_result.get("site_state")

    all_values.extend(
        [
            ("site.site_name", context.get_state("site_name")),
            ("site.site_id", context.get_state("site_id")),
            ("site.state", site_state),
            ("location.lat", center.get("lat")),
            ("location.lon", center.get("lon")),
            ("meta.pole_count", statistics.get("poles", 0)),
            ("meta.served_building_count", statistics.get("served_buildings", 0)),
            ("meta.unserved_building_count", statistics.get("unserved_buildings", 0)),
            ("computed.total_buildings", statistics.get("total_buildings", 0)),
            ("computed.cable_length_m", statistics.get("cable_length_m")),
        ]
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
    if total_kwp and served_buildings > 0:
        try:
            wp_per_conn = round(float(total_kwp) * 1000 / served_buildings, 1)
        except (TypeError, ValueError):
            pass

    all_values.extend(
        [
            ("design.design_id", design_result.get("design_id")),
            ("design.design_name", design_result.get("design_name")),
            ("bom.total_cost", cost_summary.get("total_cost", 0)),
            ("bom.main_energy_asset_cost", cost_summary.get("main_energy_asset_cost", 0)),
            ("bom.metering_cost", cost_summary.get("metering_cost", 0)),
            ("bom.bos_cost", cost_summary.get("bos_cost", 0)),
            ("bom.item_count", design_result.get("bom_item_count", 0)),
            # Energy specs from design
            ("energy.total_kwp", total_kwp),
            ("energy.total_kwh", energy_specs.get("total_kwh")),
            ("energy.total_kva", energy_specs.get("total_kva")),
            ("energy.Wp_per_conn", wp_per_conn),
            ("energy.num_subsystems", energy_specs.get("num_subsystems")),
            ("energy.num_inverters", energy_specs.get("num_inverters")),
            ("energy.num_batteries", energy_specs.get("num_batteries")),
            ("energy.num_panels", energy_specs.get("num_panels")),
        ]
    )

    # Filter out None values but keep 0s
    all_values = [(k, v) for k, v in all_values if v is not None]

    # Write to columns E/F
    creds = get_sheets_write_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Build data for columns E and F starting at row 1
    data = []
    for i, (key, value) in enumerate(all_values, start=1):
        data.append(
            {
                "range": f"'Main Input'!E{i}:F{i}",
                "values": [[key, value]],
            }
        )

    body = {"valueInputOption": "USER_ENTERED", "data": data}

    try:
        service.spreadsheets().values().batchUpdate(spreadsheetId=document_id, body=body).execute()
    except Exception as e:
        LOGGER.exception(f"Error dumping values: {e}")
        return StepResult.failure(f"Error writing values: {str(e)}")

    LOGGER.info(f"Dumped {len(all_values)} values to columns E/F")

    return StepResult(
        data={"values_dumped": len(all_values), "value_keys": [k for k, _ in all_values]},
        state_updates={"values_dumped": True},
        progress_message=f"Dumped {len(all_values)} available values to reference columns",
    )
