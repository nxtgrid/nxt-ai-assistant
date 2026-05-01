"""Write review section step handler for GTR Expert.

This handler writes the new month's review section to each grid's
Google Sheet with proper structure and formatting.
"""

import copy
import json
import re
from typing import Any, Dict, List, Optional

from orchestrator.experts.handlers.grids_technical_reviewer.fetch_pending_actions import (
    extract_year_from_month_label,
    find_year_tab,
    sheet_range,
)
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Grey color for old reviews (light grey)
GREY_BACKGROUND = {
    "red": 0.88,
    "green": 0.88,
    "blue": 0.88,
}

# Red color for "Not reviewed by engineering" tag
RED_TEXT = {
    "red": 1.0,
    "green": 0.0,
    "blue": 0.0,
}

# Review section structure (15-17 rows, columns A-I)
# Row 1:  [Month Year] Review ⚠️ Not reviewed by engineering | | | | Actions | | | |
# Row 2:  KPI | Value | Commentary (highlight any changes to grid / outcomes) | | New Issues | Actions Taken | Long Term Solution | Responsible | Urgent?
# Row 3:  FS Hours | [value] | [commentary] | | | | | | |
# Row 4:  HPS Hours | [value] | [commentary] | | | | | | |
# Row 5:  'Financial' CUF - 90 days | [value] | [commentary] | | | | | | |
# Row 6:  Technical Downtime (days) | [value] | [commentary] | | | | | | |
# Row 7:  Tickets/week | [value] | | | | | | | |
# Row 8:  Notes: | | | | Pending Issues | Actions Taken | | Responsible | Urgency/Impact
# Row 9+: [note/pending item rows with all columns from previous review]
# Row N:  [empty gap rows before next month]

# Number of columns to write (A through I = 9 columns)
NUM_COLUMNS = 9


