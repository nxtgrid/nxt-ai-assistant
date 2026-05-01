"""Fetch pending actions step handler for GTR Expert.

This handler reads previous month's pending issues from the sheet
to carry forward to the new review section.
"""

import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Column indices (0-based) for the review structure
# A=KPI, B=Value, C=Commentary, D=empty, E=Issues, F=Actions, G=Solutions, H=Responsible, I=Urgent
COL_PENDING_ISSUES = 4  # Column E

MONTH_ABBREVIATIONS = {
    "january": "jan",
    "february": "feb",
    "march": "mar",
    "april": "apr",
    "may": "may",
    "june": "jun",
    "july": "jul",
    "august": "aug",
    "september": "sep",
    "october": "oct",
    "november": "nov",
    "december": "dec",
}


def extract_year_from_month_label(month_label: str) -> int:
    """Extract year from a month label like 'January 2026'.

    Args:
        month_label: Month label string (e.g., "January 2026")

    Returns:
        Year as integer, falls back to current year
    """
    parts = month_label.split()
    if len(parts) >= 2:
        try:
            return int(parts[-1])
        except ValueError:
            pass
    return datetime.now().year


def sheet_range(tab_name: str, range_str: str) -> str:
    """Build a Sheets API range prefixed with the tab name.

    Args:
        tab_name: Tab/sheet name (e.g., "2026 Review"). Empty string = default tab.
        range_str: Base range string (e.g., "A:A", "A1:Z15")

    Returns:
        Prefixed range like "'2026 Review'!A:A", or plain range if tab_name is empty
    """
    if tab_name:
        return f"'{tab_name}'!{range_str}"
    return range_str


def find_year_tab(service, spreadsheet_id: str, year: int) -> tuple:
    """Find the tab whose title contains the given year.

    GTR spreadsheets use year-based tab names with varying formats
    (e.g., "2026 Review", "Review 2026", "2026 review").
    This searches all tabs for one containing the year string.

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        year: Year to search for (e.g., 2026)

    Returns:
        Tuple of (tab_name, sheet_id). Falls back to first tab if no match.
    """
    year_str = str(year)
    try:
        metadata = (
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets.properties.sheetId,sheets.properties.title",
            )
            .execute()
        )
        sheets = metadata.get("sheets", [])

        for s in sheets:
            props = s.get("properties", {})
            title = str(props.get("title", ""))
            if year_str in title:
                sid = int(props["sheetId"])
                LOGGER.info(f"Found year tab: '{title}' (sheetId={sid})")
                return title, sid

        # Fallback to first sheet
        if sheets:
            props = sheets[0].get("properties", {})
            title = str(props.get("title", ""))
            sid = int(props["sheetId"])
            LOGGER.warning(f"No tab containing '{year_str}' found, using first tab: '{title}'")
            return title, sid
    except Exception as e:
        LOGGER.warning(f"Failed to find year tab for '{year_str}': {e}")

    return "", 0


def matches_month_review(cell_text: str, month_label: str) -> bool:
    """Check if cell_text matches a month review header.

    Handles: "December 2025 Review", "Dec. 2025 Review", "Dec 2025 Review"

    Args:
        cell_text: Text from the sheet cell
        month_label: Month label like "December 2025"

    Returns:
        True if the cell text matches the month review pattern
    """
    cell_lower = cell_text.strip().lower()
    # Try full name match first
    if f"{month_label.lower()} review" in cell_lower:
        return True
    # Try abbreviated match (e.g., "dec. 2025 review", "dec 2025 review")
    parts = month_label.split()  # ["December", "2025"]
    if len(parts) == 2:
        month_name, year = parts
        abbr = MONTH_ABBREVIATIONS.get(month_name.lower(), "")
        if abbr and abbr in cell_lower and year in cell_lower and "review" in cell_lower:
            return True
    return False


def get_previous_review_month_label() -> str:
    """Get the month label for the month before the review period.

    If we're reviewing January 2026, we need December 2025's pending actions.

    Returns:
        Month label string like "December 2025"
    """
    now = datetime.now()

    # The review is for the previous month
    if now.month == 1:
        review_month = 12
        review_year = now.year - 1
    else:
        review_month = now.month - 1
        review_year = now.year

    # The previous review was for the month before that
    if review_month == 1:
        prev_review_month = 12
        prev_review_year = review_year - 1
    else:
        prev_review_month = review_month - 1
        prev_review_year = review_year

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
    return f"{month_names[prev_review_month]} {prev_review_year}"


