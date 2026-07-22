"""Telegram Bot API transport helpers.

Split out of chat_orchestrator/handler.py as part of the Phase 5 file split.
handler.py keeps thin delegating wrappers under the same names so existing
call sites and test patches (``patch("handler._send_telegram_typing_indicator")``)
keep working unchanged.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

import aiohttp

from orchestrator.models.schemas import ToolCallResult
from shared.utils.logging import get_logger
from shared.utils.telegram_markdown import convert_github_to_telegram_markdown
from shared.utils.telegram_send import send_telegram_photo

LOGGER = get_logger(__name__)


async def _answer_callback_query(
    callback_query_id: str,
    text: str | None = None,
    show_alert: bool = False,
) -> None:
    """Answer a callback query to remove the loading indicator.

    Args:
        callback_query_id: Telegram callback query ID
        text: Optional notification text to show (toast or alert)
        show_alert: If True, show as alert popup instead of toast
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        LOGGER.warning("TELEGRAM_BOT_TOKEN not set, cannot answer callback query")
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery"
        payload: Dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        if show_alert:
            payload["show_alert"] = True

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    LOGGER.warning(f"Failed to answer callback query: {error_text}")
                else:
                    LOGGER.debug(f"Answered callback query {callback_query_id}")
    except Exception as e:
        LOGGER.warning(f"Error answering callback query: {e}")


async def _edit_message_text(
    chat_id: str,
    message_id: int,
    text: str,
    topic_id: int | None = None,
    reply_markup: Dict[str, Any] | None = None,
) -> None:
    """Edit a Telegram message's text and optionally its reply markup.

    Args:
        chat_id: Telegram chat ID
        message_id: Message ID to edit
        text: New text for the message
        topic_id: Optional topic/thread ID (not used in edit, but kept for consistency)
        reply_markup: Optional new reply markup (None removes buttons)
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        LOGGER.warning("TELEGRAM_BOT_TOKEN not set, cannot edit message")
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageText"

        # Convert markdown
        telegram_text = _convert_to_telegram_markdown(text)

        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": telegram_text,
            "parse_mode": "Markdown",
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    # Retry without parse_mode if markdown fails
                    if "can't parse entities" in error_text.lower():
                        LOGGER.warning("Markdown parsing failed in edit, retrying as plain text")
                        payload.pop("parse_mode", None)
                        payload["text"] = text  # Use original text
                        async with session.post(
                            url,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=10),
                        ) as retry_response:
                            if retry_response.status != 200:
                                retry_error = await retry_response.text()
                                LOGGER.warning(f"Failed to edit message (retry): {retry_error}")
                    else:
                        LOGGER.warning(f"Failed to edit message: {error_text}")
                else:
                    LOGGER.debug(f"Edited message {message_id} in chat {chat_id}")
    except Exception as e:
        LOGGER.warning(f"Error editing message: {e}")


async def _edit_message_remove_buttons(
    chat_id: str,
    message_id: int,
    topic_id: int | None = None,
) -> None:
    """Remove inline buttons from a message by editing its reply_markup.

    Delegates to shared.utils.telegram_buttons.remove_buttons_from_message.
    """
    from shared.utils.telegram_buttons import remove_buttons_from_message

    await remove_buttons_from_message(chat_id, message_id)


def _convert_to_telegram_markdown(text: str) -> str:
    """Convert GitHub-style markdown to Telegram markdown format.

    This is a wrapper around the shared utility for backward compatibility.
    See shared/utils/telegram_markdown.py for the full implementation.
    """
    import re

    result = convert_github_to_telegram_markdown(text)

    # Fail-safe cleanup: remove any protection markers that weren't properly restored
    # This handles edge cases where markers might leak through
    result = re.sub(r"⟦CMD\d+⟧", "", result)
    result = re.sub(r"__PROTECTED_CMD_\d+__", "", result)
    result = re.sub(r"_\\_PROTECTED\\_CMD\\_\d+__", "", result)

    return result



async def _send_telegram_typing_indicator(chat_id: str, topic_id: str | None) -> None:
    """Send typing indicator to Telegram chat.

    Args:
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID for forum groups
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
        payload: dict = {"chat_id": chat_id, "action": "typing"}
        if topic_id:
            payload["message_thread_id"] = int(topic_id)

        async with aiohttp.ClientSession() as session:
            await session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5))
    except Exception as e:
        # Don't fail the request if typing indicator fails
        LOGGER.debug(f"Failed to send typing indicator: {e}")


