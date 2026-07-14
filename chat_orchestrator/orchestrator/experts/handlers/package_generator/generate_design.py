"""Generate design step handler for Light Preliminary Package.

This handler calls the grid_design MCP server to create a design (without
BOM). BOM is triggered separately after site layout provides real cable
distances. Any design parameter the user/LLM supplied (technology choices,
connection split, Wp/conn override, regulation constraint, 3-phase
enforcement, SPD type, tariff, ...) is forwarded; omitted ones fall back to
the engine's AppSheet-form defaults.
"""

import asyncio
import json
from datetime import datetime, timezone

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_contracts import ParamSpec, StepContract
from orchestrator.experts.step_registry import register_step
from shared.grid_design.artifact_log import sweep_state_for_artifacts
from shared.utils.error_messages import sanitize_error_for_user
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Optional design parameters forwarded to design_and_bom when the user/LLM
# supplied them (via parameter confirmation overrides, packet inputs, or
# LLM-parsed inputs). Unsupplied ones are omitted so the grid-design engine's
# AppSheet-form defaults apply. Full catalogue: grid_design list_design_options.
OPTIONAL_DESIGN_PARAMS = (
    "technology_family",
    "inverter_type",
    "battery_type",
    "mppt_type",
    "pv_type",
    "pv_inverter_type",
    "initial_residential_connections",
    "initial_business_connections",
    "initial_3phase_connections",
    "force_3phase",
    "wp_per_conn_override",
    "regulation_constraint",
    "spd_type",
    "anchor_load_kw",
    "pue_hours_per_day",
    "daily_generation_potential_kwh_kwp",
    "target_tariff_usd",
    "num_poc_teams",
)


# ParamSpecs for the optional design parameters above, plus the other genuinely
# user-tunable inputs this step reads via get_parameter_value(). Hand-written in
# the same order as OPTIONAL_DESIGN_PARAMS above — keep both in sync manually
# if either changes.
_OPTIONAL_DESIGN_PARAM_SPECS = (
    ParamSpec(
        name="technology_family",
        description="Power plant technology family/architecture ('victron' or 'deye').",
        synonyms=("technology type", "design type", "vendor", "equipment family"),
    ),
    ParamSpec(
        name="inverter_type",
        description="Inverter technology type selection.",
        synonyms=("inverter",),
    ),
    ParamSpec(
        name="battery_type",
        description="Battery technology type selection.",
        synonyms=("battery",),
    ),
    ParamSpec(
        name="mppt_type",
        description="MPPT controller type selection.",
        synonyms=("mppt",),
    ),
    ParamSpec(
        name="pv_type",
        description="PV panel technology type selection.",
        synonyms=("pv", "panel type", "solar panel type"),
    ),
    ParamSpec(
        name="pv_inverter_type",
        description="PV (string) inverter type selection.",
        synonyms=("pv inverter",),
    ),
    ParamSpec(
        name="initial_residential_connections",
        param_type="integer",
        description="Initial number of residential connections.",
        synonyms=("residential connections", "residential buildings", "household connections"),
    ),
    ParamSpec(
        name="initial_business_connections",
        param_type="integer",
        description="Initial number of business/nonresidential connections.",
        synonyms=(
            "business connections",
            "commercial connections",
            "nonresidential connections",
            "non-residential connections",
            "non residential connections",
            "business buildings",
        ),
    ),
    ParamSpec(
        name="initial_3phase_connections",
        param_type="integer",
        description="Initial number of 3-phase connections.",
        synonyms=("3-phase connections", "three phase connections"),
    ),
    ParamSpec(
        name="force_3phase",
        param_type="boolean",
        description="Force all connections to 3-phase.",
        synonyms=("force 3 phase", "3-phase enforcement"),
    ),
    ParamSpec(
        name="wp_per_conn_override",
        param_type="number",
        description="Override for Wp-per-connection sizing.",
        synonyms=("wp per conn", "wp/conn", "wp per connection", "watts per connection"),
    ),
    ParamSpec(
        name="regulation_constraint",
        description="Constrain the design to a known regulation profile.",
        synonyms=("regulation", "regulatory constraint", "Nigerian law", "DARES"),
    ),
    ParamSpec(
        name="spd_type",
        description="Surge protection device (SPD) type selection.",
        synonyms=("spd", "surge protection"),
    ),
    ParamSpec(
        name="anchor_load_kw",
        param_type="number",
        description="Anchor load in kW used for sizing.",
        synonyms=("anchor load",),
    ),
    ParamSpec(
        name="pue_hours_per_day",
        param_type="number",
        description="Productive use of energy (PUE) hours per day.",
        synonyms=("pue hours",),
    ),
    ParamSpec(
        name="daily_generation_potential_kwh_kwp",
        param_type="number",
        description="Daily solar generation potential (kWh/kWp).",
        synonyms=("daily generation potential",),
    ),
    ParamSpec(
        name="target_tariff_usd",
        param_type="number",
        description="Target tariff in USD used for sizing.",
        synonyms=("target tariff",),
    ),
    ParamSpec(
        name="num_poc_teams",
        param_type="integer",
        description="Number of point-of-connection installation teams.",
        synonyms=("poc teams",),
    ),
)


