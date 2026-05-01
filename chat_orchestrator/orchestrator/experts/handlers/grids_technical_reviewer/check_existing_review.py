"""Check existing review step handler for GTR Expert.

This handler checks if a review for the current month already exists
in the grid's Google Sheet and offers options:
1. Chat about the existing review (ask questions, analyze trends)
2. Grey out existing and create new version
3. Cancel and do something else
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

from orchestrator.experts.handlers.grids_technical_reviewer.fetch_pending_actions import (
    extract_year_from_month_label,
    find_year_tab,
    matches_month_review,
    sheet_range,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Reusable thread pool for Google API calls (which are synchronous)
_sheets_executor: Optional[ThreadPoolExecutor] = None


def _get_sheets_executor() -> ThreadPoolExecutor:
    """Get or create a thread pool executor for Sheets API calls."""
    global _sheets_executor
    if _sheets_executor is None:
        _sheets_executor = ThreadPoolExecutor(max_workers=5, thread_name_prefix="sheets_")
    return _sheets_executor


# Column indices (0-based) for the review structure
# A=KPI, B=Value, C=Commentary, D=empty, E=Issues, F=Actions, G=Solutions
COL_KPI = 0
COL_VALUE = 1
COL_COMMENTARY = 2
COL_PENDING_ISSUES = 4


def get_review_month_label() -> str:
    """Get the month label for the previous month's review.

    Reviews are for the previous month (e.g., running in Feb reviews Jan).

    Returns:
        Month label string like "January 2026"
    """
    now = datetime.now()

    # Get previous month
    if now.month == 1:
        review_month = 12
        review_year = now.year - 1
    else:
        review_month = now.month - 1
        review_year = now.year

    # Format as "Month Year"
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
    return f"{month_names[review_month]} {review_year}"


def find_review_row(
    values: List[List[str]],
    month_label: str,
) -> Optional[int]:
    """Find the row index where a review for the given month exists.

    Args:
        values: 2D list of cell values from the sheet
        month_label: Month label to search for (e.g., "January 2026")

    Returns:
        Row index (0-based) if found, None otherwise
    """
    for i, row in enumerate(values):
        if row and len(row) > 0:
            first_cell = str(row[0]).strip()
            if matches_month_review(first_cell, month_label):
                return i

    return None


def _check_sheet_sync(
    spreadsheet_id: str,
    month_label: str,
    review_year: int,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Synchronous helper to check a single sheet for existing review.

    This runs in a thread pool since Google API client is synchronous.

    Args:
        spreadsheet_id: Google Sheets spreadsheet ID
        month_label: Month label to search for
        review_year: Year of the review month (used to select the correct tab)

    Returns:
        Tuple of (exists, row_index, error_message)
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_sheets_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return False, None, "Google Sheets integration not configured"

    try:
        credentials = get_sheets_credentials()
        service = build("sheets", "v4", credentials=credentials)

        # Find the correct year tab (e.g., "2026 Review", "Review 2026")
        tab_name, _ = find_year_tab(service, spreadsheet_id, review_year)

        # Read first column to find existing reviews
        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=sheet_range(tab_name, "A:A"),
            )
            .execute()
        )

        values = result.get("values", [])

        # Search for existing review
        row_index = find_review_row(values, month_label)

        if row_index is not None:
            LOGGER.info(f"Found existing {month_label} review at row {row_index + 1}")
            return True, row_index, None
        else:
            return False, None, None

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Error checking sheet {spreadsheet_id}: {error_msg}")

        if "404" in error_msg:
            return False, None, "Sheet not found or not accessible"
        elif "403" in error_msg:
            return False, None, "Access denied to sheet"
        else:
            return False, None, f"Error reading sheet: {error_msg}"


async def check_sheet_for_existing_review(
    context: StepContext,
    spreadsheet_id: str,
    month_label: str,
    review_year: int,
) -> Tuple[bool, Optional[int], Optional[str]]:
    """Check a single sheet for existing review (async wrapper).

    Args:
        context: Step context with credentials
        spreadsheet_id: Google Sheets spreadsheet ID
        month_label: Month label to search for
        review_year: Year of the review month (used to select the correct tab)

    Returns:
        Tuple of (exists, row_index, error_message)
    """
    loop = asyncio.get_event_loop()
    executor = _get_sheets_executor()
    return await loop.run_in_executor(
        executor,
        partial(_check_sheet_sync, spreadsheet_id, month_label, review_year),
    )


async def read_existing_review_content(
    spreadsheet_id: str,
    row_index: int,
    month_label: str,
    review_year: int,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Read the content of an existing review section from the sheet.

    Extracts KPI values, commentary, and pending issues from an existing
    review section for chat mode context.

    Args:
        spreadsheet_id: Google Sheets spreadsheet ID
        row_index: Starting row index (0-based) of the review section
        month_label: Month label for logging
        review_year: Year of the review month (used to select the correct tab)

    Returns:
        Tuple of (review_content dict, error_message or None)
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_sheets_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return None, "Google Sheets integration not configured"

    try:
        credentials = get_sheets_credentials()
        service = build("sheets", "v4", credentials=credentials)

        # Find the correct year tab (e.g., "2026 Review", "Review 2026")
        tab_name, _ = find_year_tab(service, spreadsheet_id, review_year)

        # Read the review section (approximately 15 rows, all columns)
        # Row index is 0-based, Sheets API uses 1-based for A1 notation
        start_row = row_index + 1
        end_row = row_index + 16  # Read 15 rows
        range_notation = sheet_range(tab_name, f"A{start_row}:Z{end_row}")

        result = (
            service.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range=range_notation,
            )
            .execute()
        )

        values = result.get("values", [])

        if not values:
            LOGGER.warning(f"Empty review section at row {row_index + 1}")
            return {"kpis": {}, "pending_issues": [], "raw_rows": []}, None

        # Parse the review section
        review_content: Dict[str, Any] = {
            "month_label": month_label,
            "kpis": {},
            "pending_issues": [],
            "raw_rows": [],
        }

        # Row 0 is header (e.g., "January 2026 Review")
        # Row 1 is column headers (KPI, Value, Commentary, ...)
        # Rows 2-6 are KPI rows (FS Hours, HPS Hours, Financial CUF, etc.)
        # Row 7 is Notes header
        # Rows 8+ are pending issues

        for i, row in enumerate(values):
            # Store raw rows for full context
            if row:
                review_content["raw_rows"].append(row)

            # Skip header rows
            if i < 2:
                continue

            # Extract KPI data (rows 2-6 typically)
            if len(row) >= 3:
                kpi_name = str(row[COL_KPI]).strip() if row[COL_KPI] else ""
                kpi_value = (
                    str(row[COL_VALUE]).strip() if len(row) > COL_VALUE and row[COL_VALUE] else ""
                )
                commentary = (
                    str(row[COL_COMMENTARY]).strip()
                    if len(row) > COL_COMMENTARY and row[COL_COMMENTARY]
                    else ""
                )

                if kpi_name and kpi_name.lower() not in ["notes:", "kpi", ""]:
                    review_content["kpis"][kpi_name] = {
                        "value": kpi_value,
                        "commentary": commentary,
                    }

            # Extract pending issues (column E, rows after "Pending Issues" header)
            if len(row) > COL_PENDING_ISSUES:
                cell_value = str(row[COL_PENDING_ISSUES]).strip()
                if cell_value and cell_value.lower() not in [
                    "pending issues",
                    "new issues",
                    "actions",
                    "",
                ]:
                    review_content["pending_issues"].append(cell_value)

        LOGGER.info(
            f"Read existing review: {len(review_content['kpis'])} KPIs, "
            f"{len(review_content['pending_issues'])} pending issues"
        )

        return review_content, None

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Error reading review from sheet {spreadsheet_id}: {error_msg}")

        if "404" in error_msg:
            return None, "Sheet not found or not accessible"
        elif "403" in error_msg:
            return None, "Access denied to sheet"
        else:
            return None, f"Error reading sheet: {error_msg}"


@register_step("check_existing_review")
async def check_existing_review(context: StepContext) -> StepResult:
    """Check if review for current month already exists in the grid sheets.

    For each grid to review, checks if a "[Month Year] Review" section
    already exists. If found, prompts user with three options:
    1. Chat about the existing review (analyze, ask questions)
    2. Redo - grey out existing and create new version
    3. Cancel - do something else

    Args:
        context: Step execution context

    Returns:
        StepResult with existing review status per grid
    """
    grids_to_review = context.get_state("grids_to_review", [])

    if not grids_to_review:
        return StepResult.failure("No grids to review. Run resolve_grid_sheets first.")

    # Get the month we're reviewing and its year (for tab selection)
    month_label = get_review_month_label()
    review_year = extract_year_from_month_label(month_label)
    LOGGER.info(f"Checking for existing {month_label} reviews (year tab: {review_year})")

    # Check if we're resuming after user decision
    awaiting_grey_out_decision = context.get_state("awaiting_grey_out_decision")

    if awaiting_grey_out_decision and context.user_input:
        user_response = context.user_input.strip().lower()
        # Option 1: Analysis mode - load full year history for deep dive
        if user_response in ["1", "chat", "analyze"]:
            LOGGER.info("User chose analysis mode - entering GTR analysis conversation")

            return StepResult(
                data={"analysis_mode": True, "month_label": month_label},
                state_updates={
                    "analysis_mode": True,
                    "chat_mode": True,  # keeps downstream steps skipping
                    "month_label": month_label,
                    "awaiting_grey_out_decision": False,
                },
                progress_message="Entering analysis mode...",
            )

        # Option 2: Redo - grey out existing and create new
        elif user_response in ["2", "redo", "grey", "grey out"]:
            LOGGER.info("User chose to grey out existing reviews")
            return StepResult(
                data={
                    "grey_out_existing": True,
                    "month_label": month_label,
                },
                state_updates={
                    "grey_out_existing": True,
                    "month_label": month_label,
                    "awaiting_grey_out_decision": False,
                },
                progress_message="Will grey out existing reviews and create new sections",
            )

        # Option 3: Cancel - skip workflow
        elif user_response in ["3", "cancel", "no", "n", "skip"]:
            LOGGER.info("User cancelled - skipping GTR workflow")
            return StepResult(
                skip_remaining=True,
                progress_message="Cancelled. No changes made.",
            )

        else:
            # Note: New-request detection is handled centrally in expert_handler.py
            # If we got here, the input passed the centralized check but isn't a valid option
            # Ask again for a valid selection
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please choose:\n"
                    "1. **Chat** about this review (ask questions, analyze trends)\n"
                    "2. **Redo** - grey out existing and create new\n"
                    "3. **Cancel** - do something else"
                ),
            )

    # First run - check each grid for existing reviews IN PARALLEL
    existing_reviews: Dict[str, Dict[str, Any]] = {}
    errors: List[str] = []

    # Build list of grids to check (those with spreadsheet IDs)
    grids_with_sheets = []
    for grid in grids_to_review:
        grid_name = grid["name"]
        spreadsheet_id = grid.get("spreadsheet_id", "")
        if not spreadsheet_id:
            LOGGER.warning(f"No spreadsheet ID for grid {grid_name}")
            continue
        grids_with_sheets.append((grid_name, spreadsheet_id))

    if grids_with_sheets:
        # Run all sheet checks in parallel
        LOGGER.info(f"Checking {len(grids_with_sheets)} sheets in parallel for existing reviews")
        check_tasks = [
            check_sheet_for_existing_review(context, spreadsheet_id, month_label, review_year)
            for _, spreadsheet_id in grids_with_sheets
        ]
        results = await asyncio.gather(*check_tasks, return_exceptions=True)

        # Process results
        for (grid_name, spreadsheet_id), result in zip(grids_with_sheets, results):
            if isinstance(result, BaseException):
                errors.append(f"{grid_name}: {result}")
                continue

            # Type assertion for mypy - result is a tuple after Exception check
            exists, row_index, error = result  # type: ignore[misc]
            if error:
                errors.append(f"{grid_name}: {error}")
            elif exists:
                existing_reviews[grid_name] = {
                    "row_index": row_index,
                    "spreadsheet_id": spreadsheet_id,
                }

    # Handle errors
    if errors and not existing_reviews and len(errors) == len(grids_to_review):
        return StepResult.failure(
            "Could not check any sheets for existing reviews:\n" + "\n".join(errors)
        )

    # If existing reviews found, ask user what to do
    if existing_reviews:
        grid_names = list(existing_reviews.keys())
        grids_str = ", ".join(grid_names)

        LOGGER.info(f"Found existing reviews for: {grids_str}")

        return StepResult(
            state_updates={
                "existing_reviews": existing_reviews,
                "grids_with_existing_review": grid_names,
                "month_label": month_label,
                "awaiting_grey_out_decision": True,
            },
            needs_user_input=True,
            user_prompt=(
                f"**{month_label} Review** already exists for: {grids_str}\n\n"
                "How would you like to proceed?\n\n"
                "1. **Chat** about this review (ask questions, analyze trends)\n"
                "2. **Redo** - grey out existing and create new\n"
                "3. **Cancel** - do something else"
            ),
        )

    # No existing reviews - continue
    return StepResult(
        data={
            "grey_out_existing": False,
            "month_label": month_label,
            "existing_reviews": {},
        },
        state_updates={
            "grey_out_existing": False,
            "month_label": month_label,
            "existing_reviews": {},
        },
        progress_message=f"No existing {month_label} reviews found - ready to create new sections",
    )
