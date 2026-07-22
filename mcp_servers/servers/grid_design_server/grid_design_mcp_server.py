#!/usr/bin/env python3
"""
Grid Design MCP Server - Design and BOM Generation

This MCP server provides tools to:
1. Create a grid if it doesn't exist
2. Create a new design for a grid with the full AppSheet-form parameter set
3. Run design and BOM generation
4. Return energy specs, BOM items and cost summaries

Backends (GRID_DESIGN_BACKEND):
- "internal" (default): the shared grid-design engine against the Chat DB
  gd_* tables (shared/grid_design, ported from AppSheet's Apps Script).
- "appsheet": legacy AppSheet REST API v2 workflow, kept for rollback.
"""

import asyncio
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from supabase import Client, create_client

# Load environment variables BEFORE importing shared_code
load_dotenv()

from servers.grid_design_server import gd_auth, gd_crud, internal_engine
from servers.grid_design_server.internal_engine import compute_bom_cost_summary
from servers.grid_design_server.tool_schemas import TOOL_SCHEMAS
from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry

from shared.auth.auth_service import STAFF_ORG_ID
from shared.utils.http_client import HTTPClientMixin
from shared.utils.logging import get_logger

logger = get_logger("grid-design-server")

# Startup message
print("🚀 Grid Design MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("grid-design-server")
registry = ToolRegistry("grid_design")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}

# Tools implemented only by the internal backend — calling them while
# GRID_DESIGN_BACKEND=appsheet falls through to "Unknown tool", matching the
# original dispatch's behavior (the appsheet branch never had cases for them).
_INTERNAL_ONLY_TOOL_NAMES = (
    "create_design",
    "get_design",
    "list_design_artifacts",
    "get_design_artifact",
    "run_auto_design",
    "change_design_technology",
    "duplicate_design",
    "list_design_subassemblies",
    "add_subassembly",
    "remove_subassembly",
    "set_subassembly_qty",
    "list_subassembly_components",
    "add_subassembly_component",
    "remove_subassembly_component",
    "set_subassembly_component_qty",
    "duplicate_subassembly",
    "gd_describe_tables",
    "gd_list_rows",
    "gd_get_row",
    "gd_upsert_row",
    "gd_delete_row",
)

# Configuration from environment
GRID_DESIGN_APP_ID = os.getenv("GRID_DESIGN_APP_ID", "")
GRID_DESIGN_APP_KEY = os.getenv("GRID_DESIGN_APP_KEY", "")
APPSHEET_REGION = os.getenv("APPSHEET_REGION", "www.appsheet.com")
GRID_DESIGN_ACTIONS_ENABLED = os.getenv("GRID_DESIGN_ACTIONS_ENABLED", "true").lower() == "true"
# "internal" (default) = shared/grid_design engine on the Chat DB gd_* tables;
# "appsheet" = legacy AppSheet REST workflow (rollback only).
GRID_DESIGN_BACKEND = os.getenv("GRID_DESIGN_BACKEND", "internal").lower()

# Table names (configurable via env vars)
GRIDS_TABLE = os.getenv("GRID_DESIGN_GRIDS_TABLE", "Grids")
DESIGNS_TABLE = os.getenv("GRID_DESIGN_DESIGNS_TABLE", "Designs")
BOM_TABLE = os.getenv("GRID_DESIGN_BOM_TABLE", "BOM Items")
ACTIONS_TABLE = os.getenv("GRID_DESIGN_ACTIONS_TABLE", "AAL")  # Actions log table

# Component costs are read from the Chat DB (Postgres), NOT AppSheet: AppSheet's
# "Projected Cost" is a virtual column that the REST API returns unreliably
# (populated one moment, 0 the next). gd_components.projected_cost is the
# deterministic source (recomputed from the purchase ledger by anansi_app's
# cost_projection service). Reuse the shared Chat DB creds (same DB meta_server
# reads from), with legacy fallbacks.
CHAT_DB_URL = (
    os.getenv("CHAT_DB_URL") or os.getenv("MAIN_SUPABASE_URL") or os.getenv("SUPABASE_URL", "")
)
CHAT_DB_SERVICE_KEY = (
    os.getenv("CHAT_DB_SERVICE_KEY")
    or os.getenv("MAIN_SUPABASE_KEY")
    or os.getenv("SUPABASE_KEY", "")
)
GD_COMPONENTS_DB_TABLE = os.getenv("GRID_DESIGN_COMPONENTS_DB_TABLE", "gd_components")

# Action types for AAL table
ACTION_CREATE_BOM = "Create BOM"
ACTION_STATUS_REQUESTED = "Requested"
ACTION_STATUS_COMPLETED = "Completed"

# Timing configuration
DESIGN_AUTOPOPULATE_WAIT_SECONDS = int(os.getenv("GRID_DESIGN_AUTOPOPULATE_WAIT", "30"))
BOM_GENERATION_WAIT_SECONDS = int(os.getenv("GRID_DESIGN_BOM_WAIT", "80"))


@dataclass
class DesignInput:
    """Input parameters for creating a new design in AppSheet"""

    # Required inputs
    grid_name: str
    design_name: str
    max_connections: int

    # Equipment types (with defaults)
    inverter_type: str = "Quattro 15kVA"
    battery_type: str = "Pylontech UP5000"
    mppt_type: str = "Victron 250/85 MPPT"
    pv_type: str = "JA455W Panel"
    pv_inverter_type: Optional[str] = None

    # Connection distribution (calculated from max_connections if not provided)
    initial_residential_connections: Optional[int] = None  # Default: 90% of max
    initial_business_connections: Optional[int] = None  # Default: 10% of max
    initial_3phase_connections: int = 0

    # Optional energy targets (if provided, AppSheet constrains the design)
    target_kwp: Optional[float] = None
    target_kwh: Optional[float] = None

    # Surge protection device type (required by AppSheet)
    spd_type: str = "Keep default T1+T2 Type SPD (Any lightning probability)"

    # Layout-derived metrics
    avg_service_drop_length_m: Optional[float] = None

    # Other defaults
    num_poc_teams: int = 1
    anchor_load_kw: float = 0
    force_3phase: bool = False