def find_review_section_range(
    values: List[List[str]],
    month_label: str,
) -> Optional[Tuple[int, int]]:
    """Find the row range for a review section.

    Args:
        values: 2D list of cell values from the sheet
        month_label: Month label to search for (e.g., "December 2025")

    Returns:
        Tuple of (start_row, end_row) 0-based indices, or None if not found
    """
    start_row = None

    for i, row in enumerate(values):
        if row and len(row) > 0:
            first_cell = str(row[0]).strip()

            # Found start of target section
            if matches_month_review(first_cell, month_label):
                start_row = i
                continue

            # If we've found start, look for end (next "[Month] Review" header)
            if start_row is not None and "review" in first_cell.lower():
                if re.search(
                    r"\b(Jan\.?|Feb\.?|Mar\.?|Apr\.?|May\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Oct\.?|Nov\.?|Dec\.?|January|February|March|April|May|June|July|August|September|October|November|December)\b",
                    first_cell,
                    re.IGNORECASE,
                ):
                    # Found next month's review - end of our section
                    return start_row, i

    # If found start but no end, section goes to end of data
    if start_row is not None:
        return start_row, len(values)

    return None


def extract_pending_issues(
    values: List[List[str]],
    start_row: int,
    end_row: int,
) -> List[List[str]]:
    """Extract pending issues with all their associated columns.

    Looks for the "Pending Issues" header in column E and extracts
    all populated cells from column E onward for each row below it.
    This preserves whatever columns exist (E=Issues, F=Actions,
    G=Solutions, H=Responsible, I=Urgency, etc.) without hardcoding.

    Args:
        values: 2D list of cell values
        start_row: Start row of the section (0-based)
        end_row: End row of the section (0-based)

    Returns:
        List of rows, where each row is all populated cells from
        column E onward
    """
    pending_rows: List[List[str]] = []
    in_pending_section = False

    for i in range(start_row, min(end_row, len(values))):
        row = values[i] if i < len(values) else []

        # Check column E (index 4)
        if len(row) > COL_PENDING_ISSUES:
            cell_value = str(row[COL_PENDING_ISSUES]).strip()

            # Check if this is the header row
            if "pending" in cell_value.lower() and "issue" in cell_value.lower():
                in_pending_section = True
                continue

            # If in pending section, collect rows with non-empty col E
            if in_pending_section and cell_value:
                # Skip if it looks like a header/label
                if cell_value.lower() in ["pending issues", "new issues", "issues"]:
                    continue
                # Capture cols E through end of row (whatever is populated)
                row_data = [str(c).strip() if c else "" for c in row[COL_PENDING_ISSUES:]]
                # Trim trailing empty cells
                while row_data and not row_data[-1]:
                    row_data.pop()
                if row_data:
                    pending_rows.append(row_data)

    return pending_rows


