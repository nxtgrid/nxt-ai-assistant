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


# ── the deferral classifier ──────────────────────────────────────────────


def test_the_ordering_prompt_actually_substitutes_its_variables():
    """A bare PromptLibrary, and sentinels absent from the prompt's own text.

    Both halves matter. The bare library (never the shared PROMPTS singleton)
    keeps a developer's local .env from resolving this against the live
    chat_db prompts table -- see the note atop test_prompt_parity.py. The
    sentinels guard against the failure that annotations.resolve_values is
    sitting in right now: single-brace {placeholders} that render() never
    substitutes, under a test whose assertion strings happened to also appear
    in the prompt's static body, so it passed while the model got nothing.
    """
    from shared.prompts import PromptLibrary

    text = PromptLibrary().text(
        "doc_editor.order_edits",
        comments_block="ZZCOMMENTSENTINELZZ",
        markdown="ZZMARKDOWNSENTINELZZ",
    )
    assert "ZZCOMMENTSENTINELZZ" in text
    assert "ZZMARKDOWNSENTINELZZ" in text
    assert "{{" not in text
    assert "{comments_block}" not in text


def test_parse_deferred_reads_a_plain_json_array():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred(
        '[{"request": 1, "deferred": false}, {"request": 2, "deferred": true}]'
    ) == {2}


def test_parse_deferred_strips_a_code_fence():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred('```json\n[{"request": 3, "deferred": true}]\n```') == {3}


def test_parse_deferred_ignores_entries_that_are_not_deferred():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred('[{"request": 1}, {"request": 2, "deferred": "yes"}]') == set()


def test_unparseable_ordering_degrades_to_a_single_pass():
    from orchestrator.experts.handlers.doc_editor.edit_ordering import parse_deferred

    assert parse_deferred("I could not decide, sorry") == set()
    assert parse_deferred('{"request": 1}') == set()
    assert parse_deferred("") == set()


@pytest.mark.asyncio
async def test_a_single_comment_never_costs_an_llm_call(monkeypatch):
    """Nothing to order, and this is the common case — it must stay free."""
    from orchestrator.experts.handlers.doc_editor import edit_ordering

    async def _explode(*args, **kwargs):
        raise AssertionError("classify_deferred must not reach the model here")

    monkeypatch.setattr(edit_ordering, "_classify", _explode)
    assert await edit_ordering.classify_deferred([_comment("a", "x")], MARKDOWN) == set()
    assert await edit_ordering.classify_deferred([], MARKDOWN) == set()


@pytest.mark.asyncio
async def test_a_failing_classifier_never_blocks_the_edit_run(monkeypatch):
    from orchestrator.experts.handlers.doc_editor import edit_ordering

    async def _boom(*args, **kwargs):
        raise RuntimeError("gateway down")

    monkeypatch.setattr(edit_ordering, "_classify", _boom)
    result = await edit_ordering.classify_deferred(
        [_comment("a", "x"), _comment("b", "y")], MARKDOWN
    )
    assert result == set()
