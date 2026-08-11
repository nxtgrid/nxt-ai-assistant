"""Tests for SupabaseClient.save_messages stamping telegram_topic_id (plan
B6, docs/superpowers/plans/2026-08-11-ticketing-noise-and-correlation-cutover.md).

ChatWatermarkRepository counted "messages since" chat-wide, but every grid is
a *topic* within one shared Telegram group -- production ids ran
65876->65882 in 40 seconds across five grids sharing one group, so any
anchor read as scrolled-past within seconds even though the operator's own
topic sat silent all day. chat_messages carried no topic (only chat_sessions
did); save_messages is the single choke point every writer goes through, so
stamping it there (from the session row) means every caller benefits without
each one needing to pass a new parameter.

Uses the same small fake-fluent-client style as
test_supabase_client_message_archival.py, extended with `.insert()` and a
second (`chat_sessions`) table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.models.schemas import ConversationMessage
from orchestrator.services.supabase_client import SupabaseClient


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeSelectQuery:
    """Supports the max-message-index lookup (chat_messages) and the
    single-row session lookup (chat_sessions) -- both are eq/order/limit/execute."""

    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self._filters: List[tuple] = []

    def select(self, *_args, **_kwargs) -> "_FakeSelectQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeSelectQuery":
        self._filters.append((col, val))
        return self

    def order(self, *_args, **_kwargs) -> "_FakeSelectQuery":
        return self

    def limit(self, _n: int) -> "_FakeSelectQuery":
        return self

    def execute(self) -> _FakeResponse:
        matched = [
            row
            for row in self._rows
            if all(row.get(col) == val for col, val in self._filters)
        ]
        return _FakeResponse(matched)


class _FakeInsertQuery:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def execute(self) -> _FakeResponse:
        # Echo back what was inserted, as the real Supabase client does.
        return _FakeResponse(list(self._rows))


class _FakeMessagesTable:
    def __init__(self) -> None:
        self.inserted_rows: List[Dict[str, Any]] = []

    def select(self, *args, **kwargs) -> _FakeSelectQuery:
        return _FakeSelectQuery([]).select(*args, **kwargs)  # no prior messages

    def insert(self, rows: List[Dict[str, Any]]) -> _FakeInsertQuery:
        self.inserted_rows.extend(rows)
        return _FakeInsertQuery(rows)


class _FakeSessionsTable:
    def __init__(self, sessions: List[Dict[str, Any]]) -> None:
        self._sessions = sessions

    def select(self, *args, **kwargs) -> _FakeSelectQuery:
        return _FakeSelectQuery(self._sessions).select(*args, **kwargs)


class _FakeRawClient:
    def __init__(self, sessions: List[Dict[str, Any]]) -> None:
        self.messages = _FakeMessagesTable()
        self.sessions = _FakeSessionsTable(sessions)

    def table(self, name: str) -> Any:
        if name == "chat_messages":
            return self.messages
        if name == "chat_sessions":
            return self.sessions
        raise AssertionError(f"unexpected table {name!r}")


def _make_client(sessions: List[Dict[str, Any]]) -> "tuple[SupabaseClient, _FakeRawClient]":
    client = SupabaseClient(url="https://example.test", key="test-key")
    raw = _FakeRawClient(sessions)
    client._get_client = lambda: raw  # type: ignore[method-assign]
    return client, raw


SESSION_UUID = "11111111-1111-1111-1111-111111111111"


def _session_row(telegram_topic_id: Optional[str]) -> Dict[str, Any]:
    return {
        "id": SESSION_UUID,
        "session_id": "hashed-session-id",
        "telegram_topic_id": telegram_topic_id,
    }


class TestSaveMessagesStampsTopicId:
    @pytest.mark.asyncio
    async def test_stamps_telegram_topic_id_from_the_session_row(self):
        client, raw = _make_client([_session_row("42")])

        saved = await client.save_messages(
            SESSION_UUID,
            [ConversationMessage(role="user", content="hello")],
            group_id="-100555",
        )

        assert raw.messages.inserted_rows[0]["telegram_topic_id"] == "42"
        assert saved[0].telegram_topic_id == "42"

    @pytest.mark.asyncio
    async def test_omits_topic_id_when_the_session_has_none(self):
        client, raw = _make_client([_session_row(None)])

        await client.save_messages(
            SESSION_UUID,
            [ConversationMessage(role="user", content="hello")],
            group_id="-100555",
        )

        assert "telegram_topic_id" not in raw.messages.inserted_rows[0]

    @pytest.mark.asyncio
    async def test_omits_topic_id_when_the_session_is_unknown(self):
        """Best-effort: a session lookup miss (or failure -- get_session_by_id
        already swallows its own exceptions) must not block saving messages,
        it just means no topic-scoping is possible for this batch."""
        client, raw = _make_client([])  # no matching session row at all

        saved = await client.save_messages(
            SESSION_UUID,
            [ConversationMessage(role="user", content="hello")],
            group_id="-100555",
        )

        assert "telegram_topic_id" not in raw.messages.inserted_rows[0]
        assert len(saved) == 1

    @pytest.mark.asyncio
    async def test_stamps_every_message_in_a_multi_message_batch(self):
        client, raw = _make_client([_session_row("7")])

        await client.save_messages(
            SESSION_UUID,
            [
                ConversationMessage(role="user", content="first"),
                ConversationMessage(role="model", content="second"),
            ],
            group_id="-100555",
        )

        assert [row["telegram_topic_id"] for row in raw.messages.inserted_rows] == [
            "7",
            "7",
        ]
