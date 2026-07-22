"""MCP Customer Server - Customer-facing tools for payment and commissioning status checks."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional

import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server

# Load environment variables from .env file BEFORE importing shared_code
load_dotenv()

from servers.customer_server.client import customer_client
from servers.customer_server.tool_schemas import TOOL_SCHEMAS
from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry

from shared.utils.response_formatters import compose_json_response

# Configure logging to stderr for Claude Desktop visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("customer-server")

# Startup messages to stderr
print("🚀 Customer MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("customer-server")
registry = ToolRegistry("customer")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


async def get_last_gtr_summary(grid_name: str) -> Dict[str, Any]:
    """Get the last GTR (Grid Technical Report) summary from Google Sheets.

    Resolves grid name to a spreadsheet ID via expert instructions,
    then reads the most recent review section.

    Args:
        grid_name: Grid name to look up

    Returns:
        Dict with month_label, kpis, pending_issues, or error
    """
    import re
    from concurrent.futures import ThreadPoolExecutor
    from functools import partial

    # Pattern to extract grid-to-sheet mappings from expert instructions
    grid_sheet_patterns = [
        re.compile(
            r"^\s*[-*]\s*([^:]+):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
            re.MULTILINE,
        ),
        re.compile(
            r"^([A-Za-z][A-Za-z0-9\s]*):\s*(https://docs\.google\.com/spreadsheets/d/[a-zA-Z0-9_-]+)",
            re.MULTILINE,
        ),
    ]
    spreadsheet_id_pattern = re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)")

    # Step 1: Fetch expert instructions doc to get grid-to-sheet mappings
    expert_doc_id = os.getenv("EXPERT_INSTRUCTIONS_DOC_ID")
    if not expert_doc_id:
        return {"error": "GTR expert not configured"}

    try:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown
    except ImportError:
        return {"error": "Google Drive integration not available"}

    doc_text = fetch_google_doc_markdown(expert_doc_id)
    if not doc_text:
        return {"error": "Could not fetch expert instructions"}

    # Extract grid-to-sheet mappings
    mappings: Dict[str, Dict[str, str]] = {}
    for pattern in grid_sheet_patterns:
        for match in pattern.finditer(doc_text):
            name = match.group(1).strip()
            url = match.group(2).strip()
            if name.lower() in ["grid", "name", "url", "sheet"]:
                continue
            id_match = spreadsheet_id_pattern.search(url)
            sid = id_match.group(1) if id_match else ""
            key = name.lower()
            if key not in mappings:
                mappings[key] = {"name": name, "url": url, "spreadsheet_id": sid}

    if not mappings:
        return {"no_gtr": True, "message": "No GTR sheets configured"}

    # Step 2: Fuzzy-match grid name
    from shared.utils.grid_matcher import find_best_grid_match

    available_names = [g["name"] for g in mappings.values()]
    matched_name, _, _ = find_best_grid_match(grid_name, available_names)
    if not matched_name:
        return {"no_gtr": True, "message": f"No GTR sheet for grid '{grid_name}'"}

    sheet_info = mappings.get(matched_name.lower())
    if not sheet_info or not sheet_info.get("spreadsheet_id"):
        return {"no_gtr": True, "message": f"No GTR sheet for grid '{matched_name}'"}

    spreadsheet_id = sheet_info["spreadsheet_id"]

    # Step 3: Compute month label (review is for previous month)
    now = datetime.now()
    if now.month == 1:
        review_month, review_year = 12, now.year - 1
    else:
        review_month, review_year = now.month - 1, now.year
    month_names = [
        "",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    month_label = f"{month_names[review_month]} {review_year}"

    # Step 4: Read sheet (sync Google API in thread pool)
    def _read_sheet_sync(sid: str, mlabel: str) -> Dict[str, Any]:
        try:
            from googleapiclient.discovery import build

            from shared.utils.google_auth import get_sheets_credentials
        except ImportError:
            return {"error": "Google Sheets integration not configured"}

        try:
            credentials = get_sheets_credentials()
            service = build("sheets", "v4", credentials=credentials)

            # Read column A to find review row
            col_a = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range="A:A")
                .execute()
                .get("values", [])
            )

            # Find the review row
            review_row = None
            for i, row in enumerate(col_a):
                if row and f"{mlabel.lower()} review" in str(row[0]).strip().lower():
                    review_row = i
                    break

            if review_row is None:
                # Try previous-previous month as fallback
                return {"no_gtr": True, "message": f"No {mlabel} review found in sheet"}

            # Read the review section (15 rows, all columns)
            start_row = review_row + 1
            end_row = review_row + 16
            section = (
                service.spreadsheets()
                .values()
                .get(spreadsheetId=sid, range=f"A{start_row}:Z{end_row}")
                .execute()
                .get("values", [])
            )

            if not section:
                return {"no_gtr": True, "message": "Empty review section"}

            # Parse KPIs and pending issues
            kpis = {}
            pending_issues = []
            for idx, srow in enumerate(section):
                if idx < 2:
                    continue  # Skip header rows

                # KPIs (columns A=name, B=value, C=commentary)
                if len(srow) >= 3:
                    kpi_name = str(srow[0]).strip() if srow[0] else ""
                    kpi_value = str(srow[1]).strip() if len(srow) > 1 and srow[1] else ""
                    commentary = str(srow[2]).strip() if len(srow) > 2 and srow[2] else ""
                    if kpi_name and kpi_name.lower() not in ["notes:", "kpi", ""]:
                        kpis[kpi_name] = {"value": kpi_value, "commentary": commentary}

                # Pending issues (column E, index 4)
                if len(srow) > 4:
                    cell = str(srow[4]).strip()
                    if cell and cell.lower() not in ["pending issues", "new issues", "actions", ""]:
                        pending_issues.append(cell)

            return {
                "month_label": mlabel,
                "grid_name": matched_name,
                "kpis": kpis,
                "pending_issues": pending_issues,
            }

        except Exception as e:
            logger.error(f"Error reading GTR sheet {sid}: {e}")
            if "404" in str(e):
                return {"no_gtr": True, "message": "GTR sheet not found"}
            elif "403" in str(e):
                return {"no_gtr": True, "message": "Access denied to GTR sheet"}
            return {"error": f"Failed to read GTR sheet: {str(e)}"}

    loop = asyncio.get_event_loop()
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gtr_")
    try:
        result = await loop.run_in_executor(
            executor, partial(_read_sheet_sync, spreadsheet_id, month_label)
        )
        return result
    except Exception as e:
        logger.error(f"GTR summary error for {grid_name}: {e}")
        return {"error": f"Failed to fetch GTR summary: {str(e)}"}
    finally:
        executor.shutdown(wait=False)


async def get_my_open_issues(
    organization_id: int,
    issue_type: Optional[str] = None,
) -> Dict[str, Any]:
    """Return open escalations for the caller's organisation, optionally filtered by issue type."""
    chat_db_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
    chat_db_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
    if not chat_db_url or not chat_db_key:
        return {"error": "Chat database not configured"}

    try:
        from supabase import create_client  # type: ignore[attr-defined]

        client = create_client(chat_db_url, chat_db_key)

        query = (
            client.table("escalation_mappings")
            .select(
                "id, question_text, reason, action_type, created_at, thread_id, "
                "chat_threads(issue_type)"
            )
            .eq("organization_id", organization_id)
            .eq("is_active", True)
            .order("created_at", desc=True)
            .limit(50)
        )
        response = query.execute()
        rows = response.data or []

        results = []
        for row in rows:
            thread_data = row.get("chat_threads") or {}
            row_issue_type = (
                thread_data.get("issue_type") if isinstance(thread_data, dict) else None
            )
            if issue_type and row_issue_type != issue_type:
                continue
            results.append(
                {
                    "id": row.get("id"),
                    "thread_id": row.get("thread_id"),
                    "issue_type": row_issue_type or "unknown",
                    "summary": row.get("question_text"),
                    "reason": row.get("reason"),
                    "action_type": row.get("action_type"),
                    "created_at": row.get("created_at"),
                }
            )

        # Summarise counts per type for the response header
        type_counts: Dict[str, int] = {}
        for r in results:
            t = r["issue_type"]
            type_counts[t] = type_counts.get(t, 0) + 1

        return {
            "total_open": len(results),
            "by_type": type_counts,
            "issues": results,
        }
    except Exception as e:
        logger.error(f"Error fetching open issues for org={organization_id}: {e}")
        return {"error": f"Failed to fetch open issues: {str(e)}"}


