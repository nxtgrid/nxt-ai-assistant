"""Ordering comment-driven doc edits: position, deferral, and partitioning."""

import pytest

from orchestrator.experts.handlers.doc_editor.edit_ordering import (
    document_position,
    order_by_position,
)

MARKDOWN = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

The inverter tripped twice in March.

## Recommendations

Replace the DC fuse.
"""


def _comment(comment_id, quoted):
    return {"comment_id": comment_id, "highlighted_text": quoted, "instruction": "edit it"}


def test_position_is_the_character_offset_of_the_quote():
    assert document_position(MARKDOWN, "SUMMARY PLACEHOLDER") == MARKDOWN.find(
        "SUMMARY PLACEHOLDER"
    )


def test_an_absent_quote_has_no_position():
    assert document_position(MARKDOWN, "nothing like this in the doc") == -1


def test_an_empty_quote_has_no_position():
    assert document_position(MARKDOWN, "") == -1


def test_an_html_escaped_quote_still_matches():
    """Drive serves quotedFileContent as text/html — see Annotation's docstring."""
    markdown = "Costs rose 5% & margins fell."
    assert document_position(markdown, "5% &amp; margins") == markdown.find("5% & margins")


def test_a_multi_line_quote_falls_back_to_its_first_line():
    assert document_position(MARKDOWN, "Replace the DC fuse.\nAND SOMETHING ELSE") == (
        MARKDOWN.find("Replace the DC fuse.")
    )


def test_edits_run_bottom_to_top():
    ordered = order_by_position(
        [
            _comment("top", "SUMMARY PLACEHOLDER"),
            _comment("bottom", "Replace the DC fuse."),
            _comment("middle", "The inverter tripped twice in March."),
        ],
        MARKDOWN,
    )
    assert [c["comment_id"] for c in ordered] == ["bottom", "middle", "top"]


def test_unlocatable_comments_sort_last():
    ordered = order_by_position(
        [
            _comment("ghost", "text that was deleted"),
            _comment("real", "SUMMARY PLACEHOLDER"),
        ],
        MARKDOWN,
    )
    assert [c["comment_id"] for c in ordered] == ["real", "ghost"]


def test_ordering_an_empty_batch_is_not_an_error():
    assert order_by_position([], MARKDOWN) == []
