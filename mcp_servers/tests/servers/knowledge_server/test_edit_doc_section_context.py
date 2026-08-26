"""edit_doc_section must hand the generator the document it is editing.

The blind-generation bug: this handler called
generate_replacement_markdown(instruction, target_text, user_email=...) --
three positional-or-keyword arguments that left section_context="" and
expert_context=None. An instruction like "summarise the sections above" is
unanswerable from that, and the model answers anyway.
"""

import pathlib

import pytest


def _edit_handler():
    import sys

    root = pathlib.Path(__file__).resolve().parents[4]
    sys.path.insert(0, str(root))
    from servers.knowledge_server.knowledge_mcp_server import _handle_edit_doc_section

    return _handle_edit_doc_section


@pytest.mark.asyncio
async def test_generator_receives_the_document_markdown(monkeypatch):
    from shared.utils.file_annotations import MIME_SHEET

    captured = {}

    async def fake_user_can_access(*args, **kwargs):
        return True

    async def fake_get_file_mime_type(file_id):
        # Not a sheet -- MIME_SHEET is imported only to prove the contrast.
        assert file_id != MIME_SHEET
        return "application/vnd.google-apps.document"

    async def fake_fetch_doc_markdown(doc_id):
        return "# Findings\n\nThe inverter tripped twice.\n\n# Summary\n\n{{Summary}}"

    async def fake_generate_replacement_markdown(instruction, highlighted_text, **kwargs):
        captured["instruction"] = instruction
        captured["highlighted_text"] = highlighted_text
        captured.update(kwargs)
        return "rewritten"

    async def fake_edit_section(**kwargs):
        captured["written"] = kwargs["replacement_markdown"]
        return {"success": True, "elements_written": 1}

    async def fake_pin_revision(doc_id):
        return None

    monkeypatch.setattr("shared.utils.drive_permissions.user_can_access", fake_user_can_access)
    monkeypatch.setattr(
        "shared.utils.file_annotations.get_file_mime_type", fake_get_file_mime_type
    )
    monkeypatch.setattr("shared.utils.doc_editing.fetch_doc_markdown", fake_fetch_doc_markdown)
    monkeypatch.setattr(
        "shared.utils.doc_editing.generate_replacement_markdown",
        fake_generate_replacement_markdown,
    )
    monkeypatch.setattr("shared.utils.doc_editing.edit_section", fake_edit_section)
    monkeypatch.setattr("shared.utils.doc_editing.pin_revision", fake_pin_revision)

    handler = _edit_handler()
    await handler(
        {
            "document_id": "doc-1",
            "section_text": "{{Summary}}",
            "instruction": "Summarise the findings above",
            "user_email": "someone@example.com",
        }
    )

    assert "The inverter tripped twice." in captured["section_context"], (
        "the generator was not given the document it is editing"
    )
    assert captured["context_limit"] > 1500, (
        "a whole-document instruction needs the batch budget, not the section default"
    )


@pytest.mark.asyncio
async def test_a_sheet_edit_does_not_try_to_fetch_doc_markdown(monkeypatch):
    """Sheets have no markdown; fetching it would be a wasted Drive call."""
    from shared.utils.file_annotations import MIME_SHEET

    calls = []

    async def fake_user_can_access(*args, **kwargs):
        return True

    async def fake_get_file_mime_type(file_id):
        return MIME_SHEET

    async def fake_fetch_doc_markdown(doc_id):
        calls.append(doc_id)
        return ""

    async def fake_generate_replacement_markdown(*args, **kwargs):
        return "42"

    async def fake_fetch_all_grids(sheet_id):
        return {"Sheet1": [["old"]]}

    async def fake_write_cells(sheet_id, writes):
        return len(writes)

    async def fake_reply_and_resolve(file_id, comment_id, message):
        return True

    monkeypatch.setattr("shared.utils.drive_permissions.user_can_access", fake_user_can_access)
    monkeypatch.setattr(
        "shared.utils.file_annotations.get_file_mime_type", fake_get_file_mime_type
    )
    monkeypatch.setattr("shared.utils.doc_editing.fetch_doc_markdown", fake_fetch_doc_markdown)
    monkeypatch.setattr(
        "shared.utils.doc_editing.generate_replacement_markdown",
        fake_generate_replacement_markdown,
    )
    monkeypatch.setattr("shared.utils.sheet_editing.fetch_all_grids", fake_fetch_all_grids)
    monkeypatch.setattr("shared.utils.sheet_editing.write_cells", fake_write_cells)
    monkeypatch.setattr(
        "shared.utils.file_annotations.reply_and_resolve", fake_reply_and_resolve
    )

    handler = _edit_handler()
    await handler(
        {
            "document_id": "sheet-1",
            "section_text": "old",
            "instruction": "set it to 42",
            "user_email": "someone@example.com",
        }
    )

    assert calls == [], "fetched doc markdown for a spreadsheet"
