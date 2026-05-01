"""Populate BOM tab step handler for Light Preliminary Package.

Creates a "Full BOM" sheet in the LPP spreadsheet and populates it
with item Name, Quantity, and Estimated Cost grouped by Component Type.
"""

from typing import Any, Dict, List

from googleapiclient.discovery import build

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.google_auth import get_sheets_write_credentials
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

SHEET_NAME = "Full BOM"

# Columns: A=Component Type / Name, B=Quantity, C=Estimated Cost
HEADERS = ["Name", "Qty", "Estimated Cost (USD)"]


def _parse_est_cost(item: Dict[str, Any]) -> float:
    """Parse Estimated Cost from BOM item, handling currency formatting."""
    raw = (
        str(item.get("Projected Cost with contingency", "0"))
        .replace(",", "")
        .replace("$", "")
        .strip()
    )
    try:
        return float(raw) if raw else 0.0
    except ValueError:
        return 0.0


def _group_bom_items(bom_items: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Group BOM items by Component Type, excluding Tools."""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in bom_items:
        comp_type = str(item.get("Component Type", "Other")).strip()
        if not comp_type:
            comp_type = "Other"
        if "tools" in comp_type.lower():
            continue
        groups.setdefault(comp_type, []).append(item)
    return groups


def _build_sheet_rows(bom_items: List[Dict[str, Any]]) -> List[List[Any]]:
    """Build rows for the BOM sheet: headers, then items grouped by type with subtotals."""
    groups = _group_bom_items(bom_items)
    rows: List[List[Any]] = []

    # Header row
    rows.append(HEADERS)

    grand_total = 0.0

    for comp_type in sorted(groups.keys()):
        items = groups[comp_type]

        # Group header (bold via formatting later)
        rows.append([comp_type, "", ""])

        group_total = 0.0
        for item in sorted(items, key=lambda x: str(x.get("Item Name", ""))):
            name = str(item.get("Item Name", "")).strip()
            qty = item.get("Qty", item.get("Quantity", ""))
            # Try to parse quantity as number
            try:
                qty = int(float(str(qty))) if qty else ""
            except (ValueError, TypeError):
                pass
            cost = _parse_est_cost(item)
            group_total += cost
            rows.append([f"  {name}", qty, round(cost, 2) if cost else ""])

        # Group subtotal
        rows.append(["", "", round(group_total, 2)])
        grand_total += group_total

        # Blank separator
        rows.append(["", "", ""])

    # Grand total
    rows.append(["TOTAL", "", round(grand_total, 2)])

    return rows


def create_bom_sheet(
    document_id: str,
    bom_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Create and populate a Full BOM sheet tab in a Google Spreadsheet.

    This is the core logic, callable from any context (step handler or inline).

    Args:
        document_id: Google Sheets document ID
        bom_items: List of BOM item dicts with Component Type, Name, Quantity, Projected Cost

    Returns:
        Dict with success, bom_rows_written, bom_item_count, bom_groups, or error
    """
    if not bom_items:
        return {"success": True, "bom_rows_written": 0, "bom_item_count": 0, "bom_groups": []}

    LOGGER.info(f"Building BOM tab with {len(bom_items)} items for doc {document_id}")
    rows = _build_sheet_rows(bom_items)

    creds = get_sheets_write_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    # Check if sheet already exists
    spreadsheet = service.spreadsheets().get(spreadsheetId=document_id).execute()
    existing_sheets = [s["properties"]["title"] for s in spreadsheet["sheets"]]

    if SHEET_NAME in existing_sheets:
        service.spreadsheets().values().clear(
            spreadsheetId=document_id,
            range=f"'{SHEET_NAME}'!A:Z",
        ).execute()
        LOGGER.info(f"Cleared existing '{SHEET_NAME}' sheet")
    else:
        service.spreadsheets().batchUpdate(
            spreadsheetId=document_id,
            body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
        ).execute()
        LOGGER.info(f"Created new '{SHEET_NAME}' sheet")

    # Write all rows
    service.spreadsheets().values().update(
        spreadsheetId=document_id,
        range=f"'{SHEET_NAME}'!A1",
        valueInputOption="USER_ENTERED",
        body={"values": rows},
    ).execute()

    # Get sheet ID for formatting
    spreadsheet = service.spreadsheets().get(spreadsheetId=document_id).execute()
    sheet_id = None
    for s in spreadsheet["sheets"]:
        if s["properties"]["title"] == SHEET_NAME:
            sheet_id = s["properties"]["sheetId"]
            break

    # Apply formatting
    if sheet_id is not None:
        format_requests = []

        # Bold header row with grey background
        format_requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 0,
                        "endRowIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {"bold": True},
                            "backgroundColor": {
                                "red": 0.9,
                                "green": 0.9,
                                "blue": 0.9,
                            },
                        }
                    },
                    "fields": "userEnteredFormat(textFormat,backgroundColor)",
                }
            }
        )

        # Bold group headers and total row
        for i, row in enumerate(rows):
            if not row:
                continue
            cell_val = str(row[0]) if row[0] else ""
            is_group_header = (
                cell_val
                and not cell_val.startswith("  ")
                and cell_val not in ("Name", "TOTAL")
                and i > 0
            )
            is_total = cell_val == "TOTAL"

            if is_group_header:
                format_requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": i,
                                "endRowIndex": i + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                }
                            },
                            "fields": "userEnteredFormat.textFormat",
                        }
                    }
                )
            elif is_total:
                format_requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": i,
                                "endRowIndex": i + 1,
                            },
                            "cell": {
                                "userEnteredFormat": {
                                    "textFormat": {"bold": True},
                                    "borders": {
                                        "top": {
                                            "style": "SOLID",
                                            "width": 1,
                                        }
                                    },
                                }
                            },
                            "fields": "userEnteredFormat(textFormat,borders)",
                        }
                    }
                )

        # Format cost column as currency
        format_requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": len(rows),
                        "startColumnIndex": 2,
                        "endColumnIndex": 3,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "numberFormat": {
                                "type": "CURRENCY",
                                "pattern": "$#,##0.00",
                            }
                        }
                    },
                    "fields": "userEnteredFormat.numberFormat",
                }
            }
        )

        # Set column A to fixed width (250px) with text wrap
        format_requests.append(
            {
                "updateDimensionProperties": {
                    "range": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 0,
                        "endIndex": 1,
                    },
                    "properties": {"pixelSize": 250},
                    "fields": "pixelSize",
                }
            }
        )
        format_requests.append(
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startColumnIndex": 0,
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat.wrapStrategy",
                }
            }
        )

        # Auto-resize columns B and C only
        format_requests.append(
            {
                "autoResizeDimensions": {
                    "dimensions": {
                        "sheetId": sheet_id,
                        "dimension": "COLUMNS",
                        "startIndex": 1,
                        "endIndex": 3,
                    }
                }
            }
        )

        if format_requests:
            service.spreadsheets().batchUpdate(
                spreadsheetId=document_id,
                body={"requests": format_requests},
            ).execute()

    groups = _group_bom_items(bom_items)
    LOGGER.info(
        f"Populated '{SHEET_NAME}' with {len(bom_items)} items "
        f"in {len(groups)} groups, {len(rows)} rows"
    )

    return {
        "success": True,
        "bom_rows_written": len(rows),
        "bom_item_count": len(bom_items),
        "bom_groups": list(groups.keys()),
    }


