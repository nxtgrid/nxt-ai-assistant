"""MCP Reference Server — Nigerian import regulatory data lookups.

Staff-only tools (visible_to_customer: false):
- lookup_tariff: fuzzy search the Nigeria import tariff Google Sheet
- get_import_prohibition_list: fetch prohibited items from customs.gov.ng
- lookup_import_standard: fuzzy search the Nigeria import standards PDF
"""

import asyncio
import io
import logging
import os
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from rapidfuzz import fuzz

from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.response_formatters import compose_error_response, compose_json_response

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("reference-server")

server = Server("reference-server")

# --- Cache ---
# Each cache is a tuple of (data, fetched_at_monotonic) or None
_tariff_cache: Optional[Tuple[List[Dict], float]] = None
_TARIFF_TTL = 15 * 60  # 15 minutes

_prohibition_cache: Optional[Tuple[Dict, float]] = None
_PROHIBITION_TTL = 60 * 60  # 1 hour

_standards_cache: Optional[Tuple[List[Dict], float]] = None
_STANDARDS_TTL = 60 * 60  # 1 hour

PROHIBITION_URL = "https://customs.gov.ng/?page_id=3075"


# --- CET normalization ---


def _normalize_cet(s: str) -> str:
    """Strip dots and spaces from a CET code for comparison."""
    return s.replace(".", "").replace(" ", "")


# --- Tariff matching ---


def _match_tariff(query: str, rows: List[Dict]) -> List[Dict]:
    """Find matching tariff rows for query.

    Priority: CET exact > CET prefix (up to 10) > description fuzzy (threshold 70, top 3 within 5pts).
    Returns list of row dicts with extra keys: matched_by, score.
    Returns [] if no match found.
    """
    norm_query = _normalize_cet(query)
    is_numeric_query = norm_query.isdigit()

    cet_prefix_matches: List[Dict] = []
    desc_scored: List[Dict] = []

    for row in rows:
        norm_code = _normalize_cet(row.get("cet_code", ""))

        # Exact CET match — return immediately, highest priority
        if is_numeric_query and norm_code == norm_query:
            return [{**row, "matched_by": "cet_exact", "score": 100}]

        # Prefix CET match (query is a prefix of stored code)
        if is_numeric_query and norm_code.startswith(norm_query):
            cet_prefix_matches.append({**row, "matched_by": "cet_prefix", "score": 90})

        # Description fuzzy match
        score = fuzz.WRatio(query.lower(), row.get("description", "").lower())
        if score >= 70:
            desc_scored.append({**row, "matched_by": "description", "score": score})

    if cet_prefix_matches:
        return cet_prefix_matches[:10]

    if desc_scored:
        desc_scored.sort(key=lambda r: r["score"], reverse=True)
        best = desc_scored[0]["score"]
        return [r for r in desc_scored if r["score"] >= best - 5][:3]

    return []


# --- Tariff sheet ---

SHEET_COLUMNS = ["cet_code", "description", "su", "id", "vat", "lvy", "exc", "dov"]


def _parse_tariff_sheet(raw_values: List[List[str]]) -> List[Dict]:
    """Convert raw Sheets API values (list-of-lists) to list of dicts.

    Finds the header row by looking for a row containing "CET" (case-insensitive).
    Skips empty rows.
    """
    if not raw_values:
        return []

    # Find header row: first row containing "cet" (case-insensitive)
    header_idx = 0
    for i, row in enumerate(raw_values):
        if any("cet" in str(cell).lower() for cell in row):
            header_idx = i
            break

    rows = []
    for raw_row in raw_values[header_idx + 1 :]:
        if not raw_row or all(str(c).strip() == "" for c in raw_row):
            continue
        entry = {}
        for j, key in enumerate(SHEET_COLUMNS):
            entry[key] = str(raw_row[j]).strip() if j < len(raw_row) else ""
        rows.append(entry)
    return rows


