"""Tests for reference MCP server — normalization, matching, and parsing logic."""

import asyncio
import json
from unittest.mock import AsyncMock, patch

from servers.reference_server.reference_mcp_server import (
    _match_standard,
    _match_tariff,
    _normalize_cet,
    _parse_prohibition_html,
    _parse_standards_tables,
    _parse_tariff_sheet,
)

SAMPLE_TARIFF_ROWS = [
    {
        "cet_code": "0101.21.00.00",
        "description": "Pure-bred breeding horses",
        "su": "u",
        "id": "5",
        "vat": "7.5%",
        "lvy": "5%",
        "exc": "0%",
        "dov": "2023-01-01",
    },
    {
        "cet_code": "0201.10.00.00",
        "description": "Beef carcasses and half-carcasses, fresh or chilled",
        "su": "kg",
        "id": "10",
        "vat": "7.5%",
        "lvy": "20%",
        "exc": "0%",
        "dov": "2023-01-01",
    },
    {
        "cet_code": "8471.30.00.00",
        "description": "Portable automatic data processing machines",
        "su": "u",
        "id": "1",
        "vat": "7.5%",
        "lvy": "0%",
        "exc": "0%",
        "dov": "2024-06-01",
    },
]

SAMPLE_STANDARDS_ROWS = [
    {"item": "Rice (Parboiled)", "hs_codes": "1006.30", "remarks": "NIS 54:2017"},
    {"item": "Wheat Flour", "hs_codes": "1101.00", "remarks": "NIS 5:2017"},
    {"item": "Bottled Water (Sachet)", "hs_codes": "2201.10", "remarks": "NIS 456:2015"},
    {"item": "Electrical Cables", "hs_codes": "8544.19", "remarks": "NIS 212:2021"},
]


# ---------------------------------------------------------------------------
# CET normalisation
# ---------------------------------------------------------------------------


class TestNormalizeCet:
    def test_strips_dots(self):
        assert _normalize_cet("0101.21.00.00") == "0101210000"

    def test_strips_spaces(self):
        assert _normalize_cet("0101 21 00 00") == "0101210000"

    def test_strips_dots_and_spaces(self):
        assert _normalize_cet("0101. 21.00 .00") == "0101210000"

    def test_plain_digits_unchanged(self):
        assert _normalize_cet("0101210000") == "0101210000"

    def test_empty_string(self):
        assert _normalize_cet("") == ""


# ---------------------------------------------------------------------------
# Tariff matching
# ---------------------------------------------------------------------------


class TestMatchTariff:
    def test_exact_cet_match_with_dots(self):
        results = _match_tariff("0101.21.00.00", SAMPLE_TARIFF_ROWS)
        assert len(results) == 1
        assert results[0]["cet_code"] == "0101.21.00.00"
        assert results[0]["matched_by"] == "cet_exact"

    def test_exact_cet_match_no_dots(self):
        results = _match_tariff("0101210000", SAMPLE_TARIFF_ROWS)
        assert len(results) == 1
        assert results[0]["cet_code"] == "0101.21.00.00"
        assert results[0]["matched_by"] == "cet_exact"

    def test_cet_prefix_match(self):
        results = _match_tariff("01012", SAMPLE_TARIFF_ROWS)
        assert len(results) >= 1
        assert all(r["matched_by"] == "cet_prefix" for r in results)
        assert results[0]["cet_code"] == "0101.21.00.00"

    def test_description_fuzzy_match(self):
        results = _match_tariff("breeding horses", SAMPLE_TARIFF_ROWS)
        assert len(results) >= 1
        assert results[0]["matched_by"] == "description"
        assert "horse" in results[0]["description"].lower()

    def test_description_fuzzy_match_typo(self):
        results = _match_tariff("portble automatc data procesing machine", SAMPLE_TARIFF_ROWS)
        assert len(results) >= 1
        assert results[0]["matched_by"] == "description"
        assert "data processing" in results[0]["description"].lower()

    def test_no_match_below_threshold(self):
        results = _match_tariff("xyzzy foobar gibberish", SAMPLE_TARIFF_ROWS)
        assert results == []

    def test_cet_exact_beats_description_if_both_match(self):
        results = _match_tariff("0201.10.00.00", SAMPLE_TARIFF_ROWS)
        assert len(results) == 1
        assert results[0]["matched_by"] == "cet_exact"