class AppSheetClient(HTTPClientMixin):
    """Client for interacting with AppSheet REST API v2"""

    def __init__(self, app_id: str, app_key: str, region: str = "www.appsheet.com"):
        super().__init__()
        self.app_id = app_id
        self.app_key = app_key
        self.region = region
        self.base_url = f"https://{region}/api/v2/apps/{app_id}"

    def _get_url(self, table_name: str) -> str:
        """Build API URL for a table action"""
        encoded_table = urllib.parse.quote(table_name)
        return f"{self.base_url}/tables/{encoded_table}/Action?applicationAccessKey={self.app_key}"

    async def _make_request(
        self,
        table_name: str,
        action: str,
        rows: List[Dict[str, Any]],
        properties: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a request to AppSheet API"""
        session = await self.get_session()

        url = self._get_url(table_name)
        payload: Dict[str, Any] = {
            "Action": action,
            "Rows": rows,
        }

        if properties:
            payload["Properties"] = properties

        logger.info(f"AppSheet API call: {action} on {table_name}")
        logger.debug(f"Payload: {json.dumps(payload, indent=2)}")

        async with session.post(
            url, json=payload, headers={"Content-Type": "application/json"}
        ) as response:
            response_text = await response.text()

            if response.status >= 400:
                logger.error(f"AppSheet API error {response.status}: {response_text[:500]}")
                raise Exception(f"AppSheet API error {response.status}: {response_text[:200]}")

            try:
                result: Dict[str, Any] = json.loads(response_text)
                logger.debug(f"AppSheet response: {str(result)[:500]}")
                return result
            except json.JSONDecodeError:
                # Some actions return empty response on success
                logger.debug(f"AppSheet non-JSON response: {response_text[:200]}")
                return {"success": True, "raw_response": response_text}

    async def find_grid_by_name(self, grid_name: str) -> Optional[Dict[str, Any]]:
        """Find a grid by name using server-side Selector filtering."""
        try:
            escaped_name = grid_name.replace('"', '\\"')
            result = await self._make_request(
                table_name=GRIDS_TABLE,
                action="Find",
                rows=[],
                properties={
                    "Locale": "en-US",
                    "Timezone": "UTC",
                    "Selector": f'Filter({GRIDS_TABLE}, [Name]="{escaped_name}")',
                },
            )
            grids: List[Dict[str, Any]] = (
                result if isinstance(result, list) else result.get("Rows", [])
            )
            if grids:
                grid = grids[0]
                logger.info(f"Found grid '{grid_name}' with Id: {grid.get('Id')}")
                return grid
            logger.info(f"Grid '{grid_name}' not found")
            return None
        except Exception as e:
            logger.warning(f"Error finding grid '{grid_name}': {e}")
            return None

    async def create_grid(self, grid_name: str, community: Optional[str] = None) -> Dict[str, Any]:
        """Create a new grid in AppSheet"""
        row_data = {"Name": grid_name}
        if community:
            row_data["Community"] = community

        result = await self._make_request(
            table_name=GRIDS_TABLE,
            action="Add",
            rows=[row_data],
        )
        logger.info(f"Created grid: {grid_name}")
        return result

    async def create_design(self, design_input: DesignInput, grid_id: str) -> Dict[str, Any]:
        """Create a new design linked to a grid"""
        # Calculate connection distribution if not provided
        residential = design_input.initial_residential_connections
        business = design_input.initial_business_connections

        if residential is None:
            residential = int(design_input.max_connections * 0.9)
        if business is None:
            business = (
                design_input.max_connections - residential - design_input.initial_3phase_connections
            )

        row_data = {
            "Grid": grid_id,  # Reference to parent grid
            "Name": design_input.design_name,
            "Inverter Type": design_input.inverter_type,
            "Battery Type": design_input.battery_type,
            "MPPT Type": design_input.mppt_type,
            "PV Type": design_input.pv_type,
            "Max connections": design_input.max_connections,
            "Initial Residential Connections": residential,
            "Initial Business Connections": business,
            "Initial 3-phase Connections": design_input.initial_3phase_connections,
            "Number of PoC teams to install meters": design_input.num_poc_teams,
            "Anchor Load kW": design_input.anchor_load_kw,
            "Force 3-phase?": design_input.force_3phase,
            "SPD Type": design_input.spd_type,
        }

        # Add optional fields if provided
        if design_input.avg_service_drop_length_m is not None:
            row_data["Average Service Drop Length (m)"] = round(
                design_input.avg_service_drop_length_m, 1
            )
        if design_input.pv_inverter_type:
            row_data["PV Inverter Type"] = design_input.pv_inverter_type
        if design_input.target_kwp is not None:
            row_data["Target kWp"] = design_input.target_kwp
        if design_input.target_kwh is not None:
            row_data["Target kWh"] = design_input.target_kwh

        result = await self._make_request(
            table_name=DESIGNS_TABLE,
            action="Add",
            rows=[row_data],
        )
        logger.info(f"Created design: {design_input.design_name}")
        return result

    # Columns permitted for update_design. Prevents prompt injection from
    # overwriting arbitrary design fields via the MCP tool.
    ALLOWED_UPDATE_COLUMNS = {
        "Avg Distance to PV Combiner (m)",
        "Distance to Feeder Pillar (m)",
        "Average Service Drop Length (m)",
    }

    async def update_design(self, design_id: str, updates: Dict[str, Any]) -> Dict[str, Any]:
        """Update fields on an existing design row in AppSheet.

        Only columns in ALLOWED_UPDATE_COLUMNS can be modified.

        Args:
            design_id: The UNIQUEID of the design
            updates: Dict of AppSheet column names to new values

        Returns:
            The updated row data

        Raises:
            ValueError: If updates contain disallowed column names
        """
        disallowed = set(updates.keys()) - self.ALLOWED_UPDATE_COLUMNS
        if disallowed:
            raise ValueError(f"Cannot update columns: {disallowed}")

        row_data = {"Id": design_id, **updates}
        result = await self._make_request(
            table_name=DESIGNS_TABLE,
            action="Edit",
            rows=[row_data],
        )
        logger.info(f"Updated design {design_id}: {list(updates.keys())}")
        return result

    async def trigger_bom_generation(self, design_id: str) -> Dict[str, Any]:
        """
        Trigger BOM generation by adding a row to the AAL (Actions) table.

        Args:
            design_id: The UNIQUEID of the design from AppSheet

        Returns:
            The created action row
        """
        row_data = {
            "Action": ACTION_CREATE_BOM,
            "Reference": design_id,
            "Status": ACTION_STATUS_REQUESTED,
        }

        result = await self._make_request(
            table_name=ACTIONS_TABLE,
            action="Add",
            rows=[row_data],
        )
        logger.info(f"Triggered BOM generation for design: {design_id}")
        return result

    async def get_action_status(
        self, design_id: str, action_type: str = ACTION_CREATE_BOM
    ) -> Optional[Dict[str, Any]]:
        """
        Get the status of an action for a design.

        NOTE: When a design has been processed multiple times there can be
        several rows matching the same (Reference, Action) pair. This returns
        an arbitrary match, so prefer get_action_by_id() when the specific
        triggered row Id is known.

        Args:
            design_id: The design UNIQUEID
            action_type: The action type to check (default: Create BOM)

        Returns:
            The action row if found, None otherwise
        """
        try:
            result = await self._make_request(
                table_name=ACTIONS_TABLE,
                action="Find",
                rows=[],
                properties={
                    "Locale": "en-US",
                    "Timezone": "UTC",
                    "Selector": (
                        f"Filter({ACTIONS_TABLE}, "
                        f'AND([Reference]="{design_id}", [Action]="{action_type}"))'
                    ),
                },
            )
            actions: List[Dict[str, Any]] = (
                result if isinstance(result, list) else result.get("Rows", [])
            )
            return actions[0] if actions else None
        except Exception as e:
            logger.warning(f"Error fetching action status for '{design_id}': {e}")
            return None

    async def get_action_by_id(self, action_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a single action row by its UNIQUEID.

        Unlike get_action_status(), this targets the exact row that was just
        created, so stale BOM-action rows from earlier runs of the same design
        cannot satisfy or stall the completion poll.

        Args:
            action_id: The action row UNIQUEID returned when the row was added

        Returns:
            The action row if found, None otherwise
        """
        try:
            result = await self._make_request(
                table_name=ACTIONS_TABLE,
                action="Find",
                rows=[],
                properties={
                    "Locale": "en-US",
                    "Timezone": "UTC",
                    "Selector": f'Filter({ACTIONS_TABLE}, [Id]="{action_id}")',
                },
            )
            actions: List[Dict[str, Any]] = (
                result if isinstance(result, list) else result.get("Rows", [])
            )
            return actions[0] if actions else None
        except Exception as e:
            logger.warning(f"Error fetching action by id '{action_id}': {e}")
            return None

    async def get_design(self, design_id: str) -> Optional[Dict[str, Any]]:
        """Get a design by ID (using the UNIQUEID 'Id' field)."""
        try:
            result = await self._make_request(
                table_name=DESIGNS_TABLE,
                action="Find",
                rows=[],
                properties={
                    "Locale": "en-US",
                    "Timezone": "UTC",
                    "Selector": f'Filter({DESIGNS_TABLE}, [Id]="{design_id}")',
                },
            )
            designs: List[Dict[str, Any]] = (
                result if isinstance(result, list) else result.get("Rows", [])
            )
            return designs[0] if designs else None
        except Exception as e:
            logger.warning(f"Error fetching design '{design_id}': {e}")
            return None

    async def get_bom_for_design(self, design_id: str) -> List[Dict[str, Any]]:
        """
        Get active BOM items for a design.

        Args:
            design_id: The design UNIQUEID

        Returns:
            List of BOM items where Active=TRUE
        """
        try:
            result = await self._make_request(
                table_name=BOM_TABLE,
                action="Find",
                rows=[],
                properties={
                    "Locale": "en-US",
                    "Timezone": "UTC",
                    "Selector": (f'Filter(BOM Items, AND([Design]="{design_id}", [Active]="Y"))'),
                },
            )
            active_items: List[Dict[str, Any]] = (
                result if isinstance(result, list) else result.get("Rows", [])
            )
            logger.info(f"Found {len(active_items)} active BOM items for design {design_id}")
            return active_items
        except Exception as e:
            logger.warning(f"Error fetching BOM for design '{design_id}': {e}")
            return []


def _parse_money(value: Any) -> float:
    """Parse a cost/qty value, tolerating currency formatting and None."""
    raw = str(value if value is not None else "").replace(",", "").replace("$", "").strip()
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _get_chat_db_client() -> Client:
    """Supabase client for the Chat DB (holds gd_components). Mirrors meta_server."""
    if not CHAT_DB_URL or not CHAT_DB_SERVICE_KEY:
        raise ValueError(
            "Chat DB credentials not configured for component cost lookup. Set "
            "CHAT_DB_URL and CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/SUPABASE_KEY)."
        )
    return create_client(CHAT_DB_URL, CHAT_DB_SERVICE_KEY)


async def get_component_costs() -> Dict[str, Dict[str, float]]:
    """Return ``{component_id: {"projected_cost", "ddp_cost"}}`` from the Chat DB.

    ``gd_components.projected_cost`` is the deterministic projection anansi_app
    writes from the purchase ledger (see ``cost_projection.recompute_component_costs``).
    We read it here instead of AppSheet's virtual ``Projected Cost`` column, which
    the REST API returns unreliably (populated one moment, 0 the next). The
    synchronous Supabase query runs in a thread to avoid blocking the event loop.
    """

    def _query() -> Dict[str, Dict[str, float]]:
        client = _get_chat_db_client()
        rows = (
            client.table(GD_COMPONENTS_DB_TABLE)
            .select("id,projected_cost,ddp_cost")
            .eq("active", True)
            .execute()
            .data
        ) or []
        return {
            str(r["id"]): {
                "projected_cost": _parse_money(r.get("projected_cost")),
                "ddp_cost": _parse_money(r.get("ddp_cost")),
            }
            for r in rows
            if r.get("id")
        }

    try:
        costs = await asyncio.to_thread(_query)
        logger.info(f"Loaded {len(costs)} component costs from {GD_COMPONENTS_DB_TABLE}")
        return costs
    except Exception as e:
        logger.warning(f"Error loading component costs from Chat DB: {e}")
        return {}


def enrich_bom_projected_cost(
    bom_items: List[Dict[str, Any]],
    component_costs: Dict[str, Dict[str, float]],
) -> List[Dict[str, Any]]:
    """Set a reliable ``Projected Cost with contingency`` on each BOM row in place.

    AppSheet's ``Projected Cost with contingency`` is a virtual column derived from
    the (also virtual) ``Projected Cost`` on the Components table, and the REST API
    returns it as 0/blank unpredictably — which is why LPP BOMs could show $0 costs
    while the app UI showed real numbers. We recompute each line from the
    deterministic Chat DB projection instead:

        line cost = (Qty With Contingency, fallback Qty) × gd_components.projected_cost

    Fallbacks when the DB projected cost is unavailable: keep any non-zero value
    AppSheet already returned on the row, else fall back to the row's
    ``DDP Cost with contingency`` (physical-backed, reliably populated over the
    API). ``Tools`` rows are left untouched (they carry no cost). Mirrors the
    formula in ``anansi_app/grid_app/entities/virtual.py`` (``_proj``/``_ddp``).
    """
    for item in bom_items:
        component_type = str(item.get("Component Type", "")).strip().lower()
        if "tools" in component_type:
            continue

        qty = _parse_money(item.get("Qty With Contingency")) or _parse_money(item.get("Qty"))
        costs = component_costs.get(str(item.get("Item")), {})
        computed = round(qty * costs.get("projected_cost", 0.0), 2)

        if computed > 0:
            item["Projected Cost with contingency"] = computed
        elif _parse_money(item.get("Projected Cost with contingency")) > 0:
            # AppSheet already returned a usable computed value — leave it.
            pass
        else:
            # Last resort: physical-backed DDP cost so the BOM is never all-zero.
            item["Projected Cost with contingency"] = _parse_money(
                item.get("DDP Cost with contingency")
            )
    return bom_items


# compute_bom_cost_summary lives in internal_engine (imported above) — it is
# shared by both backends since rows carry the same AppSheet-style keys.


def require_appsheet_id(row: Dict[str, Any], entity: str) -> str:
    """Extract the AppSheet-assigned UNIQUEID from a returned row.

    AppSheet owns Id generation (the key column's UNIQUEID() initial value);
    Anansi never sets it. We deliberately do NOT fall back to ``_RowNumber``:
    it is a positional index, not a stable unique key, so persisting it would
    let later updates/polls target the wrong row (and rows can share a
    _RowNumber view across syncs). A missing Id usually means the change has
    not yet propagated to the underlying sheet — fail loudly so the caller can
    retry instead of continuing with a bad reference.

    Args:
        row: A row dict returned by an AppSheet Add/Find call
        entity: Human-readable entity name for the error message (e.g. "design")

    Returns:
        The AppSheet UNIQUEID as a string

    Raises:
        Exception: If the row has no truthy "Id" field
    """
    row_id = row.get("Id")
    if not row_id:
        raise Exception(
            f"AppSheet did not return an Id for the {entity}. The change may not "
            "have synced to the underlying sheet yet — please retry shortly."
        )
    return str(row_id)


# Global client instance
appsheet_client: Optional[AppSheetClient] = None


def get_client() -> AppSheetClient:
    """Get or create the AppSheet client"""
    global appsheet_client
    if appsheet_client is None:
        if not GRID_DESIGN_APP_ID or not GRID_DESIGN_APP_KEY:
            raise ValueError("GRID_DESIGN_APP_ID and GRID_DESIGN_APP_KEY must be configured")
        appsheet_client = AppSheetClient(GRID_DESIGN_APP_ID, GRID_DESIGN_APP_KEY, APPSHEET_REGION)
    return appsheet_client


async def design_and_bom_workflow(
    grid_name: str,
    design_name: str,
    max_connections: int,
    community: Optional[str] = None,
    inverter_type: str = "Quattro 15kVA",
    battery_type: str = "Pylontech UP5000",
    mppt_type: str = "Victron 250/85 MPPT",
    pv_type: str = "JA455W Panel",
    pv_inverter_type: Optional[str] = None,
    initial_residential_connections: Optional[int] = None,
    initial_business_connections: Optional[int] = None,
    initial_3phase_connections: int = 0,
    num_poc_teams: int = 1,
    anchor_load_kw: float = 0,
    force_3phase: bool = False,
    target_kwp: Optional[float] = None,
    target_kwh: Optional[float] = None,
    avg_service_drop_length_m: Optional[float] = None,
    spd_type: Optional[str] = None,
    wait_for_completion: bool = True,
    wait_for_bom: bool = True,
) -> Dict[str, Any]:
    """
    Execute the full design and BOM generation workflow:
    1. Create grid if it doesn't exist
    2. Create a new design
    3. Run design generation action
    4. Run BOM generation action
    5. Wait for completion and fetch results
    """
    client = get_client()
    # Use typed list to avoid mypy errors with .append()
    steps: List[Dict[str, Any]] = []
    workflow_result: Dict[str, Any] = {
        "steps": steps,
        "grid": None,
        "design": None,
        "bom": [],
        "success": False,
    }

    try:
        # Step 1: Check if grid exists, create if not
        logger.info(f"Step 1: Checking for grid '{grid_name}'")
        existing_grid = await client.find_grid_by_name(grid_name)

        if existing_grid:
            grid_id = existing_grid.get("Id") or existing_grid.get("Grid ID")
            steps.append({"step": "find_grid", "status": "found", "grid_id": grid_id})
            workflow_result["grid"] = existing_grid
            logger.info(f"Found existing grid: {grid_id}")
        else:
            create_result = await client.create_grid(grid_name, community)
            rows = create_result.get("Rows", [])
            if rows:
                # AppSheet returns the UNIQUEID in the "Id" field
                grid_id = require_appsheet_id(rows[0], "grid")
                workflow_result["grid"] = rows[0]
            else:
                grid_id = grid_name  # Fallback to name as reference
            steps.append({"step": "create_grid", "status": "created", "grid_id": grid_id})
            logger.info(f"Created new grid: {grid_id}")

            # Wait for AppSheet to process the new grid before creating design
            logger.info("Waiting 5s for grid to be processed...")
            await asyncio.sleep(5)

        # Step 2: Create design
        logger.info(f"Step 2: Creating design '{design_name}'")
        design_input = DesignInput(
            grid_name=grid_name,
            design_name=design_name,
            max_connections=max_connections,
            inverter_type=inverter_type,
            battery_type=battery_type,
            mppt_type=mppt_type,
            pv_type=pv_type,
            pv_inverter_type=pv_inverter_type,
            initial_residential_connections=initial_residential_connections,
            initial_business_connections=initial_business_connections,
            initial_3phase_connections=initial_3phase_connections,
            num_poc_teams=num_poc_teams,
            anchor_load_kw=anchor_load_kw,
            force_3phase=force_3phase,
            target_kwp=target_kwp,
            target_kwh=target_kwh,
            avg_service_drop_length_m=avg_service_drop_length_m,
            **({"spd_type": spd_type} if spd_type else {}),
        )

        design_result = await client.create_design(design_input, grid_id)
        rows = design_result.get("Rows", [])
        if rows:
            # AppSheet returns the UNIQUEID in the "Id" field
            design_id = require_appsheet_id(rows[0], "design")
            workflow_result["design"] = rows[0]
        else:
            raise Exception("Failed to create design - no rows returned")
        steps.append({"step": "create_design", "status": "created", "design_id": design_id})
        logger.info(f"Created design with Id: {design_id}")

        # Step 3: Wait for design to be auto-populated by AppSheet
        logger.info(
            f"Step 3: Waiting {DESIGN_AUTOPOPULATE_WAIT_SECONDS}s for design auto-population..."
        )
        await asyncio.sleep(DESIGN_AUTOPOPULATE_WAIT_SECONDS)
        steps.append(
            {
                "step": "wait_for_autopopulate",
                "status": "completed",
                "wait_seconds": DESIGN_AUTOPOPULATE_WAIT_SECONDS,
            }
        )

        # Fetch the auto-populated design
        design = await client.get_design(design_id)
        if design:
            workflow_result["design"] = design
            logger.info("Design auto-populated successfully")

        # Extract energy specs from design (available after auto-population,
        # regardless of whether BOM is generated)
        design_data = workflow_result.get("design", {})
        energy_specs = {
            "total_kwp": design_data.get("Total kWp", design_data.get("kWp", "")),
            "total_kwh": design_data.get("Total kWh", design_data.get("kWh", "")),
            "total_kva": design_data.get("Total kVA", design_data.get("kVA", "")),
            "num_subsystems": design_data.get("Number of Subsystems", ""),
            "num_inverters": design_data.get("Number of Inverters", ""),
            "num_batteries": design_data.get("Number of Batteries", ""),
            "num_panels": design_data.get("Number of Panels", ""),
        }
        logger.info(f"Energy specs from design: {energy_specs}")
        workflow_result["energy_specs"] = energy_specs
        workflow_result["output"] = {
            "energy_specs": energy_specs,
            "design_id": design_id,
            "design_name": design_data.get("Name", ""),
            "grid_name": grid_name,
            "design_parameters": {
                "inverter_type": design_data.get("Inverter Type", ""),
                "battery_type": design_data.get("Battery Type", ""),
                "mppt_type": design_data.get("MPPT Type", ""),
                "pv_type": design_data.get("PV Type", ""),
                "pv_inverter_type": design_data.get("PV Inverter Type", ""),
                "max_connections": design_data.get("Max connections", ""),
                "residential_connections": design_data.get("Initial Residential Connections", ""),
                "business_connections": design_data.get("Initial Business Connections", ""),
                "three_phase_connections": design_data.get("Initial 3-phase Connections", ""),
                "wp_per_connection": design_data.get("Wp per conn override?", ""),
                "pv_area_m2": design_data.get("PV Area (m2)", ""),
                "regulation_constraint": design_data.get("Regulation constraint on design", ""),
                "force_3phase": design_data.get("Force 3-phase?", ""),
                "anchor_load_kw": design_data.get("Anchor Load kW", ""),
                "number_of_subsystems": design_data.get("Number of Subsystems", ""),
                "subsystem_size_kva": design_data.get("Subsystem Size (kVA)", ""),
            },
        }

        # Step 4: Trigger BOM generation by adding row to AAL table
        if wait_for_bom:
            logger.info(f"Step 4: Triggering BOM generation for design '{design_id}'")
            try:
                action_result = await client.trigger_bom_generation(design_id)
                action_rows = action_result.get("Rows", [])
                action_id = action_rows[0].get("Id") if action_rows else None
                steps.append(
                    {
                        "step": "trigger_bom",
                        "status": "triggered",
                        "action_id": action_id,
                    }
                )
                logger.info(f"BOM generation triggered, action Id: {action_id}")
            except Exception as e:
                logger.error(f"Failed to trigger BOM generation: {e}")
                steps.append({"step": "trigger_bom", "status": "failed", "reason": str(e)})
                raise

            # Step 5: Wait for BOM generation to complete
            if wait_for_completion:
                logger.info(f"Step 5: Waiting {BOM_GENERATION_WAIT_SECONDS}s for BOM generation...")
                await asyncio.sleep(BOM_GENERATION_WAIT_SECONDS)
                steps.append(
                    {
                        "step": "wait_for_bom",
                        "status": "completed",
                        "wait_seconds": BOM_GENERATION_WAIT_SECONDS,
                    }
                )

                # Fetch the final design state
                design = await client.get_design(design_id)
                if design:
                    workflow_result["design"] = design

                # Fetch active BOM items
                bom_items = await client.get_bom_for_design(design_id)
                # Recompute projected costs from the Chat DB — the BOM's virtual
                # "Projected Cost with contingency" is unreliable over the AppSheet API.
                if bom_items:
                    enrich_bom_projected_cost(bom_items, await get_component_costs())
                workflow_result["bom"] = bom_items
                steps.append(
                    {
                        "step": "fetch_bom",
                        "status": "completed",
                        "active_bom_count": len(bom_items),
                    }
                )

                # Compute cost summary by component type groups
                cost_summary = compute_bom_cost_summary(bom_items)
                workflow_result["cost_summary"] = cost_summary

                # Refresh energy_specs from post-BOM design (may be more complete)
                design_data = workflow_result.get("design", {})
                energy_specs = {
                    "total_kwp": design_data.get("Total kWp", design_data.get("kWp", "")),
                    "total_kwh": design_data.get("Total kWh", design_data.get("kWh", "")),
                    "total_kva": design_data.get("Total kVA", design_data.get("kVA", "")),
                    "num_subsystems": design_data.get("Number of Subsystems", ""),
                    "num_inverters": design_data.get("Number of Inverters", ""),
                    "num_batteries": design_data.get("Number of Batteries", ""),
                    "num_panels": design_data.get("Number of Panels", ""),
                }
                logger.info(f"Energy specs refreshed after BOM: {energy_specs}")
                workflow_result["energy_specs"] = energy_specs
                workflow_result["output"]["energy_specs"] = energy_specs
                workflow_result["output"]["cost_summary"] = cost_summary
        else:
            steps.append({"step": "skip_bom", "status": "skipped", "reason": "wait_for_bom=False"})

        workflow_result["success"] = True
        return workflow_result

    except Exception as e:
        logger.error(f"Workflow error: {e}")
        workflow_result["error"] = str(e)
        steps.append({"step": "error", "message": str(e)})
        return workflow_result


@registry.pre_dispatch
async def _check_gate_and_appsheet_config(
    name: str, arguments: Dict[str, Any]
) -> Optional[List[types.TextContent]]:
    """Whole-server disabled gate, plus (for any tool but the two
    backend-independent ones) the legacy AppSheet backend's configured-check.
    Preserved verbatim as a pre_dispatch hook; per-tool backend routing still
    happens in each tool's own handler below.
    """
    if not GRID_DESIGN_ACTIONS_ENABLED:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": "Grid Design actions are disabled. Enable them in settings.",
                    }
                ),
            )
        ]

    # The design-options catalogue lives in the Chat DB regardless of backend.
    if name in ("list_design_options", "list_design_technology_families"):
        return None

    if GRID_DESIGN_BACKEND == "internal":
        return None

    # ── Legacy AppSheet backend (GRID_DESIGN_BACKEND=appsheet) ──
    # SECURITY NOTE: this rollback path does NOT enforce grid-level
    # authorization (gd_auth.assert_grid_access) the way the internal
    # backend does above. Switching to appsheet reverts to pre-Phase-A
    # behavior where any caller can read/write any grid's design. Only
    # use this for emergency rollback, and be aware the access-control
    # model is backend-dependent until this path gets its own auth gate.
    if not GRID_DESIGN_APP_ID or not GRID_DESIGN_APP_KEY:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": "Grid Design not configured. Set GRID_DESIGN_APP_ID and GRID_DESIGN_APP_KEY.",
                    }
                ),
            )
        ]
    return None


