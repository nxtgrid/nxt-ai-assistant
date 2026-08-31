"""Shared Telegram message-sending helper.

Used by both handler.py and messaging_mcp_server.py to avoid duplication.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, cast

import aiohttp

from shared.utils.telegram_markdown import balance_markdown_entities, strip_markdown

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_DOC_BYTES = 45 * 1024 * 1024  # 45 MB (Telegram limit is 50 MB)
_MAX_TELEGRAM_MESSAGE_CHARS = 4096
_ESCALATION_TOPIC_COLOR = 16749490  # 0xFF93B2 pink — distinguishable from staff-created topics

# Reuse a single ClientSession across calls to avoid per-call TCP handshakes.
_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


def _is_markdown_v1(parse_mode: Optional[str]) -> bool:
    """True for Telegram's legacy Markdown mode, the only dialect we rebalance.

    HTML and MarkdownV2 have different escaping rules, so the v1 balancer and
    stripper must not be pointed at them.
    """
    return (parse_mode or "").strip().lower() == "markdown"


def _split_telegram_message(text: str) -> list[str]:
    """Split text into Telegram-sized chunks without dropping characters."""
    if len(text) <= _MAX_TELEGRAM_MESSAGE_CHARS:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > _MAX_TELEGRAM_MESSAGE_CHARS:
        split_at = remaining.rfind("\n", 0, _MAX_TELEGRAM_MESSAGE_CHARS)
        if split_at >= 0:
            split_at += 1
        else:
            split_at = remaining.rfind(" ", 0, _MAX_TELEGRAM_MESSAGE_CHARS)
            if split_at >= 0:
                split_at += 1
            else:
                split_at = _MAX_TELEGRAM_MESSAGE_CHARS
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    chunks.append(remaining)
    return chunks


async def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = None,
    topic_id: Optional[int | str] = None,
) -> Optional[int]:
    """Send a message to a Telegram chat.

    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        text: Message text to send
        reply_markup: Optional Telegram InlineKeyboardMarkup dict
        parse_mode: Optional parse mode (e.g., "HTML", "MarkdownV2")
        topic_id: Optional topic/thread ID for forum groups

    Returns:
        The message_id of the sent message, or None on failure.
    """
    import json

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup:
            payload["reply_markup"] = json.dumps(reply_markup)
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if topic_id is not None:
            payload["message_thread_id"] = int(topic_id)

        session = _get_session()
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.warning(f"Failed to send Telegram message: {error_text}")
                return None
            resp_json = await response.json()
            msg_id: Optional[int] = resp_json.get("result", {}).get("message_id")
            return msg_id
    except Exception as e:
        logger.warning(f"Error sending Telegram message: {e}")
        return None


def is_markdown_parse_error(result: Dict[str, Any]) -> bool:
    """True if a Telegram sendMessage response failed because of malformed Markdown.

    Telegram returns HTTP 400 with a "can't parse entities" description when the
    message text contains a character it reads as an unterminated Markdown entity
    — e.g. the lone underscore in "CLEAR_TAMPER" opens an italic entity that never
    closes. Callers retry such sends as plain text so the message still gets
    delivered (see CLAUDE.md "Telegram Message Formatting").
    """
    if not result or result.get("ok"):
        return False
    description = str(result.get("description", "")).lower()
    return result.get("error_code") == 400 and "can't parse entities" in description


async def send_telegram_message_raw(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = "Markdown",
    topic_id: Optional[int | str] = None,
    reply_to_message_id: Optional[int | str] = None,
) -> Dict[str, Any]:
    """Send a message with a plain-text retry, returning Telegram's raw response.

    Sends ``text`` with ``parse_mode`` (default ``"Markdown"``). Under Markdown
    v1 the text is first rebalanced so no delimiter is left opening an entity
    that never closes. If Telegram still rejects it with a "can't parse
    entities" 400, the text is resent with the markers stripped and no
    ``parse_mode``, so the content reaches the chat as clean prose rather than
    as visible markup.

    ``reply_to_message_id`` threads the send as a reply (used by alert
    correlation to reply to a ticket's original alert post for an amend
    update). Sent with ``allow_sending_without_reply: true`` so a deleted or
    otherwise-gone parent message degrades to a plain (non-reply) send
    instead of failing the whole request.

    Returns the decoded Telegram response dict. Callers that only need the
    message id should use :func:`send_telegram_message_with_fallback`; this
    variant exists for callers that must inspect ``ok``/``description`` —
    escalation routing keys off "message thread not found" to detect a stale
    topic id. On transport failure a synthetic ``{"ok": False, "description":
    ...}`` is returned rather than raising, so every caller sees one shape.
    """
    import json

    async def _post(payload: Dict[str, Any]) -> Dict[str, Any]:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        session = _get_session()
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            try:
                return cast(Dict[str, Any], await response.json())
            except Exception:
                return {
                    "ok": False,
                    "error_code": response.status,
                    "description": await response.text(),
                }

    # Rebalance here rather than at the caller: this is the single point every
    # Markdown send funnels through, including each chunk of a split message.
    # A split lands wherever the character budget runs out, so a chunk can
    # inherit an opening delimiter whose partner ended up in the other chunk.
    if _is_markdown_v1(parse_mode):
        text = balance_markdown_entities(text)

    payload: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    if parse_mode:
        payload["parse_mode"] = parse_mode
    if topic_id is not None and str(topic_id).strip() != "":
        # A malformed topic_id must not sink the whole send — drop the thread id
        # and deliver to the group root instead of raising into the outer except.
        try:
            payload["message_thread_id"] = int(topic_id)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid topic_id %r for chat %s", topic_id, chat_id)
    if reply_to_message_id is not None and str(reply_to_message_id).strip() != "":
        try:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        except (TypeError, ValueError):
            logger.warning(
                "Ignoring invalid reply_to_message_id %r for chat %s", reply_to_message_id, chat_id
            )

    try:
        result = await _post(payload)
        if not result.get("ok") and is_markdown_parse_error(result) and "parse_mode" in payload:
            logger.info("Retrying Telegram send as plain text after Markdown parse error")
            payload.pop("parse_mode", None)
            # Drop the markers along with the parse mode. Resending the marked-up
            # text verbatim is what puts raw "*OPS-1234*" in front of the reader.
            if _is_markdown_v1(parse_mode):
                payload["text"] = strip_markdown(text)
            result = await _post(payload)
        if not result.get("ok"):
            logger.warning("Failed to send Telegram message: %s", result.get("description"))
        return result
    except Exception as e:
        logger.warning(f"Error sending Telegram message: {e}")
        return {"ok": False, "description": str(e)}


async def send_telegram_message_with_fallback(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: Optional[Dict[str, Any]] = None,
    parse_mode: Optional[str] = "Markdown",
    topic_id: Optional[int | str] = None,
    reply_to_message_id: Optional[int | str] = None,
) -> Optional[int]:
    """Send a message with an automatic plain-text retry on a Markdown parse error.

    Thin wrapper over :func:`send_telegram_message_raw` for callers that only
    need the message id.

    Returns:
        The message_id of the sent message, or None on failure.
    """
    chunks = _split_telegram_message(text)
    if len(chunks) > 1:
        logger.info("Splitting Telegram message into %d chunks", len(chunks))

    message_id: Optional[int] = None
    for index, chunk in enumerate(chunks):
        result = await send_telegram_message_raw(
            bot_token,
            chat_id,
            chunk,
            reply_markup=reply_markup if index == 0 else None,
            parse_mode=parse_mode,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id if index == 0 else None,
        )
        if not result.get("ok"):
            return None
        message_id = result.get("result", {}).get("message_id")
    return message_id


def _is_message_not_modified_error(result: Dict[str, Any]) -> bool:
    """True if an editMessageText 400 failed only because the text is unchanged.

    Telegram returns HTTP 400 with a "message is not modified" description when
    the new text is byte-identical to what's already posted. That's a legitimate
    no-op (the amendment we tried to render happened to match what's already on
    screen), not a delivery failure -- callers should treat it as success.
    """
    if not result or result.get("ok"):
        return False
    description = str(result.get("description", "")).lower()
    return result.get("error_code") == 400 and "message is not modified" in description


async def edit_telegram_message(
    bot_token: str,
    chat_id: str,
    message_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
) -> bool:
    """Edit an existing Telegram message in place.

    Used by alert-correlation amendments so joining a ticket updates the
    original Telegram post instead of spamming a new reply for every
    component that lands (see docs/superpowers/plans/
    2026-07-29-notify-correlation-followup-fixes.md).

    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        message_id: The id of the message to edit
        text: New message text
        parse_mode: Optional parse mode (e.g., "Markdown", "HTML")

    Returns:
        True if the edit succeeded, or if Telegram rejected it only because
        the text was already identical (a no-op, not a failure). False on
        any other error -- never raises, so callers can safely fall back to
        sending a new message instead.
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageText"
        if _is_markdown_v1(parse_mode):
            text = balance_markdown_entities(text)

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        async def _post() -> Dict[str, Any]:
            session = _get_session()
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                try:
                    return cast(Dict[str, Any], await response.json())
                except Exception:
                    return {
                        "ok": False,
                        "error_code": response.status,
                        "description": await response.text(),
                    }

        result = await _post()
        if not result.get("ok") and is_markdown_parse_error(result) and "parse_mode" in payload:
            # Same plain-text retry the send path has, so an amendment that
            # trips the parser still lands instead of leaving the stale text up.
            logger.info("Retrying Telegram edit as plain text after Markdown parse error")
            payload.pop("parse_mode", None)
            if _is_markdown_v1(parse_mode):
                payload["text"] = strip_markdown(text)
            result = await _post()

        if result.get("ok"):
            return True
        if _is_message_not_modified_error(result):
            logger.info(
                f"Telegram edit for message {message_id} in chat {chat_id} "
                "was a no-op (text unchanged)"
            )
            return True
        logger.warning(
            f"Failed to edit Telegram message {message_id} in chat {chat_id}: "
            f"{result.get('description')}"
        )
        return False
    except Exception as e:
        logger.warning(f"Error editing Telegram message {message_id} in chat {chat_id}: {e}")
        return False