@register_step("populate_bom_tab")
async def populate_bom_tab(context: StepContext) -> StepResult:
    """Create and populate a Full BOM sheet tab in the LPP spreadsheet.

    Reads BOM items from generate_site_bom results (or falls back to
    generate_powerplant_design) and writes them to a new "Full BOM" sheet
    grouped by Component Type.

    Requires:
    - document_id in state (from copy_lpp_template)
    - bom_items in generate_site_bom or generate_powerplant_design results
    """
    await context.send_progress_to_user("Populating Bill of Materials...")

    document_id = context.get_state("document_id")
    if not document_id:
        return StepResult.failure("No document_id in state - run copy_lpp_template first")

    # Try generate_site_bom first, fall back to generate_powerplant_design
    bom_result = context.get_previous_result("generate_site_bom") or {}
    bom_items = bom_result.get("bom_items", [])
    if not bom_items:
        design_result = context.get_previous_result("generate_powerplant_design") or {}
        bom_items = design_result.get("bom_items", [])

    if not bom_items:
        LOGGER.warning(
            "No BOM items available from generate_site_bom or generate_powerplant_design"
        )
        return StepResult(
            data={"bom_rows_written": 0},
            progress_message="No BOM items to populate",
        )

    try:
        result = create_bom_sheet(document_id, bom_items)

        if not result.get("success"):
            return StepResult.failure(f"Error creating BOM tab: {result.get('error')}")

        return StepResult(
            data=result,
            state_updates={"bom_tab_populated": True},
            progress_message=f"Created '{SHEET_NAME}' tab with {len(bom_items)} items",
        )

    except Exception as e:
        LOGGER.exception(f"Error creating BOM tab: {e}")
        return StepResult.failure(f"Error creating BOM tab: {str(e)}")