def _load_tariff_rows_sync() -> List[Dict]:
    """Synchronous sheet fetch — run via asyncio.to_thread.

    Reuses get_sheets_credentials() from shared.utils.google_auth — same pattern
    as gtr_sheet_reader.py and customer_mcp_server.py.
    """
    from googleapiclient.discovery import build

    from shared.utils.google_auth import get_sheets_credentials

    sheet_id = os.getenv("NIGERIA_IMPORT_TARIFF_SHEET_ID", "")
    if not sheet_id:
        raise ValueError("NIGERIA_IMPORT_TARIFF_SHEET_ID not configured")

    credentials = get_sheets_credentials()
    service = build("sheets", "v4", credentials=credentials)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=sheet_id, range="A:H", valueRenderOption="FORMATTED_VALUE")
        .execute()
    )
    raw = result.get("values", [])
    rows = _parse_tariff_sheet(raw)
    logger.info(f"Loaded {len(rows)} tariff rows from sheet {sheet_id}")
    return rows


async def _get_tariff_rows() -> List[Dict]:
    """Return cached tariff rows, refreshing if TTL expired."""
    global _tariff_cache
    now = time.monotonic()
    if _tariff_cache is None or (now - _tariff_cache[1]) > _TARIFF_TTL:
        try:
            rows = await asyncio.wait_for(asyncio.to_thread(_load_tariff_rows_sync), timeout=30.0)
        except asyncio.TimeoutError:
            raise TimeoutError("Tariff sheet fetch timed out after 30s")
        _tariff_cache = (rows, now)
    return _tariff_cache[0]


async def _translate_tariff_query(query: str) -> str:
    """Use Gemini Flash to translate a colloquial query into Nigeria Customs tariff terminology.

    E.g. "solar inverter" → "static converter" (CET 8504), "solar panel" → "photovoltaic module".
    Falls back to the original query on any error (fast fail, non-blocking).
    """
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return query

    prompt = (
        "Translate the following item into the shortest standard term used in the Nigeria Customs "
        "ECOWAS Common External Tariff (CET) schedule — 2 to 6 words maximum. Reply with ONLY "
        "the term, nothing else. If already standard CET terminology, return it unchanged.\n\n"
        f"Item: {query}"
    )
    try:
        model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        gateway = GeminiGateway(api_key=api_key, default_model=model)
        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=model,
                max_output_tokens=32,
                thinking="off",
            ),
        )
        translated = str(response.text).strip()
        if translated and translated.lower() != query.lower():
            logger.info(f"Tariff query translated: {query!r} → {translated!r}")
            return translated
    except Exception as e:
        logger.debug(f"Tariff query translation failed (using original): {e}")
    return query


async def _handle_lookup_tariff(args: Dict[str, Any]) -> List[types.TextContent]:
    """Handle lookup_tariff tool call."""
    query = args.get("query", "").strip()
    if not query:
        return list(compose_json_response({"error": "query is required"}))

    rows = await _get_tariff_rows()

    # Try direct fuzzy match; if low confidence, also try LLM-translated term and take the best
    matches = _match_tariff(query, rows)
    best_score = max((m.get("score", 0) for m in matches), default=0)
    if best_score < 90:
        translated = await _translate_tariff_query(query)
        if translated.lower() != query.lower():
            translated_matches = _match_tariff(translated, rows)
            translated_best = max((m.get("score", 0) for m in translated_matches), default=0)
            if translated_best >= best_score:
                matches = translated_matches

    if not matches:
        return list(compose_json_response({"error": f"No match found for '{query}'"}))

    if len(matches) == 1:
        return list(compose_json_response(matches[0]))

    return list(compose_json_response({"matches": matches}))


# --- Prohibition list ---