def build_review_section_data(
    month_label: str,
    kpi_data: Dict[str, Any],
    kpi_commentary: Dict[str, str],
    pending_actions: List[List[str]],
) -> List[List[str]]:
    """Build the 2D array of cell values for a review section.

    Args:
        month_label: Month label (e.g., "January 2026")
        kpi_data: KPI values for this grid
        kpi_commentary: Commentary for each KPI from LLM analysis
        pending_actions: List of pending issue rows (each row is cells from col E onward)

    Returns:
        2D list of cell values for the review section
    """
    rows = []

    # Row 1: Header with warning tag in col A (merged cell), "Actions" super-header in col E
    # The warning tag must be manually deleted by an engineer after review
    rows.append(
        [
            f"{month_label} Review  ⚠️ Not reviewed by engineering",
            "",
            "",
            "",
            "Actions",
            "",
            "",
            "",
            "",
        ]
    )

    # Row 2: Column headers
    rows.append(
        [
            "KPI",
            "Value",
            "Commentary (highlight any changes to grid / outcomes)",
            "",
            "New Issues",
            "Actions Taken",
            "Long Term Solution",
            "Responsible",
            "Urgent?",
        ]
    )

    # Extract KPI values
    fs_hours = _get_kpi_value(kpi_data, "fs_hours", "service_uptime")
    hps_hours = _get_kpi_value(kpi_data, "hps_hours", "service_uptime")
    financial_cuf = _get_kpi_value(kpi_data, "financial_cuf")
    downtime_days = _get_kpi_value(kpi_data, "downtime_days")
    tickets_total = _get_kpi_value(kpi_data, "tickets_total")

    # Calculate tickets/week (assuming 4.3 weeks per month)
    tickets_per_week = ""
    if tickets_total:
        try:
            tickets_per_week = f"{float(tickets_total) / 4.3:.1f}"
        except (ValueError, TypeError):
            tickets_per_week = str(tickets_total)

    # Row 3: FS Hours
    rows.append(
        [
            "FS Hours",
            _format_value(fs_hours, "h"),
            kpi_commentary.get("fs_hours", ""),
            "",
            "",  # New Issues - to be filled
            "",  # Actions Taken
            "",  # Long Term Solution
            "",
            "",
        ]
    )

    # Row 4: HPS Hours
    rows.append(
        [
            "HPS Hours",
            _format_value(hps_hours, "h"),
            kpi_commentary.get("hps_hours", ""),
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )

    # Row 5: Financial CUF
    rows.append(
        [
            "'Financial' CUF - 90 days",
            _format_value(financial_cuf, "%"),
            kpi_commentary.get("financial_cuf", ""),
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )

    # Row 6: Technical Downtime
    rows.append(
        [
            "Technical Downtime (days)",
            _format_value(downtime_days, " days"),
            kpi_commentary.get("downtime_days", ""),
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )

    # Row 7: Tickets/week
    rows.append(
        [
            "Tickets/week",
            tickets_per_week,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
        ]
    )

    # Row 8: Notes header with Pending Issues header
    rows.append(
        [
            "Notes:",
            "",
            "",
            "",
            "Pending Issues",
            "Actions Taken",
            "",
            "Responsible",
            "Urgency/Impact",
        ]
    )

    # Rows 9+: Pending issues carried forward with all their columns
    # Add at least 3 rows for pending (or more if needed)
    pending_row_count = max(3, len(pending_actions))
    for i in range(pending_row_count):
        if i < len(pending_actions):
            pending_row_data = pending_actions[i]  # List of cells from col E onward
            # Build row: cols A-D empty, then pending data from col E
            row = ["", "", "", ""] + pending_row_data
            # Pad to at least NUM_COLUMNS
            while len(row) < NUM_COLUMNS:
                row.append("")
        else:
            row = [""] * NUM_COLUMNS
        rows.append(row)

    # Add 2 empty gap rows (9 columns each)
    empty_row = [""] * NUM_COLUMNS
    rows.append(empty_row[:])
    rows.append(empty_row[:])

    return rows


def _get_kpi_value(kpi_data: Dict[str, Any], *keys: str) -> Optional[Any]:
    """Get KPI value from data, trying multiple keys.

    Args:
        kpi_data: KPI data dictionary
        *keys: Keys to try in order

    Returns:
        Value if found, None otherwise
    """
    for key in keys:
        if key in kpi_data:
            value = kpi_data[key]
            if isinstance(value, dict):
                return value.get("value")
            return value
    return None


def _format_value(value: Optional[Any], suffix: str = "") -> str:
    """Format a KPI value for display.

    Args:
        value: Value to format
        suffix: Suffix to add (e.g., "h", "%")

    Returns:
        Formatted string
    """
    if value is None:
        return ""

    try:
        num_value = float(value)
        if num_value == int(num_value):
            formatted = str(int(num_value))
        else:
            formatted = f"{num_value:.1f}"
        return f"{formatted}{suffix}"
    except (ValueError, TypeError):
        return str(value)


def parse_commentary_from_llm_response(
    response_text: str,
    grid_names: List[str],
) -> Dict[str, Dict[str, str]]:
    """Parse KPI commentary from LLM response text.

    Tries JSON extraction first (```json blocks or inline JSON), then
    line-by-line parsing for "kpi_key: commentary" patterns grouped by grid name.

    Args:
        response_text: Raw LLM response text (from analyze_kpi step)
        grid_names: List of grid names to look for

    Returns:
        Dict mapping grid_name -> {kpi_key: commentary_text}
    """
    VALID_KPI_KEYS = {"fs_hours", "hps_hours", "financial_cuf", "downtime_days"}

    # --- Attempt 1: JSON extraction ---
    # Look for ```json blocks
    json_match = re.search(r"```(?:json)?\s*\n?({[\s\S]*?})\s*\n?```", response_text)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            result = _extract_commentary_from_json(parsed, grid_names, VALID_KPI_KEYS)
            if result:
                LOGGER.info("Parsed commentary from JSON code fence")
                return result
        except json.JSONDecodeError:
            pass

    # Try inline JSON (look for {"kpi_commentary": ...} pattern)
    json_match = re.search(r'(\{"kpi_commentary"[\s\S]*?\}(?:\s*\})*)', response_text)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            result = _extract_commentary_from_json(parsed, grid_names, VALID_KPI_KEYS)
            if result:
                LOGGER.info("Parsed commentary from inline JSON")
                return result
        except json.JSONDecodeError:
            pass

    # --- Attempt 2: Line-by-line parsing ---
    result = _parse_commentary_lines(response_text, grid_names, VALID_KPI_KEYS)
    if result:
        LOGGER.info("Parsed commentary from line-by-line parsing")
    return result


def _extract_commentary_from_json(
    parsed: Dict[str, Any],
    grid_names: List[str],
    valid_keys: set,
) -> Dict[str, Dict[str, str]]:
    """Extract commentary dict from parsed JSON.

    Handles two formats:
    - {"kpi_commentary": {"GridName": {"fs_hours": "...", ...}}}
    - {"GridName": {"fs_hours": "...", ...}}
    """
    source = parsed.get("kpi_commentary", parsed)
    if not isinstance(source, dict):
        return {}

    result: Dict[str, Dict[str, str]] = {}
    # Build case-insensitive grid name lookup
    grid_lookup = {name.lower(): name for name in grid_names}

    for key, value in source.items():
        if not isinstance(value, dict):
            continue
        # Match grid name (case-insensitive)
        matched_name = grid_lookup.get(key.lower(), key)
        commentary: Dict[str, str] = {}
        for kpi_key, text in value.items():
            normalized = kpi_key.lower().replace(" ", "_")
            if normalized in valid_keys and isinstance(text, str):
                commentary[normalized] = text
        if commentary:
            result[matched_name] = commentary

    return result


def _parse_commentary_lines(
    text: str,
    grid_names: List[str],
    valid_keys: set,
) -> Dict[str, Dict[str, str]]:
    """Parse commentary from line-by-line text output.

    Expects grid headers (## GridName or **GridName**) followed by
    kpi_key: commentary lines.
    """
    result: Dict[str, Dict[str, str]] = {}
    grid_lookup = {name.lower(): name for name in grid_names}

    # KPI key aliases for flexible matching
    kpi_aliases = {
        "fs_hours": "fs_hours",
        "fs hours": "fs_hours",
        "full service": "fs_hours",
        "hps_hours": "hps_hours",
        "hps hours": "hps_hours",
        "high partial": "hps_hours",
        "financial_cuf": "financial_cuf",
        "financial cuf": "financial_cuf",
        "cuf": "financial_cuf",
        "downtime_days": "downtime_days",
        "downtime days": "downtime_days",
        "technical downtime": "downtime_days",
        "downtime": "downtime_days",
    }

    current_grid = None
    lines = text.split("\n")

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Check for grid header: ## GridName, **GridName**, or "GridName:"
        header_match = re.match(r"^(?:#{1,3}\s+|\*\*)(.*?)(?:\*\*|:?\s*$)", stripped)
        if header_match:
            candidate = header_match.group(1).strip()
            matched = grid_lookup.get(candidate.lower())
            if matched:
                current_grid = matched
                if current_grid not in result:
                    result[current_grid] = {}
                continue

        # Check for kpi_key: commentary or - kpi_key: commentary
        if current_grid:
            kpi_match = re.match(r"^[-•*]?\s*([A-Za-z_ ]+?)(?:\s*[-–:]\s+)(.+)$", stripped)
            if kpi_match:
                raw_key = kpi_match.group(1).strip().lower()
                commentary_text = kpi_match.group(2).strip()
                normalized = kpi_aliases.get(raw_key)
                if normalized and commentary_text:
                    result[current_grid][normalized] = commentary_text

    return result


async def grey_out_existing_section(
    service,
    spreadsheet_id: str,
    existing_info: Dict[str, Any],
    sheet_id: int = 0,
    section_rows: Optional[int] = None,
) -> bool:
    """Apply grey background to an existing review section.

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        existing_info: Dict with 'row_index' of existing section
        sheet_id: Actual sheet ID from find_year_tab()
        section_rows: Number of rows to grey out. If None, defaults to 15.

    Returns:
        True if successful
    """
    row_index = existing_info.get("row_index", 0)

    if section_rows is None:
        section_rows = 15

    try:
        # Build batch update request for background color
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index,
                        "endRowIndex": row_index + section_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": NUM_COLUMNS,  # Columns A-I
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "backgroundColor": GREY_BACKGROUND,
                        }
                    },
                    "fields": "userEnteredFormat.backgroundColor",
                }
            }
        ]

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

        LOGGER.info(f"Greyed out rows {row_index + 1} to {row_index + section_rows + 1}")
        return True

    except Exception as e:
        LOGGER.error(f"Failed to grey out section: {e}")
        return False