async def create_forum_topic(
    bot_token: str,
    chat_id: str,
    name: str,
) -> Optional[int]:
    """Create a forum topic in a Telegram supergroup.

    Returns the message_thread_id on success, None on failure.
    Requires the bot to be an administrator with can_manage_topics right.

    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID of the forum supergroup
        name: Topic name (truncated to 128 chars per Telegram limit)

    Returns:
        message_thread_id (int) on success, None on failure.
    """
    try:
        url = f"https://api.telegram.org/bot{bot_token}/createForumTopic"
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "name": name[:128],
            "icon_color": _ESCALATION_TOPIC_COLOR,
        }
        session = _get_session()
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=10),
        ) as response:
            data = await response.json()
            if data.get("ok"):
                return int(data["result"]["message_thread_id"])
            logger.warning("createForumTopic failed: %s", data.get("description"))
            return None
    except Exception as e:
        logger.warning("Error creating forum topic: %s", e)
        return None


_MAX_TELEGRAM_CAPTION_CHARS = 1024


async def send_telegram_photo(
    bot_token: str,
    chat_id: str | int,
    photo_data: str | bytes,
    caption: Optional[str] = None,
    topic_id: Optional[int | str] = None,
    reply_to_message_id: Optional[int] = None,
    filename: str = "image.png",
    parse_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Send a photo to a Telegram chat.

    Args:
        bot_token: Telegram bot token
        chat_id: Target chat ID
        photo_data: Raw image bytes, or a base64-encoded string
        caption: Optional caption; truncated to Telegram's 1024-character limit
        topic_id: Optional topic/thread ID for forum groups
        reply_to_message_id: Optional message to reply to
        filename: Upload filename (only affects how Telegram labels the file)
        parse_mode: Optional parse mode for the caption

    Returns:
        Telegram's raw response dict, or a synthetic
        ``{"ok": False, "description": ...}`` on transport failure.
    """
    import base64

    photo_bytes = base64.b64decode(photo_data) if isinstance(photo_data, str) else photo_data

    data = aiohttp.FormData()
    data.add_field("chat_id", str(chat_id))
    data.add_field("photo", photo_bytes, filename=filename, content_type="image/png")

    if caption:
        if len(caption) > _MAX_TELEGRAM_CAPTION_CHARS:
            caption = caption[: _MAX_TELEGRAM_CAPTION_CHARS - 4] + "..."
        if _is_markdown_v1(parse_mode):
            # The 1024-char cut above lands mid-entity often enough to matter.
            caption = balance_markdown_entities(caption)
        data.add_field("caption", caption)
        if parse_mode:
            data.add_field("parse_mode", parse_mode)
    if topic_id is not None and str(topic_id).strip() != "":
        data.add_field("message_thread_id", str(topic_id))
    if reply_to_message_id is not None:
        data.add_field("reply_to_message_id", str(reply_to_message_id))

    logger.info(
        "Sending photo to Telegram: chat_id=%s, topic_id=%s, size=%d bytes",
        chat_id,
        topic_id,
        len(photo_bytes),
    )

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    try:
        session = _get_session()
        async with session.post(
            url, data=data, timeout=aiohttp.ClientTimeout(total=30)
        ) as response:
            try:
                result = cast(Dict[str, Any], await response.json())
            except Exception:
                result = {
                    "ok": False,
                    "error_code": response.status,
                    "description": await response.text(),
                }
        if not result.get("ok"):
            logger.error("Failed to send photo: %s", result.get("description"))
        return result
    except Exception as e:
        logger.exception("Error sending Telegram photo: %s", e)
        return {"ok": False, "description": str(e)}


async def send_telegram_document(
    bot_token: str,
    chat_id: str,
    pdf_bytes: bytes,
    filename: str,
    caption: str = "",
    parse_mode: str = "Markdown",
    topic_id: Optional[int | str] = None,
) -> Optional[int]:
    """Send a PDF document to a Telegram chat.

    Args:
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        pdf_bytes: Raw PDF bytes to send
        filename: Filename shown in Telegram
        caption: Optional caption (max 1024 chars)
        parse_mode: Optional parse mode for the caption
        topic_id: Optional topic/thread ID for forum groups

    Returns:
        The message_id of the sent message, or None on failure.

    Raises:
        ValueError: If pdf_bytes exceeds the Telegram file size limit.
    """
    if len(pdf_bytes) > _MAX_TELEGRAM_DOC_BYTES:
        raise ValueError(
            f"PDF too large to send via Telegram ({len(pdf_bytes)} bytes, "
            f"max {_MAX_TELEGRAM_DOC_BYTES})"
        )

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        if caption:
            if _is_markdown_v1(parse_mode):
                caption = balance_markdown_entities(caption)
            form.add_field("caption", caption)
        if parse_mode:
            form.add_field("parse_mode", parse_mode)
        if topic_id is not None:
            form.add_field("message_thread_id", str(int(topic_id)))
        form.add_field(
            "document",
            pdf_bytes,
            filename=filename,
            content_type="application/pdf",
        )

        session = _get_session()
        async with session.post(
            url,
            data=form,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.warning(f"Failed to send Telegram document: {error_text}")
                return None
            resp_json = await response.json()
            msg_id: Optional[int] = resp_json.get("result", {}).get("message_id")
            return msg_id
    except Exception as e:
        logger.warning(f"Error sending Telegram document: {e}")
        return None