class _ProhibitionParser(HTMLParser):
    """Extract list items and meaningful table cells from HTML."""

    def __init__(self):
        super().__init__()
        self._in_li = False
        self._in_td = False
        self._skip_header = False
        self._current: List[str] = []
        self.items: List[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self._in_li = True
            self._current = []
        elif tag == "td":
            self._in_td = True
            self._current = []
        elif tag == "th":
            self._skip_header = True

    def handle_endtag(self, tag):
        if tag == "li":
            self._in_li = False
            text = "".join(self._current).strip()
            if text:
                self.items.append(text)
        elif tag == "td":
            self._in_td = False
            text = "".join(self._current).strip()
            # Skip pure numeric cells (S/N column)
            if text and not text.isdigit():
                self.items.append(text)
        elif tag == "th":
            self._skip_header = False

    def handle_data(self, data):
        if self._skip_header:
            return
        if self._in_li or self._in_td:
            self._current.append(data)


def _parse_prohibition_html(html: str) -> List[str]:
    """Extract prohibited items from customs.gov.ng page HTML."""
    parser = _ProhibitionParser()
    parser.feed(html)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for item in parser.items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


async def _fetch_prohibition_list_fresh() -> Dict:
    """Fetch and parse the prohibition list from customs.gov.ng."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Anansi/1.0)"}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            PROHIBITION_URL,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            resp.raise_for_status()
            html = await resp.text()

    items = _parse_prohibition_html(html)
    logger.info(f"Fetched {len(items)} prohibition list items")
    return {
        "source_url": PROHIBITION_URL,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "item_count": len(items),
        "items": items,
    }


async def _get_prohibition_list() -> Dict:
    """Return cached prohibition list, refreshing if TTL expired."""
    global _prohibition_cache
    now = time.monotonic()
    if _prohibition_cache is None or (now - _prohibition_cache[1]) > _PROHIBITION_TTL:
        data = await _fetch_prohibition_list_fresh()
        _prohibition_cache = (data, now)
    return _prohibition_cache[0]


async def _handle_get_import_prohibition_list(args: Dict[str, Any]) -> List[types.TextContent]:
    """Handle get_import_prohibition_list tool call."""
    data = await _get_prohibition_list()
    return list(compose_json_response(data))


# --- Import standards PDF ---


def _parse_standards_tables(tables: List[List[List]]) -> List[Dict]:
    """Parse pdfplumber table output into list of dicts.

    Each `tables` element is a list of rows; first row is the header.
    Finds Item, H S Codes, Remarks columns (case-insensitive).
    Concatenates across all pages/tables.
    """
    all_rows: List[Dict] = []

    for table in tables:
        if not table or len(table) < 2:
            continue

        header = [str(cell or "").strip().lower() for cell in table[0]]

        # Find column indices — flexible matching
        item_idx = next((i for i, h in enumerate(header) if "item" in h), None)
        hs_idx = next(
            (i for i, h in enumerate(header) if "h" in h and "s" in h and "code" in h),
            None,
        )
        remarks_idx = next((i for i, h in enumerate(header) if "remark" in h), None)

        if item_idx is None:
            continue  # Table doesn't have an Item column — skip

        for row in table[1:]:
            if not row:
                continue
            item_val = str(row[item_idx] or "").strip() if item_idx < len(row) else ""
            if not item_val:
                continue

            all_rows.append(
                {
                    "item": item_val,
                    "hs_codes": (
                        str(row[hs_idx] or "").strip()
                        if hs_idx is not None and hs_idx < len(row)
                        else ""
                    ),
                    "remarks": (
                        str(row[remarks_idx] or "").strip()
                        if remarks_idx is not None and remarks_idx < len(row)
                        else ""
                    ),
                }
            )

    return all_rows


def _load_standards_rows_sync() -> List[Dict]:
    """Download PDF from Drive and extract tables. Runs via asyncio.to_thread.

    Reuses download_from_drive() from shared.utils.drive_upload — already handles
    supportsAllDrives=True and uses the correct service account credentials.
    """
    import pdfplumber

    from shared.utils.drive_upload import download_from_drive

    pdf_id = os.getenv("NIGERIA_IMPORT_STANDARDS_PDF_ID", "")
    if not pdf_id:
        raise ValueError("NIGERIA_IMPORT_STANDARDS_PDF_ID not configured")

    pdf_bytes = download_from_drive(pdf_id)
    logger.info(f"Downloaded standards PDF ({len(pdf_bytes)} bytes) from Drive {pdf_id}")

    all_tables: List[List[List]] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            page_tables = page.extract_tables()
            if page_tables:
                all_tables.extend(page_tables)

    rows = _parse_standards_tables(all_tables)
    logger.info(f"Extracted {len(rows)} standards rows from PDF")
    return rows


async def _get_standards_rows() -> List[Dict]:
    """Return cached standards rows, refreshing if TTL expired."""
    global _standards_cache
    now = time.monotonic()
    if _standards_cache is None or (now - _standards_cache[1]) > _STANDARDS_TTL:
        rows = await asyncio.to_thread(_load_standards_rows_sync)
        _standards_cache = (rows, now)
    return _standards_cache[0]


def _match_standard(query: str, rows: List[Dict]) -> List[Dict]:
    """Fuzzy match query against Item column. Returns top match(es).

    Returns up to 3 results if their scores are within 5 points of the best.
    Returns [] if best score < 70.
    """
    scored = []
    for row in rows:
        score = fuzz.WRatio(query.lower(), row["item"].lower())
        if score >= 70:
            scored.append({**row, "score": score})

    if not scored:
        return []

    scored.sort(key=lambda r: r["score"], reverse=True)
    best = scored[0]["score"]
    return [r for r in scored if r["score"] >= best - 5][:3]


async def _handle_lookup_import_standard(args: Dict[str, Any]) -> List[types.TextContent]:
    """Handle lookup_import_standard tool call."""
    query = args.get("query", "").strip()
    if not query:
        return list(compose_json_response({"error": "query is required"}))

    rows = await _get_standards_rows()
    matches = _match_standard(query, rows)
    if not matches:
        return list(compose_json_response({"error": f"No match found for '{query}'"}))

    return list(compose_json_response({"matches": matches}))


# --- MCP handlers ---


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available reference tools."""
    return [
        types.Tool(
            name="lookup_tariff",
            description=(
                "[READ-ONLY] Look up Nigeria import tariff by item description or CET code. "
                "Accepts fuzzy item descriptions (e.g. 'breeding horses') or CET codes with or "
                "without dots (e.g. '0101.21.00.00' or '0101210000'). Returns VAT, levy (LVY), "
                "excise (EXC), and date of validity (DOV) from the Nigeria Customs import tariff schedule."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Item description or CET code (dots optional)",
                    }
                },
                "required": ["query"],
            },
            visible_to_customer=False,
        ),
        types.Tool(
            name="get_import_prohibition_list",
            description=(
                "[READ-ONLY] Fetch the current Nigeria import prohibition list from the Nigeria "
                "Customs Service website. Returns all categories of absolutely prohibited imports."
            ),
            inputSchema={"type": "object", "properties": {}},
            visible_to_customer=False,
        ),
        types.Tool(
            name="lookup_import_standard",
            description=(
                "[READ-ONLY] Look up Nigeria import standards by item name. Fuzzy-matches against "
                "the Nigeria Import Standards document and returns the applicable HS codes and remarks "
                "(standard reference, e.g. NIS 54:2017)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Item name to search for",
                    }
                },
                "required": ["query"],
            },
            visible_to_customer=False,
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Dispatch tool calls to handlers."""
    try:
        if name == "lookup_tariff":
            return await _handle_lookup_tariff(arguments)
        elif name == "get_import_prohibition_list":
            return await _handle_get_import_prohibition_list(arguments)
        elif name == "lookup_import_standard":
            return await _handle_lookup_import_standard(arguments)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


# --- Standalone entry point ---


async def main():
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="reference-server",
                server_version="1.0.0",
                capabilities=ServerCapabilities(tools={}),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