# ---------------------------------------------------------------------------
# Tariff sheet parsing
# ---------------------------------------------------------------------------


class TestParseTariffSheet:
    def test_parses_header_and_rows(self):
        raw = [
            ["CET Code", "Description", "SU", "ID", "VAT", "LVY", "EXC", "DOV"],
            [
                "0101.21.00.00",
                "Pure-bred breeding horses",
                "u",
                "5",
                "7.5%",
                "5%",
                "0%",
                "2023-01-01",
            ],
            ["0201.10.00.00", "Beef carcasses", "kg", "10", "7.5%", "20%", "0%", "2023-01-01"],
        ]
        rows = _parse_tariff_sheet(raw)
        assert len(rows) == 2
        assert rows[0]["cet_code"] == "0101.21.00.00"
        assert rows[0]["description"] == "Pure-bred breeding horses"
        assert rows[0]["vat"] == "7.5%"
        assert rows[0]["dov"] == "2023-01-01"

    def test_skips_empty_rows(self):
        raw = [
            ["CET Code", "Description", "SU", "ID", "VAT", "LVY", "EXC", "DOV"],
            [],
            ["0101.21.00.00", "Horses", "u", "5", "7.5%", "5%", "0%", "2023-01-01"],
        ]
        rows = _parse_tariff_sheet(raw)
        assert len(rows) == 1

    def test_handles_short_rows(self):
        raw = [
            ["CET Code", "Description", "SU", "ID", "VAT", "LVY", "EXC", "DOV"],
            ["0101.21.00.00", "Horses"],
        ]
        rows = _parse_tariff_sheet(raw)
        assert len(rows) == 1
        assert rows[0]["vat"] == ""
        assert rows[0]["dov"] == ""


# ---------------------------------------------------------------------------
# lookup_tariff handler
# ---------------------------------------------------------------------------


class TestHandleLookupTariff:
    def test_returns_match(self):
        from servers.reference_server.reference_mcp_server import _handle_lookup_tariff

        with patch(
            "servers.reference_server.reference_mcp_server._get_tariff_rows",
            new_callable=AsyncMock,
            return_value=SAMPLE_TARIFF_ROWS,
        ):
            result = asyncio.run(_handle_lookup_tariff({"query": "breeding horses"}))
        data = json.loads(result[0].text)
        assert data["cet_code"] == "0101.21.00.00"
        assert data["matched_by"] == "description"

    def test_returns_matches_array_when_multiple_close_scores(self):
        from servers.reference_server.reference_mcp_server import _handle_lookup_tariff

        # Prefix query "0" matches all three rows in the sample
        with patch(
            "servers.reference_server.reference_mcp_server._get_tariff_rows",
            new_callable=AsyncMock,
            return_value=SAMPLE_TARIFF_ROWS,
        ):
            result = asyncio.run(_handle_lookup_tariff({"query": "0"}))
        data = json.loads(result[0].text)
        assert "matches" in data
        assert len(data["matches"]) > 1

    def test_returns_error_when_no_match(self):
        from servers.reference_server.reference_mcp_server import _handle_lookup_tariff

        with patch(
            "servers.reference_server.reference_mcp_server._get_tariff_rows",
            new_callable=AsyncMock,
            return_value=[],
        ):
            result = asyncio.run(_handle_lookup_tariff({"query": "xyzzy nothing"}))
        data = json.loads(result[0].text)
        assert "no match" in data.get("error", "").lower()


# ---------------------------------------------------------------------------
# Prohibition list HTML parsing
# ---------------------------------------------------------------------------


