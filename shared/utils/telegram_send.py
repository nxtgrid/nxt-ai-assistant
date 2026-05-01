"""Shared Telegram message-sending helper.

Used by both handler.py and messaging_mcp_server.py to avoid duplication.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

_MAX_TELEGRAM_DOC_BYTES = 45 * 1024 * 1024  # 45 MB (Telegram limit is 50 MB)
_ESCALATION_TOPIC_COLOR = 16749490  # 0xFF93B2 pink — distinguishable from staff-created topics

# Reuse a single ClientSession across calls to avoid per-call TCP handshakes.
_session: Optional[aiohttp.ClientSession] = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession()
    return _session


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
