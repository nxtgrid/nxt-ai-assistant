"""How far a chat has scrolled past a ticket's message.

Telegram message ids increment by one per message within a chat, so the gap
between the newest id on record and a given anchor id approximates how many
messages have been posted since. TicketUpdateNotifier uses that to choose
between editing a ticket's message in place and posting a fresh reply.

Read-only, and derived from tables Anansi already writes:

* ``chat_messages.telegram_message_id`` -- group traffic the bot observes,
  including the escalation group (see the passive-save call in ``handler.py``)
* ``message_deliveries.external_message_id`` -- the bot's own ticket posts

Both reads are bounded by a recency window. ``chat_messages`` has no index on
``group_id`` alone, but ``chat_messages_group_topic_msg_idx``
(0016_chat_messages_topic.sql) covers ``(group_id, telegram_topic_id,
telegram_message_id DESC)``, and an anchor older than the window is stale by
definition -- for which "post a fresh message" is the right answer regardless.

Counted **per topic**, not chat-wide, when a caller supplies one: every grid
resolves to one shared Telegram group with a *topic per grid* (see
``shared/auth/auth_service.py``'s grid->target resolution), so a chat-wide
count reads a burst in an unrelated topic as "scrolled past" within seconds
even while the ticket's own topic sat silent -- production ids ran
65876->65882 in 40 seconds across five grids sharing one group. Passing
``topic_id=None`` keeps the original chat-wide behavior exactly (a caller
that doesn't know the topic, or a delivery anchor recorded before topics were
tracked).

The approximation runs slightly low: ``_save_passive_group_message`` skips
messages with no text or caption, so bare photos do not advance the head.
Under-counting biases toward editing in place, which is the pre-existing
behavior, so it degrades rather than misbehaves.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

#: How far back to look for chat position. Long enough to cover any ticket
#: still being actively discussed, short enough to keep the group_id scan
#: bounded by the created_at index.
LOOKBACK_DAYS = 7


class ChatWatermarkRepository:
    """Reads the newest known Telegram message id for a chat.

    Every method is best-effort: this is positioning telemetry sitting in the
    path of ticket closes, so a database hiccup must degrade the notifier's
    placement decision rather than fail the close.
    """

    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        if client is None and get_client is None:
            raise ValueError("ChatWatermarkRepository requires either `client` or `get_client`")
        self._client_instance = client
        self._get_client = get_client

    def _raw_client(self) -> Optional[Any]:
        if self._client_instance is not None:
            return self._client_instance
        if self._get_client is not None:
            try:
                return self._get_client()
            except Exception:
                LOGGER.warning("chat watermark: get_client() raised", exc_info=True)
                return None
        return None

    @staticmethod
    def _since() -> str:
        return (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    async def head(self, chat_id: str, topic_id: Optional[str] = None) -> Optional[int]:
        """Newest message id on record for ``chat_id``, or None if unknown.

        ``topic_id`` scopes both reads to that forum topic when given;
        ``None`` keeps the original chat-wide behavior.
        """
        client = self._raw_client()
        if client is None or not chat_id:
            return None

        since = self._since()
        candidates: List[int] = []

        try:
            query = (
                client.table("chat_messages")
                .select("telegram_message_id")
                .eq("group_id", str(chat_id))
            )
            if topic_id is not None:
                query = query.eq("telegram_topic_id", str(topic_id))
            response = (
                query.gte("created_at", since)
                .order("telegram_message_id", desc=True)
                .limit(1)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if rows and rows[0].get("telegram_message_id"):
                candidates.append(int(rows[0]["telegram_message_id"]))
        except Exception:
            LOGGER.debug("chat watermark: chat_messages read failed for {}", chat_id, exc_info=True)

        try:
            query = (
                client.table("message_deliveries")
                .select("external_message_id")
                .eq("external_chat_id", str(chat_id))
            )
            if topic_id is not None:
                query = query.eq("external_topic_id", str(topic_id))
            response = (
                query.gte("sent_at", since)
                .order("external_message_id", desc=True)
                .limit(1)
                .execute()
            )
            rows = getattr(response, "data", None) or []
            if rows and rows[0].get("external_message_id"):
                candidates.append(int(rows[0]["external_message_id"]))
        except Exception:
            LOGGER.debug(
                "chat watermark: message_deliveries read failed for {}", chat_id, exc_info=True
            )

        return max(candidates) if candidates else None

    async def messages_since(
        self, chat_id: str, anchor_message_id: int, topic_id: Optional[str] = None
    ) -> int:
        """Approximate message count posted in ``chat_id`` after ``anchor_message_id``.

        Returns 0 when unknown, which the notifier reads as "still on screen"
        -- matching how ticket messages behaved before this existed.
        ``topic_id`` scopes the count to that forum topic when given.
        """
        head = await self.head(chat_id, topic_id=topic_id)
        if head is None or head <= anchor_message_id:
            return 0
        return head - anchor_message_id