async def apply_red_tag_formatting(
    service,
    spreadsheet_id: str,
    row_index: int,
    sheet_id: int = 0,
) -> bool:
    """Apply red text formatting to the 'Not reviewed by engineering' tag.

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        row_index: 1-indexed row number where the header is
        sheet_id: Actual sheet ID from find_year_tab()

    Returns:
        True if successful
    """
    try:
        # Apply red bold formatting to column A (index 0) of the header row.
        # IMPORTANT: use specific field paths so we only touch color and bold,
        # preserving font size / family that copyPaste already applied from
        # the template.  "userEnteredFormat.textFormat" (without sub-paths)
        # replaces the ENTIRE textFormat, resetting font size to default.
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": row_index - 1,  # Convert to 0-indexed
                        "endRowIndex": row_index,
                        "startColumnIndex": 0,  # Column A (0-indexed)
                        "endColumnIndex": 1,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "textFormat": {
                                "foregroundColor": RED_TEXT,
                                "bold": True,
                            }
                        }
                    },
                    "fields": "userEnteredFormat.textFormat.foregroundColor,"
                    "userEnteredFormat.textFormat.bold",
                }
            }
        ]

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

        LOGGER.info(f"Applied red formatting to 'Not reviewed' tag at row {row_index}")
        return True

    except Exception as e:
        LOGGER.warning(f"Failed to apply red tag formatting: {e}")
        # Non-fatal - the tag text is still there, just not red
        return False


