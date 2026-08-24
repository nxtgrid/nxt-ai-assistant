"""A bot message deleted by an operator must never re-enter the model's context.

anansi_app's delete flow keeps the original text in ``chat_messages`` (it is the
audit record of what the bot said) and marks the row with ``metadata.deleted``.
That makes redaction the *reader's* job: every path that builds model context
has to honour the flag, or the bot keeps quoting a message an operator
deliberately pulled from the chat.
"""

from __future__ import annotations

import pytest

from orchestrator.services.supabase_client import (
    DELETED_MESSAGE_PLACEHOLDER,
    EnhancedSupabaseClient,
    content_for_llm,
)

SECRET = "Your account balance is 12,345 EUR."


def _row(**overrides):
    row = {
        "role": "model",
        "content": SECRET,
        "function_call": None,
        "tool_result": None,
        "created_at": "2026-08-24T10:00:00+00:00",
        "metadata": {},
        "message_index": 1,
    }
    row.update(overrides)
    return row


def test_content_for_llm_redacts_a_deleted_row():
    redacted = content_for_llm(_row(metadata={"deleted": True}))

    assert SECRET not in (redacted or "")
    assert redacted == DELETED_MESSAGE_PLACEHOLDER


def test_content_for_llm_passes_through_a_live_row():
    assert content_for_llm(_row()) == SECRET


def test_content_for_llm_tolerates_missing_metadata():
    assert content_for_llm(_row(metadata=None)) == SECRET


def test_content_for_llm_ignores_non_dict_metadata():
    """Bad metadata must not crash history loading for the whole session."""
    assert content_for_llm(_row(metadata="oops")) == SECRET


def test_row_to_conversation_message_redacts_deleted_content():
    message = EnhancedSupabaseClient._row_to_conversation_message(
        _row(metadata={"deleted": True, "agent_instance_id": "agent-7"})
    )

    assert message.content == DELETED_MESSAGE_PLACEHOLDER
    assert SECRET not in message.content
    # The flag itself still reaches callers that inspect metadata.
    assert message.metadata["deleted"] is True
    assert message.metadata["agent_instance_id"] == "agent-7"


def test_row_to_conversation_message_leaves_live_content_alone():
    message = EnhancedSupabaseClient._row_to_conversation_message(_row())

    assert message.content == SECRET


@pytest.mark.asyncio
async def test_get_conversation_history_redacts_deleted_messages(monkeypatch):
    """End-to-end: the main history loader must not hand the model the text."""
    rows = [
        _row(role="user", content="what is my balance?", message_index=0),
        _row(metadata={"deleted": True}, message_index=1),
    ]

    client = EnhancedSupabaseClient.__new__(EnhancedSupabaseClient)
    monkeypatch.setattr(client, "_get_client", lambda: _FakeClient(rows), raising=False)

    history = await client.get_conversation_history(session_id="sess-1")

    contents = [m.content for m in history]
    assert SECRET not in " ".join(c or "" for c in contents)
    assert DELETED_MESSAGE_PLACEHOLDER in contents


class _FakeQuery:
    def __init__(self, table, rows):
        self._table = table
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def execute(self):
        from types import SimpleNamespace

        if self._table == "chat_sessions":
            return SimpleNamespace(data=[{"id": "session-uuid", "organization_id": None}])
        return SimpleNamespace(data=list(reversed(self._rows)))


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows

    def table(self, name):
        return _FakeQuery(name, self._rows)


def test_summary_formatting_redacts_deleted_messages():
    """Rolling summaries are model context too -- same rule applies."""
    from orchestrator.services.conversation_summarizer import format_messages_for_summary

    rows = [
        {"role": "user", "content": "what is my balance?", "metadata": {}},
        {"role": "model", "content": SECRET, "metadata": {"deleted": True}},
    ]

    text = format_messages_for_summary(rows)

    assert SECRET not in text
    assert DELETED_MESSAGE_PLACEHOLDER in text
    assert "what is my balance?" in text
