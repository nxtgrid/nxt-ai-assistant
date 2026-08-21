"""Google Sheets half of the annotation loop.

Locating: Spike 0 established that a Sheets comment's `anchor` field carries
an opaque numeric object ID ({"type":"workbook-range","uid":0,"range":
"361007030"}), NOT A1 notation -- there is no path from a comment to a cell
address through it. The only workable locator is to search every tab for the
comment's `quotedFileContent`, which Drive does populate for cell-anchored
comments, as `text/html`.

That has two consequences the caller must handle, both observed in real
production data during Spike 0:
  - an empty cell quotes nothing, so it is unlocatable (hence the
    "comment on a non-empty cell" contract)
  - a quote can go stale (cell edited after commenting) or match many
    cells at once -- so this module returns *all* matches and never guesses
"""

import asyncio
import html
import logging
from dataclasses import dataclass

from googleapiclient.discovery import build

from shared.utils.google_auth import get_sheets_credentials, get_sheets_write_credentials

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CellMatch:
    """One cell whose content equals a comment's quoted text."""

    tab: str
    a1: str
    row: int  # 1-based
    column: int  # 1-based


def index_to_column_letter(index: int) -> str:
    """0-based column index to spreadsheet column letter (0=A, 26=AA)."""
    result = ""
    index += 1
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        result = chr(ord("A") + remainder) + result
    return result


def _normalise(value: str) -> str:
    """HTML-unescape and trim, so an escaped quote matches a plain cell."""
    return html.unescape(str(value)).strip()


def find_cells_in_grids(grids: dict[str, list[list]], quoted_text: str) -> list[CellMatch]:
    """Every cell across every tab whose normalised content equals quoted_text.

    Pure function over already-fetched grids so it is unit-testable without
    touching the Sheets API. Returns [] for an empty needle rather than
    matching every empty cell in the workbook.
    """
    needle = _normalise(quoted_text)
    if not needle:
        return []

    matches: list[CellMatch] = []
    for tab, rows in grids.items():
        for row_idx, row in enumerate(rows):
            for col_idx, cell in enumerate(row):
                if _normalise(cell) == needle:
                    matches.append(
                        CellMatch(
                            tab=tab,
                            a1=f"{index_to_column_letter(col_idx)}{row_idx + 1}",
                            row=row_idx + 1,
                            column=col_idx + 1,
                        )
                    )
    return matches


async def fetch_all_grids(sheet_id: str) -> dict[str, list[list]]:
    """Every tab's values, fetched once per run and reused for every comment."""
    creds = get_sheets_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    meta = await asyncio.to_thread(
        lambda: service.spreadsheets()
        .get(spreadsheetId=sheet_id, fields="sheets(properties(title))")
        .execute()
    )
    tabs = [s["properties"]["title"] for s in meta.get("sheets", [])]

    grids: dict[str, list[list]] = {}
    for tab in tabs:
        try:
            resp = await asyncio.to_thread(
                lambda t=tab: service.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=f"'{t}'")
                .execute()
            )
            grids[tab] = resp.get("values", [])
        except Exception as e:
            LOGGER.warning(f"Could not read tab {tab!r} of {sheet_id}: {e}")
    return grids


async def write_cells(sheet_id: str, writes: list[tuple[str, str, object]]) -> int:
    """Batch-write (tab, a1, value) triples. Returns the number of cells updated."""
    if not writes:
        return 0

    creds = get_sheets_write_credentials()
    service = build("sheets", "v4", credentials=creds, cache_discovery=False)

    data = [{"range": f"'{tab}'!{a1}", "values": [[value]]} for tab, a1, value in writes]
    resp = await asyncio.to_thread(
        lambda: service.spreadsheets()
        .values()
        .batchUpdate(
            spreadsheetId=sheet_id,
            body={"valueInputOption": "USER_ENTERED", "data": data},
        )
        .execute()
    )
    return int(resp.get("totalUpdatedCells", 0))


def offset_a1(match: CellMatch, columns: int) -> str:
    """A1 address `columns` to the right of a match. Used by the label form,
    which writes beside the matched label rather than into it."""
    return f"{index_to_column_letter(match.column - 1 + columns)}{match.row}"