def _find_template_row(
    service,
    spreadsheet_id: str,
    tab_name: str,
) -> Optional[int]:
    """Find a row from an existing review to use as formatting template.

    Searches for any previous "Review" header row (e.g., "December 2025 Review").

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        tab_name: Tab name to search in (e.g., "2026 Review")

    Returns:
        0-indexed row number if found, None otherwise
    """
    try:
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

        # Search for any existing review header (from bottom up to find most recent)
        for i in range(len(values) - 1, -1, -1):
            if values[i] and "Review" in str(values[i][0]):
                LOGGER.info(f"Found template review at row {i + 1}: {values[i][0]}")
                return i

        LOGGER.warning("No existing review header found for formatting template")
        return None

    except Exception as e:
        LOGGER.warning(f"Failed to find template row: {e}")
        return None


async def _ungroup_rows(
    service,
    spreadsheet_id: str,
    sheet_id: int,
    start_row: int,
    end_row: int,
) -> bool:
    """Remove any row groups overlapping the given range and expand collapsed rows.

    Google Sheets copyPaste can fail or produce incorrect results when source
    rows are in a collapsed group. This function fetches the sheet metadata,
    finds any row groups that overlap [start_row, end_row), deletes them, and
    ensures the rows are visible (not hidden by collapsed groups).

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        sheet_id: Numeric sheet/tab ID
        start_row: 0-indexed start row (inclusive)
        end_row: 0-indexed end row (exclusive)

    Returns:
        True if groups were removed (or none existed), False on error
    """
    try:
        # Fetch sheet metadata to find existing row groups
        resp = (
            service.spreadsheets()
            .get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties.sheetId,rowGroups)",
            )
            .execute()
        )

        # Find the target sheet's row groups
        groups_to_delete = []
        for sheet in resp.get("sheets", []):
            if sheet.get("properties", {}).get("sheetId") != sheet_id:
                continue
            for group in sheet.get("rowGroups", []):
                grp_range = group.get("range", {})
                grp_start = grp_range.get("startIndex", 0)
                grp_end = grp_range.get("endIndex", 0)
                # Check if this group overlaps our target range
                if grp_start < end_row and grp_end > start_row:
                    groups_to_delete.append(grp_range)

        if not groups_to_delete:
            LOGGER.debug(f"No row groups overlap rows {start_row}-{end_row}")
            return True

        LOGGER.info(
            f"Found {len(groups_to_delete)} row group(s) overlapping rows "
            f"{start_row}-{end_row}, removing them"
        )

        # Build deleteDimensionGroup requests for each overlapping group
        requests = []
        for grp_range in groups_to_delete:
            requests.append(
                {
                    "deleteDimensionGroup": {
                        "range": {
                            "sheetId": sheet_id,
                            "dimension": "ROWS",
                            "startIndex": grp_range.get("startIndex", 0),
                            "endIndex": grp_range.get("endIndex", 0),
                        }
                    }
                }
            )

        # Also unhide any rows that were hidden by the collapsed group
        unhide_request: Dict[str, Any] = {
            "updateDimensionProperties": {
                "range": {
                    "sheetId": sheet_id,
                    "dimension": "ROWS",
                    "startIndex": start_row,
                    "endIndex": end_row,
                },
                "properties": {"hiddenByUser": False},
                "fields": "hiddenByUser",
            }
        }
        requests.append(unhide_request)

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

        LOGGER.info(f"Removed {len(groups_to_delete)} row group(s) and unhid rows")
        return True

    except Exception as e:
        LOGGER.warning(f"Failed to ungroup rows {start_row}-{end_row}: {e}")
        # Non-fatal — copyPaste may still work, just with potential formatting issues
        return False


