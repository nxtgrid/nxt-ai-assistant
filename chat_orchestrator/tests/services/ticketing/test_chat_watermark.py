"""ChatWatermarkRepository: how far a chat has scrolled past a ticket message.

Derived from data Anansi already stores rather than a dedicated table:
chat_messages for observed group traffic, message_deliveries for the bot's
own ticket posts, whichever is higher.

The notifier calls messages_since() inside ticket-close paths, so an absent
or unreadable signal must degrade to 0 ("nothing has scrolled", i.e. edit in
place) rather than raise.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.chat_watermark import ChatWatermarkRepository


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], raises: bool = False) -> None:
        self._rows = rows
        self._raises = raises

    def select(self, *_a, **_k) -> "_FakeQuery":
        return self

    def eq(self, *_a, **_k) -> "_FakeQuery":
        return self

    def gte(self, *_a, **_k) -> "_FakeQuery":
        return self

    def order(self, *_a, **_k) -> "_FakeQuery":
        return self

    def limit(self, *_a, **_k) -> "_FakeQuery":
        return self

    def execute(self) -> _FakeResponse:
        if self._raises:
            raise RuntimeError("postgrest down")
        return _FakeResponse(self._rows)


class _FakeClient:
    """Serves a different row set per table, so each source can be tested alone."""

    def __init__(
        self,
        chat_messages: Optional[List[Dict[str, Any]]] = None,
        deliveries: Optional[List[Dict[str, Any]]] = None,
        raising_tables: Optional[set] = None,
    ) -> None:
        self._by_table = {
            "chat_messages": chat_messages or [],
            "message_deliveries": deliveries or [],
        }
        self._raising = raising_tables or set()

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self._by_table.get(name, []), raises=name in self._raising)


@pytest.mark.asyncio
async def test_head_reads_observed_group_traffic():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    assert await ChatWatermarkRepository(client=client).head("-100123") == 120


@pytest.mark.asyncio
async def test_head_reads_the_bots_own_ticket_posts():
    client = _FakeClient(deliveries=[{"external_message_id": 140}])
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_takes_the_higher_of_both_sources():
    client = _FakeClient(
        chat_messages=[{"telegram_message_id": 120}],
        deliveries=[{"external_message_id": 140}],
    )
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_survives_one_source_failing():
    """A broken source must not blind the other one."""
    client = _FakeClient(
        deliveries=[{"external_message_id": 140}],
        raising_tables={"chat_messages"},
    )
    assert await ChatWatermarkRepository(client=client).head("-100123") == 140


@pytest.mark.asyncio
async def test_head_is_none_when_nothing_is_on_record():
    assert await ChatWatermarkRepository(client=_FakeClient()).head("-100123") is None


@pytest.mark.asyncio
async def test_messages_since_returns_the_gap():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    assert await ChatWatermarkRepository(client=client).messages_since("-100123", 100) == 20


@pytest.mark.asyncio
async def test_messages_since_is_zero_when_nothing_is_on_record():
    """Unknown position must read as "still on screen" -- the same behavior
    as before any of this existed."""
    assert await ChatWatermarkRepository(client=_FakeClient()).messages_since("-100123", 100) == 0


@pytest.mark.asyncio
async def test_messages_since_is_zero_when_head_is_behind_the_anchor():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 50}])
    assert await ChatWatermarkRepository(client=client).messages_since("-100123", 100) == 0