# Optional design_and_bom arguments forwarded verbatim to the internal engine
# when supplied (design_writer applies the AppSheet form defaults to the rest).
_INTERNAL_PASSTHROUGH_KEYS = (
    "community",
    "technology_family",
    "pv_inverter_type",
    "initial_residential_connections",
    "initial_business_connections",
    "initial_3phase_connections",
    "num_poc_teams",
    "anchor_load_kw",
    "force_3phase",
    "target_kwp",
    "target_kwh",
    "avg_service_drop_length_m",
    "wp_per_conn_override",
    "regulation_constraint",
    "pue_hours_per_day",
    "daily_generation_potential_kwh_kwp",
    "target_tariff_usd",
    "max_distance_to_center_of_consumption_m",
    "avg_distance_to_pv_combiner_m",
    "distance_to_feeder_pillar_m",
    "spd_type",
    "created_by",
)

# New-form parameters the legacy AppSheet backend cannot map (logged + ignored
# there; spd_type IS supported by the AppSheet path).
_APPSHEET_UNSUPPORTED_KEYS = (
    "wp_per_conn_override",
    "regulation_constraint",
    "pue_hours_per_day",
    "daily_generation_potential_kwh_kwp",
    "target_tariff_usd",
    "max_distance_to_center_of_consumption_m",
    "avg_distance_to_pv_combiner_m",
    "distance_to_feeder_pillar_m",
    "auto_design",
    "technology_family",
    "created_by",
)