class TestParseProhibitionHtml:
    SAMPLE_HTML_LIST = """
    <div class="entry-content">
      <p>The following items are prohibited:</p>
      <ol>
        <li>Used motor vehicles above 15 years</li>
        <li>Mosquito coils</li>
        <li>Printed materials considered injurious to public interest</li>
      </ol>
    </div>
    """

    SAMPLE_HTML_TABLE = """
    <table>
      <tr><th>S/N</th><th>Item Description</th></tr>
      <tr><td>1</td><td>Used motor vehicles above 15 years</td></tr>
      <tr><td>2</td><td>Mosquito coils</td></tr>
    </table>
    """

    def test_extracts_items_from_list(self):
        items = _parse_prohibition_html(self.SAMPLE_HTML_LIST)
        assert len(items) >= 3
        assert any("motor vehicle" in item.lower() for item in items)
        assert any("mosquito" in item.lower() for item in items)

    def test_extracts_items_from_table(self):
        items = _parse_prohibition_html(self.SAMPLE_HTML_TABLE)
        assert len(items) >= 1
        assert any("motor vehicle" in item.lower() for item in items)

    def test_strips_whitespace(self):
        items = _parse_prohibition_html(self.SAMPLE_HTML_LIST)
        for item in items:
            assert item == item.strip()

    def test_empty_html_returns_empty_list(self):
        assert _parse_prohibition_html("<html></html>") == []


# ---------------------------------------------------------------------------
# Standards PDF table parsing
# ---------------------------------------------------------------------------


class TestParseStandardsTables:
    def test_parses_tables_with_correct_headers(self):
        tables = [
            [
                ["Item", "H S Codes", "Remarks"],
                ["Rice (Parboiled)", "1006.30", "NIS 54:2017"],
                ["Wheat Flour", "1101.00", "NIS 5:2017"],
            ]
        ]
        rows = _parse_standards_tables(tables)
        assert len(rows) == 2
        assert rows[0]["item"] == "Rice (Parboiled)"
        assert rows[0]["hs_codes"] == "1006.30"
        assert rows[0]["remarks"] == "NIS 54:2017"

    def test_case_insensitive_header_detection(self):
        tables = [
            [
                ["ITEM", "HS CODES", "REMARKS"],
                ["Rice", "1006.30", "NIS 54"],
            ]
        ]
        rows = _parse_standards_tables(tables)
        assert len(rows) == 1
        assert rows[0]["item"] == "Rice"

    def test_skips_tables_without_item_column(self):
        tables = [
            [["Name", "Code"], ["Foo", "Bar"]],
            [["Item", "H S Codes", "Remarks"], ["Rice", "1006.30", "NIS 54"]],
        ]
        rows = _parse_standards_tables(tables)
        assert len(rows) == 1

    def test_skips_empty_rows(self):
        tables = [
            [
                ["Item", "H S Codes", "Remarks"],
                ["Rice", "1006.30", "NIS 54"],
                [None, None, None],
                ["", "", ""],
            ]
        ]
        rows = _parse_standards_tables(tables)
        assert len(rows) == 1

    def test_concatenates_across_multiple_tables(self):
        tables = [
            [["Item", "H S Codes", "Remarks"], ["Rice", "1006.30", "NIS 54"]],
            [["Item", "H S Codes", "Remarks"], ["Wheat", "1101.00", "NIS 5"]],
        ]
        rows = _parse_standards_tables(tables)
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Standards matching
# ---------------------------------------------------------------------------


class TestMatchStandard:
    def test_exact_item_match(self):
        results = _match_standard("Rice (Parboiled)", SAMPLE_STANDARDS_ROWS)
        assert len(results) >= 1
        assert results[0]["item"] == "Rice (Parboiled)"

    def test_fuzzy_item_match(self):
        results = _match_standard("parboiled rice", SAMPLE_STANDARDS_ROWS)
        assert len(results) >= 1
        assert "rice" in results[0]["item"].lower()

    def test_returns_top_3_for_close_scores(self):
        results = _match_standard("electrical cable wire", SAMPLE_STANDARDS_ROWS)
        assert len(results) >= 1
        assert all("score" in r for r in results)

    def test_no_match_below_threshold(self):
        results = _match_standard("xyzzy foobar gibberish", SAMPLE_STANDARDS_ROWS)
        assert results == []

    def test_response_has_required_fields(self):
        results = _match_standard("Wheat Flour", SAMPLE_STANDARDS_ROWS)
        assert len(results) >= 1
        assert "item" in results[0]
        assert "hs_codes" in results[0]
        assert "remarks" in results[0]
        assert "score" in results[0]
