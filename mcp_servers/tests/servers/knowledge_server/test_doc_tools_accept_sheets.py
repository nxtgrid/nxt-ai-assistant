"""The doc comment tools work on spreadsheets too, not just Docs."""

import json
import pathlib

import pytest


def _schema(name):
    import sys

    root = pathlib.Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from mcp_servers.servers.knowledge_server.tool_schemas import TOOL_SCHEMAS

    return next(t for t in TOOL_SCHEMAS if t["name"] == name)


def test_scan_doc_comments_no_longer_says_google_doc_only():
    desc = _schema("scan_doc_comments")["description"]
    assert "spreadsheet" in desc.lower() or "sheet" in desc.lower()


def test_edit_doc_section_no_longer_says_google_doc_only():
    desc = _schema("edit_doc_section")["description"]
    assert "spreadsheet" in desc.lower() or "sheet" in desc.lower()


def test_exported_definitions_match_the_source_schemas():
    """tool_definitions.json is what production actually serves — it must be
    regenerated after any tool_schemas.py edit."""
    root = pathlib.Path(__file__).resolve().parents[4]
    exported = json.loads((root / "mcp_servers" / "tool_definitions.json").read_text())
    knowledge = {t["name"]: t for t in exported["tools"]["knowledge"]}
    assert knowledge["scan_doc_comments"]["description"] == _schema("scan_doc_comments")["description"]


def _edit_handler():
    import sys

    root = pathlib.Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from servers.knowledge_server.knowledge_mcp_server import _handle_edit_doc_section

    return _handle_edit_doc_section


@pytest.mark.asyncio
async def test_sheet_dispatch_searches_the_resolved_target_text_and_resolves_the_comment(
    monkeypatch,
):
    """Regression guard: the handler must search for target_text (the value
    already resolved from either section_text or a fetched comment), not the
    raw section_text argument -- a draft of this dispatch searched
    section_text directly, which is empty in comment-driven mode, so
    find_cells_in_grids's empty-needle rule silently found nothing for every
    comment-driven Sheets edit."""
    from shared.utils.file_annotations import MIME_SHEET

    calls = {}

    async def fake_user_can_access(*args, **kwargs):
        return True

    async def fake_get_comment_by_id(doc_id, comment_id):
        return {"highlighted_text": "{{site_name}}", "instruction": "fill it in"}

    async def fake_get_file_mime_type(file_id):
        return MIME_SHEET

    async def fake_fetch_all_grids(sheet_id):
        return {"Sheet1": [["{{site_name}}"]]}

    async def fake_write_cells(sheet_id, writes):
        calls["write_cells"] = (sheet_id, writes)
        return len(writes)

    async def fake_reply_and_resolve(file_id, comment_id, message):
        calls["reply_and_resolve"] = (file_id, comment_id, message)
        return True

    monkeypatch.setattr("shared.utils.drive_permissions.user_can_access", fake_user_can_access)
    monkeypatch.setattr("shared.utils.doc_editing.get_comment_by_id", fake_get_comment_by_id)
    monkeypatch.setattr(
        "shared.utils.file_annotations.get_file_mime_type", fake_get_file_mime_type
    )
    monkeypatch.setattr("shared.utils.sheet_editing.fetch_all_grids", fake_fetch_all_grids)
    monkeypatch.setattr("shared.utils.sheet_editing.write_cells", fake_write_cells)
    monkeypatch.setattr(
        "shared.utils.file_annotations.reply_and_resolve", fake_reply_and_resolve
    )

    handler = _edit_handler()
    result = await handler(
        {"document_id": "sheet123", "comment_id": "c1", "replacement_markdown": "New Site"}
    )

    assert calls["write_cells"] == ("sheet123", [("Sheet1", "A1", "New Site")])
    assert calls["reply_and_resolve"][:2] == ("sheet123", "c1")
    payload = json.loads(result[0].text)
    assert payload == {
        "success": True,
        "cells_written": 1,
        "message": "Updated 1 cell(s) in the spreadsheet.",
    }


@pytest.mark.asyncio
async def test_sheet_dispatch_replies_without_resolving_on_a_stale_quote(monkeypatch):
    from shared.utils.file_annotations import MIME_SHEET

    calls = {}

    async def fake_user_can_access(*args, **kwargs):
        return True

    async def fake_get_file_mime_type(file_id):
        return MIME_SHEET

    async def fake_fetch_all_grids(sheet_id):
        return {"Sheet1": [["something else entirely"]]}

    async def fake_write_cells(sheet_id, writes):
        calls["write_cells"] = (sheet_id, writes)
        return len(writes)

    async def fake_reply_without_resolving(file_id, comment_id, message):
        calls["reply_without_resolving"] = (file_id, comment_id, message)
        return True

    monkeypatch.setattr("shared.utils.drive_permissions.user_can_access", fake_user_can_access)
    monkeypatch.setattr(
        "shared.utils.file_annotations.get_file_mime_type", fake_get_file_mime_type
    )
    monkeypatch.setattr("shared.utils.sheet_editing.fetch_all_grids", fake_fetch_all_grids)
    monkeypatch.setattr("shared.utils.sheet_editing.write_cells", fake_write_cells)
    monkeypatch.setattr(
        "shared.utils.file_annotations.reply_without_resolving", fake_reply_without_resolving
    )

    handler = _edit_handler()
    result = await handler(
        {
            "document_id": "sheet123",
            "comment_id": "c1",
            "section_text": "some stale quote",
            "replacement_markdown": "New Site",
        }
    )

    assert "write_cells" not in calls
    assert calls["reply_without_resolving"][:2] == ("sheet123", "c1")
    assert "does not appear in the spreadsheet" in result[0].text