async def _require_grid_access_for_design(design_id: str, organization_id: Optional[int]) -> None:
    """Resolve a design's grid and enforce grid-level access before any read/write.

    Deliberately raises the SAME generic message whether the design doesn't
    exist or exists but belongs to a grid the caller can't access — revealing
    neither fact avoids leaking another organization's grid name or turning
    this into an existence oracle for design_ids the caller doesn't own.
    """
    denial = ValueError(f"You don't have access to design {design_id}, or it doesn't exist.")
    grid_name = await asyncio.to_thread(gd_auth.resolve_grid_name_for_design, design_id)
    if grid_name is None:
        raise denial from None
    try:
        await gd_auth.assert_grid_access(grid_name, organization_id)
    except gd_auth.GridAccessDenied:
        raise denial from None


async def _require_grid_access_for_subassembly_row(
    row_id: str, organization_id: Optional[int]
) -> str:
    """Resolve a design_subassembly row's design/grid and enforce access.

    Returns the design_id since some callers (none currently, but kept for
    symmetry/future use) may need it after the check.

    Same generic-denial reasoning as `_require_grid_access_for_design` applies
    here: a missing row_id and a row_id that resolves to a design/grid the
    caller can't access must be indistinguishable to the caller. So this
    function's OWN "row not found" path AND the delegated design-level check's
    failure (which would otherwise surface a *different*-worded, design_id-
    bearing message) are both collapsed into this single row-scoped wording —
    otherwise the two differently-worded denials would themselves become the
    oracle this fix is meant to close.
    """
    denial = ValueError(f"You don't have access to row {row_id}, or it doesn't exist.")
    design_id: Optional[str] = await internal_engine.get_design_id_for_subassembly_row(row_id)
    if design_id is None:
        raise denial from None
    try:
        await _require_grid_access_for_design(design_id, organization_id)
    except ValueError:
        raise denial from None
    return design_id


