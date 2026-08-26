"""The comment-driven branch: two passes, document order, fresh context."""

import pytest

from orchestrator.experts.handlers.doc_editor.process_doc_edits import process_doc_edits

BEFORE = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

FINDINGS PLACEHOLDER
"""

AFTER = """## Executive summary

SUMMARY PLACEHOLDER

## Findings

The inverter tripped twice in March.
"""


class _FakeContext:
    """Only the surface process_doc_edits actually touches."""

    def __init__(self, inputs):
        self._inputs = inputs
        self.packet_state = {}
        self.effective_email = "editor@example.com"
        self.effective_org_id = None
        self.progress = []
        self.mcp_executor = None

    def get_input(self, key, default=None):
        return self._inputs.get(key, default)

    async def send_progress_to_user(self, message):
        self.progress.append(message)


# Creation order matters here, and it is deliberately the awkward one: the
# summary comment was added LAST, which is how someone actually writes one --
# you notice you want a summary after the rest of the template exists. The old
# `reversed(creation order)` therefore put the summary FIRST, which is exactly
# backwards. Flip these two entries and the headline test passes against the
# unfixed code, proving nothing.
_FINDINGS = {
    "comment_id": "findings",
    "instruction": "Write up the March inverter trips",
    "highlighted_text": "FINDINGS PLACEHOLDER",
    "author_email": "a@x.com",
    "created_time": "2026-08-26T09:00:00Z",
}
_SUMMARY = {
    "comment_id": "summary",
    "instruction": "Add a summary here after finishing the rest",
    "highlighted_text": "SUMMARY PLACEHOLDER",
    "author_email": "a@x.com",
    "created_time": "2026-08-26T09:01:00Z",
}


@pytest.fixture
def wired(monkeypatch):
    """Patch every I/O seam and record what the handler did, in order."""
    import shared.utils.doc_editing as doc_editing
    import shared.utils.gdrive_doc_fetcher as fetcher
    from shared.utils import doc_edit_ordering as edit_ordering

    calls = {"edits": [], "generated": [], "scans": 0, "pins": 0}
    fetches = [BEFORE, AFTER]

    async def _scan(doc_id):
        """First call: both threads open. Second: pass one resolved 'findings'."""
        calls["scans"] += 1
        return [_FINDINGS, _SUMMARY] if calls["scans"] == 1 else [_SUMMARY]

    def _fetch(doc_id):
        return fetches.pop(0) if fetches else AFTER

    async def _classify(comments, markdown):
        return {2}  # the summary comment, second in scan order

    async def _generate(instruction, highlighted_text, **kwargs):
        calls["generated"].append((highlighted_text, kwargs.get("section_context", "")))
        return f"generated for {highlighted_text}"

    async def _edit(doc_id, target_text, replacement_markdown, comment_id=None):
        calls["edits"].append(comment_id)
        return {"success": True, "elements_written": 1}

    async def _pin(doc_id):
        calls["pins"] += 1
        return True

    monkeypatch.setattr(doc_editing, "scan_comments", _scan)
    monkeypatch.setattr(doc_editing, "generate_replacement_markdown", _generate)
    monkeypatch.setattr(doc_editing, "edit_section", _edit)
    monkeypatch.setattr(doc_editing, "pin_revision", _pin)
    monkeypatch.setattr(fetcher, "fetch_google_doc_markdown", _fetch)
    monkeypatch.setattr(edit_ordering, "classify_deferred", _classify)
    return calls


@pytest.mark.asyncio
async def test_the_deferred_edit_is_written_last(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert wired["edits"] == ["findings", "summary"]


@pytest.mark.asyncio
async def test_the_deferred_edit_sees_the_finished_document(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    contexts = {highlighted: context for highlighted, context in wired["generated"]}
    summary_context = contexts["SUMMARY PLACEHOLDER"]
    assert "The inverter tripped twice in March." in summary_context
    assert "FINDINGS PLACEHOLDER" not in summary_context


@pytest.mark.asyncio
async def test_the_first_pass_also_gets_document_context(wired):
    """The bug this plan exists to fix: section_context used to be always ''."""
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    contexts = {highlighted: context for highlighted, context in wired["generated"]}
    findings_context = contexts["FINDINGS PLACEHOLDER"]
    assert "Executive summary" in findings_context


@pytest.mark.asyncio
async def test_the_revision_is_pinned_once_for_the_whole_run(wired):
    await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert wired["pins"] == 1


@pytest.mark.asyncio
async def test_both_edits_are_reported(wired):
    result = await process_doc_edits(_FakeContext({"document_id": "doc-1"}))
    assert result.data["succeeded"] == 2
    assert result.data["failed"] == 0
    assert result.data["deferred"] == 1
