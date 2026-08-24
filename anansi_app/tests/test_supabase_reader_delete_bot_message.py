"""Unit tests for SupabaseReader.delete_bot_message's soft-delete contract.

Deleting a bot message removes it from Telegram but MUST keep the original
text in chat_messages -- the row is the operator-facing audit record of what
the bot actually said. The ``metadata.deleted`` flag is what marks it, not a
destroyed ``content`` column.

A small fake supabase-py client captures the update payload so the assertions
run against the real method rather than a mock of it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from services.supabase_reader import SupabaseReader

ORIGINAL = "The meter reading for MTR-001 is 42 kWh."


class _FakeTable:
    def __init__(self, store: dict[str, Any]):
        self._store = store
        self._mode: str | None = None
        self._payload: dict[str, Any] | None = None

    def select(self, *_args, **_kwargs):
        self._mode = "select"
        return self

    def update(self, payload):
        self._mode = "update"
        self._payload = payload
        return self

    def eq(self, _col, _val):
        return self

    def single(self):
        return self

    def execute(self):
        if self._mode == "select":
            return SimpleNamespace(data={"metadata": self._store["metadata"]})
        self._store["updates"].append(self._payload)
        return SimpleNamespace(data=[{}])


class _FakeClient:
    def __init__(self, store: dict[str, Any]):
        self._store = store

    def table(self, _name):
        return _FakeTable(self._store)


def _reader(existing_metadata: dict[str, Any] | None = None):
    store: dict[str, Any] = {"metadata": existing_metadata or {}, "updates": []}
    reader = SupabaseReader.__new__(SupabaseReader)
    reader.client = _FakeClient(store)
    return reader, store


def _telegram_ok(monkeypatch, ok: bool = True):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")

    def fake_post(*_args, **_kwargs):
        return SimpleNamespace(
            json=lambda: {"ok": ok} if ok else {"ok": False, "description": "message too old"}
        )

    monkeypatch.setattr("requests.post", fake_post)


def test_delete_bot_message_does_not_overwrite_content(monkeypatch):
    """The bot's original words stay in the chat db after deletion."""
    _telegram_ok(monkeypatch)
    reader, store = _reader()

    reader.delete_bot_message(message_id="msg-1", chat_id="123", telegram_message_id=555)

    assert len(store["updates"]) == 1
    payload = store["updates"][0]
    assert "content" not in payload, (
        "delete_bot_message must not touch the content column -- the message "
        f"text is the audit record. Got payload: {payload}"
    )


def test_delete_bot_message_sets_deleted_flag(monkeypatch):
    _telegram_ok(monkeypatch)
    reader, store = _reader()

    reader.delete_bot_message(message_id="msg-1", chat_id="123", telegram_message_id=555)

    metadata = store["updates"][0]["metadata"]
    assert metadata["deleted"] is True
    assert metadata["deleted_at"]


def test_delete_bot_message_preserves_unrelated_metadata(monkeypatch):
    """agent_instance_id and friends survive -- reply routing depends on them."""
    _telegram_ok(monkeypatch)
    reader, store = _reader({"agent_instance_id": "agent-7", "total_tokens": 120})

    reader.delete_bot_message(message_id="msg-1", chat_id="123", telegram_message_id=555)

    metadata = store["updates"][0]["metadata"]
    assert metadata["agent_instance_id"] == "agent-7"
    assert metadata["total_tokens"] == 120


def test_delete_bot_message_records_telegram_rejection(monkeypatch):
    """A message Telegram refused to delete is still visible to the customer.

    The flag must say so, otherwise the admin UI claims a removal that never
    happened (Telegram rejects group deletions past 48h).
    """
    _telegram_ok(monkeypatch, ok=False)
    reader, store = _reader()

    result = reader.delete_bot_message(
        message_id="msg-1", chat_id="123", telegram_message_id=555
    )

    metadata = store["updates"][0]["metadata"]
    assert metadata["deleted_from_telegram"] is False
    assert result["telegram_deleted"] is False


def test_delete_bot_message_records_telegram_success(monkeypatch):
    _telegram_ok(monkeypatch, ok=True)
    reader, store = _reader()

    result = reader.delete_bot_message(
        message_id="msg-1", chat_id="123", telegram_message_id=555
    )

    assert store["updates"][0]["metadata"]["deleted_from_telegram"] is True
    assert result["success"] is True
    assert result["telegram_deleted"] is True