async def _require_staff_org(organization_id: Optional[int], action_label: str) -> None:
    """Catalogue-level edits (subassembly templates, not per-design instances) are
    staff-only: a change here affects EVERY design across every organization that
    references this subassembly/component template on its next auto_design/BOM regen.
    """
    if organization_id != STAFF_ORG_ID:
        raise gd_auth.GridAccessDenied(f"You don't have access to {action_label}.")


def _parse_updates(raw: Any) -> Dict[str, Any]:
    """updates arrives as a JSON object string (Gemini-safe schema); accept
    dicts too for backward compatibility with older callers."""
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("updates must be a JSON object")
        return parsed
    return dict(raw or {})


def _internal_design_args(arguments: Dict[str, Any]) -> Dict[str, Any]:
    """Build the internal-engine design_and_bom args from tool arguments."""
    technology_family = arguments.get("technology_family")
    args: Dict[str, Any] = {
        "grid_name": arguments["grid_name"],
        "design_name": arguments["design_name"],
        "max_connections": arguments["max_connections"],
        "auto_design": arguments.get("auto_design", True),
        "wait_for_bom": arguments.get("wait_for_bom", True),
    }

    equipment_defaults = {
        "inverter_type": "Quattro 15kVA",
        "battery_type": "Pylontech UP5000",
        "mppt_type": "Victron 250/85 MPPT",
        "pv_type": "JA455W Panel",
    }
    for key, default in equipment_defaults.items():
        if key in arguments:
            args[key] = arguments[key]
        elif not technology_family:
            args[key] = default

    for key in _INTERNAL_PASSTHROUGH_KEYS:
        if arguments.get(key) is not None:
            args[key] = arguments[key]
    return args


