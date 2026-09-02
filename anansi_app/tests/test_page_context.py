"""Tests for page_context: the dataclass and its entity_context projection.

The projection is what actually reaches the model -- chat_orchestrator's
conversation_graph._format_entity_context renders EntityContext's typed id
fields and every additional_context key into an "[Entity Context]" block
prepended to the user turn. anansi_app cannot import EntityContext itself
(see test_no_orchestrator_imports.py), so these tests pin the dict shape
that stands in for it.
"""

from __future__ import annotations

from nicegui_app.page_context import (
    MAX_SELECTION_CHARS,
    MAX_SUMMARY_CHARS,
    MAX_SUMMARY_LINES,
    PageContext,
    to_entity_context,
)


def test_typed_identifiers_land_on_entity_context_top_level():
    page = PageContext(kind="grid_record", label="Grid: Alpha", identifiers={"grid_id": "17"})
    payload = to_entity_context(page)
    assert payload["grid_id"] == "17"
    assert "grid_id" not in payload["additional_context"]


def test_untyped_identifiers_land_in_additional_context_humanised():
    page = PageContext(kind="ticket", label="Ticket OPS-1", identifiers={"ticket_ref": "OPS-1"})
    payload = to_entity_context(page)
    assert payload["additional_context"]["Ticket ref"] == "OPS-1"


def test_page_label_and_kind_are_always_published():
    page = PageContext(kind="ticket_list", label="Tickets (open)")
    extra = to_entity_context(page)["additional_context"]
    assert extra["Page"] == "Tickets (open)"
    assert extra["Page type"] == "ticket_list"


def test_summary_lines_join_and_detail_hint_is_published():
    page = PageContext(
        kind="ticket",
        label="Ticket OPS-1",
        summary_lines=["Status: open", "Grid: Alpha"],
        detail_hint="Call the ticket tool with ticket_ref for comments.",
    )
    extra = to_entity_context(page)["additional_context"]
    assert extra["Page summary"] == "Status: open\nGrid: Alpha"
    assert extra["To go deeper"] == "Call the ticket tool with ticket_ref for comments."


def test_blank_summary_lines_are_dropped():
    page = PageContext(kind="ticket", label="T", summary_lines=["a", "", "   ", "b"])
    assert to_entity_context(page)["additional_context"]["Page summary"] == "a\nb"


def test_summary_is_capped_by_line_count_then_characters():
    page = PageContext(kind="x", label="x", summary_lines=[f"line {i}" for i in range(50)])
    summary = page.summary_text()
    assert summary.count("\n") == MAX_SUMMARY_LINES - 1
    assert len(summary) <= MAX_SUMMARY_CHARS


def test_selection_is_capped_and_stripped():
    payload = to_entity_context(None, selection="  " + ("z" * (MAX_SELECTION_CHARS + 500)) + "  ")
    assert len(payload["additional_context"]["Highlighted text"]) == MAX_SELECTION_CHARS


def test_selection_alone_is_enough_to_produce_context():
    payload = to_entity_context(None, selection="permanent employment contract is MANDATORY")
    assert payload["additional_context"]["Highlighted text"].endswith("MANDATORY")


def test_nothing_attached_means_no_entity_context_at_all():
    assert to_entity_context(None, selection="   ") is None


def test_chip_label_truncates_long_labels():
    page = PageContext(kind="x", label="y" * 80)
    assert len(page.chip_label()) == 40
    assert page.chip_label().endswith("…")
