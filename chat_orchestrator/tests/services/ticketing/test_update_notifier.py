"""TicketUpdateNotifier: scroll-aware ticket update delivery.

Placement policy under test:
  * anchor still on screen (<= SCROLL_THRESHOLD messages since) -> edit it
  * anchor scrolled away                                        -> fresh reply
  * edit rejected by Telegram                                   -> fresh reply
  * no anchor at all                                            -> stay silent
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.update_notifier import (
    SCROLL_THRESHOLD,
    TicketEvent,
    TicketUpdateNotifier,
)


class _FakeTickets:
    def __init__(self, record: Optional[Any] = None, comments: Optional[List[Dict]] = None) -> None:
        self._record = record
        self._comments = comments or []

    async def get_by_ref(self, ref: str) -> Any:
        return self._record

    async def list_comments_by_ref(self, ref: str, *, limit: int = 5) -> List[Dict]:
        return self._comments


class _FakeDeliveries:
    def __init__(self, anchor: Optional[Dict] = None) -> None:
        self._anchor = anchor
        self.recorded: List[Dict] = []

    async def latest_for_ticket(self, ticket_id: str) -> Optional[Dict]:
        return self._anchor

    async def record(self, **kwargs: Any) -> Dict:
        self.recorded.append(kwargs)
        return kwargs


class _FakeWatermark:
    def __init__(self, gap: int = 0) -> None:
        self._gap = gap
        self.calls: List[Dict[str, Any]] = []

    async def messages_since(
        self, chat_id: str, anchor_message_id: int, topic_id: Optional[str] = None
    ) -> int:
        self.calls.append(
            {"chat_id": chat_id, "anchor_message_id": anchor_message_id, "topic_id": topic_id}
        )
        return self._gap


class _Record:
    id = "t-1"
    ticket_ref = "ANS-42"
    backend = "internal"
    status = "done"
    summary = "Inverter 3 offline"


def _notifier(*, gap: int, anchor: Optional[Dict], edit_ok: bool = True, tickets=None):
    edits: List[Dict] = []
    sends: List[Dict] = []

    async def _edit(bot_token, chat_id, message_id, text, parse_mode=None):
        edits.append({"chat_id": chat_id, "message_id": message_id, "text": text})
        return edit_ok

    async def _send(bot_token, chat_id, text, parse_mode=None, topic_id=None,
                    reply_to_message_id=None):
        sends.append({"chat_id": chat_id, "text": text,
                      "reply_to_message_id": reply_to_message_id})
        return 9999

    deliveries = _FakeDeliveries(anchor=anchor)
    notifier = TicketUpdateNotifier(
        tickets=tickets or _FakeTickets(record=_Record()),
        deliveries=deliveries,
        watermark=_FakeWatermark(gap=gap),
        bot_token="tok",
        gateway=None,
        model="fake-model",
        edit_fn=_edit,
        send_fn=_send,
    )
    return notifier, edits, sends, deliveries


_ANCHOR = {
    "external_chat_id": "-100123",
    "external_topic_id": "77",
    "external_message_id": 500,
}


@pytest.mark.asyncio
async def test_threads_the_anchors_topic_id_into_the_watermark_call():
    """B6: the anchor already carries external_topic_id (the _ANCHOR fixture
    below, also used for the send/reply itself) -- it must also reach
    messages_since, so the scroll decision is scoped to the ticket's own
    topic instead of the whole shared group."""

    async def _edit(bot_token, chat_id, message_id, text, parse_mode=None) -> bool:
        return True

    watermark = _FakeWatermark(gap=0)
    notifier = TicketUpdateNotifier(
        tickets=_FakeTickets(record=_Record()),
        deliveries=_FakeDeliveries(anchor=_ANCHOR),
        watermark=watermark,
        bot_token="tok",
        gateway=None,
        model="fake-model",
        edit_fn=_edit,
    )

    await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition", to_status="done"))

    assert len(watermark.calls) == 1
    assert watermark.calls[0]["chat_id"] == "-100123"
    assert watermark.calls[0]["anchor_message_id"] == 500
    assert watermark.calls[0]["topic_id"] == "77"


@pytest.mark.asyncio
async def test_edits_in_place_when_anchor_is_still_on_screen():
    notifier, edits, sends, _ = _notifier(gap=SCROLL_THRESHOLD, anchor=_ANCHOR)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert len(edits) == 1
    assert edits[0]["message_id"] == 500
    assert sends == []


@pytest.mark.asyncio
async def test_posts_fresh_reply_once_the_anchor_has_scrolled():
    notifier, edits, sends, deliveries = _notifier(gap=SCROLL_THRESHOLD + 1, anchor=_ANCHOR)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert edits == []
    assert len(sends) == 1
    assert sends[0]["reply_to_message_id"] == 500
    # The new message becomes the anchor for the next update.
    assert deliveries.recorded[0]["external_message_id"] == 9999
    assert deliveries.recorded[0]["purpose"] == "update"


@pytest.mark.asyncio
async def test_falls_back_to_a_fresh_reply_when_the_edit_is_rejected():
    notifier, edits, sends, _ = _notifier(gap=0, anchor=_ANCHOR, edit_ok=False)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is True
    assert len(edits) == 1
    assert len(sends) == 1


@pytest.mark.asyncio
async def test_stays_silent_when_the_ticket_was_never_announced():
    """No anchor and no fallback destination -- most callers (e.g.
    TicketService.transition_to_done's own generic callers) have neither, so
    this must stay the default."""
    notifier, edits, sends, _ = _notifier(gap=0, anchor=None)
    posted = await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                               to_status="done"))
    assert posted is False
    assert edits == []
    assert sends == []


@pytest.mark.asyncio
async def test_constructs_a_fresh_message_when_no_anchor_but_a_fallback_is_given():
    """A ticket the bot never announced (e.g. filed directly in Jira) can
    still get its first update if the caller knows where it belongs -- the
    Jira webhook handler, via the escalation mapping's chat/topic. The
    result must read like an original notification: a fresh top-level post,
    not a reply to anything, and it becomes the anchor for future updates."""
    notifier, edits, sends, deliveries = _notifier(gap=0, anchor=None)

    posted = await notifier.notify(TicketEvent(
        ticket_ref="ANS-42", kind="transition", to_status="in_progress",
        fallback_chat_id="-100999", fallback_topic_id="42",
    ))

    assert posted is True
    assert edits == []
    assert len(sends) == 1
    assert sends[0]["chat_id"] == "-100999"
    assert sends[0]["reply_to_message_id"] is None
    assert deliveries.recorded[0]["external_chat_id"] == "-100999"
    assert deliveries.recorded[0]["external_topic_id"] == "42"
    assert deliveries.recorded[0]["external_message_id"] == 9999
    # Not "update" -- this is the ticket's first-ever delivery.
    assert deliveries.recorded[0]["purpose"] == "notification"


@pytest.mark.asyncio
async def test_fallback_message_send_failure_returns_false(monkeypatch):
    async def _send_fails(*_a, **_k):
        return None

    notifier = TicketUpdateNotifier(
        tickets=_FakeTickets(record=_Record()),
        deliveries=_FakeDeliveries(anchor=None),
        watermark=_FakeWatermark(gap=0),
        bot_token="tok",
        send_fn=_send_fails,
    )

    posted = await notifier.notify(TicketEvent(
        ticket_ref="ANS-42", kind="transition", to_status="in_progress",
        fallback_chat_id="-100999", fallback_topic_id=None,
    ))

    assert posted is False


@pytest.mark.asyncio
async def test_stays_silent_for_an_unknown_ticket_ref():
    notifier, _, _, _ = _notifier(gap=0, anchor=_ANCHOR, tickets=_FakeTickets(record=None))
    posted = await notifier.notify(TicketEvent(ticket_ref="NOPE-1", kind="transition",
                                               to_status="done"))
    assert posted is False


@pytest.mark.asyncio
async def test_notify_never_raises_into_the_caller():
    """A ticket close must succeed even when the notifier is entirely broken."""

    class _Exploding:
        async def get_by_ref(self, ref):
            raise RuntimeError("db gone")

    notifier, _, _, _ = _notifier(gap=0, anchor=_ANCHOR, tickets=_Exploding())
    assert await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                             to_status="done")) is False


@pytest.mark.asyncio
async def test_stays_silent_with_no_bot_token():
    edits: List[Dict] = []

    async def _edit(*_a, **_k):
        edits.append({})
        return True

    notifier = TicketUpdateNotifier(
        tickets=_FakeTickets(record=_Record()),
        deliveries=_FakeDeliveries(anchor=_ANCHOR),
        watermark=_FakeWatermark(gap=0),
        bot_token="",
        edit_fn=_edit,
    )
    assert await notifier.notify(TicketEvent(ticket_ref="ANS-42", kind="transition",
                                             to_status="done")) is False
    assert edits == []


@pytest.mark.asyncio
async def test_comment_events_require_significance_classification():
    """Without a configured LLM gateway, comments must not spam the group --
    only transitions are unconditionally significant."""
    notifier, edits, sends, _ = _notifier(gap=0, anchor=_ANCHOR)
    posted = await notifier.notify(
        TicketEvent(ticket_ref="ANS-42", kind="comment", comment_body="Fixed the fuse.")
    )
    assert posted is False
    assert edits == []
    assert sends == []


@pytest.mark.asyncio
async def test_significant_comment_posts_when_the_gateway_says_so():
    class _Gateway:
        async def generate(self, messages, options):
            class _Result:
                text = '{"significant": true, "summary": "root cause found"}'

            return _Result()

    edits: List[Dict] = []

    async def _edit(bot_token, chat_id, message_id, text, parse_mode=None):
        edits.append({"message_id": message_id})
        return True

    notifier = TicketUpdateNotifier(
        tickets=_FakeTickets(record=_Record()),
        deliveries=_FakeDeliveries(anchor=_ANCHOR),
        watermark=_FakeWatermark(gap=0),
        bot_token="tok",
        gateway=_Gateway(),
        model="fake-model",
        edit_fn=_edit,
    )
    posted = await notifier.notify(
        TicketEvent(ticket_ref="ANS-42", kind="comment", comment_body="Root cause: blown fuse.")
    )
    assert posted is True
    assert len(edits) == 1