async def copy_formatting_from_template(
    service,
    spreadsheet_id: str,
    template_row: int,
    target_row: int,
    num_rows: int,
    sheet_id: int = 0,
) -> bool:
    """Copy formatting from a template review section to the new section.

    Copies borders, background colors, text formatting, and column widths
    from an existing review section to the newly written section.

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        template_row: 0-indexed row of template section header
        target_row: 0-indexed row of new section header
        num_rows: Number of rows to copy formatting for
        sheet_id: Actual sheet ID from find_year_tab()

    Returns:
        True if successful
    """
    try:
        # Use copyPaste request to copy formatting (not values) from template to target
        requests = [
            {
                "copyPaste": {
                    "source": {
                        "sheetId": sheet_id,
                        "startRowIndex": template_row,
                        "endRowIndex": template_row + num_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": NUM_COLUMNS,
                    },
                    "destination": {
                        "sheetId": sheet_id,
                        "startRowIndex": target_row,
                        "endRowIndex": target_row + num_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": NUM_COLUMNS,
                    },
                    "pasteType": "PASTE_FORMAT",  # Only copy formatting, not values
                }
            }
        ]

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

        LOGGER.info(
            f"Copied formatting from rows {template_row + 1}-{template_row + num_rows + 1} "
            f"to rows {target_row + 1}-{target_row + num_rows + 1}"
        )
        return True

    except Exception as e:
        LOGGER.warning(f"Failed to copy formatting from template: {e}")
        # Non-fatal - the data is still written
        return False


async def apply_word_wrap(
    service,
    spreadsheet_id: str,
    start_row: int,
    num_rows: int,
    sheet_id: int = 0,
) -> bool:
    """Apply word wrap formatting to cells in the review section.

    Enables text wrapping for all cells so long content displays properly.

    Args:
        service: Google Sheets API service
        spreadsheet_id: Spreadsheet ID
        start_row: 0-indexed starting row
        num_rows: Number of rows to format
        sheet_id: Actual sheet ID from find_year_tab()

    Returns:
        True if successful
    """
    try:
        requests = [
            {
                "repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": start_row,
                        "endRowIndex": start_row + num_rows,
                        "startColumnIndex": 0,
                        "endColumnIndex": NUM_COLUMNS,
                    },
                    "cell": {
                        "userEnteredFormat": {
                            "wrapStrategy": "WRAP",
                        }
                    },
                    "fields": "userEnteredFormat.wrapStrategy",
                }
            }
        ]

        service.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"requests": requests},
        ).execute()

        LOGGER.debug(f"Applied word wrap to rows {start_row + 1}-{start_row + num_rows + 1}")
        return True

    except Exception as e:
        LOGGER.warning(f"Failed to apply word wrap: {e}")
        return False