async def _send_tool_images_to_telegram(
    tool_results: List[ToolCallResult],
    bot_token: str,
    chat_id: str,
    topic_id: str | None,
    reply_to_message_id: int | None = None,
) -> None:
    """Extract and send images from tool results to Telegram.

    Args:
        tool_results: List of tool call results that may contain images
        bot_token: Telegram bot token
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID
        reply_to_message_id: Optional message ID to reply to
    """
    LOGGER.info(f"_send_tool_images_to_telegram: processing {len(tool_results)} tool results")
    for result in tool_results:
        if not result.raw_response:
            LOGGER.debug(f"Tool {result.name}: no raw_response, skipping")
            continue

        # Extract images from MCP response format
        # Format: {"success": true, "result": [{"type": "image", "data": "base64...", "mimeType": "image/png"}, ...]}
        mcp_result = result.raw_response.get("result", [])
        LOGGER.info(
            f"Tool {result.name}: raw_response has result of type {type(mcp_result).__name__}, "
            f"len={len(mcp_result) if isinstance(mcp_result, list) else 'N/A'}"
        )
        if not isinstance(mcp_result, list):
            continue

        for content_item in mcp_result:
            if not isinstance(content_item, dict):
                continue

            item_type = content_item.get("type")
            LOGGER.debug(f"Tool {result.name}: content item type={item_type}")

            # Check if this is an image
            if item_type == "image":
                image_data = content_item.get("data")
                if image_data:
                    # Extract tool name for caption
                    tool_name = result.name.replace("_", " ").title()
                    caption = f"📊 {tool_name}"

                    try:
                        await send_telegram_photo(
                            bot_token,
                            chat_id,
                            image_data,
                            caption=caption,
                            topic_id=topic_id,
                            reply_to_message_id=reply_to_message_id,
                        )
                        LOGGER.info(f"Sent image from tool {result.name} to Telegram")
                    except Exception as e:
                        LOGGER.error(f"Failed to send image from tool {result.name}: {e}")


def _extract_message_id(response_json: dict) -> int | None:
    """Extract message_id from a Telegram Bot API sendMessage response."""
    try:
        if response_json.get("ok"):
            msg_id: int = response_json["result"]["message_id"]
            return msg_id
    except (KeyError, TypeError):
        pass
    return None


