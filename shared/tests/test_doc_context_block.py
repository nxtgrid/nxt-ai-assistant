"""The SURROUNDING CONTEXT block and its per-caller truncation budget."""

import pytest

from shared.utils.doc_editing import build_context_block


def test_no_context_produces_no_block():
    assert build_context_block("") == ""


def test_a_block_is_labelled_for_the_model():
    assert build_context_block("the whole doc") == "\nSURROUNDING CONTEXT:\nthe whole doc"


def test_the_default_budget_matches_the_single_edit_path():
    """Mode 2 passed markdown[:1500]; the default must not change its prompt."""
    assert build_context_block("x" * 5000).endswith("x" * 1500)
    assert len(build_context_block("x" * 5000)) == len("\nSURROUNDING CONTEXT:\n") + 1500


def test_a_caller_can_ask_for_a_larger_budget():
    block = build_context_block("y" * 20000, context_limit=12000)
    assert len(block) == len("\nSURROUNDING CONTEXT:\n") + 12000


def test_context_shorter_than_the_budget_is_passed_whole():
    assert build_context_block("short", context_limit=12000).endswith("short")


@pytest.mark.asyncio
async def test_fetch_doc_markdown_returns_empty_when_drive_fails(monkeypatch):
    """A doc we cannot read must degrade to no context, never raise."""
    import shared.utils.gdrive_doc_fetcher as fetcher
    from shared.utils.doc_editing import fetch_doc_markdown

    def _explode(_doc_id):
        raise RuntimeError("Drive is down")

    monkeypatch.setattr(fetcher, "fetch_google_doc_markdown", _explode)
    assert await fetch_doc_markdown("doc-1") == ""


@pytest.mark.asyncio
async def test_fetch_doc_markdown_returns_the_document(monkeypatch):
    import shared.utils.gdrive_doc_fetcher as fetcher
    from shared.utils.doc_editing import fetch_doc_markdown

    monkeypatch.setattr(fetcher, "fetch_google_doc_markdown", lambda _id: "# Title\n\nBody")
    assert await fetch_doc_markdown("doc-1") == "# Title\n\nBody"