async def write_section_to_sheet(
    spreadsheet_id: str,
    section_data: List[List[str]],
    grey_out_existing: bool,
    existing_info: Optional[Dict[str, Any]],
    review_year: int,
) -> tuple[bool, Optional[str]]:
    """Write the review section to a Google Sheet.

    Writes data to columns A-I and copies formatting from the most recent
    existing review section to maintain consistent styling.

    Args:
        spreadsheet_id: Google Sheets spreadsheet ID
        section_data: 2D list of cell values to write
        grey_out_existing: Whether to grey out existing section first
        existing_info: Info about existing section (if any)
        review_year: Year of the review month (used to select the correct tab)

    Returns:
        Tuple of (success, error_message)
    """
    try:
        from googleapiclient.discovery import build

        from shared.utils.google_auth import get_sheets_write_credentials
    except ImportError as e:
        LOGGER.error(f"Google API packages not available: {e}")
        return False, "Google Sheets integration not configured"

    try:
        credentials = get_sheets_write_credentials()
        service = build("sheets", "v4", credentials=credentials)

        # Find the correct year tab (e.g., "2026 Review", "Review 2026")
        # Returns both the tab name (for range strings) and sheet ID (for batchUpdate)
        tab_name, sheet_id = find_year_tab(service, spreadsheet_id, review_year)

        # Find a template row for formatting BEFORE greying out
        # (grey-out destroys borders/bold, so we must capture the template first)
        template_row = _find_template_row(service, spreadsheet_id, tab_name)

        # Find the last row with data across ALL review columns (A-I)
        # Reading only A:A misses pending issue rows that have data in E-I but not A,
        # which causes the new section to overlap existing content.
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
        last_row = len(values)

        LOGGER.info(f"Last row with data (A:I) in '{tab_name}' tab: {last_row}")

        # Add a gap row before the new section (leave one blank row)
        insert_row = last_row + 2  # 1-indexed, plus 1 blank row gap

        # Write the new section to columns A-I in the correct year tab
        range_notation = sheet_range(
            tab_name, f"A{insert_row}:I{insert_row + len(section_data) - 1}"
        )

        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range=range_notation,
            valueInputOption="RAW",
            body={"values": section_data},
        ).execute()

        LOGGER.info(f"Wrote review section starting at row {insert_row} (columns A-I)")

        # Number of rows in the review section (excluding trailing gap rows)
        content_rows = len(section_data) - 2  # Exclude the 2 gap rows at end

        # Copy formatting from template BEFORE greying out the old section
        # (copyPaste PASTE_FORMAT copies borders, bold, colors from the source;
        # if we grey out first, we'd copy grey formatting instead of the original)
        if template_row is not None:
            # Ungroup/uncollapse template rows first — copyPaste fails on collapsed groups
            await _ungroup_rows(
                service,
                spreadsheet_id,
                sheet_id=sheet_id,
                start_row=template_row,
                end_row=template_row + content_rows,
            )

            await copy_formatting_from_template(
                service,
                spreadsheet_id,
                template_row=template_row,
                target_row=insert_row - 1,  # Convert to 0-indexed
                num_rows=content_rows,
                sheet_id=sheet_id,
            )

            # Remove any groups on the newly written rows (they can inherit grouping
            # from adjacent grouped ranges or from the copyPaste operation)
            await _ungroup_rows(
                service,
                spreadsheet_id,
                sheet_id=sheet_id,
                start_row=insert_row - 1,  # 0-indexed
                end_row=insert_row - 1 + content_rows,
            )

        # NOW grey out existing section (after we've already copied its formatting)
        # Use actual section size: from existing row_index to last data row,
        # NOT a hardcoded 15 which bleeds into the spacer rows.
        if grey_out_existing and existing_info:
            old_row_index = existing_info.get("row_index", 0)
            old_section_rows = last_row - old_row_index  # exact content extent
            await grey_out_existing_section(
                service,
                spreadsheet_id,
                existing_info,
                sheet_id=sheet_id,
                section_rows=old_section_rows,
            )

        # Apply word wrap to all cells (ensures long text displays properly)
        await apply_word_wrap(
            service,
            spreadsheet_id,
            start_row=insert_row - 1,  # Convert to 0-indexed
            num_rows=content_rows,
            sheet_id=sheet_id,
        )

        # Apply red formatting to the "Not reviewed by engineering" tag
        await apply_red_tag_formatting(service, spreadsheet_id, insert_row, sheet_id=sheet_id)

        return True, None

    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Error writing to sheet {spreadsheet_id}: {error_msg}")

        if "404" in error_msg:
            return False, "Sheet not found"
        elif "403" in error_msg:
            return False, "Access denied - check write permissions"
        else:
            return False, f"Error: {error_msg}"