async def _send_telegram_chunk(
    webhook_url: str,
    payload: dict,
    reply_to_message_id: int | None = None,
) -> int | None:
    """Send a single message chunk to Telegram.

    Args:
        webhook_url: Telegram Bot API webhook URL
        payload: Message payload dict
        reply_to_message_id: Original message ID for retry logic

    Returns:
        The sent message's message_id from Telegram API, or None on failure.
    """
    async with aiohttp.ClientSession() as session:
        async with session.post(
            webhook_url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                LOGGER.error(
                    f"Webhook request failed: status={response.status}, error={error_text}"
                )

                # Retry without reply_to_message_id if we got "message to be replied not found"
                if (
                    response.status == 400
                    and "message to be replied not found" in error_text.lower()
                ):
                    if reply_to_message_id:
                        LOGGER.warning(
                            f"Retrying without reply_to_message_id={reply_to_message_id} due to Telegram error"
                        )
                        # Remove reply_to_message_id and retry
                        payload.pop("reply_to_message_id", None)
                        async with session.post(
                            webhook_url,
                            json=payload,
                            timeout=aiohttp.ClientTimeout(total=30),
                        ) as retry_response:
                            if retry_response.status == 200:
                                LOGGER.info(
                                    "Successfully sent response to webhook (retry without reply)"
                                )
                                return _extract_message_id(await retry_response.json())
                            else:
                                retry_error = await retry_response.text()
                                LOGGER.error(
                                    f"Retry failed: status={retry_response.status}, error={retry_error}"
                                )

                # Retry without parse_mode if we got markdown parsing error
                elif response.status == 400 and "can't parse entities" in error_text.lower():
                    LOGGER.warning(
                        "Markdown parsing failed, retrying without parse_mode (plain text)"
                    )
                    # Remove parse_mode and retry as plain text
                    payload.pop("parse_mode", None)
                    async with session.post(
                        webhook_url,
                        json=payload,
                        timeout=aiohttp.ClientTimeout(total=30),
                    ) as retry_response:
                        if retry_response.status == 200:
                            LOGGER.info(
                                "Successfully sent response to webhook (plain text fallback)"
                            )
                            return _extract_message_id(await retry_response.json())
                        else:
                            retry_error = await retry_response.text()
                            LOGGER.error(
                                f"Plain text retry failed: status={retry_response.status}, error={retry_error}"
                            )
                return None
            else:
                LOGGER.info("Successfully sent response to webhook")
                return _extract_message_id(await response.json())


async def _send_telegram_response(
    webhook_url: str,
    chat_id: str,
    topic_id: str | None,
    text: str,
    reply_to_message_id: int | None = None,
    reply_markup: Dict[str, Any] | None = None,
) -> int | None:
    """Send response to Telegram via outgoing webhook in Bot API format.

    Args:
        webhook_url: Telegram Bot API webhook URL (e.g., https://api.telegram.org/bot<token>/sendMessage)
        chat_id: Original Telegram chat ID (with -100 prefix if present)
        topic_id: Optional topic/thread ID for forum groups
        text: Response text to send
        reply_to_message_id: Optional message ID to reply to (tags the original message)
        reply_markup: Optional InlineKeyboardMarkup for inline buttons

    Returns:
        The sent message's message_id from Telegram API, or None.
    """
    try:
        # Convert GitHub markdown to Telegram markdown
        telegram_text = _convert_to_telegram_markdown(text)

        # Telegram's max message length is 4096 characters
        MAX_TELEGRAM_LENGTH = 4096

        # Split message if too long
        if len(telegram_text) > MAX_TELEGRAM_LENGTH:
            LOGGER.warning(
                f"Message too long ({len(telegram_text)} chars), splitting into multiple messages"
            )
            # Split into chunks, trying to break at newlines for readability
            chunks = []
            current_chunk = ""

            for line in telegram_text.split("\n"):
                if len(current_chunk) + len(line) + 1 <= MAX_TELEGRAM_LENGTH:
                    current_chunk += line + "\n"
                else:
                    if current_chunk:
                        chunks.append(current_chunk.rstrip())
                    current_chunk = line + "\n"

            if current_chunk:
                chunks.append(current_chunk.rstrip())

            # Send each chunk
            last_message_id = None
            for i, chunk in enumerate(chunks):
                chunk_payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "Markdown",
                }

                # Add topic ID if present (for forum groups)
                if topic_id:
                    chunk_payload["message_thread_id"] = int(topic_id)

                # Only add reply_to_message_id for the first chunk
                if reply_to_message_id and i == 0:
                    chunk_payload["reply_to_message_id"] = int(reply_to_message_id)

                LOGGER.info(
                    f"Sending chunk {i + 1}/{len(chunks)} to webhook: chat_id={chat_id}, "
                    f"topic_id={topic_id}, text_length={len(chunk)}"
                )

                last_message_id = await _send_telegram_chunk(
                    webhook_url, chunk_payload, reply_to_message_id if i == 0 else None
                )

            return last_message_id

        # Single message (normal case)
        # Build Telegram Bot API payload
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": telegram_text,
            "parse_mode": "Markdown",
        }

        # Add topic ID if present (for forum groups)
        if topic_id:
            payload["message_thread_id"] = int(topic_id)

        # Add reply_to_message_id if present (tags the original message)
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)

        # Add reply_markup if present (inline keyboard buttons)
        if reply_markup:
            payload["reply_markup"] = reply_markup

        LOGGER.info(
            f"Sending response to webhook: chat_id={chat_id}, "
            f"topic_id={topic_id}, text_length={len(text)}, has_buttons={reply_markup is not None}"
        )

        return await _send_telegram_chunk(webhook_url, payload, reply_to_message_id)

    except Exception as e:
        LOGGER.exception(f"Error sending telegram response: {e}")
        raise
