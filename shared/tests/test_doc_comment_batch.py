"""The batch tool must sequence comments itself, not in creation order.

The fixture is deliberately adversarial: the summary comment is created
LAST but must be written LAST too, while the findings comment sits BELOW it
in the document. Creation order and document order disagree, so a handler
that just iterates the scan list -- or naively reverses it -- gets a
different answer than a correct one.
"""

import pytest

from shared.utils import doc_comment_batch

_MARKDOWN = "\n".join(
    [
        "# Introduction",
        "",
        "{{Summary}}",
        "",
        "# Findings",
        "",
        "{{Findings}}",
    ]
)

_COMMENTS = [
    {
        "comment_id": "c-summary",
        "instruction": "Add a summary of the learnings here after editing the rest",
        "highlighted_text": "{{Summary}}",
        "author_email": "a@example.com",
        "created_time": "2026-08-26T09:00:00Z",
    },
    {
        "comment_id": "c-findings",
        "instruction": "Describe the two inverter trips",
        "highlighted_text": "{{Findings}}",
        "author_email": "a@example.com",
        "created_time": "2026-08-26T09:01:00Z",
    },
]


@pytest.mark.asyncio
async def test_the_summary_is_written_after_the_section_it_summarises(monkeypatch):
    written: list[str] = []

    async def _scan(_doc_id):
        return list(_COMMENTS)

    async def _markdown(_doc_id):
        return _MARKDOWN

    async def _classify(comments, _markdown):
        # comment 1 in scan order is the summary -- defer it
        return {1}

    async def _generate(**kwargs):
        return f"content for {kwargs['highlighted_text']}"

    async def _edit_section(**kwargs):
        written.append(kwargs["target_text"])
        return {"success": True, "elements_written": 1}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(doc_comment_batch, "scan_comments", _scan)
    monkeypatch.setattr(doc_comment_batch, "fetch_doc_markdown", _markdown)
    monkeypatch.setattr(doc_comment_batch.ordering, "classify_deferred", _classify)
    monkeypatch.setattr(doc_comment_batch, "generate_replacement_markdown", _generate)
    monkeypatch.setattr(doc_comment_batch, "edit_section", _edit_section)
    monkeypatch.setattr(doc_comment_batch, "pin_revision", _noop)

    result = await doc_comment_batch.process_comments("doc-1", user_email="a@example.com")

    assert written == ["{{Findings}}", "{{Summary}}"], (
        f"summary must be written last, got {written}"
    )
    assert result["succeeded"] == 2
    assert result["deferred"] == 1


@pytest.mark.asyncio
async def test_no_comments_is_not_an_error(monkeypatch):
    async def _scan(_doc_id):
        return []

    monkeypatch.setattr(doc_comment_batch, "scan_comments", _scan)
    result = await doc_comment_batch.process_comments("doc-1", user_email="a@example.com")
    assert result["edits"] == 0
    assert result["succeeded"] == 0


@pytest.mark.asyncio
async def test_a_failed_edit_does_not_stop_the_rest_of_the_batch(monkeypatch):
    async def _scan(_doc_id):
        return list(_COMMENTS)

    async def _markdown(_doc_id):
        return _MARKDOWN

    async def _classify(_comments, _markdown):
        return set()

    async def _generate(**kwargs):
        return f"content for {kwargs['highlighted_text']}"

    async def _edit_section(**kwargs):
        if kwargs["target_text"] == "{{Findings}}":
            return {"success": False, "error": "Could not find target text"}
        return {"success": True, "elements_written": 1}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(doc_comment_batch, "scan_comments", _scan)
    monkeypatch.setattr(doc_comment_batch, "fetch_doc_markdown", _markdown)
    monkeypatch.setattr(doc_comment_batch.ordering, "classify_deferred", _classify)
    monkeypatch.setattr(doc_comment_batch, "generate_replacement_markdown", _generate)
    monkeypatch.setattr(doc_comment_batch, "edit_section", _edit_section)
    monkeypatch.setattr(doc_comment_batch, "pin_revision", _noop)

    result = await doc_comment_batch.process_comments("doc-1", user_email="a@example.com")

    assert result["succeeded"] == 1
    assert result["failed"] == 1


@pytest.mark.asyncio
async def test_a_run_over_the_cap_is_truncated(monkeypatch):
    many = [
        {
            "comment_id": f"c-{i}",
            "instruction": "fill it in",
            "highlighted_text": f"{{{{Field{i}}}}}",
            "author_email": "a@example.com",
            "created_time": f"2026-08-26T09:{i:02d}:00Z",
        }
        for i in range(15)
    ]

    async def _scan(_doc_id):
        return many

    async def _markdown(_doc_id):
        return ""

    async def _generate(**kwargs):
        return "x"

    async def _edit_section(**kwargs):
        return {"success": True, "elements_written": 1}

    async def _noop(*_a, **_k):
        return None

    monkeypatch.setattr(doc_comment_batch, "scan_comments", _scan)
    monkeypatch.setattr(doc_comment_batch, "fetch_doc_markdown", _markdown)
    monkeypatch.setattr(doc_comment_batch, "generate_replacement_markdown", _generate)
    monkeypatch.setattr(doc_comment_batch, "edit_section", _edit_section)
    monkeypatch.setattr(doc_comment_batch, "pin_revision", _noop)

    result = await doc_comment_batch.process_comments("doc-1", user_email="a@example.com")

    assert result["edits"] == doc_comment_batch.MAX_EDITS_PER_RUN