@register_step("write_review_section")
async def write_review_section(context: StepContext) -> StepResult:
    """Write the new month's review section to each grid's sheet.

    Creates the structured review section with:
    - Month header with "Not reviewed by engineering" tag (red)
    - KPI values and commentary
    - Pending issues carried forward from previous month
    - Empty rows for new issues and actions

    Optionally greys out existing review sections.

    In chat mode, this step is skipped as we're discussing existing review data.

    Args:
        context: Step execution context

    Returns:
        StepResult with write status per grid
    """
    # Skip in chat mode - we're discussing existing review, not writing new data
    if context.get_state("chat_mode", False):
        LOGGER.debug("Chat mode - skipping write_review_section")
        return StepResult(
            data={"skipped": True},
            progress_message="Skipped (chat mode)",
        )

    grids_to_review = context.get_state("grids_to_review", [])
    month_label = context.get_state("month_label", "")
    kpi_data = context.get_state("kpi_data", {})
    pending_actions = context.get_state("pending_actions", {})
    grey_out_existing = context.get_state("grey_out_existing", False)
    existing_reviews = context.get_state("existing_reviews", {})

    # Get KPI commentary from the analyze_kpi LLM step
    analyze_result = context.get_previous_result("analyze_kpi") or {}
    all_commentary = analyze_result.get("kpi_commentary", {})

    # Fallback: LLM stores result as {"response": "...text..."}, not {"kpi_commentary": {...}}
    if not all_commentary and "response" in analyze_result:
        grid_names = [g["name"] for g in grids_to_review]
        all_commentary = parse_commentary_from_llm_response(analyze_result["response"], grid_names)
        LOGGER.info(f"Parsed commentary from LLM response for {len(all_commentary)} grid(s)")

    if not grids_to_review:
        return StepResult.failure("No grids to review")

    if not month_label:
        return StepResult.failure("Month label not set. Run check_existing_review first.")

    # Extract year from month label to target the correct year tab
    review_year = extract_year_from_month_label(month_label)

    # Send progress message before long operation
    await context.send_progress_to_user(
        f"Writing {month_label} review sections to {len(grids_to_review)} sheet(s)..."
    )

    write_results: Dict[str, Dict[str, Any]] = {}
    success_count = 0
    error_count = 0

    for grid in grids_to_review:
        grid_name = grid["name"]
        grid_lower = grid_name.lower()
        spreadsheet_id = grid.get("spreadsheet_id", "")

        if not spreadsheet_id:
            LOGGER.warning(f"No spreadsheet ID for grid {grid_name}")
            write_results[grid_name] = {"success": False, "error": "No spreadsheet ID"}
            error_count += 1
            continue

        # Deep copy per-grid data to prevent cross-grid mutation
        grid_kpi_data = copy.deepcopy(kpi_data.get(grid_lower, {}))
        grid_pending = copy.deepcopy(pending_actions.get(grid_name, []))
        grid_commentary = copy.deepcopy(all_commentary.get(grid_name, {}))
        existing_info = copy.deepcopy(existing_reviews.get(grid_name))

        try:
            # Build section data (uses deep-copied data, safe from cross-grid leaks)
            section_data = build_review_section_data(
                month_label=month_label,
                kpi_data=grid_kpi_data,
                kpi_commentary=grid_commentary,
                pending_actions=grid_pending,
            )

            # Write to sheet (year tab selected automatically)
            success, error = await write_section_to_sheet(
                spreadsheet_id=spreadsheet_id,
                section_data=section_data,
                grey_out_existing=grey_out_existing,
                existing_info=existing_info,
                review_year=review_year,
            )

            if success:
                write_results[grid_name] = {
                    "success": True,
                    "spreadsheet_id": spreadsheet_id,
                    "url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
                }
                success_count += 1
            else:
                write_results[grid_name] = {"success": False, "error": error}
                error_count += 1
        except Exception as e:
            LOGGER.error(f"Unexpected error writing review for {grid_name}: {e}")
            write_results[grid_name] = {"success": False, "error": str(e)}
            error_count += 1

    # Build summary
    if success_count == len(grids_to_review):
        message = f"Successfully wrote review sections for all {success_count} grid(s)"
    elif success_count > 0:
        message = (
            f"Wrote {success_count}/{len(grids_to_review)} review sections ({error_count} failed)"
        )
    else:
        return StepResult.failure(
            "Failed to write any review sections. Errors:\n"
            + "\n".join(f"- {name}: {r.get('error')}" for name, r in write_results.items())
        )

    return StepResult(
        data={
            "write_results": write_results,
            "success_count": success_count,
            "error_count": error_count,
        },
        state_updates={
            "write_results": write_results,
        },
        progress_message=message,
    )