async def _handle_internal_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Route a tool call to the internal (Chat DB) engine backend.

    Every branch enforces grid-level (or staff-only, for catalogue-level
    tools) access BEFORE calling into internal_engine. organization_id is
    injected into `arguments` non-LLM-controllably by tool_executor.py — it
    is not part of any tool's inputSchema.
    """
    organization_id = arguments.get("organization_id")

    if name == "design_and_bom":
        await gd_auth.assert_grid_access(arguments["grid_name"], organization_id)
        result = await internal_engine.design_and_bom(_internal_design_args(arguments))
    elif name == "find_grid":
        await gd_auth.assert_grid_access(arguments["grid_name"], organization_id)
        result = await internal_engine.find_grid(arguments["grid_name"])
    elif name == "create_design":
        await gd_auth.assert_grid_access(arguments["grid_name"], organization_id)
        params = _parse_updates(arguments.get("params", "{}"))
        args = {
            "grid_name": arguments["grid_name"],
            "design_name": arguments["design_name"],
            **params,
            "auto_design": arguments.get("run_auto_design", False),
            "wait_for_bom": arguments.get("generate_bom", False),
        }
        result = await internal_engine.design_and_bom(args)
    elif name == "get_design_bom":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.get_design_bom(arguments["design_id"])
    elif name == "update_design":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.update_design(
            arguments["design_id"],
            _parse_updates(arguments["updates"]),
            rerun_auto_design=arguments.get("rerun_auto_design", False),
            regenerate_bom=arguments.get("regenerate_bom", False),
            force=arguments.get("force", False),
        )
    elif name == "trigger_bom":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.trigger_bom(
            arguments["design_id"], arguments.get("grid_name", "")
        )
    elif name == "get_design":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.get_design(arguments["design_id"])
    elif name == "list_design_artifacts":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.list_design_artifacts(arguments["design_id"])
    elif name == "get_design_artifact":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.get_design_artifact(
            arguments["design_id"], arguments["artifact_type"], arguments.get("version", 0)
        )
    elif name == "run_auto_design":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        param_overrides = _parse_updates(arguments.get("param_overrides", "{}"))
        result = await internal_engine.run_auto_design(
            arguments["design_id"], param_overrides, arguments.get("force", False)
        )
    elif name == "change_design_technology":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.change_design_technology(
            arguments["design_id"],
            arguments["technology_family"],
            rerun_auto_design=arguments.get("rerun_auto_design", True),
            regenerate_bom=arguments.get("regenerate_bom", True),
            force=arguments.get("force", False),
        )
    elif name == "duplicate_design":
        await _require_grid_access_for_design(arguments["source_design_id"], organization_id)
        param_overrides = _parse_updates(arguments.get("param_overrides", "{}"))
        result = await internal_engine.duplicate_design(
            arguments["source_design_id"],
            arguments["new_design_name"],
            param_overrides,
            arguments.get("run_auto_design", True),
            arguments.get("generate_bom", True),
        )
    elif name == "list_design_subassemblies":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.list_design_subassemblies(arguments["design_id"])
    elif name == "add_subassembly":
        await _require_grid_access_for_design(arguments["design_id"], organization_id)
        result = await internal_engine.add_subassembly(
            arguments["design_id"], arguments["subassembly_name"], arguments["qty"]
        )
    elif name == "remove_subassembly":
        await _require_grid_access_for_subassembly_row(
            arguments["design_subassembly_row_id"], organization_id
        )
        result = await internal_engine.remove_subassembly(arguments["design_subassembly_row_id"])
    elif name == "set_subassembly_qty":
        await _require_grid_access_for_subassembly_row(
            arguments["design_subassembly_row_id"], organization_id
        )
        result = await internal_engine.set_subassembly_qty(
            arguments["design_subassembly_row_id"], arguments["qty"]
        )
    elif name == "list_subassembly_components":
        await _require_staff_org(organization_id, "subassembly catalogue composition")
        result = await internal_engine.list_subassembly_components(arguments["subassembly_id"])
    elif name == "add_subassembly_component":
        await _require_staff_org(organization_id, "subassembly catalogue composition")
        result = await internal_engine.add_subassembly_component(
            arguments["subassembly_id"],
            arguments.get("component_name"),
            arguments.get("child_subassembly_name"),
            arguments.get("qty", 1),
            arguments.get("unit"),
        )
    elif name == "remove_subassembly_component":
        await _require_staff_org(organization_id, "subassembly catalogue composition")
        result = await internal_engine.remove_subassembly_component(arguments["row_id"])
    elif name == "set_subassembly_component_qty":
        await _require_staff_org(organization_id, "subassembly catalogue composition")
        result = await internal_engine.set_subassembly_component_qty(
            arguments["row_id"], arguments["qty"]
        )
    elif name == "duplicate_subassembly":
        await _require_staff_org(organization_id, "subassembly catalogue cloning")
        result = await internal_engine.duplicate_subassembly(
            arguments["source_subassembly_id"], arguments["new_description"]
        )
    elif name == "gd_describe_tables":
        result = await gd_crud.gd_describe_tables()
    elif name == "gd_list_rows":
        filters = _parse_updates(arguments.get("filters", "{}"))
        grid_name = arguments.get("grid_name")
        # Precedence: the dedicated `grid_name` argument wins over a
        # `grid_name` the model may have embedded inside the `filters` JSON
        # instead — it's a typed, purpose-built field, so treat any
        # JSON-embedded value as a fallback rather than letting it silently
        # override an explicit top-level arg.
        if grid_name:
            filters["grid_name"] = grid_name
        result = await gd_crud.gd_list_rows(
            arguments["table"],
            organization_id,
            filters=filters,
            limit=arguments.get("limit", 50),
            include_inactive=arguments.get("include_inactive", False),
        )
    elif name == "gd_get_row":
        result = await gd_crud.gd_get_row(arguments["table"], arguments["row_id"], organization_id)
    elif name == "gd_upsert_row":
        user_email = arguments.get("user_email")
        values = _parse_updates(arguments["values"])
        result = await gd_crud.gd_upsert_row(
            arguments["table"],
            organization_id,
            user_email,
            row_id=arguments.get("row_id"),
            values=values,
        )
    elif name == "gd_delete_row":
        result = await gd_crud.gd_delete_row(
            arguments["table"], arguments["row_id"], organization_id
        )
    else:
        result = {"success": False, "error": f"Unknown tool: {name}"}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _json_envelope_errors(fn):
    """Wrap a tool handler so exceptions become the same
    ``{"success": False, "error": ...}`` JSON envelope the original
    ``handle_call_tool``'s one shared ``except Exception`` block produced —
    grid-access denials in particular (``gd_auth.GridAccessDenied`` and
    friends, raised from ``_handle_internal_tool``'s access checks) are
    expected as JSON by existing callers and tests. The registry's own
    generic catch-all instead returns plain "Error: ..." text
    (``compose_error_response``), which doesn't match.
    """

    async def _wrapped(arguments: Dict[str, Any]) -> List[types.TextContent]:
        try:
            return await fn(arguments)
        except Exception as e:
            logger.error(f"Error in tool {fn.__name__}: {e}")
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": str(e)}),
                )
            ]

    return _wrapped


@registry.tool(
    "design_and_bom", _SCHEMAS_BY_NAME["design_and_bom"], gated=True, refuse_when_disabled=False
)
@_json_envelope_errors
async def _tool_design_and_bom(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if GRID_DESIGN_BACKEND == "internal":
        return await _handle_internal_tool("design_and_bom", arguments)

    ignored = [k for k in _APPSHEET_UNSUPPORTED_KEYS if arguments.get(k) is not None]
    if ignored:
        logger.warning(f"AppSheet backend ignores unsupported design parameters: {ignored}")
    result = await design_and_bom_workflow(
        grid_name=arguments["grid_name"],
        design_name=arguments["design_name"],
        max_connections=arguments["max_connections"],
        community=arguments.get("community"),
        inverter_type=arguments.get("inverter_type", "Quattro 15kVA"),
        battery_type=arguments.get("battery_type", "Pylontech UP5000"),
        mppt_type=arguments.get("mppt_type", "Victron 250/85 MPPT"),
        pv_type=arguments.get("pv_type", "JA455W Panel"),
        pv_inverter_type=arguments.get("pv_inverter_type"),
        initial_residential_connections=arguments.get("initial_residential_connections"),
        initial_business_connections=arguments.get("initial_business_connections"),
        initial_3phase_connections=arguments.get("initial_3phase_connections", 0),
        num_poc_teams=arguments.get("num_poc_teams", 1),
        anchor_load_kw=arguments.get("anchor_load_kw", 0),
        force_3phase=arguments.get("force_3phase", False),
        target_kwp=arguments.get("target_kwp"),
        target_kwh=arguments.get("target_kwh"),
        avg_service_drop_length_m=arguments.get("avg_service_drop_length_m"),
        spd_type=arguments.get("spd_type"),
        wait_for_completion=arguments.get("wait_for_completion", True),
        wait_for_bom=arguments.get("wait_for_bom", True),
    )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


@registry.tool("find_grid", _SCHEMAS_BY_NAME["find_grid"], gated=True, refuse_when_disabled=False)
@_json_envelope_errors
async def _tool_find_grid(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if GRID_DESIGN_BACKEND == "internal":
        return await _handle_internal_tool("find_grid", arguments)

    client = get_client()
    grid = await client.find_grid_by_name(arguments["grid_name"])
    result = (
        {"success": True, "grid": grid} if grid else {"success": False, "error": "Grid not found"}
    )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


@registry.tool(
    "get_design_bom", _SCHEMAS_BY_NAME["get_design_bom"], gated=True, refuse_when_disabled=False
)
@_json_envelope_errors
async def _tool_get_design_bom(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if GRID_DESIGN_BACKEND == "internal":
        return await _handle_internal_tool("get_design_bom", arguments)

    client = get_client()
    bom = await client.get_bom_for_design(arguments["design_id"])
    # Recompute projected costs from the Chat DB — the BOM's virtual
    # "Projected Cost with contingency" is unreliable over the AppSheet API.
    if bom:
        enrich_bom_projected_cost(bom, await get_component_costs())
    result = {"success": True, "bom_items": bom, "count": len(bom)}
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


@registry.tool(
    "update_design", _SCHEMAS_BY_NAME["update_design"], gated=True, refuse_when_disabled=False
)
@_json_envelope_errors
async def _tool_update_design(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if GRID_DESIGN_BACKEND == "internal":
        return await _handle_internal_tool("update_design", arguments)

    client = get_client()
    result = await client.update_design(arguments["design_id"], _parse_updates(arguments["updates"]))
    return [
        types.TextContent(
            type="text",
            text=json.dumps({"success": True, "updated": result}, indent=2, default=str),
        )
    ]


@registry.tool(
    "trigger_bom", _SCHEMAS_BY_NAME["trigger_bom"], gated=True, refuse_when_disabled=False
)
@_json_envelope_errors
async def _tool_trigger_bom(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if GRID_DESIGN_BACKEND == "internal":
        return await _handle_internal_tool("trigger_bom", arguments)

    client = get_client()
    design_id = arguments["design_id"]

    # Trigger BOM
    action_result = await client.trigger_bom_generation(design_id)
    action_rows = action_result.get("Rows", [])
    action_id = action_rows[0].get("Id") if action_rows else None
    logger.info(f"BOM triggered for {design_id}, action: {action_id}")

    # Poll for BOM completion instead of blind sleep.
    # Track the exact action row we just created (action_id) rather than
    # any "Create BOM" row for the design — otherwise stale rows from a
    # previous run can break the loop early or never report completion,
    # which is what made the LPP flow appear stuck at create-bom.
    logger.info(f"Polling for BOM completion (max {BOM_GENERATION_WAIT_SECONDS}s)...")
    poll_interval = 10
    elapsed = 0
    bom_items = []
    completed = False
    while elapsed < BOM_GENERATION_WAIT_SECONDS:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        if action_id:
            action = await client.get_action_by_id(action_id)
        else:
            # Fallback when AppSheet did not return the new row Id
            action = await client.get_action_status(design_id)
        if action and action.get("Status") == ACTION_STATUS_COMPLETED:
            logger.info(f"BOM completed after {elapsed}s")
            completed = True
            break
        logger.debug(f"BOM not yet complete ({elapsed}s elapsed)...")

    # Fetch results
    design = await client.get_design(design_id)
    bom_items = await client.get_bom_for_design(design_id)

    # Fail explicitly when the action never confirmed completion AND no
    # BOM items were produced, instead of silently returning a $0 BOM.
    # A clean error lets the workflow surface the problem and retry,
    # rather than populating the package with empty/zero values.
    if not completed and not bom_items:
        logger.error(
            f"BOM did not complete for design {design_id} within "
            f"{BOM_GENERATION_WAIT_SECONDS}s and 0 items were returned "
            f"(action_id={action_id})."
        )
        result = {
            "success": False,
            "error": (
                "BOM generation did not finish in time. AppSheet did not "
                "report the Create BOM action as completed and produced no "
                "BOM items. Please retry the package — if it keeps failing, "
                "check the AppSheet Create BOM automation for this design."
            ),
            "design_id": design_id,
            "wait_seconds": BOM_GENERATION_WAIT_SECONDS,
        }
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    if not bom_items:
        logger.warning(
            f"BOM fetch returned 0 items for design {design_id} after "
            f"{elapsed}s — AppSheet may still be generating. "
            "cost_summary will show $0 for all categories."
        )

    # Recompute projected costs from the Chat DB — the BOM's virtual
    # "Projected Cost with contingency" is unreliable over the AppSheet API.
    if bom_items:
        component_costs = await get_component_costs()
        enrich_bom_projected_cost(bom_items, component_costs)
        if not component_costs:
            logger.warning(
                f"No component costs from Chat DB for design {design_id}; BOM "
                "projected costs fell back to DDP / raw AppSheet values."
            )

    cost_summary = compute_bom_cost_summary(bom_items)
    if cost_summary.get("total_cost", 0) == 0 and bom_items:
        logger.warning(
            f"BOM cost summary is $0 for design {design_id} despite "
            f"{len(bom_items)} items — projected and DDP costs both empty "
            "over the AppSheet API."
        )

    # Extract energy specs
    design_data = design or {}
    energy_specs = {
        "total_kwp": design_data.get("Total kWp", design_data.get("kWp", "")),
        "total_kwh": design_data.get("Total kWh", design_data.get("kWh", "")),
        "total_kva": design_data.get("Total kVA", design_data.get("kVA", "")),
        "num_subsystems": design_data.get("Number of Subsystems", ""),
        "num_inverters": design_data.get("Number of Inverters", ""),
        "num_batteries": design_data.get("Number of Batteries", ""),
        "num_panels": design_data.get("Number of Panels", ""),
    }

    # Build design_parameters
    design_parameters = {
        "inverter_type": design_data.get("Inverter Type", ""),
        "battery_type": design_data.get("Battery Type", ""),
        "mppt_type": design_data.get("MPPT Type", ""),
        "pv_type": design_data.get("PV Type", ""),
        "pv_inverter_type": design_data.get("PV Inverter Type", ""),
        "max_connections": design_data.get("Max connections", ""),
        "residential_connections": design_data.get("Initial Residential Connections", ""),
        "business_connections": design_data.get("Initial Business Connections", ""),
        "three_phase_connections": design_data.get("Initial 3-phase Connections", ""),
        "number_of_subsystems": design_data.get("Number of Subsystems", ""),
        "subsystem_size_kva": design_data.get("Subsystem Size (kVA)", ""),
    }

    result = {
        "success": True,
        "design": design_data,
        "bom": bom_items,
        "cost_summary": cost_summary,
        "energy_specs": energy_specs,
        "output": {
            "design_parameters": design_parameters,
            "energy_specs": energy_specs,
            "cost_summary": cost_summary,
            "design_id": design_id,
            "design_name": design_data.get("Name", ""),
            "grid_name": arguments.get("grid_name", ""),
        },
    }
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


@registry.tool(
    "list_design_options",
    _SCHEMAS_BY_NAME["list_design_options"],
    gated=True,
    refuse_when_disabled=False,
)
@_json_envelope_errors
async def _tool_list_design_options(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await internal_engine.list_design_options()
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


@registry.tool(
    "list_design_technology_families",
    _SCHEMAS_BY_NAME["list_design_technology_families"],
    gated=True,
    refuse_when_disabled=False,
)
@_json_envelope_errors
async def _tool_list_design_technology_families(
    arguments: Dict[str, Any],
) -> List[types.TextContent]:
    result = await internal_engine.list_design_technology_families()
    return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


def _make_internal_only_handler(tool_name: str):
    """Factory for the ~21 tools the internal backend alone implements.

    Calling one of these while GRID_DESIGN_BACKEND=appsheet falls through to
    the same "Unknown tool" JSON the original appsheet branch produced for
    them (it never had a case for them either).
    """

    async def _handler(arguments: Dict[str, Any]) -> List[types.TextContent]:
        if GRID_DESIGN_BACKEND == "internal":
            return await _handle_internal_tool(tool_name, arguments)
        result = {"success": False, "error": f"Unknown tool: {tool_name}"}
        return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    _handler.__name__ = tool_name
    return _json_envelope_errors(_handler)


for _name in _INTERNAL_ONLY_TOOL_NAMES:
    registry.tool(_name, _SCHEMAS_BY_NAME[_name], gated=True, refuse_when_disabled=False)(
        _make_internal_only_handler(_name)
    )


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def main():
    """Main entry point"""
    logger.info("Starting Grid Design MCP Server...")
    logger.info(f"App ID configured: {'Yes' if GRID_DESIGN_APP_ID else 'No'}")
    logger.info(f"Actions enabled: {GRID_DESIGN_ACTIONS_ENABLED}")
    await run_stdio_server(
        server,
        name="grid-design-server",
        label="Grid Design",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(), experimental_capabilities={}
        ),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Grid Design server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Grid Design server crashed: {e}", file=sys.stderr)
        sys.exit(1)