def _get_requester_name(context: StepContext) -> str:
    """Extract requester name from email or return default.

    Uses the part before @ from the email address.
    """
    email: str | None = context.effective_email
    if email and "@" in email:
        # Extract name part and capitalize
        name_part = email.split("@")[0]
        # Handle firstname.lastname format
        if "." in name_part:
            parts = name_part.split(".")
            return " ".join(p.capitalize() for p in parts)
        return name_part.capitalize()
    return "Staff"


@register_step(
    "generate_powerplant_design",
    contract=StepContract(
        description=(
            "Creates a grid design (no BOM yet) via the grid_design MCP server, using "
            "site submission / distribution-map data and any user-supplied design "
            "parameters."
        ),
        # site_name is the only hard requirement (ParamSpec required=True
        # below; `if not site_name: return StepResult.failure(...)`).
        consumes_state=("site_name",),
        # design_generated/design_id: idempotency guard, absence just runs the
        # main path. max_connections: `get_parameter_value(...) or
        # default_max_connections` (served_buildings). editable_total_kwp/kwh:
        # only passed to the design engine `if target_kwp:` / `if
        # target_kwh:` -- omitted entirely when falsy/absent, per the "empty
        # lets the engine calculate freely" contract documented on their
        # ParamSpecs below. OPTIONAL_DESIGN_PARAMS: forwarded only `if value
        # is not None and value != ""`, omitted otherwise so the engine's
        # AppSheet-form defaults apply (see the module docstring and
        # OPTIONAL_DESIGN_PARAMS' own comment) -- none of these are produced
        # by any step either, so leaving them required would permanently
        # block `satisfied` whenever the user hasn't supplied them (i.e.
        # almost always).
        optional_consumes_state=(
            "design_generated",
            "design_id",
            "max_connections",
            "editable_total_kwp",
            "editable_total_kwh",
        )
        + OPTIONAL_DESIGN_PARAMS,
        produces_state=(
            "design_generated",
            "design_id",
            "design_name",
            "total_kwp",
            "total_kwh",
            "total_kva",
            "num_subsystems",
            "num_inverters",
            "num_batteries",
            "num_panels",
            "editable_total_kwp",
            "editable_total_kwh",
        ),
        consumes_results=("generate_distribution_map",),
        params=(
            ParamSpec(
                name="site_name",
                description="Name of the site to generate the design for.",
                required=True,
            ),
            ParamSpec(
                name="max_connections",
                param_type="integer",
                description="Maximum number of connections for the design (defaults to served buildings).",
                synonyms=("connections", "max connections"),
            ),
            ParamSpec(
                name="editable_total_kwp",
                param_type="number",
                description="User-confirmed target total kWp (empty lets the engine calculate freely).",
            ),
            ParamSpec(
                name="editable_total_kwh",
                param_type="number",
                description="User-confirmed target total kWh (empty lets the engine calculate freely).",
            ),
        )
        + _OPTIONAL_DESIGN_PARAM_SPECS,
        guard_keys=("design_generated",),
        side_effects=(
            "Calls the grid_design_design_and_bom MCP tool, which creates a design row "
            "in the Chat DB (gd_designs); also sweeps packet_state for previously "
            "uploaded Drive artifact IDs and attaches them to the new design."
        ),
    ),
)
async def generate_powerplant_design(context: StepContext) -> StepResult:
    """Generate a grid design using site submission data.

    Maps pd_site_submissions fields to design_and_bom inputs:
    - grid_name <- site_name
    - design_name <- "{site_name} LPP {datetime} by {requester}"
    - max_connections <- served_building_count
    - residential/business split <- engine's 90/10 default unless overridden

    Requires:
    - site_id and site_name in state (from generate_distribution_map)
    - mcp_executor available for tool calls
    """
    # Idempotency guard: design already created (handles recovery re-entry)
    if context.get_state("design_generated"):
        design_id = context.get_state("design_id")
        LOGGER.info(f"generate_powerplant_design: already done (design_id={design_id}), skipping")
        return StepResult(
            data={"design_id": design_id, "design_generated": True},
            state_updates={},
            progress_message="Design already created.",
        )

    # Use get_parameter_value() to respect user overrides from confirmation flow
    site_name = context.get_parameter_value("site_name") or context.get_state("site_name")

    # site_name is the grid_name — the only identifier this step sends.
    # The community route has no DB site_id, so require site_name only.
    if not site_name:
        return StepResult.failure("No site data in state - run generate_distribution_map first")

    if not context.mcp_executor:
        return StepResult.failure("MCP executor not available")

    # Get statistics from previous step (generate_distribution_map stores these)
    prev_result = context.get_previous_result("generate_distribution_map")
    statistics = prev_result.get("statistics", {}) if prev_result else {}

    # Debug: Log what we received from generate_distribution_map
    LOGGER.info(
        f"generate_powerplant_design received from generate_distribution_map: "
        f"prev_result keys={list(prev_result.keys()) if prev_result else 'None'}, "
        f"statistics={statistics}"
    )
    LOGGER.debug(f"Full accumulated_results keys: {list(context.accumulated_results.keys())}")

    # served_buildings = total connections (buildings within pole coverage)
    served_buildings = statistics.get("served_buildings", 0)
    default_max_connections = served_buildings

    # Check for user override of max_connections
    max_connections = context.get_parameter_value("max_connections")
    if max_connections is None:
        max_connections = default_max_connections
    else:
        LOGGER.info(
            f"Using user-specified max_connections: {max_connections} (default was {default_max_connections})"
        )

    if served_buildings == 0:
        LOGGER.error(
            f"No served buildings! prev_result={prev_result}, "
            f"accumulated_results={context.accumulated_results}"
        )
        return StepResult.failure(f"No served buildings found for site '{site_name}'")

    # Check for user-specified energy targets (from parameter confirmation)
    target_kwp = context.get_parameter_value("editable_total_kwp")
    target_kwh = context.get_parameter_value("editable_total_kwh")

    # Build design_and_bom arguments
    # Residential/business split defaults to 90/10 in the engine unless overridden below
    # Create design name with date/time and requester for tracking
    requester_name = _get_requester_name(context)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    design_name = f"{site_name} LPP {timestamp} by {requester_name}"

    design_args = {
        "grid_name": site_name,
        "design_name": design_name,
        "max_connections": max_connections,
        "wait_for_completion": True,
    }

    # Only pass targets if user specified them (non-empty)
    if target_kwp:
        design_args["target_kwp"] = float(target_kwp)
    if target_kwh:
        design_args["target_kwh"] = float(target_kwh)

    # Forward any user/LLM-supplied design parameters; omitted ones keep the
    # engine's AppSheet-form defaults.
    forwarded = []
    for param in OPTIONAL_DESIGN_PARAMS:
        value = context.get_parameter_value(param)
        if value is not None and value != "":
            design_args[param] = value
            forwarded.append(param)
    if forwarded:
        LOGGER.info(f"Forwarding user-specified design parameters: {forwarded}")

    email: str | None = context.effective_email
    if email:
        design_args["created_by"] = email

    # Calculate average service drop length from layout statistics
    drop_cable_total_m = statistics.get("drop_cable_length_m", 0)
    if drop_cable_total_m and served_buildings > 0:
        design_args["avg_service_drop_length_m"] = drop_cable_total_m / served_buildings

    LOGGER.info(
        f"Calling design_and_bom for {site_name}: {max_connections} connections "
        f"({served_buildings} served), design: {design_name}"
    )

    # Skip BOM — it will be triggered separately after site layout
    # provides real cable distances (feeder pillar, PV combiner)
    design_args["wait_for_bom"] = False

    # Send immediate progress message to user before long operation
    technology_family = context.get_parameter_value("technology_family") or context.get_state(
        "technology_family"
    )
    technology_label = str(technology_family or "default").upper()
    await context.send_progress_to_user(
        f"⏳ Generating {technology_label} design for {site_name}...\nThis may take a while."
    )

    try:
        result_str = await context.mcp_executor.call_tool(
            "grid_design_design_and_bom",  # server_toolname format
            design_args,
        )

        # call_tool returns JSON string from MCP server - parse it
        try:
            result = json.loads(result_str) if isinstance(result_str, str) else result_str
        except json.JSONDecodeError as e:
            LOGGER.error(f"Failed to parse design_and_bom response: {e}")
            return StepResult.failure("Design generation returned invalid response")

        if not result.get("success"):
            error = result.get("error", "Unknown error")
            LOGGER.error(f"design_and_bom failed: {error}")
            return StepResult.failure(f"Design generation failed: {error}")

        # Extract key outputs
        design_data = result.get("design", {})
        cost_summary = result.get("cost_summary", {})
        energy_specs = result.get("energy_specs", {})

        # Extract output data which contains design_parameters and energy_specs
        output_data = result.get("output", {})
        if not energy_specs and output_data:
            energy_specs = output_data.get("energy_specs", {})

        # Extract design parameters (equipment types, constraints, etc.)
        design_parameters = output_data.get("design_parameters", {})

        # Log what we received for debugging
        LOGGER.info(
            f"Design result for {site_name}: "
            f"energy_specs={energy_specs}, cost_summary keys={list(cost_summary.keys())}"
        )

        # Build state updates — design only, no BOM yet
        design_id = design_data.get("Id")

        # Earlier LPP steps (map generation, community resolution, site layout)
        # may have already uploaded artifacts to Drive and stashed their file
        # IDs in packet_state under `*_drive_id` keys — before this design
        # existed to attach them to. Sweep those in now that we have a
        # design_id. Non-fatal: sweep_state_for_artifacts never raises.
        if design_id:
            await asyncio.to_thread(
                sweep_state_for_artifacts,
                design_id,
                context.packet_state,
                packet_id=context.packet_id,
            )

        state_updates = {
            "design_generated": True,
            "design_id": design_id,
            "design_name": design_data.get("Name"),
            # Energy specs from autopopulated design
            "total_kwp": energy_specs.get("total_kwp", ""),
            "total_kwh": energy_specs.get("total_kwh", ""),
            "total_kva": energy_specs.get("total_kva", ""),
            "num_subsystems": energy_specs.get("num_subsystems", ""),
            "num_inverters": energy_specs.get("num_inverters", ""),
            "num_batteries": energy_specs.get("num_batteries", ""),
            "num_panels": energy_specs.get("num_panels", ""),
            # Editable parameters for confirmation flow
            "editable_total_kwp": energy_specs.get("total_kwp", ""),
            "editable_total_kwh": energy_specs.get("total_kwh", ""),
        }

        return StepResult(
            data={
                "design_id": design_id,
                "design_name": design_data.get("Name"),
                "energy_specs": energy_specs,
                "design_parameters": design_parameters,
            },
            state_updates=state_updates,
            progress_message=(
                f"Design created for {site_name}: "
                f"{energy_specs.get('total_kwp', '?')} kWp, "
                f"{energy_specs.get('total_kwh', '?')} kWh "
                f"(BOM will be generated after site layout)"
            ),
        )

    except Exception as e:
        LOGGER.exception(f"Error calling design_and_bom: {e}")
        return StepResult.failure(sanitize_error_for_user(str(e)))
