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
    def __init__(
        self,
        rows: List[Dict[str, Any]],
        raises: bool = False,
        eq_calls: Optional[List[tuple]] = None,
    ) -> None:
        self._rows = rows
        self._raises = raises
        self._filters: List[tuple] = []
        # Recorded for tests that assert *which* filters were requested,
        # independent of whether any fixture row happens to carry that
        # column (see eq_calls_by_table on _FakeClient).
        self.eq_calls = eq_calls if eq_calls is not None else []

    def select(self, *_a, **_k) -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append((col, val))
        self.eq_calls.append((col, val))
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

        def _row_matches(row: Dict[str, Any]) -> bool:
            # A column absent from a fixture row is "don't care" (matches
            # any filter on it) -- lets every pre-existing, topic-agnostic
            # fixture in this file keep matching unchanged, while a fixture
            # that DOES set e.g. telegram_topic_id gets real filtering.
            return all(col not in row or row[col] == val for col, val in self._filters)

        return _FakeResponse([row for row in self._rows if _row_matches(row)])


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
        self.eq_calls_by_table: Dict[str, List[tuple]] = {
            "chat_messages": [],
            "message_deliveries": [],
        }

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(
            self._by_table.get(name, []),
            raises=name in self._raising,
            eq_calls=self.eq_calls_by_table.setdefault(name, []),
        )


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


# ---------------------------------------------------------------------------
# Topic scoping (plan B6) -- every grid is a *topic* within one shared
# Telegram group, so a chat-wide count reads a burst in a different topic as
# "scrolled past" within seconds even though the operator's own topic sat
# silent. topic_id=None must keep today's chat-wide behavior exactly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_head_with_no_topic_does_not_filter_by_topic():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    assert await ChatWatermarkRepository(client=client).head("-100123") == 120
    assert "telegram_topic_id" not in dict(client.eq_calls_by_table["chat_messages"])


@pytest.mark.asyncio
async def test_head_with_a_topic_filters_chat_messages_by_it():
    client = _FakeClient(chat_messages=[{"telegram_message_id": 120}])
    await ChatWatermarkRepository(client=client).head("-100123", topic_id="ops-topic")
    assert ("telegram_topic_id", "ops-topic") in client.eq_calls_by_table["chat_messages"]


@pytest.mark.asyncio
async def test_head_with_a_topic_filters_deliveries_by_external_topic_id():
    client = _FakeClient(deliveries=[{"external_message_id": 140}])
    await ChatWatermarkRepository(client=client).head("-100123", topic_id="ops-topic")
    assert ("external_topic_id", "ops-topic") in client.eq_calls_by_table["message_deliveries"]


@pytest.mark.asyncio
async def test_head_with_topic_ignores_a_different_topics_traffic():
    """The production shape this fixes: five grids share one group, so a
    burst in another topic pushed ids from 65876 to 65882 in 40 seconds --
    the operator's own topic's real head (100) must not be shadowed by it."""
    client = _FakeClient(
        chat_messages=[
            {"telegram_topic_id": "other-grid", "telegram_message_id": 65882},
            {"telegram_topic_id": "ops-topic", "telegram_message_id": 100},
        ]
    )
    assert (
        await ChatWatermarkRepository(client=client).head("-100123", topic_id="ops-topic")
        == 100
    )


@pytest.mark.asyncio
async def test_messages_since_with_matching_topic_traffic_reports_the_real_gap():
    """Same-topic traffic *does* scroll the anchor -- update_notifier reads
    this as "post a fresh reply", the correct behavior when the ticket's own
    topic actually moved."""
    client = _FakeClient(
        chat_messages=[{"telegram_topic_id": "ops-topic", "telegram_message_id": 130}]
    )
    gap = await ChatWatermarkRepository(client=client).messages_since(
        "-100123", 100, topic_id="ops-topic"
    )
    assert gap == 30


@pytest.mark.asyncio
async def test_messages_since_ignores_a_different_topics_burst():
    """Traffic in a different topic of the same chat must not scroll the
    anchor -- update_notifier reads gap<=SCROLL_THRESHOLD as "still on
    screen" and edits in place."""
    client = _FakeClient(
        chat_messages=[{"telegram_topic_id": "other-grid", "telegram_message_id": 65882}]
    )
    gap = await ChatWatermarkRepository(client=client).messages_since(
        "-100123", 100, topic_id="ops-topic"
    )
    assert gap == 0
