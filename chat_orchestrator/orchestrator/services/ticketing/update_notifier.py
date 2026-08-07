"""TicketUpdateNotifier -- the single author of Telegram ticket updates.

Anansi, not Jira, posts ticket updates. Both backends flow through here, so a
Jira transition and an internal transition produce an identical card from
identical code.

Placement policy
----------------
Every ticket already has an anchor: the most recent Telegram message Anansi
posted about it (``message_deliveries``). When that anchor is still on screen
the card is edited in place, which keeps one message per ticket instead of a
growing trail. Once roughly ``SCROLL_THRESHOLD`` messages have gone by, an
edit would go unnoticed, so a fresh reply to the anchor is posted instead --
and that reply becomes the next anchor.

Because the card is a full current-state render (see ``update_render``),
both branches emit the same text and neither depends on message history.

Everything here is best-effort. ``notify()`` returns a bool and never raises:
it runs inside ticket-close paths, and a Telegram outage must not roll back a
closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, Optional

from shared.utils.logging import get_logger
from shared.utils.telegram_markdown import convert_github_to_telegram_markdown

from .update_render import (
    classify_significance,
    fallback_summary,
    render_update_card,
    summarize_activity,
)

LOGGER = get_logger(__name__)

#: How many messages may pass before the anchor counts as scrolled away.
#: Matches the operator's rule of thumb: an update to a ticket message more
#: than five messages back will not be seen, so post a fresh reply.
SCROLL_THRESHOLD = 5

#: How many recent comments feed the activity summary.
COMMENT_WINDOW = 5


@dataclass(frozen=True)
class TicketEvent:
    """Something that happened to a ticket and may deserve a Telegram update."""

    ticket_ref: str
    kind: Literal["transition", "comment"]
    from_status: str = ""
    to_status: str = ""
    comment_body: str = ""
    comment_author: str = ""
    ticket_url: Optional[str] = None
    #: Where to post this ticket's *first* update card when it has never
    #: been announced on Telegram (no message_deliveries row at all -- e.g.
    #: a ticket filed directly in Jira, with no /notify or escalation post
    #: to anchor on). Only populated by callers that actually know a
    #: destination for this ticket, e.g. the Jira webhook handler via the
    #: escalation mapping's chat/topic. Left unset, the notifier keeps its
    #: original behavior: stay silent rather than guess where to post.
    fallback_chat_id: Optional[str] = None
    fallback_topic_id: Optional[str] = None


class TicketUpdateNotifier:
    """Renders and places ticket update cards on Telegram.

    Collaborators are injected rather than constructed so the placement policy
    can be tested without a database, an LLM, or a bot token -- the same
    posture ``AlertCorrelator`` takes.
    """

    def __init__(
        self,
        *,
        tickets: Any,
        deliveries: Any,
        watermark: Any,
        bot_token: str,
        gateway: Any = None,
        model: str = "",
        edit_fn: Optional[Callable[..., Awaitable[bool]]] = None,
        send_fn: Optional[Callable[..., Awaitable[Optional[int]]]] = None,
    ) -> None:
        self._tickets = tickets
        self._deliveries = deliveries
        self._watermark = watermark
        self._bot_token = bot_token
        self._gateway = gateway
        self._model = model
        self._edit_fn = edit_fn
        self._send_fn = send_fn

    # -- injected transport ------------------------------------------------

    async def _edit(self, chat_id: str, message_id: int, text: str) -> bool:
        if self._edit_fn is not None:
            return await self._edit_fn(
                self._bot_token, chat_id, message_id, text, parse_mode="Markdown"
            )
        from shared.utils.telegram_send import edit_telegram_message

        return await edit_telegram_message(
            self._bot_token, chat_id, message_id, text, parse_mode="Markdown"
        )

    async def _send(
        self, chat_id: str, text: str, topic_id: Optional[str], reply_to: Optional[int]
    ) -> Optional[int]:
        if self._send_fn is not None:
            return await self._send_fn(
                self._bot_token,
                chat_id,
                text,
                parse_mode="Markdown",
                topic_id=topic_id,
                reply_to_message_id=reply_to,
            )
        from shared.utils.telegram_send import send_telegram_message_with_fallback

        return await send_telegram_message_with_fallback(
            self._bot_token,
            chat_id,
            text,
            parse_mode="Markdown",
            topic_id=topic_id,
            reply_to_message_id=reply_to,
        )

    async def _send_fallback(self, ticket: Any, event: TicketEvent, text: str) -> bool:
        """Post a ticket's first-ever update card, at a caller-supplied
        fallback destination, since there is no anchor message to place it
        against. A fresh top-level post (not a reply to anything), recorded
        as the ticket's ``notification`` delivery so later updates find it
        as their anchor."""
        chat_id = event.fallback_chat_id or ""
        if not chat_id:
            return False
        topic_id = event.fallback_topic_id

        message_id = await self._send(chat_id, text, topic_id, None)
        if message_id is None:
            LOGGER.warning(
                "ticket update: fallback send failed for {}", event.ticket_ref
            )
            return False

        try:
            await self._deliveries.record(
                ticket_id=ticket.id,
                escalation_id=None,
                purpose="notification",
                external_chat_id=chat_id,
                external_topic_id=str(topic_id) if topic_id is not None else None,
                external_message_id=int(message_id),
            )
        except Exception:
            LOGGER.warning(
                "ticket update: failed to record fallback delivery for {} -- the "
                "next update will post another fresh message instead of anchoring",
                event.ticket_ref,
                exc_info=True,
            )
        LOGGER.info(
            "ticket update: posted first-ever card for {} (chat={} msg={})",
            event.ticket_ref,
            chat_id,
            message_id,
        )
        return True

    # -- policy ------------------------------------------------------------

    async def _is_worth_posting(self, event: TicketEvent) -> bool:
        """Transitions always post; comments must clear the significance bar."""
        if event.kind == "transition":
            return True
        if self._gateway is None:
            # No classifier configured -- stay quiet rather than relay every
            # comment into the group.
            return False
        return await classify_significance(self._gateway, self._model, event.comment_body)

    async def _activity_line(self, event: TicketEvent) -> str:
        try:
            comments = await self._tickets.list_comments_by_ref(
                event.ticket_ref, limit=COMMENT_WINDOW
            )
        except Exception:
            LOGGER.warning(
                "ticket update: comment lookup failed for {}", event.ticket_ref, exc_info=True
            )
            comments = []
        if self._gateway is None:
            return fallback_summary(comments)
        return await summarize_activity(self._gateway, self._model, comments)

    # -- entry point -------------------------------------------------------

    async def notify(self, event: TicketEvent) -> bool:
        """Post or update this ticket's card. Returns True if Telegram was touched."""
        try:
            return await self._notify_inner(event)
        except Exception:
            LOGGER.warning(
                "ticket update: notification failed for {} (non-fatal)",
                event.ticket_ref,
                exc_info=True,
            )
            return False

    async def _notify_inner(self, event: TicketEvent) -> bool:
        if not self._bot_token:
            LOGGER.debug("ticket update: no bot token configured -- skipping")
            return False

        ticket = await self._tickets.get_by_ref(event.ticket_ref)
        if ticket is None:
            LOGGER.debug("ticket update: unknown ref {}", event.ticket_ref)
            return False

        if not await self._is_worth_posting(event):
            LOGGER.debug("ticket update: {} judged not significant", event.ticket_ref)
            return False

        anchor = await self._deliveries.latest_for_ticket(ticket.id)
        if not anchor and not event.fallback_chat_id:
            # Never announced on Telegram (e.g. a ticket filed by a sweep),
            # and this caller has no destination to start one at either.
            LOGGER.debug("ticket update: no delivery anchor for {}", event.ticket_ref)
            return False

        activity = await self._activity_line(event)
        card = render_update_card(
            ticket_ref=ticket.ticket_ref or event.ticket_ref,
            summary=ticket.summary or "",
            status=event.to_status or ticket.status,
            activity=activity,
            url=event.ticket_url,
        )
        text = convert_github_to_telegram_markdown(card)

        if not anchor:
            # No prior message, but the caller knows where this ticket
            # belongs (e.g. the Jira webhook handler, via the escalation
            # mapping's chat/topic). The card above is a full current-state
            # render, so this reads exactly like an original notification
            # would have -- and becomes the anchor for future updates.
            return await self._send_fallback(ticket, event, text)

        chat_id = str(anchor.get("external_chat_id") or "")
        topic_id = anchor.get("external_topic_id")
        anchor_message_id = int(anchor.get("external_message_id") or 0)
        if not chat_id or not anchor_message_id:
            return False

        gap = await self._watermark.messages_since(chat_id, anchor_message_id)
        if gap <= SCROLL_THRESHOLD:
            if await self._edit(chat_id, anchor_message_id, text):
                LOGGER.info(
                    "ticket update: edited {} in place (chat={} msg={} gap={})",
                    event.ticket_ref,
                    chat_id,
                    anchor_message_id,
                    gap,
                )
                # The anchor is unchanged, so there is no new receipt to write.
                return True
            LOGGER.warning(
                "ticket update: edit of msg={} rejected -- posting a reply instead",
                anchor_message_id,
            )

        message_id = await self._send(chat_id, text, topic_id, anchor_message_id)
        if message_id is None:
            LOGGER.warning("ticket update: send failed for {}", event.ticket_ref)
            return False

        try:
            await self._deliveries.record(
                ticket_id=ticket.id,
                escalation_id=None,
                purpose="update",
                external_chat_id=chat_id,
                external_topic_id=str(topic_id) if topic_id is not None else None,
                external_message_id=int(message_id),
            )
        except Exception:
            LOGGER.warning(
                "ticket update: failed to record receipt for {} -- the next update "
                "will anchor on the older message",
                event.ticket_ref,
                exc_info=True,
            )
        LOGGER.info(
            "ticket update: posted reply for {} (chat={} msg={} gap={})",
            event.ticket_ref,
            chat_id,
            message_id,
            gap,
        )
        return True