@registry.tool("meter_information", _SCHEMAS_BY_NAME["meter_information"])
async def _tool_meter_information(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.meter_information(
        meter_number=arguments.get("meter_number"),
        user_email=arguments.get("user_email"),
        organization_id=arguments.get("organization_id"),
    )
    return list(compose_json_response(result))


@registry.tool("customer_get_meter_consumption", _SCHEMAS_BY_NAME["customer_get_meter_consumption"])
async def _tool_customer_get_meter_consumption(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_meter_consumption(
        meter_number=arguments.get("meter_number", ""),
        organization_id=int(organization_id),
        days_back=int(arguments.get("days_back", 30)),
    )

    # If result includes a chart, return it as an image + JSON data
    chart_b64 = None
    if isinstance(result, dict):
        chart_b64 = result.pop("chart_base64", None)

    content_list = []
    if chart_b64:
        content_list.append(
            types.ImageContent(type="image", data=chart_b64, mimeType="image/png")
        )
    content_list.append(
        types.TextContent(type="text", text=json.dumps(result, default=str))
    )
    return content_list


@registry.tool("customer_get_grid_chat_chronology", _SCHEMAS_BY_NAME["customer_get_grid_chat_chronology"])
async def _tool_customer_get_grid_chat_chronology(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_grid_chat_chronology(
        grid_name=arguments.get("grid_name", ""),
        organization_id=int(organization_id),
        days_back=int(arguments.get("days_back", 7)),
    )
    return list(compose_json_response(result))


@registry.tool("customer_list_grid_meters", _SCHEMAS_BY_NAME["customer_list_grid_meters"])
async def _tool_customer_list_grid_meters(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    grid_name = arguments.get("grid_name")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    if not grid_name:
        return [
            types.TextContent(
                type="text",
                text="Error: grid_name is required",
            )
        ]
    result = await customer_client.list_grid_meters(
        grid_name=grid_name,
        organization_id=int(organization_id),
    )
    return list(compose_json_response(result))


@registry.tool("customer_get_meters_on_pole", _SCHEMAS_BY_NAME["customer_get_meters_on_pole"])
async def _tool_customer_get_meters_on_pole(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_meters_on_pole(
        pole_reference=arguments.get("pole_reference", ""),
        organization_id=int(organization_id),
        grid_name=arguments.get("grid_name"),
    )
    return list(compose_json_response(result))


@registry.tool("customer_get_grid_status", _SCHEMAS_BY_NAME["customer_get_grid_status"])
async def _tool_customer_get_grid_status(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # organization_id is injected by orchestrator, not passed by LLM
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_grid_status(
        organization_id=int(organization_id),
        grid_name=arguments.get("grid_name"),
        user_email=arguments.get("user_email"),
    )
    return list(compose_json_response(result))


@registry.tool("customer_get_all_grids_status", _SCHEMAS_BY_NAME["customer_get_all_grids_status"])
async def _tool_customer_get_all_grids_status(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # organization_id is injected by orchestrator, not passed by LLM
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_all_grids_status(
        organization_id=int(organization_id),
    )
    return list(compose_json_response(result))


@registry.tool("customer_get_last_gtr_summary", _SCHEMAS_BY_NAME["customer_get_last_gtr_summary"])
async def _tool_customer_get_last_gtr_summary(arguments: Dict[str, Any]) -> List[types.TextContent]:
    grid_name = arguments.get("grid_name")
    if not grid_name:
        return [
            types.TextContent(
                type="text",
                text="Error: grid_name is required",
            )
        ]
    result = await get_last_gtr_summary(grid_name=grid_name)
    return list(compose_json_response(result))


@registry.tool("customer_get_fs_daily_summary", _SCHEMAS_BY_NAME["customer_get_fs_daily_summary"])
async def _tool_customer_get_fs_daily_summary(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    if not organization_id:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await customer_client.get_fs_daily_summary(
        organization_id=int(organization_id),
        grid_name=arguments.get("grid_name", ""),
        start_date=arguments.get("start_date"),
        end_date=arguments.get("end_date"),
    )
    return list(compose_json_response(result))


@registry.tool("check_payment_completion", _SCHEMAS_BY_NAME["check_payment_completion"])
async def _tool_check_payment_completion(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.check_payment_completion(
        transaction_reference=arguments.get("transaction_reference"),
        user_email=arguments.get("user_email"),
        organization_id=arguments.get("organization_id"),
    )
    return list(compose_json_response(result))


@registry.tool("retry_commissioning", _SCHEMAS_BY_NAME["retry_commissioning"])
async def _tool_retry_commissioning(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.retry_commissioning(
        meter_number=arguments.get("meter_number"),
        user_email=arguments.get("user_email"),
        organization_id=arguments.get("organization_id"),
    )
    return list(compose_json_response(result))


@registry.tool("unassign_meter", _SCHEMAS_BY_NAME["unassign_meter"])
async def _tool_unassign_meter(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.unassign_meter(
        meter_number=arguments.get("meter_number"),
        user_email=arguments.get("user_email"),
        organization_id=arguments.get("organization_id"),
    )
    return list(compose_json_response(result))


@registry.tool("set_meter_power_limit", _SCHEMAS_BY_NAME["set_meter_power_limit"])
async def _tool_set_meter_power_limit(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    power_limit_watts = int(arguments.get("power_limit_watts", 0))
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.set_meter_power_limit(
        meter_number=meter_number,
        power_limit_watts=power_limit_watts,
        user_email=user_email,
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("set_meter_date", _SCHEMAS_BY_NAME["set_meter_date"])
async def _tool_set_meter_date(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.set_meter_date(
        meter_number=meter_number,
        user_email=user_email,
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("turn_meter_on", _SCHEMAS_BY_NAME["turn_meter_on"])
async def _tool_turn_meter_on(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.send_relay_state(
        meter_number=meter_number,
        user_email=user_email,
        interaction_type="TURN_ON",
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("turn_meter_off", _SCHEMAS_BY_NAME["turn_meter_off"])
async def _tool_turn_meter_off(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.send_relay_state(
        meter_number=meter_number,
        user_email=user_email,
        interaction_type="TURN_OFF",
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("resend_meter_token", _SCHEMAS_BY_NAME["resend_meter_token"])
async def _tool_resend_meter_token(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.resend_meter_token(
        meter_number=meter_number,
        user_email=user_email,
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("resend_clear_tamper_token", _SCHEMAS_BY_NAME["resend_clear_tamper_token"])
async def _tool_resend_clear_tamper_token(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.resend_clear_tamper_token(
        meter_number=meter_number,
        user_email=user_email,
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("resend_power_limit_token", _SCHEMAS_BY_NAME["resend_power_limit_token"])
async def _tool_resend_power_limit_token(arguments: Dict[str, Any]) -> List[types.TextContent]:
    meter_number = arguments.get("meter_number", "")
    user_email = arguments.get("user_email", "")
    raw_org = arguments.get("organization_id")
    organization_id = int(raw_org) if raw_org is not None else None
    result = await customer_client.resend_power_limit_token(
        meter_number=meter_number,
        user_email=user_email,
        organization_id=organization_id,
    )
    return [types.TextContent(type="text", text=json.dumps(result, default=str))]


@registry.tool("find_payment", _SCHEMAS_BY_NAME["find_payment"])
async def _tool_find_payment(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.find_payment(
        customer_name=arguments.get("customer_name", ""),
        amount=arguments.get("amount"),
        date=arguments.get("date"),
        organization_name=arguments.get("organization_name"),
        user_email=arguments.get("user_email", ""),
        organization_id=arguments.get("organization_id"),
        time_window_hours=float(arguments.get("time_window_hours", 2.0)),
    )
    return list(compose_json_response(result))


@registry.tool("lookup_transactions", _SCHEMAS_BY_NAME["lookup_transactions"])
async def _tool_lookup_transactions(arguments: Dict[str, Any]) -> List[types.TextContent]:
    result = await customer_client.lookup_transactions(
        user_email=arguments.get("user_email", ""),
        organization_id=arguments.get("organization_id"),
        date_from=arguments.get("date_from"),
        date_to=arguments.get("date_to"),
        reference_number=arguments.get("reference_number"),
        amount=arguments.get("amount"),
        receiver_name=arguments.get("receiver_name"),
        limit=arguments.get("limit"),
    )
    return list(compose_json_response(result))


@registry.tool("get_my_open_issues", _SCHEMAS_BY_NAME["get_my_open_issues"])
async def _tool_get_my_open_issues(arguments: Dict[str, Any]) -> List[types.TextContent]:
    organization_id = arguments.get("organization_id")
    if organization_id is None:
        return [
            types.TextContent(
                type="text",
                text="Error: organization_id is required (should be injected by orchestrator)",
            )
        ]
    result = await get_my_open_issues(
        organization_id=int(organization_id),
        issue_type=arguments.get("issue_type"),
    )
    return list(compose_json_response(result))



handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def main():
    """Main entry point."""
    logger.info("Starting Customer MCP Server...")
    await run_stdio_server(
        server,
        name="customer-server",
        label="Customer",
        on_cleanup=customer_client.close,
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Customer server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Customer server crashed: {e}", file=sys.stderr)
        sys.exit(1)
