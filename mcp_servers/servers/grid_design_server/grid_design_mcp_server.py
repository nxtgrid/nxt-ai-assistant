#!/usr/bin/env python3
"""
Grid Design MCP Server - AppSheet Integration for Design and BOM Generation

This MCP server provides tools to:
1. Create a grid in AppSheet if it doesn't exist
2. Create a new design for a grid with specific inputs
3. Run design and BOM generation actions
4. Wait for completion and fetch results

Uses the AppSheet REST API v2.
"""

import asyncio
import json
import os
import sys
import urllib.parse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

# Load environment variables BEFORE importing shared_code
load_dotenv()

from shared_code.utils.http_client import HTTPClientMixin
from shared_code.utils.logger import setup_logger

logger = setup_logger("grid-design-server")

# Startup message
print("🚀 Grid Design MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("grid-design-server")

# Configuration from environment
GRID_DESIGN_APP_ID = os.getenv("GRID_DESIGN_APP_ID", "")
GRID_DESIGN_APP_KEY = os.getenv("GRID_DESIGN_APP_KEY", "")
APPSHEET_REGION = os.getenv("APPSHEET_REGION", "www.appsheet.com")
GRID_DESIGN_ACTIONS_ENABLED = os.getenv("GRID_DESIGN_ACTIONS_ENABLED", "true").lower() == "true"

# Table names (configurable via env vars)
GRIDS_TABLE = os.getenv("GRID_DESIGN_GRIDS_TABLE", "Grids")
DESIGNS_TABLE = os.getenv("GRID_DESIGN_DESIGNS_TABLE", "Designs")
BOM_TABLE = os.getenv("GRID_DESIGN_BOM_TABLE", "BOM Items")
ACTIONS_TABLE = os.getenv("GRID_DESIGN_ACTIONS_TABLE", "AAL")  # Actions log table

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


def compute_bom_cost_summary(bom_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute cost summary by component type groups.

    Groups:
    - Main Energy Asset: Items with Component Type containing "Main Energy Asset"
    - Metering: Items with Component Type containing "Metering"
    - BoS (Balance of System): All other items

    Excludes: Items with Component Type = "Tools"

    Returns:
        Dict with cost totals per group and item counts
    """
    # Initialize accumulators
    costs = {
        "main_energy_asset": 0.0,
        "metering": 0.0,
        "bos": 0.0,
    }
    counts = {
        "main_energy_asset": 0,
        "metering": 0,
        "bos": 0,
        "tools_excluded": 0,
    }

    for item in bom_items:
        component_type = str(item.get("Component Type", "")).strip()

        # Skip Tools
        if "tools" in component_type.lower():
            counts["tools_excluded"] += 1
            continue

        # Parse Estimated Cost (handle empty strings and currency formatting)
        # Note: Estimated Cost is already the line total (unit price * qty)
        est_cost_str = (
            str(item.get("Projected Cost with contingency", "0")).replace(",", "").strip()
        )
        try:
            item_total = float(est_cost_str) if est_cost_str else 0.0
        except ValueError:
            item_total = 0.0

        # Categorize by component type
        if "main energy asset" in component_type.lower():
            costs["main_energy_asset"] += item_total
            counts["main_energy_asset"] += 1
        elif "metering" in component_type.lower():
            costs["metering"] += item_total
            counts["metering"] += 1
        else:
            # Everything else is Balance of System (BoS)
            costs["bos"] += item_total
            counts["bos"] += 1

    # Calculate total
    total_cost = costs["main_energy_asset"] + costs["metering"] + costs["bos"]

    return {
        "main_energy_asset_cost": round(costs["main_energy_asset"], 2),
        "metering_cost": round(costs["metering"], 2),
        "bos_cost": round(costs["bos"], 2),
        "total_cost": round(total_cost, 2),
        "item_counts": counts,
    }


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
            grid_id = (
                existing_grid.get("Id")
                or existing_grid.get("Grid ID")
                or existing_grid.get("_RowNumber")
            )
            steps.append({"step": "find_grid", "status": "found", "grid_id": grid_id})
            workflow_result["grid"] = existing_grid
            logger.info(f"Found existing grid: {grid_id}")
        else:
            create_result = await client.create_grid(grid_name, community)
            rows = create_result.get("Rows", [])
            if rows:
                # AppSheet returns the UNIQUEID in the "Id" field
                grid_id = rows[0].get("Id") or rows[0].get("_RowNumber")
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
        )

        design_result = await client.create_design(design_input, grid_id)
        rows = design_result.get("Rows", [])
        if rows:
            # AppSheet returns the UNIQUEID in the "Id" field
            design_id = rows[0].get("Id") or rows[0].get("_RowNumber")
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


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available tools"""
    if not GRID_DESIGN_ACTIONS_ENABLED:
        logger.info("Grid Design actions disabled - no tools listed")
        return []

    tools = [
        types.Tool(
            name="design_and_bom",
            description=(
                "Create a grid design and generate Bill of Materials (BOM) in AppSheet. "
                "This tool: 1) Creates a grid if it doesn't exist by name, "
                "2) Creates a new design with specified parameters, "
                "3) Triggers design and BOM generation actions, "
                "4) Waits for completion and returns the results. "
                "Use this when users ask to create a new solar grid design or generate a BOM."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "grid_name": {
                        "type": "string",
                        "description": "Name of the grid (will be created if it doesn't exist)",
                    },
                    "design_name": {
                        "type": "string",
                        "description": "Name for the new design",
                    },
                    "max_connections": {
                        "type": "integer",
                        "description": "Maximum number of connections for this grid design",
                    },
                    "community": {
                        "type": "string",
                        "description": "Community name/location for the grid",
                    },
                    "inverter_type": {
                        "type": "string",
                        "description": "Inverter type (default: Quattro 15kVA)",
                        "default": "Quattro 15kVA",
                    },
                    "battery_type": {
                        "type": "string",
                        "description": "Battery type (default: Pylontech UP5000)",
                        "default": "Pylontech UP5000",
                    },
                    "mppt_type": {
                        "type": "string",
                        "description": "MPPT type (default: Victron 250/85 MPPT)",
                        "default": "Victron 250/85 MPPT",
                    },
                    "pv_type": {
                        "type": "string",
                        "description": "PV panel type (default: JA455W Panel)",
                        "default": "JA455W Panel",
                    },
                    "pv_inverter_type": {
                        "type": "string",
                        "description": "PV Inverter type (optional)",
                    },
                    "initial_residential_connections": {
                        "type": "integer",
                        "description": "Initial residential connections (default: 90% of max_connections)",
                    },
                    "initial_business_connections": {
                        "type": "integer",
                        "description": "Initial business connections (default: 10% of max_connections)",
                    },
                    "initial_3phase_connections": {
                        "type": "integer",
                        "description": "Initial 3-phase connections (default: 0)",
                        "default": 0,
                    },
                    "num_poc_teams": {
                        "type": "integer",
                        "description": "Number of PoC teams to install meters (default: 1)",
                        "default": 1,
                    },
                    "anchor_load_kw": {
                        "type": "number",
                        "description": "Anchor load in kW (default: 0)",
                        "default": 0,
                    },
                    "force_3phase": {
                        "type": "boolean",
                        "description": "Force 3-phase design (default: false)",
                        "default": False,
                    },
                    "target_kwp": {
                        "type": "number",
                        "description": "Target kWp to constrain the design (optional, AppSheet calculates freely if not provided)",
                    },
                    "target_kwh": {
                        "type": "number",
                        "description": "Target kWh to constrain the design (optional, AppSheet calculates freely if not provided)",
                    },
                    "wait_for_completion": {
                        "type": "boolean",
                        "description": "Whether to wait for generation to complete (default: true)",
                        "default": True,
                    },
                    "wait_for_bom": {
                        "type": "boolean",
                        "description": "Whether to trigger BOM generation. Set False to skip BOM and return after design autopopulate (default: true)",
                        "default": True,
                    },
                },
                "required": ["grid_name", "design_name", "max_connections"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="find_grid",
            description="Find an existing grid by name in AppSheet",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid_name": {
                        "type": "string",
                        "description": "Name of the grid to find",
                    },
                },
                "required": ["grid_name"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="get_design_bom",
            description="Get the Bill of Materials for an existing design",
            inputSchema={
                "type": "object",
                "properties": {
                    "design_id": {
                        "type": "string",
                        "description": "ID of the design to get BOM for",
                    },
                },
                "required": ["design_id"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="update_design",
            description="Update fields on an existing design row in AppSheet. Use after site layout to set distances.",
            inputSchema={
                "type": "object",
                "properties": {
                    "design_id": {
                        "type": "string",
                        "description": "UNIQUEID of the design to update",
                    },
                    "updates": {
                        "type": "object",
                        "description": "Dict of AppSheet column names to new values (e.g. {'Avg Distance to PV Combiner (m)': 15.5})",
                    },
                },
                "required": ["design_id", "updates"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="trigger_bom",
            description="Trigger BOM generation for a design, wait for completion, and return results.",
            inputSchema={
                "type": "object",
                "properties": {
                    "design_id": {
                        "type": "string",
                        "description": "UNIQUEID of the design to generate BOM for",
                    },
                    "grid_name": {
                        "type": "string",
                        "description": "Grid name (for output context)",
                    },
                },
                "required": ["design_id"],
            },
            visible_to_customer=False,
        ),
    ]

    logger.info(f"Grid Design server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls"""

    try:
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

        if name == "design_and_bom":
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
                wait_for_completion=arguments.get("wait_for_completion", True),
                wait_for_bom=arguments.get("wait_for_bom", True),
            )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "find_grid":
            client = get_client()
            grid = await client.find_grid_by_name(arguments["grid_name"])
            result = (
                {"success": True, "grid": grid}
                if grid
                else {"success": False, "error": "Grid not found"}
            )
            return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "get_design_bom":
            client = get_client()
            bom = await client.get_bom_for_design(arguments["design_id"])
            result = {"success": True, "bom_items": bom, "count": len(bom)}
            return [types.TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

        elif name == "update_design":
            client = get_client()
            result = await client.update_design(arguments["design_id"], arguments["updates"])
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"success": True, "updated": result}, indent=2, default=str),
                )
            ]

        elif name == "trigger_bom":
            client = get_client()
            design_id = arguments["design_id"]

            # Trigger BOM
            action_result = await client.trigger_bom_generation(design_id)
            action_rows = action_result.get("Rows", [])
            action_id = action_rows[0].get("Id") if action_rows else None
            logger.info(f"BOM triggered for {design_id}, action: {action_id}")

            # Poll for BOM completion instead of blind sleep
            logger.info(f"Polling for BOM completion (max {BOM_GENERATION_WAIT_SECONDS}s)...")
            poll_interval = 10
            elapsed = 0
            bom_items = []
            while elapsed < BOM_GENERATION_WAIT_SECONDS:
                await asyncio.sleep(poll_interval)
                elapsed += poll_interval
                action = await client.get_action_status(design_id)
                if action and action.get("Status") == ACTION_STATUS_COMPLETED:
                    logger.info(f"BOM completed after {elapsed}s")
                    break
                logger.debug(f"BOM not yet complete ({elapsed}s elapsed)...")

            # Fetch results
            design = await client.get_design(design_id)
            bom_items = await client.get_bom_for_design(design_id)
            if not bom_items:
                logger.warning(
                    f"BOM fetch returned 0 items for design {design_id} after "
                    f"{elapsed}s — AppSheet may still be generating. "
                    "cost_summary will show $0 for all categories."
                )
            cost_summary = compute_bom_cost_summary(bom_items)

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

        else:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps({"success": False, "error": f"Unknown tool: {name}"}),
                )
            ]

    except Exception as e:
        logger.error(f"Error in {name}: {e}")
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"success": False, "error": str(e)}),
            )
        ]


async def main():
    """Main entry point"""
    try:
        logger.info("Starting Grid Design MCP Server...")
        logger.info(f"App ID configured: {'Yes' if GRID_DESIGN_APP_ID else 'No'}")
        logger.info(f"Actions enabled: {GRID_DESIGN_ACTIONS_ENABLED}")
        print("✅ Grid Design server initialized successfully", file=sys.stderr)

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="grid-design-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Grid Design server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Grid Design server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Grid Design server crashed: {e}", file=sys.stderr)
        sys.exit(1)