async def fetch_pending_from_sheet(
    spreadsheet_id: str,
    month_label: str,
) -> Tuple[List[List[str]], Optional[str]]:
    """Fetch pending issues from a single sheet.

    Args:
        spreadsheet_id: Google Sheets spreadsheet ID
        month_label: Month label to search for (e.g., "December 2025")

    Returns:
        Tuple of (pending_issues rows, error_message or None)
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_sheets_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return [], "Google Sheets integration not configured"

    try:
        credentials = get_sheets_credentials()
        service = build("sheets", "v4", credentials=credentials)

        # Find the correct year tab (e.g., "2025 Review", "Review 2025")
        year = extract_year_from_month_label(month_label)
        tab_name, _ = find_year_tab(service, spreadsheet_id, year)

        # Read columns A through Z to capture all populated columns
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=sheet_range(tab_name, "A:Z"),
            )
            .execute()
        )

        values = result.get("values", [])

        if not values:
            LOGGER.warning(f"Sheet {spreadsheet_id} is empty")
            return [], None

        # Log all review-like headers found in column A for diagnostics
        review_headers = []
        for i, row in enumerate(values):
            if row and len(row) > 0:
                cell = str(row[0]).strip()
                if "review" in cell.lower():
                    review_headers.append(f"  row {i + 1}: '{cell}'")
        if review_headers:
            LOGGER.info(f"Review headers in sheet {spreadsheet_id}:\n" + "\n".join(review_headers))
        else:
            LOGGER.warning(f"No review headers found in sheet {spreadsheet_id}")

        # Find the previous month's review section
        section_range = find_review_section_range(values, month_label)

        if not section_range:
            LOGGER.info(f"No {month_label} review found in sheet {spreadsheet_id}")
            return [], None

        start_row, end_row = section_range
        LOGGER.info(f"Found {month_label} review at rows {start_row + 1} to {end_row}")

        # Extract pending issues from that section
        pending_issues = extract_pending_issues(values, start_row, end_row)
        LOGGER.info(f"Found {len(pending_issues)} pending issues")

        return pending_issues, None

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Error fetching from sheet {spreadsheet_id}: {error_msg}")

        if "404" in error_msg:
            return [], "Sheet not found"
        elif "403" in error_msg:
            return [], "Access denied"
        else:
            return [], f"Error: {error_msg}"


@register_step("fetch_pending_actions")
async def fetch_pending_actions(context: StepContext) -> StepResult:
    """Fetch pending issues from previous review sections.

    When grey_out_existing is True (redo mode), copies pending issues from
    the EXISTING review being replaced (same month).

    Otherwise, looks for the previous month's review to carry forward issues.

    Note: All columns from E onward are carried forward (issues,
    actions, solutions, responsible, urgency, etc.).

    In chat mode, this step is skipped as we're discussing existing review data.

    Args:
        context: Step execution context

    Returns:
        StepResult with pending_actions per grid
    """
    # Skip in chat mode - we're discussing existing review, not generating new data
    if context.get_state("chat_mode", False):
        LOGGER.debug("Chat mode - skipping fetch_pending_actions")
        return StepResult(
            data={"skipped": True},
            progress_message="Skipped (chat mode)",
        )

    grids_to_review = context.get_state("grids_to_review", [])

    if not grids_to_review:
        return StepResult.failure("No grids to review. Run resolve_grid_sheets first.")

    # Check if we're in redo mode (grey out existing)
    grey_out_existing = context.get_state("grey_out_existing", False)
    current_month_label = context.get_state("month_label", "")

    # Determine which month's pending issues to fetch
    if grey_out_existing and current_month_label:
        # Redo mode: copy from the existing review we're replacing
        target_month_label = current_month_label
        LOGGER.info(f"Redo mode: copying pending issues from existing {target_month_label} review")
    else:
        # New review: get from previous month
        target_month_label = get_previous_review_month_label()
        LOGGER.info(f"Looking for {target_month_label} pending issues to carry forward")

    # Send progress to user
    await context.send_progress_to_user(
        f"📋 Fetching pending issues from {target_month_label} reviews..."
    )

    import asyncio

    pending_actions: Dict[str, List[List[str]]] = {}
    errors: List[str] = []
    total_pending = 0

    # Build list of grids with spreadsheet IDs for parallel fetching
    grids_with_sheets = []
    for grid in grids_to_review:
        grid_name = grid["name"]
        spreadsheet_id = grid.get("spreadsheet_id", "")
        if not spreadsheet_id:
            LOGGER.warning(f"No spreadsheet ID for grid {grid_name}")
            continue
        grids_with_sheets.append((grid_name, spreadsheet_id))

    if grids_with_sheets:
        # Fetch all sheets in parallel
        fetch_tasks = [
            fetch_pending_from_sheet(spreadsheet_id, target_month_label)
            for _, spreadsheet_id in grids_with_sheets
        ]
        results = await asyncio.gather(*fetch_tasks, return_exceptions=True)

        for (grid_name, spreadsheet_id), result_or_exc in zip(grids_with_sheets, results):
            if isinstance(result_or_exc, BaseException):
                errors.append(f"{grid_name}: {result_or_exc}")
                continue

            issues, error = result_or_exc
            if error:
                errors.append(f"{grid_name}: {error}")
            elif issues:
                pending_actions[grid_name] = issues
                total_pending += len(issues)
                LOGGER.info(f"  {grid_name}: {len(issues)} pending issues")
            else:
                pending_actions[grid_name] = []
                LOGGER.info(f"  {grid_name}: No pending issues")

    # Build summary message
    if total_pending > 0:
        if grey_out_existing:
            message = f"Copied {total_pending} pending issue(s) from existing review"
        else:
            message = f"Found {total_pending} pending issue(s) to carry forward"
    else:
        message = "No pending issues found"

    if errors:
        message += f" ({len(errors)} sheet(s) had errors)"

    return StepResult(
        data={
            "pending_actions": pending_actions,
            "source_month_label": target_month_label,
        },
        state_updates={
            "pending_actions": pending_actions,
            "source_month_label": target_month_label,
        },
        progress_message=message,
    )
