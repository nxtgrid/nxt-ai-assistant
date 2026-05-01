"""Read Grid Technical Review history from Google Sheets.

Shared reader used by both the GTR expert workflow and the
get_grid_review_history MCP tool. This module is the source of truth
for sheet reading logic — if the GTR analysis step is retired, the
MCP tool continues to work.

Returns structured monthly review data: KPIs, commentary, actions,
and pending issues.
"""

import asyncio
import calendar
import logging
import os
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

LOGGER = logging.getLogger(__name__)


def get_analysis_month_labels(months_back: int = 12) -> List[str]:
    """Get month labels for the analysis period in reverse chronological order.

    Returns:
        List of month labels like ["January 2026", "December 2025", ...]
    """
    now = datetime.now()
    labels = []
    for i in range(1, months_back + 1):
        m = now.month - i
        y = now.year
        while m <= 0:
            m += 12
            y -= 1
        labels.append(f"{calendar.month_name[m]} {y}")
    return labels


def read_year_tab_sync(
    spreadsheet_id: str,
    year: int,
) -> Tuple[Optional[List[List[str]]], Optional[str], Optional[str]]:
    """Read all data from a year tab (synchronous, runs via asyncio.to_thread).

    Returns:
        Tuple of (values 2D list, tab_name, error_message)
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_sheets_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return None, None, "Google Sheets integration not configured"

    try:
        # Import sheet helpers from GTR module
        from orchestrator.experts.handlers.grids_technical_reviewer.fetch_pending_actions import (
            find_year_tab,
            sheet_range,
        )

        credentials = get_sheets_credentials()
        service = build("sheets", "v4", credentials=credentials)

        tab_name, _ = find_year_tab(service, spreadsheet_id, year)

        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=sheet_range(tab_name, "A:I"),
            )
            .execute()
        )

        values = result.get("values", [])
        LOGGER.info(f"Read {len(values)} rows from tab '{tab_name}' (year {year})")
        return values, tab_name, None

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Error reading year tab {year} from {spreadsheet_id}: {error_msg}")
        if "404" in error_msg:
            return None, None, "Sheet not found"
        elif "403" in error_msg:
            return None, None, "Access denied"
        return None, None, f"Error: {error_msg}"


def parse_review_section(
    values: List[List[str]],
    start_row: int,
    end_row: int,
    month_label: str,
) -> Dict[str, Any]:
    """Parse a review section into structured data.

    Extracts KPIs by name (not position), actions, and pending issues.

    Returns:
        Dict with month_label, kpis, actions, pending_issues keys
    """
    section: Dict[str, Any] = {
        "month_label": month_label,
        "kpis": {},
        "actions": [],
        "pending_issues": [],
    }

    # Known KPI names (match by prefix in column A, case-insensitive)
    kpi_names = [
        "CUF",
        "Losses",
        "Full Service Hours",
        "Revenue Collection",
        "Connections",
        "Faults",
        "Availability",
    ]

    in_actions = False
    in_pending = False

    for row_idx in range(start_row, end_row):
        if row_idx >= len(values):
            break
        row = values[row_idx]
        if not row:
            continue

        cell_a = str(row[0]).strip() if len(row) > 0 else ""
        cell_a_lower = cell_a.lower()

        # Detect section headers
        if "action" in cell_a_lower and "taken" in cell_a_lower:
            in_actions = True
            in_pending = False
            continue
        if "pending" in cell_a_lower or "outstanding" in cell_a_lower:
            in_pending = True
            in_actions = False
            continue

        # Parse KPIs
        if not in_actions and not in_pending:
            for kpi_name in kpi_names:
                if cell_a_lower.startswith(kpi_name.lower()):
                    value = str(row[1]).strip() if len(row) > 1 else ""
                    commentary = str(row[2]).strip() if len(row) > 2 else ""
                    section["kpis"][kpi_name] = {
                        "value": value,
                        "commentary": commentary,
                    }
                    break

        # Parse actions
        if in_actions and cell_a and cell_a_lower not in ("issue", "action taken", ""):
            action = {
                "issue": cell_a,
                "action_taken": str(row[1]).strip() if len(row) > 1 else "",
                "solution": str(row[2]).strip() if len(row) > 2 else "",
                "responsible": str(row[3]).strip() if len(row) > 3 else "",
                "urgent": str(row[4]).strip() if len(row) > 4 else "",
            }
            if action["issue"] and action["issue"].lower() != "issue":
                section["actions"].append(action)

        # Parse pending issues
        if in_pending and cell_a and cell_a_lower not in ("issue", "pending", "outstanding", ""):
            pending = {
                "issue": cell_a,
                "status": str(row[1]).strip() if len(row) > 1 else "",
                "responsible": str(row[2]).strip() if len(row) > 2 else "",
            }
            if pending["issue"].lower() not in ("issue", "pending"):
                section["pending_issues"].append(pending)

    return section


def format_section_as_markdown(section: Dict[str, Any]) -> str:
    """Format a parsed review section as markdown.

    Returns:
        Markdown string for this month's review
    """
    lines = [f"### {section['month_label']}"]

    # KPIs table
    kpis = section.get("kpis", {})
    if kpis:
        lines.append("#### KPIs")
        lines.append("| KPI | Value | Commentary |")
        lines.append("|-----|-------|-----------|")
        for kpi_name, kpi_data in kpis.items():
            value = kpi_data.get("value", "")
            commentary = kpi_data.get("commentary", "")
            lines.append(f"| {kpi_name} | {value} | {commentary} |")
    else:
        lines.append("#### KPIs")
        lines.append("_No KPI data recorded_")

    # Actions table
    actions = section.get("actions", [])
    if actions:
        lines.append("")
        lines.append("#### Actions")
        lines.append("| Issue | Action Taken | Long Term Solution | Responsible | Urgent |")
        lines.append("|-------|-------------|-------------------|-------------|--------|")
        for a in actions:
            lines.append(
                f"| {a.get('issue', '')} | {a.get('action_taken', '')} "
                f"| {a.get('solution', '')} | {a.get('responsible', '')} "
                f"| {a.get('urgent', '')} |"
            )

    # Pending issues
    pending = section.get("pending_issues", [])
    if pending:
        lines.append("")
        lines.append("#### Pending Issues")
        lines.append("| Issue | Status | Responsible |")
        lines.append("|-------|--------|-------------|")
        for p in pending:
            lines.append(
                f"| {p.get('issue', '')} | {p.get('status', '')} | {p.get('responsible', '')} |"
            )

    lines.append("")
    lines.append("---")

    return "\n".join(lines)


async def load_grid_review_history(
    grids: List[Dict[str, Any]],
    months_back: int = 0,
) -> str:
    """Load historical GTR reviews from Google Sheets.

    This is the main entry point for both the MCP tool and the GTR step.

    Args:
        grids: List of dicts with 'name' and 'spreadsheet_id'
        months_back: How many months back (0 = use GTR_ANALYSIS_MONTHS_BACK env var)

    Returns:
        Markdown string with all reviews
    """
    from orchestrator.experts.handlers.grids_technical_reviewer.fetch_pending_actions import (
        extract_year_from_month_label,
        find_review_section_range,
    )

    if not months_back:
        months_back = int(os.getenv("GTR_ANALYSIS_MONTHS_BACK", "12"))

    target_months = get_analysis_month_labels(months_back)
    needed_years = sorted({extract_year_from_month_label(m) for m in target_months})

    LOGGER.info(
        f"Loading GTR history: {months_back} months back, "
        f"years: {needed_years}, grids: {[g['name'] for g in grids]}"
    )

    all_grid_sections: List[str] = []

    for grid in grids:
        grid_name = grid["name"]
        spreadsheet_id = grid.get("spreadsheet_id", "")

        if not spreadsheet_id:
            LOGGER.warning(f"No spreadsheet ID for grid {grid_name}")
            continue

        # Read year tabs in parallel
        read_tasks = [
            asyncio.to_thread(partial(read_year_tab_sync, spreadsheet_id, year))
            for year in needed_years
        ]
        results = await asyncio.gather(*read_tasks, return_exceptions=True)

        # Find month sections
        month_sections: Dict[str, Dict[str, Any]] = {}

        for year, result in zip(needed_years, results):
            if isinstance(result, BaseException):
                LOGGER.error(f"Failed to read year {year} for {grid_name}: {result}")
                continue

            values, tab_name, error = result
            if error or not values:
                LOGGER.warning(f"No data for year {year}, grid {grid_name}: {error}")
                continue

            for month_label in target_months:
                if month_label in month_sections:
                    continue
                section_range = find_review_section_range(values, month_label)
                if section_range:
                    start_row, end_row = section_range
                    parsed = parse_review_section(values, start_row, end_row, month_label)
                    month_sections[month_label] = parsed

        # Build markdown
        earliest = target_months[-1]
        latest = target_months[0]

        grid_md_parts = [
            f"# GTR Review History: {grid_name}",
            f"## Period: {earliest} - {latest} ({len(target_months)} months)",
            "",
        ]

        months_found = 0
        for month_label in target_months:
            if month_label in month_sections:
                grid_md_parts.append(format_section_as_markdown(month_sections[month_label]))
                months_found += 1
            else:
                grid_md_parts.append(f"### {month_label}")
                grid_md_parts.append("_No review data found_")
                grid_md_parts.append("")
                grid_md_parts.append("---")

        LOGGER.info(f"Grid {grid_name}: found {months_found}/{len(target_months)} months")
        all_grid_sections.append("\n".join(grid_md_parts))

    if not all_grid_sections:
        return "No historical review data found."

    result_md = "\n\n".join(all_grid_sections)

    # Cap size
    max_size = int(os.getenv("GTR_ANALYSIS_MAX_HISTORY_SIZE", "50000"))
    if len(result_md) > max_size:
        result_md = result_md[:max_size] + "\n\n_[Truncated — oldest months omitted]_"

    return result_md
