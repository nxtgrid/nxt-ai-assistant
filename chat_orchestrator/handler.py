"""
DigitalOcean Serverless Function Handler for Anansi

This module provides the serverless function entry point for handling
chat requests from webhooks (Telegram, Roam, etc.) or direct API calls.
Each invocation processes one round of conversation, saves state to Supabase,
and returns the response.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import uuid as uuid_mod
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import aiohttp

# Load environment variables from .env file (for local development)
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    # dotenv not installed, rely on system environment variables
    pass

from orchestrator.config.settings import AppSettings, GeminiModelConfig
from orchestrator.models.schemas import (
    ConversationMessage,
    MediaAttachment,
    MessageSourceLiteral,
    ToolCallResult,
    UserContext,
    WebhookRequest,
)
from orchestrator.services.supabase_client import get_supabase_client
from orchestrator.services.thread_assignment import (
    assign_passive_thread,
    is_thread_disentanglement_enabled,
)
from orchestrator.utils.session_id import generate_session_id
from shared.auth import get_auth_service
from shared.auth.auth_service import STAFF_ORG_ID as _STAFF_ORG_ID
from shared.utils.error_messages import ErrorCategory, categorize_error, get_user_message
from shared.utils.langfuse_utils import langfuse_observe, update_trace
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import (
    ESCALATION_CLOSE_NOTIFY_PREFIX,
    ESCALATION_CLOSE_SILENT_PREFIX,
    ESCALATION_OFFER_PREFIX,
    ESCALATION_TRACK_CALLBACK_PREFIX,
    PROCEDURE_CALLBACK_PREFIX,
    STEP_INPUT_CALLBACK_PREFIX,
    is_procedure_buttons_enabled,
    parse_callback_data,
    parse_procedure_buttons,
)
from shared.utils.telegram_markdown import convert_github_to_telegram_markdown

# Import shared tele_debug utility
try:
    from shared.utils import tele_debug, tele_debug_sync
except ImportError:
    # Fallback if shared utils not available
    tele_debug = None
    tele_debug_sync = None

LOGGER = get_logger(__name__)

# =============================================================================
# Webhook Deduplication (prevents Telegram retry from reprocessing)
# =============================================================================

# TTL cache for recently processed message_ids to prevent duplicate processing
# Key: (chat_id, message_id), Value: timestamp when processed
_PROCESSED_MESSAGES: Dict[tuple, float] = {}
_MESSAGE_CACHE_TTL_SECONDS = 300  # 5 minutes (Telegram retries within ~60 seconds)
_MESSAGE_CACHE_MAX_SIZE = 1000  # Prevent unbounded growth

# Media group aggregation buffer — collects album photos that arrive as separate
# webhooks (linked by Telegram's media_group_id) and flushes them as one merged
# request after a 2-second inactivity timeout.
# NOTE: These buffers are process-local. Album aggregation requires all webhooks
# for the same media_group_id to hit the same worker. With a single uvicorn worker
# (current deployment), this is guaranteed.
_MEDIA_GROUP_BUFFERS: Dict[str, list] = {}  # media_group_id -> [body, body, ...]
_MEDIA_GROUP_TIMERS: Dict[str, asyncio.Task] = {}
_MEDIA_GROUP_FLUSH_DELAY = 2.0  # seconds (community consensus for Telegram albums)
_MAX_MEDIA_GROUP_SIZE = 10  # Telegram album limit
_MAX_ACTIVE_MEDIA_GROUPS = 200  # Prevent unbounded memory growth


def _is_duplicate_webhook(chat_id: str, message_id: int | None) -> bool:
    """Check if this message has already been processed recently.

    Args:
        chat_id: Telegram chat ID
        message_id: Telegram message ID

    Returns:
        True if this is a duplicate (should be ignored), False if new
    """
    import time

    if not message_id:
        return False  # Can't dedupe without message_id

    cache_key = (chat_id, message_id)
    now = time.time()

    # Clean up expired entries periodically (every 100 checks)
    if len(_PROCESSED_MESSAGES) > _MESSAGE_CACHE_MAX_SIZE:
        expired_keys = [
            k for k, v in _PROCESSED_MESSAGES.items() if now - v > _MESSAGE_CACHE_TTL_SECONDS
        ]
        for k in expired_keys:
            del _PROCESSED_MESSAGES[k]

    # Check if already processed
    if cache_key in _PROCESSED_MESSAGES:
        age = now - _PROCESSED_MESSAGES[cache_key]
        if age < _MESSAGE_CACHE_TTL_SECONDS:
            LOGGER.warning(
                f"Duplicate webhook detected: chat_id={chat_id}, message_id={message_id}, "
                f"age={age:.1f}s. Ignoring Telegram retry."
            )
            return True
        # Expired entry, will be reprocessed
        del _PROCESSED_MESSAGES[cache_key]

    # Mark as processed
    _PROCESSED_MESSAGES[cache_key] = now
    return False


# =============================================================================
# Telegram Update Helpers
# =============================================================================


def _get_tg_message(args: Dict[str, Any]) -> Dict[str, Any]:
    """Return the Telegram message dict from a webhook payload.

    Handles both fresh messages and edited messages transparently.
    Always use this instead of args.get("message") so edited_message
    updates are never silently dropped.
    """
    msg = args.get("message") or args.get("edited_message")
    return msg if isinstance(msg, dict) else {}


# Media Group Aggregation
# =============================================================================


async def _buffer_media_group_message(args: Dict[str, Any]) -> bool:
    """Buffer a Telegram message that belongs to a media group (album).

    Returns True if the message was buffered (caller should return 200 immediately).
    Returns False if the message is already merged (re-entry from flush) or not a media group.
    """
    telegram_msg = _get_tg_message(args)
    if not telegram_msg:
        return False
    media_group_id = telegram_msg.get("media_group_id")

    if not media_group_id:
        return False

    # Re-entry guard: merged messages from _flush_media_group have _photo_file_ids
    if "_photo_file_ids" in telegram_msg:
        return False

    # Dedup at buffer time — prevent Telegram retries from double-buffering
    chat_id = str(telegram_msg.get("chat", {}).get("id", ""))
    msg_id = telegram_msg.get("message_id")
    if _is_duplicate_webhook(chat_id, msg_id):
        return True  # Already buffered/processed, just ack

    # Reject if too many concurrent media groups are being buffered
    if (
        len(_MEDIA_GROUP_BUFFERS) >= _MAX_ACTIVE_MEDIA_GROUPS
        and media_group_id not in _MEDIA_GROUP_BUFFERS
    ):
        LOGGER.warning(
            f"Too many active media groups ({len(_MEDIA_GROUP_BUFFERS)}), "
            f"dropping media_group_id={media_group_id!r:.64}"
        )
        return True  # Ack but drop

    # Reject if this album already has too many items
    buffer = _MEDIA_GROUP_BUFFERS.setdefault(media_group_id, [])
    if len(buffer) >= _MAX_MEDIA_GROUP_SIZE:
        LOGGER.warning(f"Media group {media_group_id!r:.64} at capacity ({len(buffer)}), dropping")
        return True  # Ack but drop

    # Buffer the FULL body (preserves _auth_method injected by app.py)
    buffer.append(args)

    # Send typing indicator on first photo in the group
    if len(buffer) == 1:
        topic_id = (
            str(telegram_msg["message_thread_id"]) if "message_thread_id" in telegram_msg else None
        )
        asyncio.create_task(_send_telegram_typing_indicator(chat_id, topic_id))

    # Cancel previous timer, start new one (resets on each new photo)
    if media_group_id in _MEDIA_GROUP_TIMERS:
        _MEDIA_GROUP_TIMERS[media_group_id].cancel()
    _MEDIA_GROUP_TIMERS[media_group_id] = asyncio.create_task(_flush_media_group(media_group_id))

    LOGGER.info(f"Buffered media group {media_group_id!r:.64}: {len(buffer)} items so far")
    return True


async def _flush_media_group(media_group_id: str) -> None:
    """Wait for flush delay, then merge buffered album messages and process as one."""
    try:
        await asyncio.sleep(_MEDIA_GROUP_FLUSH_DELAY)
    except asyncio.CancelledError:
        return  # New timer was started, this one is superseded

    bodies = _MEDIA_GROUP_BUFFERS.pop(media_group_id, [])
    _MEDIA_GROUP_TIMERS.pop(media_group_id, None)
    if not bodies:
        return

    # Deep-copy the first body as base to avoid mutating the original dict
    merged_body = copy.deepcopy(bodies[0])
    base_msg = merged_body.get("message", {})

    # Collect all photo file_ids (largest size from each message)
    photo_file_ids = []
    for body in bodies:
        msg = body.get("message", {})
        photos = msg.get("photo", [])
        if photos:
            photo_file_ids.append(photos[-1]["file_id"])

    # Inject merged photo list into the base message (prefixed with _ to signal synthetic)
    base_msg["_photo_file_ids"] = photo_file_ids

    # Reply to last message in album (most visible to user in Telegram UI)
    base_msg["message_id"] = max((b.get("message", {}).get("message_id", 0) for b in bodies))

    LOGGER.info(
        f"Media group {media_group_id!r:.64}: flushing {len(bodies)} items, "
        f"{len(photo_file_ids)} photos"
    )

    # Re-enter async_main with the merged body — _auth_method is preserved from first body,
    # and the _photo_file_ids guard prevents re-buffering.
    # Mark as internal re-entry so field sanitization is skipped.
    merged_body["_internal_reentry"] = True
    try:
        await async_main(merged_body)
    except asyncio.CancelledError:
        LOGGER.warning(f"Media group {media_group_id!r:.64} processing cancelled")
    except Exception:
        LOGGER.exception(f"Failed to process merged media group {media_group_id!r:.64}")
        # Notify the user so they know their album was not processed
        chat_id = str(base_msg.get("chat", {}).get("id", ""))
        if chat_id:
            try:
                bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                if bot_token:
                    await _send_telegram_message(
                        bot_token,
                        chat_id,
                        "Sorry, I couldn't process your photos. Please try sending them again.",
                    )
            except Exception:
                pass  # Best-effort error notification


# =============================================================================
# Emoji Detection and Feedback Mapping
# =============================================================================

# Emoji to feedback type mapping (covers standard Telegram reactions)
# Used for both message reactions AND emoji-only messages
EMOJI_TO_FEEDBACK = {
    # Positive reactions
    "👍": "thumbs_up",
    "❤️": "thumbs_up",
    "🔥": "thumbs_up",
    "🎉": "thumbs_up",
    "🤩": "thumbs_up",
    "😍": "thumbs_up",
    "❤️‍🔥": "thumbs_up",
    "⭐": "thumbs_up",
    "💯": "thumbs_up",
    "👏": "thumbs_up",
    "🙏": "thumbs_up",
    "🤗": "thumbs_up",
    "🫡": "thumbs_up",
    "👌": "thumbs_up",
    "🏆": "thumbs_up",
    "💋": "thumbs_up",
    "😘": "thumbs_up",
    "🥰": "thumbs_up",
    "😇": "thumbs_up",
    "🤝": "thumbs_up",
    "💘": "thumbs_up",
    "🦄": "thumbs_up",
    "😎": "thumbs_up",
    "🤣": "thumbs_up",
    "😁": "thumbs_up",
    "🍾": "thumbs_up",
    "⚡": "thumbs_up",
    "✅": "thumbs_up",
    "💪": "thumbs_up",
    "🙌": "thumbs_up",
    "😊": "thumbs_up",
    "🥳": "thumbs_up",
    # Negative reactions
    "👎": "thumbs_down",
    "😢": "thumbs_down",
    "😭": "thumbs_down",
    "😡": "thumbs_down",
    "🤬": "thumbs_down",
    "💩": "thumbs_down",
    "🤮": "thumbs_down",
    "💔": "thumbs_down",
    "😱": "thumbs_down",
    "😨": "thumbs_down",
    "🖕": "thumbs_down",
    "🤡": "thumbs_down",
    "😤": "thumbs_down",
    "😠": "thumbs_down",
    "❌": "thumbs_down",
}

# Skin tone modifiers (U+1F3FB to U+1F3FF) and variation selector (U+FE0F)
SKIN_TONE_MODIFIERS = {chr(c) for c in range(0x1F3FB, 0x1F400)}
VARIATION_SELECTOR = "\ufe0f"


def _normalize_emoji(emoji_str: str) -> str:
    """Strip skin tone modifiers and variation selectors from emoji."""
    return "".join(c for c in emoji_str if c not in SKIN_TONE_MODIFIERS and c != VARIATION_SELECTOR)


# Build normalized lookup table for consistent matching
NORMALIZED_EMOJI_TO_FEEDBACK = {_normalize_emoji(k): v for k, v in EMOJI_TO_FEEDBACK.items()}


def _is_emoji_only_message(text: str) -> tuple[bool, str | None, str | None]:
    """
    Check if a message consists only of emoji(s).

    Returns:
        Tuple of (is_emoji_only, feedback_type, emoji)
        - is_emoji_only: True if the message is just emoji(s)
        - feedback_type: 'thumbs_up', 'thumbs_down', or None if unmapped
        - emoji: The normalized emoji string
    """
    if not text:
        return False, None, None

    # Strip whitespace
    text = text.strip()

    # Normalize the text (remove skin tones and variation selectors)
    normalized = _normalize_emoji(text)

    # Check if the normalized text is a known feedback emoji
    feedback_type = NORMALIZED_EMOJI_TO_FEEDBACK.get(normalized)
    if feedback_type:
        return True, feedback_type, text

    # Check if text is entirely emoji characters (even if not in our mapping)
    # This handles cases like "😀😀" or other emoji not in our mapping
    import re

    # Regex pattern for emoji (covers most emoji including combined ones)
    emoji_pattern = re.compile(
        "["
        "\U0001f600-\U0001f64f"  # emoticons
        "\U0001f300-\U0001f5ff"  # symbols & pictographs
        "\U0001f680-\U0001f6ff"  # transport & map symbols
        "\U0001f1e0-\U0001f1ff"  # flags (iOS)
        "\U00002702-\U000027b0"  # dingbats
        "\U000024c2-\U0001f251"  # enclosed characters
        "\U0001f900-\U0001f9ff"  # supplemental symbols
        "\U0001fa00-\U0001fa6f"  # chess symbols
        "\U0001fa70-\U0001faff"  # symbols and pictographs extended-A
        "\U00002600-\U000026ff"  # misc symbols
        "\U00002700-\U000027bf"  # dingbats
        "\ufe0f"  # variation selector
        "\u200d"  # zero width joiner (for combined emoji)
        "]+",
        re.UNICODE,
    )

    # Remove all emoji characters and see if anything remains
    remaining = emoji_pattern.sub("", text)
    remaining = remaining.strip()

    if not remaining:
        # It's all emoji, but not in our feedback mapping
        return True, None, text

    return False, None, None


async def _handle_emoji_only_message(
    args: Dict[str, Any],
    normalized_args: Dict[str, Any],
    feedback_type: str | None,
    emoji: str,
) -> Dict[str, Any]:
    """
    Handle messages that consist only of emoji.

    Treats emoji-only messages as feedback (similar to reactions) rather than
    as queries that need LLM processing.

    Args:
        args: Original Telegram webhook args
        normalized_args: Normalized args from _normalize_telegram_webhook
        feedback_type: 'thumbs_up', 'thumbs_down', or None
        emoji: The emoji that was sent

    Returns:
        Response dict with success status
    """
    chat_id = normalized_args.get("chat_id", "")
    user_id = normalized_args.get("user_id", "")
    username = normalized_args.get("username", "Unknown")

    LOGGER.info(
        f"Handling emoji-only message: emoji={emoji}, feedback_type={feedback_type}, "
        f"user={username}, chat={chat_id}"
    )

    # If it's a feedback emoji, save it like we do for reactions
    if feedback_type:
        try:
            # Get the most recent bot message to attach feedback to
            supabase_client = get_supabase_client()

            session_obj = await supabase_client.get_session_by_chat_id(
                source="telegram",
                chat_id=chat_id,
            )

            if session_obj:
                # Save feedback to the most recent bot message
                await _save_emoji_message_feedback(
                    session_id=str(session_obj.id),
                    user_id=user_id,
                    user_name=username,
                    feedback_type=feedback_type,
                    emoji=emoji,
                )
                LOGGER.info(f"Saved emoji feedback: {feedback_type} from {username}")

        except Exception as e:
            LOGGER.warning(f"Failed to save emoji feedback: {e}")

    # Send a simple acknowledgment via Telegram (no LLM needed)
    # Only respond if we recognized the feedback type
    if feedback_type and normalized_args.get("_auth_method") == "telegram":
        try:
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
            if bot_token and chat_id:
                # Simple acknowledgment based on feedback type
                if feedback_type == "thumbs_up":
                    response_text = "👍 Got it, thanks for the feedback!"
                else:
                    response_text = (
                        "👍 Thanks for letting me know. Is there something I can help with?"
                    )

                await _send_telegram_message(bot_token, chat_id, response_text)
        except Exception as e:
            LOGGER.warning(f"Failed to send emoji acknowledgment: {e}")

    return {
        "success": True,
        "message": f"Handled emoji-only message: {emoji}",
        "statusCode": 200,
    }


async def _save_emoji_message_feedback(
    session_id: str,
    user_id: str,
    user_name: str,
    feedback_type: str,
    emoji: str,
) -> None:
    """
    Save emoji message feedback to the most recent bot message in the session.

    Args:
        session_id: Session UUID
        user_id: User ID
        user_name: User's display name
        feedback_type: 'thumbs_up' or 'thumbs_down'
        emoji: The emoji that was sent
    """

    supabase_client = get_supabase_client()
    client = supabase_client._get_client()

    # Find the most recent bot message
    messages_response = (
        client.table("chat_messages")
        .select("id, metadata")
        .eq("session_id", session_id)
        .eq("role", "model")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )

    if not messages_response.data:
        LOGGER.warning(f"No model messages found in session {session_id}")
        return

    msg = messages_response.data[0]
    msg_id = msg["id"]
    current_metadata = msg.get("metadata") or {}

    # Get existing feedback array
    existing_feedback = current_metadata.get("feedback", [])
    if isinstance(existing_feedback, dict):
        existing_feedback = [existing_feedback]

    # Create new feedback entry
    new_feedback_entry = {
        "type": feedback_type,
        "emoji": emoji,
        "telegram_user_id": user_id,
        "user_name": user_name,
        "source": "emoji_message",  # Distinguish from reaction
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Check if user already gave feedback - update instead of duplicate
    user_found = False
    for i, fb in enumerate(existing_feedback):
        if fb.get("telegram_user_id") == user_id:
            existing_feedback[i] = new_feedback_entry
            user_found = True
            break

    if not user_found:
        existing_feedback.append(new_feedback_entry)

    # Update metadata
    current_metadata["feedback"] = existing_feedback

    client.table("chat_messages").update({"metadata": current_metadata}).eq("id", msg_id).execute()

    LOGGER.info(f"Saved emoji message feedback to message {msg_id}")


@langfuse_observe(name="chat-request")
async def _process_webhook_with_graph(
    user_input: str,
    user_context: UserContext,
    entity_context: Dict[str, Any] | None = None,
    media: List[MediaAttachment] | None = None,
    session_id: str | None = None,
    metadata: Dict[str, Any] | None = None,
) -> tuple[str, List[ToolCallResult], Dict[str, Any] | None]:
    """Process webhook request using the full LangGraph conversation graph.

    This is the Phase 3 implementation that replaces _process_webhook_async
    with a cleaner graph-based flow.

    Args:
        user_input: User's message text
        user_context: User identity and context
        entity_context: Optional entity context dict
        media: Optional media attachments
        session_id: Session identifier
        metadata: Additional metadata

    Returns:
        Tuple of (final response text, list of tool call results, optional reply_markup for inline buttons)
    """
    # Set Langfuse trace metadata (skipped for warmup requests)
    user_email = getattr(user_context, "email", "")
    if user_email != "warmup@system":
        update_trace(
            user_id=session_id,
            session_id=session_id,
            metadata={
                "org_id": getattr(user_context, "organization_id", None),
                "mode": getattr(user_context, "mode", None),
            },
            tags=[t for t in [getattr(user_context, "mode", None), "production"] if t],
        )

    from orchestrator.graphs.full_conversation_graph import (
        build_full_conversation_graph,
        invoke_full_graph,
    )

    LOGGER.info(f"Processing webhook with LangGraph full graph (session={session_id})")

    try:
        # Checkpointer removed — single-turn graph doesn't need state persistence.
        # Multi-turn decisions (duplicate detection, resume) use pending_decisions table.
        graph = build_full_conversation_graph()
        final_state = await invoke_full_graph(
            graph=graph,
            user_input=user_input,
            user_context=user_context,
            session_id=session_id or "",
            metadata=metadata,
            entity_context=entity_context,
        )

        # Extract final response
        final_response: str = final_state.get("final_response", "") or ""

        if not final_response:
            LOGGER.warning("Graph returned empty final_response")
            final_response = get_user_message(ErrorCategory.SYSTEM, "empty_response")

        # Extract tool results (may contain images)
        tool_results: List[ToolCallResult] = final_state.get("accumulated_tool_results", [])

        # Extract reply_markup for inline buttons (decision prompts)
        reply_markup: Dict[str, Any] | None = final_state.get("reply_markup")

        LOGGER.info(
            f"Graph execution complete: response_len={len(final_response)}, "
            f"rounds={final_state.get('current_round', 0)}, "
            f"tool_results={len(tool_results)}, has_reply_markup={reply_markup is not None}"
        )

        return final_response, tool_results, reply_markup

    except Exception as e:
        LOGGER.exception(f"Error in LangGraph full graph processing: {e}")
        # Fall back to error message
        _, error_message = categorize_error(e)
        return error_message, [], None


def validate_and_override_source(
    request_source: MessageSourceLiteral,
    debug_mode: bool = False,
) -> MessageSourceLiteral:
    """
    Validate and override the source in serverless context.

    Security rules:
    1. In production (DEBUG=false), source must be provided but cannot be arbitrary
    2. Only in DEBUG mode can source be freely specified
    3. For serverless, we cannot derive from request origin, so we validate against expected values

    Args:
        request_source: Source provided in the request payload
        debug_mode: Whether DEBUG mode is enabled

    Returns:
        The validated source to use
    """
    # In DEBUG mode, allow any source
    if debug_mode:
        LOGGER.debug(f"DEBUG mode: allowing source={request_source}")
        return request_source

    # In production, validate source is one of the allowed values
    allowed_sources = ["telegram", "roam", "web", "api"]
    if request_source not in allowed_sources:
        LOGGER.warning(f"Invalid source '{request_source}' in production. Defaulting to 'api'")
        return "api"

    return request_source


# Allowed domains for fetching training images (for security)
ALLOWED_IMAGE_DOMAINS = ["drive.google.com", "docs.google.com"]

# Maximum file size for media downloads (5MB)
MAX_MEDIA_SIZE_BYTES = 5 * 1024 * 1024


async def _download_telegram_photo(file_id: str, bot_token: str) -> tuple:
    """
    Download a photo from Telegram using the Bot API.

    Args:
        file_id: Telegram file_id of the photo
        bot_token: Telegram bot token

    Returns:
        Tuple of (base64_data, mime_type) or (None, None) on failure
    """
    import base64

    try:
        # Get file path from Telegram
        get_file_url = f"https://api.telegram.org/bot{bot_token}/getFile"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                get_file_url, params={"file_id": file_id}, timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                result = await response.json()

                if not result.get("ok"):
                    LOGGER.error(f"Failed to get file info: {result}")
                    return None, None

                file_path = result.get("result", {}).get("file_path")
                file_size = result.get("result", {}).get("file_size", 0)

                if not file_path:
                    LOGGER.error("No file_path in response")
                    return None, None

                # Check file size
                if file_size > MAX_MEDIA_SIZE_BYTES:
                    LOGGER.warning(f"File too large: {file_size} bytes > {MAX_MEDIA_SIZE_BYTES}")
                    return None, None

            # Download the file
            download_url = f"https://api.telegram.org/file/bot{bot_token}/{file_path}"
            async with session.get(
                download_url, timeout=aiohttp.ClientTimeout(total=30)
            ) as response:
                if response.status != 200:
                    LOGGER.error(f"Failed to download file: {response.status}")
                    return None, None

                file_data = await response.read()

                # Determine mime type from file path
                if file_path.endswith(".jpg") or file_path.endswith(".jpeg"):
                    mime_type = "image/jpeg"
                elif file_path.endswith(".png"):
                    mime_type = "image/png"
                elif file_path.endswith(".gif"):
                    mime_type = "image/gif"
                elif file_path.endswith(".mp4"):
                    mime_type = "video/mp4"
                elif file_path.endswith(".ogg") or file_path.endswith(".oga"):
                    mime_type = "audio/ogg"
                elif file_path.endswith(".mp3"):
                    mime_type = "audio/mpeg"
                elif file_path.endswith(".wav"):
                    mime_type = "audio/wav"
                elif file_path.endswith(".m4a"):
                    mime_type = "audio/mp4"
                elif file_path.endswith(".aac"):
                    mime_type = "audio/aac"
                elif file_path.endswith(".flac"):
                    mime_type = "audio/flac"
                else:
                    mime_type = "image/jpeg"  # Default

                # Encode to base64
                base64_data = base64.b64encode(file_data).decode("utf-8")

                LOGGER.info(f"Downloaded Telegram media: {len(file_data)} bytes, {mime_type}")
                return base64_data, mime_type

    except Exception as e:
        LOGGER.exception(f"Error downloading Telegram photo: {e}")
        return None, None


async def _download_training_image(url: str) -> tuple:
    """
    Download a training image from a URL (must be from allowed domains).

    Args:
        url: URL to download the image from (must be from ALLOWED_IMAGE_DOMAINS)

    Returns:
        Tuple of (base64_data, mime_type, error_message)
        On success: (data, mime, None)
        On failure: (None, None, error_message)
    """
    import base64
    from urllib.parse import urlparse

    # Validate URL domain
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()

        if not any(allowed in domain for allowed in ALLOWED_IMAGE_DOMAINS):
            return (
                None,
                None,
                f"URL domain '{domain}' not allowed. Must be from: {', '.join(ALLOWED_IMAGE_DOMAINS)}",
            )
    except Exception as e:
        return None, None, f"Invalid URL: {str(e)}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                # Check content length before downloading
                content_length = response.headers.get("Content-Length")
                if content_length and int(content_length) > MAX_MEDIA_SIZE_BYTES:
                    return (
                        None,
                        None,
                        f"File too large: {content_length} bytes (max {MAX_MEDIA_SIZE_BYTES})",
                    )

                if response.status != 200:
                    return None, None, f"Failed to download: HTTP {response.status}"

                file_data = await response.read()

                # Check actual size
                if len(file_data) > MAX_MEDIA_SIZE_BYTES:
                    return (
                        None,
                        None,
                        f"File too large: {len(file_data)} bytes (max {MAX_MEDIA_SIZE_BYTES})",
                    )

                # Determine mime type
                content_type = response.headers.get("Content-Type", "")
                if "jpeg" in content_type or "jpg" in content_type:
                    mime_type = "image/jpeg"
                elif "png" in content_type:
                    mime_type = "image/png"
                elif "gif" in content_type:
                    mime_type = "image/gif"
                elif "mp4" in content_type:
                    mime_type = "video/mp4"
                else:
                    mime_type = "image/jpeg"  # Default

                # Encode to base64
                base64_data = base64.b64encode(file_data).decode("utf-8")

                LOGGER.info(f"Downloaded training image from {url}: {len(file_data)} bytes")
                return base64_data, mime_type, None

    except Exception as e:
        LOGGER.exception(f"Error downloading training image: {e}")
        return None, None, f"Download failed: {str(e)}"


def _extract_topic_name(telegram_msg: Dict[str, Any]) -> Optional[str]:
    """Extract forum topic name from a Telegram message if available.

    In forum groups, non-reply messages have reply_to_message pointing to the
    topic's service message which contains forum_topic_created.name.
    """
    reply_to = telegram_msg.get("reply_to_message", {})
    if reply_to:
        ftc = reply_to.get("forum_topic_created")
        if ftc and ftc.get("name"):
            return str(ftc["name"])
    # Also check if this message itself is a topic creation/edit service message
    ftc = telegram_msg.get("forum_topic_created")
    if ftc and ftc.get("name"):
        return str(ftc["name"])
    fte = telegram_msg.get("forum_topic_edited")
    if fte and fte.get("name"):
        return str(fte["name"])
    return None


def _normalize_telegram_webhook(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize native Telegram webhook format to internal WebhookRequest format.

    Telegram sends:
    {
        "message": {
            "message_id": 123,
            "from": {"id": 123456789, "username": "user", "first_name": "Name"},
            "text": "message text",
            "chat": {"id": -100123456789, "type": "group"},
            "message_thread_id": 42  // optional
        }
    }

    Converts to:
    {
        "message": "message text",
        "user_id": "123456789",
        "username": "user",
        "source": "telegram",
        "chat_id": "-100123456789",
        "topic_id": "42",
        "metadata": {"telegram_message_id": 123, "chat_type": "group"}
    }

    Args:
        args: Raw Telegram webhook payload

    Returns:
        Normalized webhook request dict
    """
    # Debug logging
    LOGGER.info(f"_normalize_telegram_webhook called with keys: {list(args.keys())}")
    if "message" in args:
        LOGGER.info(
            f"'message' field type: {type(args['message'])}, value: {str(args.get('message'))[:200]}"
        )
    if "update_id" in args:
        LOGGER.info("Detected update_id field - this looks like a Telegram webhook")

    # Check if this is a native Telegram format
    # Telegram format has nested "message" or "edited_message" object with "from" and "text" fields
    telegram_msg = None
    if "message" in args and isinstance(args["message"], dict) and "from" in args["message"]:
        telegram_msg = args["message"]
    elif (
        "edited_message" in args
        and isinstance(args["edited_message"], dict)
        and "from" in args["edited_message"]
    ):
        telegram_msg = args["edited_message"]
        LOGGER.info("Processing edited_message from Telegram")

    if telegram_msg:
        # Extract fields from nested Telegram structure
        from_user = telegram_msg.get("from", {})
        chat = telegram_msg.get("chat", {})

        # Get message text (caption for photos, text for regular messages)
        message_text = telegram_msg.get("text", "") or telegram_msg.get("caption", "")

        # Extract reply context if user is replying to a previous message
        # This helps the LLM understand what "yes", "no", "that one" etc. refer to
        reply_context = None
        reply_to_message = telegram_msg.get("reply_to_message")
        if reply_to_message:
            reply_text = reply_to_message.get("text", "") or reply_to_message.get("caption", "")
            reply_from = reply_to_message.get("from", {})
            reply_sender = reply_from.get("first_name") or reply_from.get("username") or "Someone"
            reply_is_bot = reply_from.get("is_bot", False)

            if reply_text:
                # Truncate very long replied messages
                if len(reply_text) > 500:
                    reply_text = reply_text[:497] + "..."

                # Format the reply context
                sender_label = "the bot" if reply_is_bot else reply_sender
                reply_context = f'[In reply to {sender_label}: "{reply_text}"]'
                LOGGER.info(f"Extracted reply context: {reply_context[:100]}...")

        # Prepend reply context to message if present
        if reply_context:
            message_text = f"{reply_context}\n\n{message_text}"

        normalized = {
            "message": message_text,
            "user_id": str(from_user.get("id", "")),
            # Prefer first_name (actual name) over username (Telegram handle)
            "username": from_user.get("first_name") or from_user.get("username") or "Unknown",
            "source": "telegram",
            "chat_id": str(chat.get("id", "")),
            "metadata": {
                "telegram_message_id": telegram_msg.get("message_id"),
                "chat_type": chat.get("type"),
                "chat_title": chat.get("title"),
            },
        }

        # Store reply-to metadata for context jump in init_services
        if reply_to_message:
            reply_from = reply_to_message.get("from", {})
            reply_sender = reply_from.get("first_name") or reply_from.get("username") or "Someone"
            reply_is_bot = reply_from.get("is_bot", False)
            reply_date = reply_to_message.get("date")
            # Convert Unix timestamp to ISO format if present
            if reply_date and isinstance(reply_date, (int, float)):
                from datetime import datetime, timezone

                reply_date = datetime.fromtimestamp(reply_date, tz=timezone.utc).isoformat()
            normalized["metadata"]["reply_to"] = {
                "text": (reply_to_message.get("text", "") or reply_to_message.get("caption", ""))[
                    :500
                ],
                "sender": reply_sender,
                "is_bot": reply_is_bot,
                "date": reply_date,
                "message_id": reply_to_message.get("message_id"),
            }

        # Handle photos — prefer _photo_file_ids from media group aggregation
        if "_photo_file_ids" in telegram_msg:
            normalized["metadata"]["photo_file_ids"] = telegram_msg["_photo_file_ids"]
            LOGGER.info(f"Telegram album with {len(telegram_msg['_photo_file_ids'])} photos")
        elif "photo" in telegram_msg and telegram_msg["photo"]:
            # Single photo — existing behavior
            largest_photo = telegram_msg["photo"][-1]
            normalized["metadata"]["photo_file_id"] = largest_photo.get("file_id")
            LOGGER.info(f"Telegram message contains photo: {largest_photo.get('file_id')}")

        # Handle videos
        if "video" in telegram_msg and telegram_msg["video"]:
            video = telegram_msg["video"]
            normalized["metadata"]["video_file_id"] = video.get("file_id")
            LOGGER.info(f"Telegram message contains video: {video.get('file_id')}")

        # Handle voice messages
        if "voice" in telegram_msg and telegram_msg["voice"]:
            voice = telegram_msg["voice"]
            normalized["metadata"]["voice_file_id"] = voice.get("file_id")
            LOGGER.info(f"Telegram message contains voice: {voice.get('file_id')}")

        # Handle audio files
        if "audio" in telegram_msg and telegram_msg["audio"]:
            audio = telegram_msg["audio"]
            normalized["metadata"]["audio_file_id"] = audio.get("file_id")
            LOGGER.info(f"Telegram message contains audio: {audio.get('file_id')}")

        # Add optional topic_id for forum groups
        if "message_thread_id" in telegram_msg:
            normalized["topic_id"] = str(telegram_msg["message_thread_id"])
            normalized["metadata"]["topic_id"] = str(telegram_msg["message_thread_id"])
            # Try to extract topic name from reply_to_message's service message
            topic_name = _extract_topic_name(telegram_msg)
            if topic_name:
                normalized["metadata"]["topic_name"] = topic_name

        # Preserve _auth_method if set by app.py
        if "_auth_method" in args:
            normalized["_auth_method"] = args["_auth_method"]

        LOGGER.info(
            f"Normalized Telegram webhook: user={normalized['user_id']}, "
            f"chat={normalized['chat_id']}, text={normalized['message'][:50]}..."
        )

        return normalized

    # Not a Telegram format, return as-is
    return args


async def _save_passive_group_message(telegram_msg: Dict[str, Any], chat: Dict[str, Any]) -> None:
    """
    Save a group message to chat history without processing it (passive listening).

    This allows the bot to have conversation context when someone does @mention it later.
    The message is saved with role="user" but no bot response is generated.

    Args:
        telegram_msg: Raw Telegram message object
        chat: Chat object from the message
    """
    # Extract message details
    chat_id = str(chat.get("id", ""))
    topic_id = telegram_msg.get("message_thread_id")
    message_text = telegram_msg.get("text", "") or telegram_msg.get("caption", "")
    from_user = telegram_msg.get("from", {})
    sender_name = (
        from_user.get("first_name", "")
        or from_user.get("username", "")
        or str(from_user.get("id", "Unknown"))
    )

    if not message_text or not chat_id:
        return

    # Extract topic name from forum topic service messages (if available)
    topic_name = _extract_topic_name(telegram_msg)

    # Generate session_id using the same logic as main handler
    session_id = generate_session_id(
        source="telegram",
        chat_id=chat_id,
        topic_id=str(topic_id) if topic_id else None,
        user_id=str(from_user.get("id", "")),
    )

    # Build a meaningful session title: "GroupTitle / TopicName" or fallback
    chat_title = chat.get("title", "")
    if topic_name:
        session_title = f"{chat_title} / {topic_name}" if chat_title else topic_name
    elif chat_title:
        session_title = chat_title
    else:
        session_title = f"Group Chat {chat_id[:15]}"

    # Create supabase client and get/create session
    supabase_client = get_supabase_client()

    # Try to find existing session (handles both hashed and legacy formats)
    session_obj = await supabase_client.get_session_by_chat_id(
        source="telegram",
        chat_id=chat_id,
        topic_id=str(topic_id) if topic_id else None,
    )

    if not session_obj:
        # Create new session with descriptive title
        session_obj = await supabase_client.create_session(
            session_id=session_id,
            user_id=None,
            title=session_title,
            organization_id=None,  # Will be determined when bot is @mentioned
            telegram_chat_id=chat_id,
            telegram_topic_id=str(topic_id) if topic_id else None,
        )
        LOGGER.info(f"Created passive listening session: {session_id} ({session_title})")
    elif topic_name:
        # Session exists but topic may have been renamed — refresh the title
        # so the admin sidebar shows the current name, not the stale original
        existing_title = getattr(session_obj, "title", "") or ""
        if existing_title != session_title:
            await supabase_client.update_session_title(session_obj.session_id, session_title)
            LOGGER.info(f"Updated session title: {existing_title!r} → {session_title!r}")

    # Prepend sender name to provide context about who said what
    # This is important for group chats where multiple people talk
    content_with_sender = f"[{sender_name}]: {message_text}"

    # Extract Telegram metadata for thread disentanglement
    telegram_message_id = telegram_msg.get("message_id")
    reply_to_msg = telegram_msg.get("reply_to_message")
    reply_to_telegram_message_id = reply_to_msg.get("message_id") if reply_to_msg else None
    sender_id = str(from_user.get("id")) if from_user.get("id") else None

    # Deterministic thread assignment (no LLM — too expensive for every passive message)
    thread_id = None
    if is_thread_disentanglement_enabled():
        try:
            history = await supabase_client.get_messages(
                session_uuid=session_obj.id,
                max_age_hours=2,
                max_messages=50,
            )
            thread_id = assign_passive_thread(history, reply_to_telegram_message_id)
        except Exception:
            LOGGER.warning("Passive thread assignment failed (non-fatal)", exc_info=True)

    # Build message metadata with topic info
    msg_metadata: Dict[str, Any] = {}
    if topic_id:
        msg_metadata["topic_id"] = str(topic_id)
    if topic_name:
        msg_metadata["topic_name"] = topic_name

    # Create the message to save
    passive_message = ConversationMessage(
        role="user",
        content=content_with_sender,
        metadata=msg_metadata,
        telegram_message_id=telegram_message_id,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        sender_id=sender_id,
        thread_id=thread_id,
    )

    # Determine group_id
    group_id = chat_id if chat_id.startswith("-") else None

    # Save the message
    await supabase_client.save_messages(
        session_uuid=session_obj.id,
        messages=[passive_message],
        from_chat_id=chat_id,
        group_id=group_id,
    )

    LOGGER.debug(
        "Saved passive group message: session=%s, sender=%s, topic=%s, thread_id=%s",
        session_id,
        sender_name,
        topic_name or topic_id,
        thread_id,
    )


async def _maybe_queue_agent_event(telegram_msg: Dict[str, Any], chat: Dict[str, Any]) -> None:
    """Check if a Telegram message should wake a persistent agent.

    Runs AFTER the normal message flow decision (respond/ignore) but
    in parallel with passive message saving. Non-fatal — never breaks
    the main webhook handler.
    """
    if os.getenv("PERSISTENT_AGENTS_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return

    chat_id = str(chat.get("id", ""))
    raw_topic = telegram_msg.get("message_thread_id")
    topic_id = str(raw_topic) if raw_topic is not None else None
    message_text = telegram_msg.get("text", "") or telegram_msg.get("caption", "")
    message_id = str(telegram_msg.get("message_id", ""))
    from_user = telegram_msg.get("from", {})

    if not message_text or not chat_id:
        return

    # Pre-filter: should any agent care about this message?
    from orchestrator.services.agent_event_filter import get_event_filter

    event_filter = get_event_filter()
    should_wake, event_type = event_filter.should_wake_agent(message_text, from_user)

    # Find persistent agent instances watching this chat.
    # Optimization: if the event filter didn't match, only query for user_agent
    # instances (they wake on any message). This avoids a DB query for ~90% of
    # messages that are irrelevant to system agents.
    try:
        supabase = get_supabase_client()._get_client()

        query = (
            supabase.table("persistent_agent_instances")
            .select("id, instance_name, status, anchor_metadata, expert_id, last_woke_at")
            .in_("status", ["active", "executing"])
            .eq("anchor_metadata->>telegram_chat_id", chat_id)
        )
        if not should_wake:
            # Only user agents wake on unfiltered messages
            query = query.eq("expert_id", "user_agent")

        result = query.execute()

        matching_instances = []
        now = datetime.now(timezone.utc)
        for inst in result.data or []:
            meta = inst.get("anchor_metadata", {})
            # Topic matching still needed client-side (optional field)
            raw_inst_topic = meta.get("telegram_topic_id")
            inst_topic_id = str(raw_inst_topic) if raw_inst_topic is not None else None

            if inst_topic_id and topic_id and inst_topic_id != topic_id:
                continue

            is_user_agent = inst.get("expert_id") == "user_agent"

            # System agents: only wake if event filter matched
            if not is_user_agent and not should_wake:
                continue

            # User agents: any message in their anchored group wakes them,
            # but rate-limited to max once every 5 minutes per instance
            if is_user_agent:
                last_woke = inst.get("last_woke_at")
                if last_woke:
                    try:
                        last_dt = datetime.fromisoformat(last_woke.replace("Z", "+00:00"))
                        if (now - last_dt).total_seconds() < 300:
                            LOGGER.debug(
                                f"Skipping user agent {inst['instance_name']}: "
                                f"woke {int((now - last_dt).total_seconds())}s ago (< 300s)"
                            )
                            continue
                    except (ValueError, TypeError):
                        pass

            matching_instances.append((inst, "group_message" if is_user_agent else event_type))

        if not matching_instances:
            return

        # Queue event for each matching agent
        for inst, evt_type in matching_instances:
            event_data = {
                "text": message_text[:2000],
                "from": {
                    "id": from_user.get("id"),
                    "first_name": from_user.get("first_name", ""),
                    "username": from_user.get("username", ""),
                },
                "date": telegram_msg.get("date", ""),
                "message_id": message_id,
                "event_type": evt_type,
            }
            try:
                supabase.table("agent_events").insert(
                    {
                        "target_instance_id": str(inst["id"]),
                        "event_type": evt_type,
                        "event_data": event_data,
                        "source_message_id": f"tg_{chat_id}_{message_id}",
                    }
                ).execute()
                LOGGER.info(
                    f"Queued {evt_type} event for agent {inst['instance_name']} "
                    f"(msg_id={message_id})"
                )
            except Exception as e:
                # Dedup constraint violation is expected and fine
                if "duplicate" in str(e).lower() or "unique" in str(e).lower():
                    LOGGER.debug(f"Duplicate event skipped for agent {inst['instance_name']}")
                else:
                    LOGGER.warning(f"Failed to queue event for {inst['instance_name']}: {e}")

    except Exception as e:
        LOGGER.warning(f"Agent event queueing failed (non-fatal): {e}")


async def _lookup_agent_for_message(chat_id: str, message_id: int) -> str | None:
    """Look up which persistent agent sent a bot message.

    Checks chat_messages.metadata->>'agent_instance_id' for the given
    telegram_message_id. Returns the instance_id or None.
    """
    if os.getenv("PERSISTENT_AGENTS_ENABLED", "false").lower() not in ("true", "1", "yes"):
        return None

    try:
        supabase = get_supabase_client()._get_client()
        result = (
            supabase.table("chat_messages")
            .select("metadata")
            .eq("telegram_message_id", int(message_id))
            .eq("group_id", chat_id)
            .limit(1)
            .execute()
        )
        if result.data:
            metadata = result.data[0].get("metadata") or {}
            agent_id: str | None = metadata.get("agent_instance_id")
            return agent_id
    except Exception as e:
        LOGGER.warning(f"Agent message lookup failed: {e}")
    return None


async def _queue_reply_to_agent(
    telegram_msg: Dict[str, Any],
    chat: Dict[str, Any],
    agent_instance_id: str,
) -> None:
    """Queue a staff reply as an agent event for a persistent agent."""
    message_text = telegram_msg.get("text", "") or telegram_msg.get("caption", "")
    message_id = str(telegram_msg.get("message_id", ""))
    chat_id = str(chat.get("id", ""))

    if not message_text:
        return

    supabase = get_supabase_client()._get_client()

    try:
        supabase.table("agent_events").insert(
            {
                "target_instance_id": agent_instance_id,
                "event_type": "staff_reply",
                "event_data": {
                    "text": message_text[:2000],
                    "from": telegram_msg.get("from", {}),
                    "date": telegram_msg.get("date", ""),
                    "message_id": message_id,
                    "chat_id": chat_id,
                },
                "source_message_id": f"tg_{chat_id}_{message_id}",
            }
        ).execute()

        LOGGER.info(f"Queued staff_reply event for agent {agent_instance_id} (msg_id={message_id})")
    except Exception as e:
        # Dedup constraint violation is expected on webhook retries
        if "duplicate" in str(e).lower() or "unique" in str(e).lower():
            LOGGER.debug(f"Duplicate staff_reply event skipped for agent {agent_instance_id}")
        else:
            raise


async def _handle_message_reaction(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Telegram message_reaction updates.

    Telegram webhook format for reactions:
    {
        "update_id": 123456789,
        "message_reaction": {
            "chat": {"id": -100123456789, "type": "group"},
            "message_id": 42,
            "user": {"id": 987654321, "username": "user"},
            "date": 1699999999,
            "old_reaction": [{"type": "emoji", "emoji": "👍"}],
            "new_reaction": [{"type": "emoji", "emoji": "❤️"}]
        }
    }

    Args:
        args: Telegram webhook payload with message_reaction

    Returns:
        Response indicating success/failure
    """
    try:
        reaction_data = args.get("message_reaction", {})

        # Extract relevant fields
        chat_id = reaction_data.get("chat", {}).get("id")
        message_id = reaction_data.get("message_id")
        user = reaction_data.get("user", {})
        user_id = user.get("id")
        new_reactions = reaction_data.get("new_reaction", [])

        if not all([chat_id, message_id, user_id]):
            LOGGER.warning(
                f"Incomplete reaction data: chat_id={chat_id}, message_id={message_id}, user_id={user_id}"
            )
            return {"success": True, "message": "Ignored incomplete reaction"}

        # If no new reactions, user removed all reactions - ignore
        if not new_reactions:
            LOGGER.info(f"User {user_id} removed reactions from message {message_id}")
            return {"success": True, "message": "Reaction removed, not saved"}

        # Process each new reaction (usually just one, but could be multiple)
        # Uses module-level EMOJI_TO_FEEDBACK and NORMALIZED_EMOJI_TO_FEEDBACK
        saved_count = 0
        for reaction in new_reactions:
            if reaction.get("type") != "emoji":
                continue  # Skip custom emojis for now

            emoji = reaction.get("emoji")
            # Normalize emoji by stripping skin tone modifiers (👌🏽 -> 👌) and variation selectors
            normalized_emoji = _normalize_emoji(emoji) if emoji else emoji
            feedback_type = NORMALIZED_EMOJI_TO_FEEDBACK.get(normalized_emoji)

            if not feedback_type:
                LOGGER.info(f"Ignoring unmapped emoji reaction: {emoji}")
                continue

            # Get user display name (prefer first_name over Telegram handle)
            user_name = user.get("first_name") or user.get("username") or "Unknown"

            # Save feedback to database
            await _save_reaction_feedback(
                chat_id=chat_id,
                message_id=message_id,
                user_id=str(user_id),
                user_name=user_name,
                feedback_type=feedback_type,
                emoji=emoji,
            )
            saved_count += 1

            # Trigger escalation for negative feedback (thumbs_down)
            if feedback_type == "thumbs_down":
                await _escalate_negative_feedback(
                    chat_id=chat_id,
                    user_id=str(user_id),
                    user_name=user_name,
                    emoji=emoji,
                )

        LOGGER.info(f"Saved {saved_count} reaction(s) from user {user_id} on message {message_id}")

        return {"success": True, "message": f"Saved {saved_count} reaction(s)", "statusCode": 200}

    except Exception as e:
        LOGGER.exception(f"Error handling message reaction: {e}")
        return {"success": False, "error": str(e), "statusCode": 500}


async def _handle_callback_query(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Telegram callback_query updates (inline button clicks).

    Telegram webhook format for callback queries:
    {
        "update_id": 123456789,
        "callback_query": {
            "id": "unique_callback_id",
            "from": {"id": 987654321, "username": "user", "first_name": "Name"},
            "message": {
                "message_id": 42,
                "chat": {"id": -100123456789, "type": "group"},
                "text": "Original message text",
                "reply_markup": {...}
            },
            "chat_instance": "...",
            "data": "pd:abc12345:run_new"
        }
    }

    Args:
        args: Telegram webhook payload with callback_query

    Returns:
        Response indicating success/failure
    """
    try:
        from orchestrator.services.pending_decision_service import PendingDecisionService

        callback_query = args.get("callback_query", {})

        # Extract relevant fields
        callback_id = callback_query.get("id")
        callback_data = callback_query.get("data", "")
        from_user = callback_query.get("from", {})
        user_id = str(from_user.get("id", ""))
        user_name = from_user.get("first_name") or from_user.get("username") or "User"
        message = callback_query.get("message", {})
        chat = message.get("chat", {})
        chat_id = str(chat.get("id", ""))
        message_id = message.get("message_id")
        topic_id = message.get("message_thread_id")

        LOGGER.info(
            f"Processing callback query: id={callback_id}, data={callback_data}, "
            f"user={user_name} ({user_id}), chat={chat_id}"
        )

        # Parse the callback data
        parsed = parse_callback_data(callback_data)
        if not parsed:
            LOGGER.warning(f"Failed to parse callback_data: {callback_data}")
            await _answer_callback_query(callback_id, "Invalid button data")
            return {"success": True, "message": "Invalid callback data", "statusCode": 200}

        callback_type = parsed["type"]

        # =================================================================
        # PROCEDURE CALLBACKS (pc:choice) - LLM-generated options
        # =================================================================
        if callback_type == PROCEDURE_CALLBACK_PREFIX:
            return await _handle_procedure_callback(
                callback_id=callback_id,
                choice=parsed["choice"],
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                topic_id=topic_id,
                message_id=message_id,
                original_text=message.get("text", ""),
            )

        # =================================================================
        # STEP INPUT CALLBACKS (si:number) - Expert step handler options
        # =================================================================
        if callback_type == STEP_INPUT_CALLBACK_PREFIX:
            return await _handle_step_input_callback(
                callback_id=callback_id,
                choice=parsed["choice"],
                user_id=user_id,
                user_name=user_name,
                chat_id=chat_id,
                topic_id=topic_id,
                message_id=message_id,
                original_text=message.get("text", ""),
            )

        # =================================================================
        # ESCALATION TRACKING CALLBACKS (es:mapping_id) - Track as JIRA ticket
        # =================================================================
        if callback_type == ESCALATION_TRACK_CALLBACK_PREFIX:
            return await _handle_escalation_track_callback(
                callback_id=callback_id,
                mapping_id=parsed["mapping_id"],
                chat_id=chat_id,
                message_id=message_id,
                original_text=message.get("text", ""),
                clicker_telegram_id=user_id,
            )

        # =================================================================
        # ESCALATION CLOSE CALLBACKS (ec/en:mapping_id)
        # =================================================================
        if callback_type in (ESCALATION_CLOSE_SILENT_PREFIX, ESCALATION_CLOSE_NOTIFY_PREFIX):
            notify_customer = callback_type == ESCALATION_CLOSE_NOTIFY_PREFIX
            return await _handle_escalation_close_callback(
                callback_id=callback_id,
                mapping_id=parsed["mapping_id"],
                chat_id=chat_id,
                message_id=message_id,
                original_text=message.get("text", ""),
                notify_customer=notify_customer,
                clicker_telegram_id=user_id,
            )

        # =================================================================
        # ESCALATION OFFER CALLBACKS (eo:session_id) - Customer requests support
        # =================================================================
        if callback_type == ESCALATION_OFFER_PREFIX:
            return await _handle_escalation_offer_callback(
                callback_id=callback_id,
                session_id=parsed["session_id"],
                chat_id=chat_id,
                message_id=message_id,
                topic_id=topic_id,
                from_user=from_user,
            )

        # =================================================================
        # DECISION CALLBACKS (pd:id:action) - Expert workflow decisions
        # =================================================================
        id_prefix = parsed["id_prefix"]
        action = parsed["action"]

        # Find the pending decision by ID prefix
        decision_service = PendingDecisionService()

        # Look up the pending decision by full ID (callback_data now carries the full UUID)
        decision_result = await decision_service.get_decision_by_id(id_prefix)

        if not decision_result or decision_result.get("resolved_at"):
            LOGGER.warning(f"No pending decision found for id: {id_prefix}")
            await _answer_callback_query(callback_id, "This option has expired")
            # Edit message to indicate expired
            await _edit_message_remove_buttons(chat_id, message_id, topic_id)
            return {"success": True, "message": "Decision expired", "statusCode": 200}

        decision = decision_result
        decision_id = decision["id"]
        decision_context = decision.get("context") or {}

        # =================================================================
        # AUTHORIZATION: Validate who can click buttons
        # - Anyone can click "cancel" (prevents group chat lockup)
        # - Original user who triggered the decision
        # - Staff members (organization_id matches STAFF_ORG_ID)
        # =================================================================
        is_cancel_action = action in ("cancel", "abandon")
        original_user_id = decision_context.get("original_user_id")
        # Check if clicker is the original user
        is_original_user = user_id == original_user_id

        # Check if clicker is staff (need to look up their organization)
        is_staff = False
        if not is_original_user and not is_cancel_action:
            try:
                auth_service = get_auth_service()
                clicker_permissions = await auth_service.resolve_user_permissions(
                    user_id, source="telegram"
                )
                if clicker_permissions and clicker_permissions.organization_id == _STAFF_ORG_ID:
                    is_staff = True
                    LOGGER.info(f"Staff user {user_name} ({user_id}) clicking button")
            except Exception as auth_error:
                LOGGER.warning(f"Could not check staff status for {user_id}: {auth_error}")

        # Validate authorization
        if not (is_cancel_action or is_original_user or is_staff):
            LOGGER.info(
                f"Unauthorized button click: user={user_id}, original={original_user_id}, "
                f"action={action}, is_staff={is_staff}"
            )
            await _answer_callback_query(
                callback_id,
                "Only the person who started this or staff can select this option",
                show_alert=True,
            )
            return {"success": True, "message": "Unauthorized", "statusCode": 200}

        # NOTE: Do NOT resolve the decision here. The expert_router will find
        # it via get_pending_decision() and resolve it after handling the action.
        # Resolving early causes the router to miss the decision and fall through
        # to normal Gemini processing (e.g., "1" treated as a regular message).
        LOGGER.info(
            f"Keeping decision {decision_id} pending for graph processing (action={action})"
        )

        # Answer the callback query (removes loading indicator)
        # Extract the full button label from the inline keyboard for human-readable display
        action_display = action.replace("_", " ").title()
        reply_markup = message.get("reply_markup", {})
        for row in reply_markup.get("inline_keyboard", []):
            for btn in row:
                if btn.get("callback_data") == callback_data:
                    action_display = btn["text"]
                    break
        await _answer_callback_query(callback_id, f"Selected: {action_display[:50]}")

        # Edit the original message to show selection and remove buttons
        original_text = message.get("text", "")
        updated_text = f"{original_text}\n\n✓ *Selected: {action_display}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Trigger processing immediately — don't wait for the user's next message.
        # Map the action back to the number the expert_router expects (e.g., run_new → "1").
        is_resumable = decision_context.get("is_resumable", False)
        action_to_number = {
            "run_new": "1",
            "resume": "2" if is_resumable else "1",
            "start_fresh": "2",
            "cancel": "3" if is_resumable else "2",
            "abandon": "3",
        }
        user_input = action_to_number.get(action, action)

        # Build user context and process through the graph
        auth_service = get_auth_service()
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        response_text, tool_results, reply_markup = await _process_webhook_with_graph(
            user_input=user_input,
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_decision_button": True,
                "decision_action": action,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Decision resolved: {action}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling callback query: {e}")
        # Try to answer the callback to remove loading state
        try:
            callback_id = args.get("callback_query", {}).get("id")
            if callback_id:
                await _answer_callback_query(callback_id, "An error occurred")
        except Exception:
            pass
        return {"success": False, "error": str(e), "statusCode": 500}


def _extract_button_text(original_text: str, choice: str) -> str:
    """Extract the full text of a button option from the original message.

    The original message contains button options in one of these formats:
    - Numbered: "1. Check all grid statuses" or "1) Check all grid statuses"
    - Unnumbered (parsed by line position): Just "Check all grid statuses"

    Args:
        original_text: The original message text containing button options
        choice: The choice number as string (e.g., "1", "2")

    Returns:
        The full text of the selected option, or a descriptive fallback
    """
    import re

    try:
        choice_num = int(choice)
    except ValueError:
        return f"Option {choice}"

    # Try to find numbered option pattern: "1. Option text" or "1) Option text"
    # Look for the pattern at the start of a line
    pattern = rf"^\s*{choice_num}[.\)]\s*(.+?)\s*$"
    match = re.search(pattern, original_text, re.MULTILINE)
    if match:
        return match.group(1).strip()

    # Fallback: Try to find options in a BUTTONS block and get by position
    # Look for lines that might be button options (non-empty, not the BUTTONS markers)
    lines = original_text.split("\n")
    button_lines = []
    in_buttons_block = False

    for line in lines:
        line_stripped = line.strip().lower()
        # Detect start of buttons section
        if "buttons" in line_stripped and "/" not in line_stripped:
            in_buttons_block = True
            continue
        # Detect end of buttons section
        if "/buttons" in line_stripped:
            in_buttons_block = False
            continue
        # Collect button option lines
        if in_buttons_block and line.strip():
            # Remove any leading number if present
            clean_line = re.sub(r"^\s*\d+[.\)]\s*", "", line.strip())
            if clean_line:
                button_lines.append(clean_line)

    # Return the option at the selected index (1-based)
    if 0 < choice_num <= len(button_lines):
        return button_lines[choice_num - 1]

    # Final fallback
    return f"Option {choice}"


async def _handle_procedure_callback(
    callback_id: str,
    choice: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    topic_id: int | None,
    message_id: int,
    original_text: str,
) -> Dict[str, Any]:
    """Handle procedure choice callback (pc:choice).

    When user clicks a procedure button, we:
    1. Answer the callback query
    2. Edit the message to show the selection
    3. Process the choice as a user message
    4. Send the LLM's response

    Args:
        callback_id: Telegram callback query ID
        choice: The choice number ("1", "2", etc.)
        user_id: Telegram user ID
        user_name: User display name
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID
        message_id: Original message ID
        original_text: Original message text

    Returns:
        Response dict
    """
    try:
        LOGGER.info(
            f"Handling procedure callback: choice={choice}, user={user_name} ({user_id}), "
            f"chat={chat_id}"
        )

        # Extract the full text of the selected option from the original message
        # Buttons were formatted as "1. Option text" or just "Option text" on separate lines
        selected_text = _extract_button_text(original_text, choice)
        LOGGER.info(f"Extracted button text for choice {choice}: {selected_text}")

        # Answer the callback query immediately
        await _answer_callback_query(callback_id, f"Selected: {selected_text[:50]}")

        # Edit the message to show selection and remove buttons
        updated_text = f"{original_text}\n\n✓ *Selected: {selected_text}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Get user context and process the choice as a message
        auth_service = get_auth_service()

        # Resolve user permissions
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        # Build user context
        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        # Generate session ID
        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        # Process the choice as a user message through the graph
        # The choice number becomes the user's input
        response_text, tool_results, reply_markup = await _process_webhook_with_graph(
            user_input=selected_text,  # Send the full selected option text
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_procedure_button": True,
                "original_choice_number": choice,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            # Check for new procedure buttons in the response
            if is_procedure_buttons_enabled():
                clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
                if proc_keyboard:
                    response_text = clean_text
                    # Procedure buttons take precedence over decision buttons
                    reply_markup = proc_keyboard

            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Processed procedure choice: {choice}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling procedure callback: {e}")
        # Try to send error message
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            try:
                webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await _send_telegram_response(
                    webhook_url=webhook_url,
                    chat_id=chat_id,
                    topic_id=str(topic_id) if topic_id else None,
                    text="Sorry, I encountered an error processing your selection. Please try again.",
                )
            except Exception:
                pass
        return {"success": False, "error": str(e), "statusCode": 500}


async def _handle_step_input_callback(
    callback_id: str,
    choice: str,
    user_id: str,
    user_name: str,
    chat_id: str,
    topic_id: int | None,
    message_id: int,
    original_text: str,
) -> Dict[str, Any]:
    """Handle step input callback (si:choice).

    Similar to procedure callback but sends the choice NUMBER as user_input,
    since step handlers parse by number (e.g., "1", "2", "3") or keyword.

    Args:
        callback_id: Telegram callback query ID
        choice: The choice number ("1", "2", etc.)
        user_id: Telegram user ID
        user_name: User display name
        chat_id: Telegram chat ID
        topic_id: Optional topic/thread ID
        message_id: Original message ID
        original_text: Original message text

    Returns:
        Response dict
    """
    try:
        LOGGER.info(
            f"Handling step input callback: choice={choice}, user={user_name} ({user_id}), "
            f"chat={chat_id}"
        )

        # Extract the full text of the selected option from the original message
        selected_text = _extract_button_text(original_text, choice)
        LOGGER.info(f"Extracted step input text for choice {choice}: {selected_text}")

        # Answer the callback query immediately
        await _answer_callback_query(callback_id, f"Selected: {selected_text[:50]}")

        # Edit the message to show selection and remove buttons
        updated_text = f"{original_text}\n\n✓ *Selected: {selected_text}*"
        await _edit_message_text(
            chat_id, message_id, updated_text, topic_id, reply_markup={"inline_keyboard": []}
        )

        # Get user context and process the choice as a message
        auth_service = get_auth_service()

        # Resolve user permissions
        try:
            permissions = await auth_service.resolve_user_permissions(user_id, source="telegram")
            user_email = permissions.email
            organization_ids = (
                [str(permissions.organization_id)] if permissions.organization_id else []
            )
        except Exception as auth_error:
            LOGGER.warning(f"Could not resolve permissions for {user_id}: {auth_error}")
            user_email = f"telegram_{user_id}"
            organization_ids = []

        # Build user context
        user_context = UserContext(
            user_id=user_id,
            user_email=user_email,
            username=user_name,
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            is_group=chat_id.startswith("-"),
            organization_ids=organization_ids,
        )

        # Generate session ID
        session_id = generate_session_id(
            source="telegram",
            chat_id=chat_id,
            topic_id=str(topic_id) if topic_id else None,
            user_id=user_id,
        )

        # Process the choice NUMBER through graph (step handlers parse numbers)
        # KEY DIFFERENCE from procedure callback: send "1" not the full option text
        response_text, tool_results, reply_markup = await _process_webhook_with_graph(
            user_input=choice,
            user_context=user_context,
            session_id=session_id,
            metadata={
                "original_chat_id": chat_id,
                "topic_id": str(topic_id) if topic_id else None,
                "from_step_input_button": True,
                "original_choice_number": choice,
            },
        )

        # Send the response
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token and response_text:
            # Check for new procedure buttons in the response
            if is_procedure_buttons_enabled():
                clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
                if proc_keyboard:
                    response_text = clean_text
                    reply_markup = proc_keyboard

            webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            await _send_telegram_response(
                webhook_url=webhook_url,
                chat_id=chat_id,
                topic_id=str(topic_id) if topic_id else None,
                text=response_text,
                reply_markup=reply_markup,
            )

        return {
            "success": True,
            "message": f"Processed step input choice: {choice}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling step input callback: {e}")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if bot_token:
            try:
                webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                await _send_telegram_response(
                    webhook_url=webhook_url,
                    chat_id=chat_id,
                    topic_id=str(topic_id) if topic_id else None,
                    text="Sorry, I encountered an error processing your selection. Please try again.",
                )
            except Exception:
                pass
        return {"success": False, "error": str(e), "statusCode": 500}


async def _handle_escalation_offer_callback(
    callback_id: str,
    session_id: str,
    chat_id: str,
    message_id: int,
    topic_id: Optional[int],
    from_user: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle escalation offer callback (eo:session_id).

    Customer clicked "Contact support" after a system error.
    Triggers escalation, removes the button, confirms to customer.
    """
    from orchestrator.services.escalation_service import EscalationService
    from orchestrator.services.supabase_client import SupabaseClient

    try:
        await _answer_callback_query(callback_id, "Contacting support...")
        await _edit_message_remove_buttons(chat_id, message_id, topic_id)

        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        customer_username = (
            from_user.get("username") or f"{first_name} {last_name}".strip() or "Customer"
        )

        # Look up organization from session for proper escalation routing
        organization_id = None
        try:
            supabase_client = SupabaseClient(
                url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", ""),
                key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", ""),
            )
            session_obj = await supabase_client.get_session(session_id)
            if session_obj:
                organization_id = session_obj.organization_id
        except Exception as e:
            LOGGER.warning(f"Could not look up org for escalation offer: {e}")

        escalation_service = EscalationService()
        if escalation_service.is_enabled():
            result = await escalation_service.escalate_to_support(
                question_summary="Customer requested support after system error",
                session_id=session_id,
                organization_id=organization_id,
                customer_chat_id=str(chat_id),
                customer_topic_id=str(topic_id) if topic_id else None,
                customer_username=customer_username,
                reason="user_requested",
            )
            if result.get("success"):
                LOGGER.info(f"Escalation offer accepted for session={session_id}")
            else:
                LOGGER.error(f"Escalation offer failed: {result.get('error')}")
        else:
            LOGGER.warning("Escalation service not enabled for offer callback")

        return {"success": True, "message": "Escalation offer handled", "statusCode": 200}

    except Exception as e:
        LOGGER.exception(f"Error handling escalation offer callback: {e}")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


async def _handle_escalation_track_callback(
    callback_id: str,
    mapping_id: str,
    chat_id: str,
    message_id: int,
    original_text: str,
    clicker_telegram_id: str = "",
) -> Dict[str, Any]:
    """Handle escalation tracking callback (es:mapping_id).

    Atomically claims the escalation, creates a JIRA ticket, edits the
    escalation message to show the ticket key, and notifies the customer.
    """
    from orchestrator.services.escalation_service import EscalationService

    try:
        # Authorization: only allow from escalation group
        escalation_group_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if chat_id != escalation_group_id:
            await _answer_callback_query(callback_id, "Unauthorized", show_alert=True)
            return {"success": True, "message": "Unauthorized", "statusCode": 403}

        try:
            uuid_mod.UUID(mapping_id)
        except ValueError:
            await _answer_callback_query(callback_id, "Invalid escalation ID", show_alert=True)
            return {"success": True, "message": "Invalid mapping_id", "statusCode": 400}

        supabase_client = get_supabase_client()

        # Atomic claim: only first click wins
        escalation = await supabase_client.claim_escalation_for_tracking(mapping_id)
        if not escalation:
            await _answer_callback_query(
                callback_id, "Escalation already closed or tracked", show_alert=True
            )
            await _edit_message_remove_buttons(chat_id, message_id)
            return {"success": True, "message": "Already claimed", "statusCode": 200}

        # Resolve clicker's email for JIRA assignment (non-blocking)
        clicker_email = None
        if clicker_telegram_id:
            try:
                from shared.auth import get_auth_service

                clicker_email = await get_auth_service().get_user_email(
                    clicker_telegram_id, source="telegram"
                )
            except Exception as e:
                LOGGER.debug(f"Could not resolve clicker email: {e}")

        # Show "Creating ticket..." toast immediately
        await _answer_callback_query(callback_id, "Creating JIRA ticket...")

        # Create ticket + notify customer + close escalation
        escalation_service = EscalationService()
        result = await escalation_service.track_as_ticket(
            escalation_mapping=escalation,
            assignee_email=clicker_email,
        )

        if result.get("success"):
            jira_key = result["jira_ticket_key"]
            # Edit escalation message: append ticket ref, remove button
            updated_text = f"{original_text}\n\n\u2705 Tracked as {jira_key}"
            await _edit_message_text(
                chat_id,
                message_id,
                updated_text,
                reply_markup={"inline_keyboard": []},
            )
            LOGGER.info(f"Escalation {mapping_id} tracked as {jira_key}")
        else:
            # Revert: re-activate the escalation since ticket creation failed
            await supabase_client.reactivate_escalation(mapping_id)
            error_msg = result.get("error", "Unknown error")
            LOGGER.error(f"Failed to track escalation {mapping_id}: {error_msg}")
            # Edit message to show failure (can't answer callback twice)
            failure_text = f"{original_text}\n\n\u274c Ticket creation failed — try again"
            await _edit_message_text(chat_id, message_id, failure_text)

        return {
            "success": True,
            "message": f"Escalation tracking: {result.get('jira_ticket_key', 'failed')}",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling escalation track callback: {e}")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


async def _handle_escalation_close_callback(
    callback_id: str,
    mapping_id: str,
    chat_id: str,
    message_id: int,
    original_text: str,
    notify_customer: bool = False,
    clicker_telegram_id: str = "",
) -> Dict[str, Any]:
    """Handle escalation close callbacks (ec/en:mapping_id).

    Closes the escalation. If notify_customer is True, sends a resolution
    message to the customer. Otherwise closes silently.
    """
    from orchestrator.services.escalation_service import EscalationService

    claimed = False
    supabase_client = None
    try:
        # Authorization: only allow from escalation group
        escalation_group_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        if chat_id != escalation_group_id:
            await _answer_callback_query(callback_id, "Unauthorized", show_alert=True)
            return {"success": True, "message": "Unauthorized", "statusCode": 403}

        try:
            uuid_mod.UUID(mapping_id)
        except ValueError:
            await _answer_callback_query(callback_id, "Invalid escalation ID", show_alert=True)
            return {"success": True, "message": "Invalid mapping_id", "statusCode": 400}

        supabase_client = get_supabase_client()

        # Atomic claim: only first click wins
        escalation = await supabase_client.claim_escalation_for_tracking(mapping_id)
        if not escalation:
            await _answer_callback_query(
                callback_id, "Escalation already closed or tracked", show_alert=True
            )
            await _edit_message_remove_buttons(chat_id, message_id)
            return {"success": True, "message": "Already claimed", "statusCode": 200}

        claimed = True

        # Set resolved_at immediately so orphan recovery (which reactivates
        # is_active=False rows without resolved_at) never re-opens this intentional close.
        try:
            _db = supabase_client._get_client()
            _db.table("escalation_mappings").update(
                {"resolved_at": datetime.now(timezone.utc).isoformat()}
            ).eq("id", mapping_id).execute()
        except Exception:
            LOGGER.warning("Could not set resolved_at for mapping %s", mapping_id, exc_info=True)

        session_id = escalation.get("session_id")
        if not session_id:
            await _answer_callback_query(callback_id, "No session found", show_alert=True)
            return {"success": False, "error": "No session_id", "statusCode": 500}

        action_label = "Closing & notifying..." if notify_customer else "Closing silently..."
        await _answer_callback_query(callback_id, action_label)

        # The claim already set this mapping to is_active=false.
        # Only release the session if no other blocking escalations remain.
        # Non-blocking ones (safety_escalation) never set is_escalated=True,
        # so they shouldn't prevent session release.
        remaining = await supabase_client.count_active_blocking_escalations(session_id)
        if remaining == 0:
            await supabase_client.update_session_escalation_status(
                session_id=session_id, is_escalated=False
            )

        # Notify customer if requested
        if notify_customer:
            customer_chat_id = escalation.get("customer_chat_id", "")
            customer_topic_id = escalation.get("customer_topic_id")
            if customer_chat_id:
                escalation_service = EscalationService()
                await escalation_service.notify_customer_resolved(
                    customer_chat_id, customer_topic_id
                )

        # Update message: show what happened, remove buttons
        if notify_customer:
            status_text = f"{original_text}\n\n\u2705 Closed — customer notified"
        else:
            status_text = f"{original_text}\n\n\U0001f507 Closed silently"

        await _edit_message_text(
            chat_id,
            message_id,
            status_text,
            reply_markup={"inline_keyboard": []},
        )
        claimed = False  # Success — no rollback needed

        # Transition Jira to Done if the escalation had a tracked ticket.
        # Non-fatal — failure is logged inside _transition_jira_to_done.
        jira_key = escalation.get("jira_ticket_key")
        if jira_key:
            escalation_svc = EscalationService()
            await escalation_svc._transition_jira_to_done(jira_key)

        LOGGER.info(
            f"Escalation {mapping_id} closed by {clicker_telegram_id} (notify={notify_customer})"
        )

        return {
            "success": True,
            "message": f"Escalation closed (notify={notify_customer})",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling escalation close callback: {e}")
        if claimed and supabase_client:
            try:
                await supabase_client.reactivate_escalation(mapping_id)
            except Exception:
                LOGGER.error(f"CRITICAL: Failed to reactivate escalation {mapping_id} after error")
        try:
            await _answer_callback_query(callback_id, "An error occurred", show_alert=True)
        except Exception:
            pass
        return {"success": False, "error": "Internal error", "statusCode": 500}


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


async def _save_reaction_feedback(
    chat_id: int,
    message_id: int,
    user_id: str,
    user_name: str,
    feedback_type: str,
    emoji: str,
) -> None:
    """
    Save reaction feedback to the chat_messages metadata column.

    Feedback is stored as an array to support multiple users reacting to the same message.
    If a user changes their reaction, their previous feedback is updated (not duplicated).

    Args:
        chat_id: Telegram chat ID
        message_id: Telegram message ID
        user_id: Telegram user ID
        user_name: Telegram username or first_name for display
        feedback_type: 'thumbs_up' or 'thumbs_down'
        emoji: The actual emoji used
    """
    try:
        from datetime import datetime, timezone

        from orchestrator.services.supabase_client import SupabaseClient

        # Initialize Supabase client (chat database with legacy fallback)
        supabase_client = SupabaseClient(
            url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", ""),
            key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", ""),
        )

        # Look up session using helper that handles both hashed and legacy formats
        session_obj = await supabase_client.get_session_by_chat_id(
            source="telegram",
            chat_id=str(chat_id),
        )

        if not session_obj:
            LOGGER.warning(f"Session not found for chat_id {chat_id}, cannot save feedback")
            return

        # Find the most recent bot message for this session
        client = supabase_client._get_client()

        messages_response = (
            client.table("chat_messages")
            .select("id, metadata")
            .eq("session_id", str(session_obj.id))
            .eq("role", "model")  # Reactions are typically on bot responses
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        if not messages_response.data:
            LOGGER.warning(f"No model messages found in session {session_obj.session_id}")
            return

        msg = messages_response.data[0]
        msg_id = msg["id"]
        current_metadata = msg.get("metadata") or {}

        # Get existing feedback array (or migrate from old single-object format)
        existing_feedback = current_metadata.get("feedback", [])

        # Migrate old single-object format to array format
        if isinstance(existing_feedback, dict):
            existing_feedback = [existing_feedback]

        # Create new feedback entry
        new_feedback_entry = {
            "type": feedback_type,
            "emoji": emoji,
            "telegram_user_id": user_id,
            "user_name": user_name,
            "telegram_message_id": message_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        # Check if this user already gave feedback - update instead of duplicate
        user_found = False
        for i, fb in enumerate(existing_feedback):
            if fb.get("telegram_user_id") == user_id:
                existing_feedback[i] = new_feedback_entry
                user_found = True
                break

        if not user_found:
            existing_feedback.append(new_feedback_entry)

        # Update metadata with feedback array
        current_metadata["feedback"] = existing_feedback

        # Update the message metadata
        client.table("chat_messages").update({"metadata": current_metadata}).eq(
            "id", msg_id
        ).execute()

        LOGGER.info(f"Saved {feedback_type} feedback for message {msg_id} from {user_name}")

    except Exception as e:
        LOGGER.exception(f"Error saving reaction feedback: {e}")
        # Don't raise - we don't want to fail the webhook response


async def _escalate_negative_feedback(
    chat_id: int,
    user_id: str,
    user_name: str,
    emoji: str,
) -> None:
    """
    Escalate to support when a customer gives negative feedback (thumbs down).

    This creates an escalation with the bot's response and the negative reaction,
    allowing support to review and potentially follow up with the customer.

    Args:
        chat_id: Telegram chat ID
        user_id: Telegram user ID
        user_name: User display name
        emoji: The negative emoji that was used
    """
    try:
        from orchestrator.services.escalation_service import EscalationService
        from orchestrator.services.supabase_client import SupabaseClient

        LOGGER.info(f"Escalating negative feedback ({emoji}) from {user_name} in chat {chat_id}")

        # Initialize clients
        supabase_client = SupabaseClient(
            url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", ""),
            key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", ""),
        )
        escalation_service = EscalationService(
            supabase_url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY"),
        )

        if not escalation_service.is_enabled():
            LOGGER.warning("Escalation service not enabled - skipping negative feedback escalation")
            return

        # Get the session to find the bot message that received the reaction
        session_obj = await supabase_client.get_session_by_chat_id(
            source="telegram",
            chat_id=str(chat_id),
        )

        if not session_obj:
            LOGGER.warning(f"No session found for chat_id {chat_id} - cannot escalate feedback")
            return

        # Get the most recent bot message (the one that was reacted to)
        client = supabase_client._get_client()
        messages_response = (
            client.table("chat_messages")
            .select("id, content, created_at")
            .eq("session_id", str(session_obj.id))
            .eq("role", "model")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        bot_message = ""
        if messages_response.data:
            bot_message = messages_response.data[0].get("content", "")

        # Get the most recent user message for context
        user_messages_response = (
            client.table("chat_messages")
            .select("content")
            .eq("session_id", str(session_obj.id))
            .eq("role", "user")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )

        user_question = ""
        if user_messages_response.data:
            user_question = user_messages_response.data[0].get("content", "")

        # Build escalation summary
        summary = f"Customer gave negative feedback ({emoji}) to bot response"
        if user_question:
            # Truncate long questions
            truncated_question = (
                user_question[:200] + "..." if len(user_question) > 200 else user_question
            )
            summary += f"\n\nOriginal question: {truncated_question}"
        if bot_message:
            # Truncate long responses
            truncated_response = (
                bot_message[:500] + "..." if len(bot_message) > 500 else bot_message
            )
            summary += f"\n\nBot response: {truncated_response}"

        # Escalate to support with negative_feedback reason
        result = await escalation_service.escalate_to_support(
            question_summary=summary,
            customer_chat_id=str(chat_id),
            session_id=str(session_obj.id),
            organization_id=session_obj.organization_id,
            customer_username=user_name,
            reason="negative_feedback",
        )

        if result.get("success"):
            LOGGER.info(f"Successfully escalated negative feedback from {user_name}")
        else:
            LOGGER.error(f"Failed to escalate negative feedback: {result.get('error')}")

    except Exception as e:
        LOGGER.exception(f"Error escalating negative feedback: {e}")
        # Don't raise - we don't want to fail the webhook response


async def _handle_escalation_reply(telegram_msg: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle support team reply to an escalation message.

    When support replies to an escalation in the escalation group,
    forward their response back to the customer chat.

    Args:
        telegram_msg: Telegram message dict with reply_to_message

    Returns:
        WebhookResponse dict
    """
    try:
        from orchestrator.services.escalation_service import EscalationService

        # Extract reply information
        reply_to_message = telegram_msg.get("reply_to_message", {})
        reply_to_message_id = reply_to_message.get("message_id")
        reply_text = telegram_msg.get("text", "")
        from_user = telegram_msg.get("from", {})
        # Prefer first_name (actual name) over username (Telegram handle)
        from_username = from_user.get("first_name") or from_user.get("username") or "Support"

        if not reply_to_message_id:
            LOGGER.warning("Reply message has no reply_to_message_id")
            return {
                "success": False,
                "error": "No reply_to_message_id found",
                "statusCode": 400,
            }

        if not reply_text:
            LOGGER.info("Reply has no text, ignoring (might be media or sticker)")
            return {
                "success": True,
                "message": "Non-text reply ignored",
                "statusCode": 200,
            }

        # Check if this is a "Closed" command
        escalation_service = EscalationService(
            supabase_url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL"),
            supabase_key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY"),
        )

        # Check for close/closed command (case insensitive, strip whitespace and punctuation)
        import re

        cleaned_text = re.sub(r"[^\w\s]", "", reply_text).strip().lower()
        LOGGER.info(
            f"Reply text: '{reply_text}', cleaned: '{cleaned_text}', reply_to_message_id: {reply_to_message_id}"
        )
        if cleaned_text in ("reopen", "reopened", "re open"):
            LOGGER.info(f"Detected 'Reopen' command for escalation message {reply_to_message_id}")

            supabase_client = escalation_service._get_supabase_client()
            mapping = None
            if supabase_client:
                mapping = await supabase_client.get_escalation_mapping(reply_to_message_id)

            if mapping:
                session_id = mapping.get("session_id")
                if session_id:
                    reopen_result = await escalation_service.reopen_escalation(
                        session_id, reply_to_message_id
                    )
                    _reopen_topic_id = mapping.get("escalation_topic_id")
                    if reopen_result.get("success"):
                        await escalation_service._send_telegram_reply(
                            chat_id=escalation_service._escalation_chat_id,
                            reply_to_message_id=reply_to_message_id,
                            text="🔓 Escalation reopened. You can now reply to assist the customer.",
                            topic_id=_reopen_topic_id,
                        )
                    else:
                        await escalation_service._send_telegram_reply(
                            chat_id=escalation_service._escalation_chat_id,
                            reply_to_message_id=reply_to_message_id,
                            text="❌ Failed to reopen escalation.",
                            topic_id=_reopen_topic_id,
                        )
                else:
                    LOGGER.warning(f"No session_id in mapping for reopen: {mapping}")
            else:
                LOGGER.warning(
                    f"No escalation mapping found for reopen message_id {reply_to_message_id}"
                )

            return {
                "success": True,
                "message": "Reopen command processed",
                "statusCode": 200,
            }

        if cleaned_text in ("close", "closed"):
            LOGGER.info(f"Detected 'Closed' command for escalation message {reply_to_message_id}")

            # Find mapping from database
            supabase_client = escalation_service._get_supabase_client()
            mapping = None
            if supabase_client:
                mapping = await supabase_client.get_escalation_mapping(reply_to_message_id)

            if mapping:
                # Get session_id from mapping (already stored in database)
                session_id = mapping.get("session_id")
                customer_chat_id = mapping.get("customer_chat_id", "")
                customer_topic_id = mapping.get("customer_topic_id")

                if session_id:
                    # Close ALL escalations for this session so the user
                    # resumes chatting with the bot immediately
                    LOGGER.info(f"Attempting to close escalation for session {session_id}")
                    close_result = await escalation_service.close_escalation(session_id)
                    LOGGER.info(f"Close escalation result: {close_result}")

                    if close_result.get("success"):
                        # Send resolution message to customer
                        resolution_result = await escalation_service._send_telegram_message(
                            chat_id=customer_chat_id,
                            topic_id=customer_topic_id,
                            text=(
                                "✅ Your support request has been resolved. "
                                "If you need further assistance, please feel free to reach out again!"
                            ),
                        )
                        LOGGER.info(f"Customer notification result: {resolution_result}")

                        # Send confirmation to escalation group
                        confirmation_text = "✅ Escalation closed"
                        if resolution_result.get("ok"):
                            confirmation_text += " and customer notified."
                        else:
                            confirmation_text += " (customer notification failed)."

                        confirmation_result = await escalation_service._send_telegram_reply(
                            chat_id=escalation_service._escalation_chat_id,
                            reply_to_message_id=reply_to_message_id,
                            text=confirmation_text,
                            topic_id=mapping.get("escalation_topic_id"),
                        )
                        LOGGER.info(f"Escalation group confirmation result: {confirmation_result}")

                        # Transition Jira to Done + remove buttons from escalation message.
                        # reply_to_message_id IS the escalation_message_id (the message staff
                        # replied "Closed" to). Run concurrently; failures are non-fatal.
                        cleanup_coros = []
                        jira_key = mapping.get("jira_ticket_key")
                        if jira_key:
                            cleanup_coros.append(
                                escalation_service._transition_jira_to_done(jira_key)
                            )
                        cleanup_coros.append(
                            _edit_message_remove_buttons(
                                str(escalation_service._escalation_chat_id),
                                reply_to_message_id,
                                topic_id=mapping.get("escalation_topic_id"),
                            )
                        )
                        await asyncio.gather(*cleanup_coros, return_exceptions=True)

                        # Return 200 even if customer notification failed - escalation is closed
                        LOGGER.info(
                            f"Closed escalation for session {session_id}. "
                            f"Customer notified: {resolution_result.get('ok', False)}"
                        )
                        return {
                            "success": True,
                            "message": "Escalation closed",
                            "statusCode": 200,
                        }
                    else:
                        LOGGER.error(f"Failed to close escalation in database: {close_result}")
                        return {
                            "success": False,
                            "error": "Failed to close escalation in database",
                            "statusCode": 500,
                        }
                else:
                    LOGGER.warning(f"No session_id in mapping: {mapping}")
            else:
                LOGGER.warning(f"No escalation mapping found for message_id {reply_to_message_id}")

            # Return success even if session_id not found (don't forward "Closed" to customer)
            return {
                "success": True,
                "message": "Closed command processed",
                "statusCode": 200,
            }

        # Forward reply to customer (normal support response)
        result = await escalation_service.handle_support_reply(
            reply_to_message_id=reply_to_message_id,
            reply_text=reply_text,
            from_username=from_username,
        )

        if result.get("success"):
            LOGGER.info(f"Successfully forwarded support reply from {from_username}")
            return {
                "success": True,
                "message": "Reply forwarded to customer",
                "statusCode": 200,
            }
        else:
            error = result.get("error", "Unknown error")
            LOGGER.warning(f"Could not forward support reply: {error}")
            # Return 200 to acknowledge the message and clear Telegram's retry queue
            # even if we couldn't find the escalation mapping
            return {
                "success": True,
                "message": f"Acknowledged (not forwarded: {error})",
                "statusCode": 200,
            }

    except Exception as e:
        LOGGER.exception(f"Error handling escalation reply: {e}")
        return {
            "success": False,
            "error": str(e),
            "statusCode": 500,
        }


def _handle_jira_webhook(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Jira webhook for ticket comments and status changes.

    Jira webhook format:
    {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SUP-123",
            "fields": {
                "status": {"name": "Closed"},
                "customfield_10057": "grid-123"  // Grid custom field
            }
        },
        "comment": {
            "body": "Comment text",
            "author": {"displayName": "John Doe"},
            "jiraProperties": [
                {"key": "sd.public.comment", "value": {"internal": false}}
            ]
        },
        "changelog": {
            "items": [
                {"field": "status", "toString": "Closed"}
            ]
        }
    }

    Args:
        args: Jira webhook payload

    Returns:
        WebhookResponse dict
    """
    try:
        # Validate API key
        api_key = os.getenv("JIRA_WEBHOOK_API_KEY", "")
        provided_key = args.get("api_key") or args.get("headers", {}).get("X-API-Key")

        if not api_key or provided_key != api_key:
            LOGGER.warning("Unauthorized Jira webhook request: invalid API key")
            return {
                "success": False,
                "error": "Unauthorized",
                "statusCode": 401,
            }

        webhook_event = args.get("webhookEvent", "")
        issue = args.get("issue", {})
        issue_key = issue.get("key")

        if not issue_key:
            LOGGER.warning("Jira webhook missing issue key")
            return {
                "success": False,
                "error": "Missing issue key",
                "statusCode": 400,
            }

        # Handle status change to Closed
        if webhook_event == "jira:issue_updated":
            changelog = args.get("changelog", {})
            status_changes = [
                item
                for item in changelog.get("items", [])
                if item.get("field") == "status" and item.get("toString") == "Closed"
            ]

            if status_changes:
                LOGGER.info(f"Jira ticket {issue_key} closed, handling closure")
                return _handle_jira_closure(issue_key)

        # Handle comment added
        comment = args.get("comment")
        if comment:
            # Check if comment is public (reply to customer)
            jira_properties = comment.get("jsdPublic", False) or any(
                prop.get("key") == "sd.public.comment"
                and not prop.get("value", {}).get("internal", True)
                for prop in comment.get("jiraProperties", [])
            )

            if jira_properties:
                comment_body = comment.get("body", "")
                comment_author = comment.get("author", {}).get("displayName", "Support")

                LOGGER.info(
                    f"Jira ticket {issue_key} received public comment from {comment_author}"
                )
                return _handle_jira_comment(issue_key, comment_body, comment_author)

        # Acknowledge webhook but no action needed
        return {
            "success": True,
            "message": "Webhook processed, no action needed",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling Jira webhook: {e}")
        return {
            "success": False,
            "error": str(e),
            "statusCode": 500,
        }


def _handle_jira_closure(issue_key: str) -> Dict[str, Any]:
    """
    Handle Jira ticket closure - find session and send resolution message to customer.

    Args:
        issue_key: Jira ticket key (e.g., "SUP-123")

    Returns:
        Response dict
    """
    # Jira escalation tracking is not currently supported
    # All escalations use Telegram as the primary channel
    LOGGER.warning(f"Jira closure webhook received for {issue_key} but Jira tracking not supported")
    return {
        "success": False,
        "error": "Jira escalation tracking not supported - use Telegram escalations",
        "statusCode": 501,
    }


def _handle_jira_comment(issue_key: str, comment_body: str, comment_author: str) -> Dict[str, Any]:
    """
    Handle Jira comment - forward to customer as if it came from LLM.

    Args:
        issue_key: Jira ticket key
        comment_body: Comment text
        comment_author: Comment author name

    Returns:
        Response dict
    """
    # Jira escalation tracking is not currently supported
    # All escalations use Telegram as the primary channel
    LOGGER.warning(f"Jira comment webhook received for {issue_key} but Jira tracking not supported")
    return {
        "success": False,
        "error": "Jira escalation tracking not supported - use Telegram escalations",
        "statusCode": 501,
    }


def main(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main serverless function handler for webhook requests.

    Supports two webhook formats:

    1. Native Telegram format:
    {
        "message": {
            "from": {"id": 123456789, "username": "user"},
            "text": "message text",
            "chat": {"id": -100123456789, "type": "group"},
            "message_thread_id": 42  // optional
        }
    }

    2. Internal webhook format (for all platforms):
    {
        "message": "User message text",
        "user_id": "Platform user ID",
        "source": "telegram|roam|web|api",
        "username": "Display name (optional)",
        "chat_id": "Chat/group ID (optional)",
        "topic_id": "Topic/thread ID (optional)",
        "user_email": "Email (optional, fallback if auth lookup fails)",
        "media": [...],
        "entity_context": {...},
        "metadata": {...}
    }

    Returns:
        WebhookResponse with success, message, session_id
    """
    try:
        # Check if this is a Jira webhook (has webhookEvent field)
        if "webhookEvent" in args and args.get("webhookEvent", "").startswith("jira:"):
            LOGGER.info(f"Received Jira webhook: {args.get('webhookEvent')}")
            return _handle_jira_webhook(args)

        # Check if this is a message_reaction update (Telegram reactions)
        if "message_reaction" in args:
            LOGGER.info("Received message_reaction update")
            return asyncio.run(_handle_message_reaction(args))

        # Check if this is a callback_query update (inline button click)
        if "callback_query" in args:
            LOGGER.info("Received callback_query update")
            return asyncio.run(_handle_callback_query(args))

        # Ignore non-message Telegram update types (my_chat_member, chat_member, etc.)
        # These are membership/status updates that don't contain user messages
        _ignored_update_types = {
            "my_chat_member",
            "chat_member",
            "chat_join_request",
            "poll",
            "poll_answer",
        }
        if any(key in args for key in _ignored_update_types):
            ignored_type = next(key for key in _ignored_update_types if key in args)
            LOGGER.debug(f"Ignoring Telegram update type: {ignored_type}")
            return {"success": True, "statusCode": 200}

        # Normalize Telegram webhook format if needed
        normalized_args = _normalize_telegram_webhook(args)

        # Validate webhook format
        if (
            "message" not in normalized_args
            or "user_id" not in normalized_args
            or "source" not in normalized_args
        ):
            return {
                "success": False,
                "error": "Invalid request format. Required fields: message, user_id, source",
                "statusCode": 400,
            }

        return _handle_webhook(normalized_args)

    except Exception as e:
        LOGGER.exception(f"Error processing request: {e}")

        # Send Telegram error notification with auto-captured context
        if tele_debug_sync:
            error_msg = f"❌ {type(e).__name__}: {str(e)}\n"
            error_msg += f"Source: {args.get('source', 'unknown')}\n"
            error_msg += f"User: {args.get('user_id', 'unknown')}\n"
            error_msg += f"Chat: {args.get('chat_id', 'unknown')}"
            tele_debug_sync(error_msg, include_traceback=True)

        # Get settings to check DEBUG mode
        try:
            settings = _get_settings()
            debug_mode = settings.debug
        except Exception:
            # Fallback if settings can't be loaded
            debug_mode = os.getenv("DEBUG", "false").lower() == "true"

        # Mask internal errors in production
        if debug_mode:
            error_detail = str(e)
        else:
            error_detail = "Internal server error"

        return {
            "success": False,
            "error": error_detail,
            "statusCode": 500,
        }


async def async_main(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Async version of main serverless function handler for webhook requests.

    Use this entry point when calling from an async context (e.g., FastAPI).
    """
    try:
        # SECURITY: Strip metadata from Telegram webhook requests to prevent auth bypass.
        # API-key-authenticated requests (e.g., broadcast scheduler) are trusted.
        # Internal re-entries (e.g., _flush_media_group) also keep their metadata.
        if not args.get("_internal_reentry") and args.get("_auth_method") != "api":
            args.pop("metadata", None)

        # Check if this is a Jira webhook (has webhookEvent field)
        if "webhookEvent" in args and args.get("webhookEvent", "").startswith("jira:"):
            LOGGER.info(f"Received Jira webhook: {args.get('webhookEvent')}")
            return _handle_jira_webhook(args)

        # Check if this is a message_reaction update (Telegram reactions)
        if "message_reaction" in args:
            LOGGER.info("Received message_reaction update")
            return await _handle_message_reaction(args)

        # Check if this is a callback_query update (inline button click)
        if "callback_query" in args:
            LOGGER.info("Received callback_query update")
            return await _handle_callback_query(args)

        # Ignore non-message Telegram update types (my_chat_member, chat_member, etc.)
        # These are membership/status updates that don't contain user messages
        _ignored_update_types = {
            "my_chat_member",
            "chat_member",
            "chat_join_request",
            "poll",
            "poll_answer",
        }
        if any(key in args for key in _ignored_update_types):
            ignored_type = next(key for key in _ignored_update_types if key in args)
            LOGGER.debug(f"Ignoring Telegram update type: {ignored_type}")
            return {"success": True, "statusCode": 200}

        # Resolve the Telegram message object (works for both message and edited_message)
        _tg_msg = _get_tg_message(args) or None

        # FILTER: Ignore messages from bots (including this bot and other bots)
        if _tg_msg:
            from_user = _tg_msg.get("from", {})
            if from_user.get("is_bot", False):
                LOGGER.info(
                    f"Ignoring message from bot: {from_user.get('username', 'unknown')} "
                    f"(id: {from_user.get('id', 'unknown')})"
                )
                return {
                    "success": True,
                    "message": "Ignored (message from bot)",
                    "statusCode": 200,
                }

        # FILTER: In group chats, only respond if bot is mentioned or replied to
        if _tg_msg:
            chat = _tg_msg.get("chat", {})
            chat_type = chat.get("type", "")

            # Only filter group/supergroup chats (not private chats)
            if chat_type in ("group", "supergroup"):
                current_chat_id_str = str(chat.get("id", ""))

                # NO-REPLY GROUPS: Save messages but never respond, even when tagged.
                # Comma-separated chat IDs in env var.
                no_reply_ids = os.getenv("NO_REPLY_CHAT_IDS", "")
                if no_reply_ids and current_chat_id_str in [
                    cid.strip() for cid in no_reply_ids.split(",")
                ]:
                    LOGGER.debug(f"No-reply group {current_chat_id_str}: saving passively")
                    try:
                        await _save_passive_group_message(_tg_msg, chat)
                    except Exception as e:
                        LOGGER.warning(f"Failed to save passive group message: {e}")
                    # Check if persistent agents should be notified
                    try:
                        await _maybe_queue_agent_event(_tg_msg, chat)
                    except Exception as e:
                        LOGGER.warning(f"Agent event check failed (non-fatal): {e}")
                    return {
                        "success": True,
                        "message": "Ignored (no-reply group)",
                        "statusCode": 200,
                    }

                message_text = _tg_msg.get("text", "") or _tg_msg.get("caption", "")
                reply_to = _tg_msg.get("reply_to_message", {})

                # Get bot username from environment (fallback to common name)
                bot_username = os.getenv("TELEGRAM_BOT_USERNAME", "YourSupportBot")

                # Forwarded messages: an @mention in forwarded text is not a fresh
                # request — it's past context being shared. Ignore bot mentions
                # in forwarded messages so the bot doesn't respond unprompted.
                is_forwarded = bool(
                    _tg_msg.get("forward_origin")
                    or _tg_msg.get("forward_from")
                    or _tg_msg.get("forward_from_chat")
                    or _tg_msg.get("forward_date")
                )

                # Check if bot is mentioned in message (skip for forwarded messages)
                bot_mentioned = not is_forwarded and f"@{bot_username}" in message_text

                # Check if this is a reply to THIS bot's message (not any bot)
                reply_from = reply_to.get("from", {})
                reply_to_bot = (
                    reply_from.get("is_bot", False)
                    and reply_from.get("username", "") == bot_username
                )

                if not bot_mentioned and not reply_to_bot:
                    LOGGER.debug(
                        f"Ignoring group message without bot mention or reply: "
                        f"chat={chat.get('id')}, user={_tg_msg.get('from', {}).get('id')}"
                    )
                    # PASSIVE LISTENING: Save message to chat history even when not responding
                    # This allows the bot to have context when someone does @mention it later
                    try:
                        await _save_passive_group_message(_tg_msg, chat)
                    except Exception as e:
                        LOGGER.warning(f"Failed to save passive group message: {e}")
                    # Check if persistent agents should be notified
                    try:
                        await _maybe_queue_agent_event(_tg_msg, chat)
                    except Exception as e:
                        LOGGER.warning(f"Agent event check failed (non-fatal): {e}")
                    return {
                        "success": True,
                        "message": "Ignored (group message without bot mention or reply)",
                        "statusCode": 200,
                    }

                LOGGER.info(
                    f"Processing group message: mentioned={bot_mentioned}, reply_to_bot={reply_to_bot}"
                )

        # SPECIAL CASE: Check if this is from the escalation group (before normalization)
        # Must check original Telegram structure before it gets normalized.
        # Check both "message" and "edited_message" — Telegram sends edited_message
        # when a user edits their message, and it must be caught here too.
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
        _esc_msg = _get_tg_message(args)
        if escalation_chat_id and _esc_msg:
            telegram_msg = _esc_msg
            chat = telegram_msg.get("chat", {})
            current_chat_id = str(chat.get("id", ""))

            if current_chat_id == escalation_chat_id:
                # This message is from the escalation group
                if "reply_to_message" in telegram_msg:
                    # It's a reply to an escalated message - forward to original chat
                    LOGGER.info("Detected reply in escalation group, handling support response")
                    return await _handle_escalation_reply(telegram_msg)
                else:
                    # Not a reply - ignore this message (don't process as normal chat)
                    LOGGER.info(
                        f"Ignoring non-reply message in escalation group from user "
                        f"{telegram_msg.get('from', {}).get('id', 'unknown')}"
                    )
                    return {
                        "success": True,
                        "message": "Ignored (escalation group message without reply)",
                        "statusCode": 200,
                    }

        # ── STAFF GROUP + UNKNOWN GROUP PRE-CHECK ──────────────────────
        # For group chats where the bot is @mentioned (not escalation, not NO_REPLY):
        # 1. Staff group → process in staff mode
        # 2. Known org group → fall through to normal processing
        # 3. Unknown group → silent no-reply (passive save + agent notify)
        if _tg_msg and _tg_msg.get("chat", {}).get("type") in ("group", "supergroup"):
            _pre_chat = _tg_msg["chat"]
            _pre_chat_id = str(_pre_chat.get("id", ""))
            _is_escalation = escalation_chat_id and _pre_chat_id == escalation_chat_id

            if not _is_escalation:
                from orchestrator.services.instructions_provider import get_staff_group

                staff_group = get_staff_group(_pre_chat_id)

                if staff_group:
                    # Staff group: still require bot mention or reply-to-bot
                    _sg_text = _tg_msg.get("text", "") or _tg_msg.get("caption", "")
                    _sg_bot_user = os.getenv("TELEGRAM_BOT_USERNAME", "YourSupportBot")
                    _sg_reply = _tg_msg.get("reply_to_message", {})
                    _sg_mentioned = f"@{_sg_bot_user}" in _sg_text
                    _sg_reply_from = _sg_reply.get("from", {})
                    _sg_reply_to_bot = (
                        _sg_reply_from.get("is_bot", False)
                        and _sg_reply_from.get("username", "") == _sg_bot_user
                    )

                    if not _sg_mentioned and not _sg_reply_to_bot:
                        LOGGER.debug(
                            f"Staff group {staff_group['name']}: ignoring (no bot mention or reply)"
                        )
                        try:
                            await _save_passive_group_message(_tg_msg, _pre_chat)
                        except Exception as e:
                            LOGGER.warning(f"Failed to save passive message: {e}")
                        return {
                            "success": True,
                            "message": "Ignored (staff group, no mention)",
                            "statusCode": 200,
                        }

                    # Staff group: verify user is staff
                    user_tg_id = str(_tg_msg.get("from", {}).get("id", ""))
                    auth_svc = get_auth_service()
                    user_org_id = await auth_svc.get_org_id_for_telegram_user(user_tg_id)

                    if not user_org_id or user_org_id != _STAFF_ORG_ID:
                        LOGGER.info(
                            f"Non-staff user {user_tg_id} in staff group "
                            f"{staff_group['name']}, ignoring"
                        )
                        return {
                            "success": True,
                            "message": "Ignored (non-staff in staff group)",
                            "statusCode": 200,
                        }

                    # Check if this is a reply to a persistent agent's message.
                    # If so, route to the agent instead of the normal conversation graph.
                    if _sg_reply_to_bot and _sg_reply.get("message_id"):
                        _agent_instance_id = await _lookup_agent_for_message(
                            _pre_chat_id, _sg_reply["message_id"]
                        )
                        if _agent_instance_id:
                            LOGGER.info(
                                f"Staff group {staff_group['name']}: routing reply "
                                f"to persistent agent {_agent_instance_id}"
                            )
                            try:
                                await _queue_reply_to_agent(_tg_msg, _pre_chat, _agent_instance_id)
                            except Exception as e:
                                LOGGER.warning(f"Agent reply queue failed: {e}")
                            return {
                                "success": True,
                                "message": "Routed to persistent agent",
                                "statusCode": 200,
                            }

                    # Staff user in staff group → inject staff auth bypass
                    args.setdefault("metadata", {})
                    args["metadata"]["staff_group_auth"] = True
                    args["metadata"]["staff_group_organization_id"] = _STAFF_ORG_ID
                    LOGGER.info(
                        f"Staff group {staff_group['name']}: "
                        f"processing as staff for user {user_tg_id}"
                    )
                    # Fall through to normal processing

                else:
                    # Not a staff group — check if it's a known org group
                    auth_svc = get_auth_service()
                    raw_topic = _tg_msg.get("message_thread_id")
                    topic_id = str(raw_topic) if raw_topic is not None else None
                    org_id = await auth_svc.get_organization_from_chat(
                        _pre_chat_id, topic_id=topic_id
                    )
                    if not org_id:
                        # Unknown group → silent no-reply
                        LOGGER.info(f"Unknown group {_pre_chat_id}: defaulting to no-reply")
                        try:
                            await _save_passive_group_message(_tg_msg, _pre_chat)
                        except Exception as e:
                            LOGGER.warning(f"Failed to save passive message: {e}")
                        try:
                            await _maybe_queue_agent_event(_tg_msg, _pre_chat)
                        except Exception as e:
                            LOGGER.warning(f"Agent event queue failed: {e}")
                        return {
                            "success": True,
                            "message": "Ignored (unknown group)",
                            "statusCode": 200,
                        }

        # Sanitize: strip synthetic underscore-prefixed fields from raw webhooks.
        # These fields (_photo_file_ids, _merged_text) are only valid when injected
        # internally by _flush_media_group. An external caller must not inject them.
        # Skip sanitization for internal re-entry (marked by _internal_reentry flag).
        raw_msg = _get_tg_message(args)
        if raw_msg and not args.get("_internal_reentry"):
            for key in [k for k in raw_msg if k.startswith("_")]:
                del raw_msg[key]

        # MEDIA GROUP AGGREGATION: Buffer album photos and process as one batch.
        # Must come AFTER guard clauses (bot filter, group filter, escalation) but
        # BEFORE normalization so we never buffer non-message payloads.
        # NOTE: _flush_media_group re-enters async_main() with a merged body containing
        # _photo_file_ids. The re-entry guard in _buffer_media_group_message detects
        # this and returns False, letting the merged body proceed to normalization.
        if await _buffer_media_group_message(args):
            return {"success": True, "statusCode": 200}

        # Normalize Telegram webhook format if needed
        normalized_args = _normalize_telegram_webhook(args)

        # Validate webhook format
        if (
            "message" not in normalized_args
            or "user_id" not in normalized_args
            or "source" not in normalized_args
        ):
            return {
                "success": False,
                "error": "Invalid request format. Required fields: message, user_id, source",
                "statusCode": 400,
            }

        # EMOJI-ONLY MESSAGE HANDLING: Treat standalone emoji as feedback, not queries
        # This prevents the LLM from hallucinating responses to emoji-only messages
        message_text = normalized_args.get("message", "")
        is_emoji_only, feedback_type, emoji = _is_emoji_only_message(message_text)
        if is_emoji_only:
            LOGGER.info(
                f"Detected emoji-only message: '{emoji}', treating as feedback "
                f"(type={feedback_type})"
            )
            return await _handle_emoji_only_message(args, normalized_args, feedback_type, emoji)

        return await _handle_webhook_async(normalized_args)

    except Exception as e:
        LOGGER.exception(f"Error processing request: {e}")

        # Send Telegram error notification with auto-captured context
        if tele_debug_sync:
            error_msg = f"❌ {type(e).__name__}: {str(e)}\n"
            error_msg += f"Source: {args.get('source', 'unknown')}\n"
            error_msg += f"User: {args.get('user_id', 'unknown')}\n"
            error_msg += f"Chat: {args.get('chat_id', 'unknown')}"
            tele_debug_sync(error_msg, include_traceback=True)

        # Get settings to check DEBUG mode
        try:
            settings = _get_settings()
            debug_mode = settings.debug
        except Exception:
            # Fallback if settings can't be loaded
            debug_mode = os.getenv("DEBUG", "false").lower() == "true"

        # Mask internal errors in production
        if debug_mode:
            error_detail = str(e)
        else:
            error_detail = "Internal server error"

        return {
            "success": False,
            "error": error_detail,
            "statusCode": 500,
        }


def _handle_webhook(args: Dict[str, Any]) -> Dict[str, Any]:
    """Handle webhook requests from Telegram/Roam."""
    try:
        # SPECIAL CASE: Check if this is a reply in the escalation group
        # This needs to be checked before normal webhook processing
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")

        _esc_msg_sync = _get_tg_message(args)
        if escalation_chat_id and _esc_msg_sync:
            telegram_msg = _esc_msg_sync
            chat = telegram_msg.get("chat", {})
            current_chat_id = str(chat.get("id", ""))

            # Check if message is from escalation group AND is a reply
            if current_chat_id == escalation_chat_id and "reply_to_message" in telegram_msg:
                LOGGER.info("Detected reply in escalation group, handling support response")
                return asyncio.run(_handle_escalation_reply(telegram_msg))

        # Parse webhook request
        webhook_req = WebhookRequest(**args)

        # Get settings for DEBUG mode check
        settings = _get_settings()

        # SECURITY: Validate and override source based on DEBUG mode
        validated_source = validate_and_override_source(
            request_source=webhook_req.source,
            debug_mode=settings.debug,
        )

        # Override the source in the webhook request
        webhook_req.source = validated_source

        LOGGER.info(
            f"Processing webhook: source={validated_source}, user_id={webhook_req.user_id}, "
            f"chat_id={webhook_req.chat_id}, topic_id={webhook_req.topic_id}"
        )

        # Use singleton auth service for connection pool reuse
        auth_service = get_auth_service()

        # Keep original chat_id with -100 prefix for database lookup (if present)
        original_chat_id = webhook_req.chat_id  # Keep original for webhook response and DB lookup

        # For Telegram, use chat-based auth ONLY for groups/supergroups
        # Use user-based auth for private DMs
        user_email = None
        chat_type = webhook_req.metadata.get("chat_type") if webhook_req.metadata else None
        is_telegram_group = chat_type in ("group", "supergroup", "channel")

        if validated_source == "telegram" and webhook_req.chat_id and is_telegram_group:
            # Chat-based authentication for Telegram groups/channels
            LOGGER.info(
                f"Using chat-based auth for {chat_type} chat_id={webhook_req.chat_id}, "
                f"topic_id={webhook_req.topic_id}"
            )
            # Set placeholder email for chat-based auth
            user_email = f"chat_{webhook_req.chat_id}"
        else:
            # User-based authentication (for DMs or non-Telegram sources)
            user_email = asyncio.run(
                auth_service.get_user_email(user_id=webhook_req.user_id, source=validated_source)
            )

            if not user_email:
                # If user_email was provided in webhook, use it as fallback
                if webhook_req.user_email:
                    user_email = webhook_req.user_email
                else:
                    LOGGER.error(
                        f"Could not resolve email for {webhook_req.source} "
                        f"user {webhook_req.user_id}"
                    )
                    return {
                        "success": False,
                        "error": f"User not found in auth database (source: {webhook_req.source})",
                        "message": f"Sorry, you are not registered in the system. Please contact support with your {webhook_req.source.capitalize()} ID: {webhook_req.user_id}",
                        "statusCode": 403,
                    }

            LOGGER.info(
                f"Resolved {webhook_req.source} user {webhook_req.user_id} to email {user_email}"
            )

        # Use the chat_id as-is (including -100 prefix for supergroups)
        # Database stores the full Telegram chat ID with prefix
        normalized_chat_id = webhook_req.chat_id
        LOGGER.info(f"Using Telegram chat_id: {normalized_chat_id}")

        # Build user context with resolved email
        user_context = UserContext(
            user_id=webhook_req.user_id,
            user_email=user_email,
            username=webhook_req.username,
            source=webhook_req.source,
            chat_id=normalized_chat_id,
            topic_id=webhook_req.topic_id,
            is_group=bool(
                normalized_chat_id
            ),  # True if chat_id exists (group/channel), False for DM
            chat_title=webhook_req.metadata.get("chat_title"),
        )

        # Generate session_id with hierarchy: topic > chat > DM
        # Session IDs are hashed for unpredictability (prevents enumeration attacks)
        # Original chat_id/topic_id stored separately for admin lookups
        session_id = generate_session_id(
            source=webhook_req.source,
            chat_id=normalized_chat_id,
            topic_id=webhook_req.topic_id,
            user_id=webhook_req.user_id,
        )

        LOGGER.info(
            f"Webhook request from {webhook_req.source}: "
            f"user={webhook_req.user_id}, session={session_id}"
        )

        # If outgoing webhook URL is provided, process async and return immediate ACK
        if webhook_req.outgoing_webhook_url:
            # Pass original_chat_id and topic_id in metadata for database lookup and tool calls
            enriched_metadata = {
                **webhook_req.metadata,
                "original_chat_id": original_chat_id,  # For DB lookup
                "topic_id": webhook_req.topic_id,  # For schedule tools
            }
            webhook_req.metadata = enriched_metadata

            # Fire off async processing in background
            asyncio.run(
                _process_and_respond_async(
                    webhook_req=webhook_req,
                    user_context=user_context,
                    session_id=session_id,
                    original_chat_id=original_chat_id,
                )
            )

            # Return immediate acknowledgment
            return {
                "success": True,
                "message": "Processing your request...",
                "session_id": session_id,
                "statusCode": 200,
            }

        # Otherwise, process synchronously and return response
        # Pass original_chat_id for database lookup (with -100 prefix if present)
        enriched_metadata = {
            **webhook_req.metadata,
            "original_chat_id": original_chat_id,  # For DB lookup
            "topic_id": webhook_req.topic_id,  # For schedule tools
        }
        response_text, tool_results, reply_markup = asyncio.run(
            _process_webhook_with_graph(
                user_input=webhook_req.message,
                user_context=user_context,
                entity_context=webhook_req.entity_context,
                media=webhook_req.media,
                session_id=session_id,
                metadata=enriched_metadata,
            )
        )

        return {
            "success": True,
            "message": response_text,
            "session_id": session_id,
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling webhook: {e}")

        # Send Telegram error notification (for non-PermissionError)
        if not isinstance(e, PermissionError) and tele_debug_sync:
            error_msg = f"❌ {type(e).__name__}: {str(e)}\n"
            error_msg += f"Source: {args.get('source', 'unknown')}\n"
            error_msg += f"User: {args.get('user_id', 'unknown')}\n"
            error_msg += f"Chat: {args.get('chat_id', 'unknown')}"
            tele_debug_sync(error_msg, include_traceback=True)

        # Get settings to check DEBUG mode
        # Check both settings object and environment variable directly
        try:
            settings = _get_settings()
            debug_mode = settings.debug
        except Exception:
            debug_mode = False

        # Also check environment variable directly as fallback
        if not debug_mode:
            debug_mode = os.getenv("DEBUG", "false").lower() == "true"

        # Categorize error and get appropriate user message
        error_category, user_message = categorize_error(e)
        error_detail = str(e)

        if error_category == ErrorCategory.PERMISSION:
            status_code = 403
            LOGGER.error(f"Authorization error: {error_detail}")
        elif error_category == ErrorCategory.TRANSIENT:
            status_code = 200  # Return 200 to prevent retries
            LOGGER.warning(f"Transient error: {error_detail}")
        elif error_category == ErrorCategory.REPHRASE:
            status_code = 200  # Not a server error
            LOGGER.info(f"Input understanding issue: {error_detail}")
        else:
            # SYSTEM errors or unknown - mask in production
            if debug_mode:
                LOGGER.error(f"Returning error to client (DEBUG=true): {error_detail}")
            else:
                error_detail = "Internal server error"
                LOGGER.error(f"Masking error from client (DEBUG=false): {str(e)}")
            status_code = 500

        return {
            "success": False,
            "error": error_detail,
            "message": user_message,
            "statusCode": status_code,
        }


async def _handle_webhook_async(args: Dict[str, Any]) -> Dict[str, Any]:
    """Async version of _handle_webhook for use with FastAPI/async contexts."""
    try:
        # Extract auth method (set by app.py based on which header was used)
        # "api" = return response in body, "telegram" = send via Bot API
        auth_method = args.pop("_auth_method", "api")

        # Parse webhook request
        webhook_req = WebhookRequest(**args)

        # Deduplicate Telegram webhooks (prevent retry from reprocessing)
        if webhook_req.source == "telegram" and webhook_req.metadata:
            message_id = webhook_req.metadata.get("telegram_message_id")
            if _is_duplicate_webhook(webhook_req.chat_id, message_id):
                return {
                    "success": True,
                    "message": "Duplicate webhook ignored",
                    "statusCode": 200,
                }

        # Get settings for DEBUG mode check
        settings = _get_settings()

        # SECURITY: Validate and override source based on DEBUG mode
        validated_source = validate_and_override_source(
            request_source=webhook_req.source,
            debug_mode=settings.debug,
        )

        # Override the source in the webhook request
        webhook_req.source = validated_source

        LOGGER.info(
            f"Processing webhook: source={validated_source}, user_id={webhook_req.user_id}, "
            f"chat_id={webhook_req.chat_id}, topic_id={webhook_req.topic_id}"
        )

        # Use singleton auth service for connection pool reuse
        auth_service = get_auth_service()

        # Keep original chat_id with -100 prefix for database lookup (if present)
        original_chat_id = webhook_req.chat_id

        # For Telegram, use chat-based auth ONLY for groups/supergroups
        # Use user-based auth for private DMs
        user_email = None
        chat_type = webhook_req.metadata.get("chat_type") if webhook_req.metadata else None
        is_telegram_group = chat_type in ("group", "supergroup", "channel")

        if validated_source == "telegram" and webhook_req.chat_id and is_telegram_group:
            # Chat-based authentication for Telegram groups/channels
            LOGGER.info(
                f"Using chat-based auth for {chat_type} chat_id={webhook_req.chat_id}, "
                f"topic_id={webhook_req.topic_id}"
            )
            user_email = f"chat_{webhook_req.chat_id}"
        else:
            # User-based authentication (for DMs or non-Telegram sources)
            user_email = await auth_service.get_user_email(
                user_id=webhook_req.user_id, source=validated_source
            )

            if not user_email:
                if webhook_req.user_email:
                    user_email = webhook_req.user_email
                else:
                    LOGGER.error(
                        f"Could not resolve email for {webhook_req.source} "
                        f"user {webhook_req.user_id}"
                    )
                    return {
                        "success": False,
                        "error": f"User not found in auth database (source: {webhook_req.source})",
                        "message": f"Sorry, you are not registered in the system. Please contact support with your {webhook_req.source.capitalize()} ID: {webhook_req.user_id}",
                        "statusCode": 403,
                    }

            LOGGER.info(
                f"Resolved {webhook_req.source} user {webhook_req.user_id} to email {user_email}"
            )

        normalized_chat_id = webhook_req.chat_id
        LOGGER.info(f"Using Telegram chat_id: {normalized_chat_id}")

        # Build user context with resolved email
        user_context = UserContext(
            user_id=webhook_req.user_id,
            user_email=user_email,
            username=webhook_req.username,
            source=webhook_req.source,
            chat_id=normalized_chat_id,
            topic_id=webhook_req.topic_id,
            is_group=bool(normalized_chat_id),
            chat_title=webhook_req.metadata.get("chat_title"),
        )

        # Generate session_id with hierarchy: topic > chat > DM
        # IMPORTANT: Use generate_session_id() for consistent hashing across all code paths
        session_id = generate_session_id(
            source=webhook_req.source,
            chat_id=normalized_chat_id,
            topic_id=webhook_req.topic_id,
            user_id=webhook_req.user_id,
        )

        LOGGER.info(
            f"Webhook request from {webhook_req.source}: "
            f"user={webhook_req.user_id}, session={session_id}"
        )

        # Send typing indicator for Telegram
        if validated_source == "telegram" and original_chat_id:
            await _send_telegram_typing_indicator(original_chat_id, webhook_req.topic_id)

        # Enrich metadata with chat context
        enriched_metadata = {
            **webhook_req.metadata,
            "original_chat_id": original_chat_id,
            "topic_id": webhook_req.topic_id,  # For schedule tools
        }
        webhook_req.metadata = enriched_metadata

        # =================================================================
        # TELEGRAM: Process in background, return 200 immediately
        # Telegram retries webhooks after ~60 seconds if no response.
        # Long-running workflows (LPP, reports) can take minutes.
        # =================================================================
        if auth_method == "telegram":
            LOGGER.info(
                f"Telegram webhook: processing async for session={session_id}, "
                f"chat_id={original_chat_id}"
            )

            # Fire off async processing in background (create task, don't await)
            asyncio.create_task(
                _process_telegram_async(
                    webhook_req=webhook_req,
                    user_context=user_context,
                    session_id=session_id,
                    original_chat_id=original_chat_id,
                )
            )

            # Return 200 immediately to prevent Telegram retries
            return {
                "success": True,
                "statusCode": 200,
            }

        # =================================================================
        # OUTGOING WEBHOOK: Process async with custom webhook URL
        # =================================================================
        if webhook_req.outgoing_webhook_url:
            asyncio.create_task(
                _process_and_respond_async(
                    webhook_req=webhook_req,
                    user_context=user_context,
                    session_id=session_id,
                    original_chat_id=original_chat_id,
                )
            )

            return {
                "success": True,
                "message": "Processing your request...",
                "session_id": session_id,
                "statusCode": 200,
            }

        # =================================================================
        # DIRECT API CALLS: Process synchronously, return response in body
        # =================================================================
        response_text, tool_results, reply_markup = await _process_webhook_with_graph(
            user_input=webhook_req.message,
            user_context=user_context,
            entity_context=webhook_req.entity_context,
            media=webhook_req.media,
            session_id=session_id,
            metadata=enriched_metadata,
        )

        LOGGER.info(
            f"Direct API response: session={session_id}, "
            f"response_len={len(response_text) if response_text else 0}"
        )

        return {
            "success": True,
            "message": response_text,
            "session_id": session_id,
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling webhook: {e}")

        if not isinstance(e, PermissionError) and tele_debug_sync:
            error_msg = f"❌ {type(e).__name__}: {str(e)}\n"
            error_msg += f"Source: {args.get('source', 'unknown')}\n"
            error_msg += f"User: {args.get('user_id', 'unknown')}\n"
            error_msg += f"Chat: {args.get('chat_id', 'unknown')}"
            tele_debug_sync(error_msg, include_traceback=True)

        try:
            settings = _get_settings()
            debug_mode = settings.debug
        except Exception:
            debug_mode = False

        if not debug_mode:
            debug_mode = os.getenv("DEBUG", "false").lower() == "true"

        # Categorize error and get appropriate user message
        error_category, user_message = categorize_error(e)
        error_detail = str(e)

        if error_category == ErrorCategory.PERMISSION:
            status_code = 403
            LOGGER.error(f"Authorization error: {error_detail}")
        elif error_category == ErrorCategory.TRANSIENT:
            # Return 200 to Telegram to prevent unnecessary retries
            status_code = 200
            LOGGER.warning(f"Transient error: {error_detail}")
        elif error_category == ErrorCategory.REPHRASE:
            # User should rephrase - not a server error
            status_code = 200
            LOGGER.info(f"Input understanding issue: {error_detail}")
        else:
            # SYSTEM errors or unknown
            if debug_mode:
                LOGGER.error(f"Returning error to client (DEBUG=true): {error_detail}")
            else:
                error_detail = "Internal server error"
                LOGGER.error(f"Masking error from client (DEBUG=false): {str(e)}")
            status_code = 500

        # Send error message to Telegram if this was a Telegram webhook
        if args.get("_auth_method") == "telegram":
            original_chat_id = (
                args.get("chat", {}).get("id")
                if isinstance(args.get("chat"), dict)
                else args.get("chat_id")
            )
            # Extract message_id from Telegram webhook
            message_obj = args.get("message") or args.get("edited_message")
            reply_to_message_id = (
                message_obj.get("message_id") if isinstance(message_obj, dict) else None
            )
            topic_id = _get_tg_message(args).get("message_thread_id") or None
            bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

            if bot_token and original_chat_id:
                try:
                    webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                    # asyncio imported at module level (line 12)
                    asyncio.create_task(
                        _send_telegram_response(
                            webhook_url=webhook_url,
                            chat_id=str(original_chat_id),
                            topic_id=topic_id,
                            text=user_message,
                            reply_to_message_id=reply_to_message_id,
                        )
                    )
                except Exception as send_error:
                    LOGGER.exception(f"Failed to send error message to Telegram: {send_error}")

        return {
            "success": False,
            "error": error_detail,
            "message": user_message,
            "statusCode": status_code,
        }


async def _process_and_respond_async(
    webhook_req: WebhookRequest,
    user_context: UserContext,
    session_id: str,
    original_chat_id: str,
) -> None:
    """Process webhook request and send response to outgoing webhook URL."""
    try:
        # Process with LangGraph full conversation graph
        response_text, tool_results, reply_markup = await _process_webhook_with_graph(
            user_input=webhook_req.message,
            user_context=user_context,
            entity_context=webhook_req.entity_context,
            media=webhook_req.media,
            session_id=session_id,
            metadata=webhook_req.metadata,
        )

        reply_to_message_id = webhook_req.metadata.get("telegram_message_id")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

        # Send any images from tool results BEFORE the text response
        # This ensures charts/images appear before the text explanation
        if tool_results and bot_token:
            await _send_tool_images_to_telegram(
                tool_results=tool_results,
                bot_token=bot_token,
                chat_id=original_chat_id,
                topic_id=webhook_req.topic_id,
                reply_to_message_id=reply_to_message_id,
            )

        # Check for procedure buttons in the response (LLM-generated [BUTTONS] blocks)
        if is_procedure_buttons_enabled() and response_text:
            clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
            if proc_keyboard:
                response_text = clean_text
                # Procedure buttons take precedence over decision buttons
                reply_markup = proc_keyboard
                LOGGER.info("Extracted procedure buttons from LLM response")

        # Send text response to outgoing webhook (with inline buttons if present)
        sent_message_id = await _send_telegram_response(
            webhook_url=webhook_req.outgoing_webhook_url,
            chat_id=original_chat_id,
            topic_id=webhook_req.topic_id,
            text=response_text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

        # Store message_id in packet state so buttons can be removed on workflow resume
        await _store_buttons_message_id(sent_message_id, reply_markup, original_chat_id, session_id)

        # Back-fill telegram_message_id on latest bot message for admin UI deletion
        if sent_message_id:
            await _backfill_bot_message_id(session_id, sent_message_id)

    except Exception as e:
        LOGGER.exception(f"Error in async processing: {e}")
        # Attempt to send error message to user
        if webhook_req.outgoing_webhook_url:
            try:
                reply_to_message_id = webhook_req.metadata.get("telegram_message_id")
                await _send_telegram_response(
                    webhook_url=webhook_req.outgoing_webhook_url,
                    chat_id=original_chat_id,
                    topic_id=webhook_req.topic_id,
                    text="Sorry, I encountered an error processing your message.",
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as send_error:
                LOGGER.exception(f"Failed to send error message: {send_error}")


def _is_system_error_response(response_text: str) -> bool:
    """Return True if response_text is a SYSTEM-category error message from error_messages.py."""
    from shared.utils.error_messages import ERROR_MESSAGES, ErrorCategory

    system_errors = set(ERROR_MESSAGES.get(ErrorCategory.SYSTEM, {}).values())
    return bool(response_text) and response_text.strip() in system_errors


def _format_tool_results_for_escalation(
    tool_results: Optional[List["ToolCallResult"]],
    max_items: int = 20,
    output_chars_per_item: int = 400,
) -> str:
    """Render accumulated tool results as a compact summary for staff escalation context.

    Includes each tool's name, success flag, and a truncated peek at its output so staff
    can see what the bot already investigated before giving up.
    """
    if not tool_results:
        return ""

    lines: List[str] = []
    for tr in tool_results[:max_items]:
        status = "ok" if getattr(tr, "success", True) else "FAIL"
        name = getattr(tr, "name", "<unknown>")
        peek_source = (
            getattr(tr, "error", None) if status == "FAIL" else getattr(tr, "output", None)
        )
        peek = ""
        if peek_source is not None:
            try:
                peek_str = (
                    peek_source
                    if isinstance(peek_source, str)
                    else json.dumps(peek_source, default=str)
                )
            except Exception:
                peek_str = str(peek_source)
            peek_str = " ".join(peek_str.split())  # collapse whitespace
            if len(peek_str) > output_chars_per_item:
                peek_str = peek_str[:output_chars_per_item] + "…"
            peek = f" — {peek_str}"
        lines.append(f"• {name} [{status}]{peek}")

    remainder = len(tool_results) - max_items
    if remainder > 0:
        lines.append(f"• … and {remainder} more tool call(s)")

    return "\n\nInvestigation steps the bot completed before failing:\n" + "\n".join(lines)


async def _auto_escalate_on_error_response(
    session_id: str,
    user_context,
    webhook_req,
    tool_results: Optional[List["ToolCallResult"]] = None,
) -> bool:
    """Escalate to support when the bot is about to send a system error to a customer.

    Returns True if an escalation task was fired, False otherwise (already escalated,
    rate-limited, service disabled, or exception).
    """
    try:
        from orchestrator.services.escalation_service import EscalationService

        escalation_service = EscalationService()
        if not escalation_service.is_enabled():
            return False

        already_escalated = await escalation_service.is_session_escalated(session_id)
        if already_escalated:
            return False

        # Rate limit: one auto-escalation per session per 10 minutes
        existing = await escalation_service.get_escalation_info(session_id)
        if existing:
            created_at_str = existing.get("created_at", "")
            if created_at_str:
                try:
                    created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
                    age_seconds = (datetime.now(timezone.utc) - created_at).total_seconds()
                    if age_seconds < 600:
                        LOGGER.info(
                            f"Rate-limiting error-response auto-escalation for session={session_id} "
                            f"({age_seconds:.0f}s since last escalation)"
                        )
                        return False
                except (ValueError, AttributeError):
                    pass

        organization_id = None
        if user_context.organization_ids:
            try:
                organization_id = int(user_context.organization_ids[0])
            except (ValueError, TypeError):
                pass

        user_msg = (webhook_req.message or "")[:500]
        tool_summary = _format_tool_results_for_escalation(tool_results)
        asyncio.create_task(
            escalation_service.escalate_to_support(
                question_summary=f"[BOT ERROR] {user_msg[:200] or 'Bot returned system error'}",
                session_id=session_id,
                organization_id=organization_id,
                organization_short_name=user_context.organization_name,
                customer_chat_id=user_context.chat_id,
                customer_topic_id=webhook_req.topic_id,
                customer_username=user_context.username,
                customer_email=user_context.user_email,
                conversation_context=(
                    "[AUTO-ESCALATION: Bot returned system error to customer]\n\n"
                    f"Customer message: {user_msg or 'N/A'}\n\n"
                    "The bot was unable to process the customer's request." + tool_summary
                ),
                reason="system_error",
            ),
            name=f"auto-escalate-error-{session_id}",
        )
        LOGGER.info(f"Auto-escalated system error response for session={session_id}")
        return True

    except Exception as esc_error:
        LOGGER.exception(f"Failed to auto-escalate system error response: {esc_error}")
        return False


async def _process_telegram_async(
    webhook_req: WebhookRequest,
    user_context: UserContext,
    session_id: str,
    original_chat_id: str,
) -> None:
    """Process Telegram webhook in background and send response via Bot API.

    This function runs as a background task after immediately returning 200 to Telegram.
    Telegram retries webhooks after ~60 seconds if no response, so we must respond quickly.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    reply_to_message_id = (
        webhook_req.metadata.get("telegram_message_id") if webhook_req.metadata else None
    )

    try:
        # Keep typing indicator alive during processing (refreshes every 8s).
        # The context manager always cancels the background task on exit,
        # even if processing raises — no risk of stuck indicators.
        async with _TypingIndicator(original_chat_id, webhook_req.topic_id):
            # Process with LangGraph full conversation graph
            response_text, tool_results, reply_markup = await _process_webhook_with_graph(
                user_input=webhook_req.message,
                user_context=user_context,
                entity_context=webhook_req.entity_context,
                media=webhook_req.media,
                session_id=session_id,
                metadata=webhook_req.metadata,
            )

        LOGGER.info(
            f"Telegram async processing complete: session={session_id}, "
            f"response_len={len(response_text) if response_text else 0}, "
            f"tool_results={len(tool_results) if tool_results else 0}, "
            f"has_reply_markup={reply_markup is not None}"
        )

        if not bot_token:
            LOGGER.error("TELEGRAM_BOT_TOKEN not set, cannot send response")
            return

        # Send any tool-generated images first
        if tool_results:
            for i, tr in enumerate(tool_results):
                has_raw = tr.raw_response is not None
                raw_keys = list(tr.raw_response.keys()) if has_raw else []
                LOGGER.info(
                    f"Tool result {i}: name={tr.name}, has_raw_response={has_raw}, "
                    f"raw_keys={raw_keys}"
                )
            await _send_tool_images_to_telegram(
                tool_results=tool_results,
                bot_token=bot_token,
                chat_id=original_chat_id,
                topic_id=webhook_req.topic_id,
                reply_to_message_id=reply_to_message_id,
            )

        # Check for procedure buttons in the response (LLM-generated [BUTTONS] blocks)
        if is_procedure_buttons_enabled() and response_text:
            clean_text, proc_keyboard, _ = parse_procedure_buttons(response_text)
            if proc_keyboard:
                response_text = clean_text
                # Procedure buttons take precedence over decision buttons
                reply_markup = proc_keyboard
                LOGGER.info("Extracted procedure buttons from LLM response")

        # Attach View State button for user agent creation responses
        if not reply_markup and tool_results:
            for tr in tool_results:
                if (
                    tr.name == "schedule_create_user_agent"
                    and tr.raw_response
                    and tr.raw_response.get("success")
                ):
                    tr_result = tr.raw_response.get("result", [])
                    result_text = ""
                    if isinstance(tr_result, list) and tr_result:
                        result_text = (
                            tr_result[0].text
                            if hasattr(tr_result[0], "text")
                            else str(tr_result[0])
                        )
                    try:
                        import re

                        from orchestrator.mini_app.schemas import build_agent_state_url

                        uuid_match = re.search(
                            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
                            result_text,
                        )
                        if uuid_match:
                            view_url = build_agent_state_url(uuid_match.group())
                            if view_url:
                                reply_markup = {
                                    "inline_keyboard": [
                                        [{"text": "View Agent State", "web_app": {"url": view_url}}]
                                    ]
                                }
                                LOGGER.info("Attached View State button to agent creation response")
                    except Exception:
                        pass
                    break

        # Auto-escalate before sending a system error to a non-staff customer
        if response_text and not user_context.is_staff and _is_system_error_response(response_text):
            escalated = await _auto_escalate_on_error_response(
                session_id=session_id,
                user_context=user_context,
                webhook_req=webhook_req,
                tool_results=tool_results,
            )
            if escalated:
                response_text = (
                    "Something went wrong on our end. "
                    "Our support team has been notified and will follow up with you shortly."
                )

        # Send text response via Telegram Bot API (with inline buttons if present)
        webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        sent_message_id = await _send_telegram_response(
            webhook_url=webhook_url,
            chat_id=original_chat_id,
            topic_id=webhook_req.topic_id,
            text=response_text,
            reply_to_message_id=reply_to_message_id,
            reply_markup=reply_markup,
        )

        # Store message_id in packet state so buttons can be removed on workflow resume
        await _store_buttons_message_id(sent_message_id, reply_markup, original_chat_id, session_id)

        # Store telegram_message_id on the latest bot message so it can be deleted
        # from the admin UI. Messages are saved by save_history before we get the
        # Telegram message_id, so we back-fill it here.
        if sent_message_id:
            await _backfill_bot_message_id(session_id, sent_message_id)

    except Exception as e:
        LOGGER.exception(f"Error in Telegram async processing: {e}")

        # Send error notification to debug channel
        if tele_debug:
            try:
                error_msg = f"❌ Telegram async error: {type(e).__name__}: {str(e)}\n"
                error_msg += f"Session: {session_id}\n"
                error_msg += f"Chat: {original_chat_id}"
                await tele_debug(error_msg, include_traceback=True)
            except Exception:
                pass

        # Auto-escalate to support for non-staff customers so someone follows up.
        # Fire as a background task so the user error message is not delayed.
        customer_escalation_active = False
        if not user_context.is_staff:
            try:
                from orchestrator.services.escalation_service import EscalationService

                escalation_service = EscalationService()
                if escalation_service.is_enabled():
                    customer_escalation_active = True
                    # Idempotency: skip if session already has a blocking escalation
                    already_escalated = await escalation_service.is_session_escalated(session_id)
                    should_fire = not already_escalated
                    if should_fire:
                        # Rate limit: one auto-escalation per session per 10 minutes
                        existing = await escalation_service.get_escalation_info(session_id)
                        if existing:
                            created_at_str = existing.get("created_at", "")
                            if created_at_str:
                                try:
                                    created_at = datetime.fromisoformat(
                                        created_at_str.replace("Z", "+00:00")
                                    )
                                    age_seconds = (
                                        datetime.now(timezone.utc) - created_at
                                    ).total_seconds()
                                    if age_seconds < 600:
                                        should_fire = False
                                        LOGGER.info(
                                            f"Rate-limiting auto-escalation for session={session_id} "
                                            f"(last escalation {age_seconds:.0f}s ago)"
                                        )
                                except (ValueError, AttributeError):
                                    pass
                    if should_fire:
                        organization_id = None
                        if user_context.organization_ids:
                            try:
                                organization_id = int(user_context.organization_ids[0])
                            except (ValueError, TypeError):
                                pass
                        asyncio.create_task(
                            escalation_service.escalate_to_support(
                                question_summary=f"[SYSTEM ERROR] Unhandled exception: {type(e).__name__}",
                                session_id=session_id,
                                organization_id=organization_id,
                                organization_short_name=user_context.organization_name,
                                customer_chat_id=user_context.chat_id,
                                customer_topic_id=webhook_req.topic_id,
                                customer_username=user_context.username,
                                customer_email=user_context.user_email,
                                conversation_context=(
                                    "[AUTO-ESCALATION: Unhandled exception in _process_telegram_async]\n\n"
                                    f"Exception type: {type(e).__name__} "
                                    f"(see server logs for session={session_id})\n\n"
                                    "Please follow up with the customer."
                                ),
                                reason="system_error",
                            ),
                            name=f"auto-escalate-{session_id}",
                        )
                        LOGGER.info(f"Auto-escalated system error for session={session_id}")
            except Exception as esc_error:
                LOGGER.exception(f"Failed to auto-escalate system error: {esc_error}")

        # Send user-friendly error message immediately (escalation runs in background)
        if bot_token and original_chat_id:
            try:
                webhook_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
                error_text = (
                    "Something went wrong on our end. Our support team has been notified and will follow up with you shortly."
                    if customer_escalation_active
                    else "Sorry, I encountered an error processing your message. Please try again."
                )
                await _send_telegram_response(
                    webhook_url=webhook_url,
                    chat_id=original_chat_id,
                    topic_id=webhook_req.topic_id,
                    text=error_text,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as send_error:
                LOGGER.exception(f"Failed to send error message to Telegram: {send_error}")


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


def _detect_escalation_claim(response_text: str) -> bool:
    """Detect if the response claims to escalate without actually calling the tool.

    Returns True if the response contains affirmative escalation language
    (e.g., "I will escalate", "I have escalated") but NOT negations
    (e.g., "cannot escalate", "won't escalate").
    """
    import re

    text_lower = response_text.lower()

    # Patterns indicating the bot claims to escalate
    escalation_patterns = [
        r"i will (now )?escalate",
        r"i('ve| have) escalated",
        r"escalating (this|your) (request|issue|matter)",
        r"i('m| am) escalating",
        r"let me escalate",
        r"escalate this (to|for)",
        # Patterns implying handoff to staff without using "escalate"
        r"(staff|team|support) will (now )?review",
        r"forwarded? (to|for) (the )?(staff|team|support)",
        r"forwarded? .{1,80}to (the )?(staff|team|support)",  # "forwarded X to the support team"
        r"passed (on )?(to|for) (the )?(staff|team|support)",
        r"passed .{1,80}to (the )?(staff|team|support)",  # "passed X to the support team"
        r"notif(y|ied) (the )?(staff|team|support)",
    ]

    # Negation patterns that indicate NOT escalating
    negation_patterns = [
        r"cannot escalate",
        r"can't escalate",
        r"won't escalate",
        r"will not escalate",
        r"unable to escalate",
        r"don't need to escalate",
        r"no need to escalate",
    ]

    # Check for escalation claim
    claimed_escalation = any(re.search(p, text_lower) for p in escalation_patterns)

    # Check for negation
    is_negation = any(re.search(p, text_lower) for p in negation_patterns)

    return claimed_escalation and not is_negation


def _extract_escalation_summary(response_text: str) -> str:
    """Extract escalation summary from bot response.

    The bot typically formats escalations with a "Summary:" section.
    Falls back to first 200 chars of response if no summary found.
    """
    import re

    # Try to extract "Summary: ..." section
    summary_match = re.search(
        r"(?:\*\*)?summary[:\s]*(?:\*\*)?[\s]*([^\n]+(?:\n[^\n*#]+)*)",
        response_text,
        re.IGNORECASE,
    )
    if summary_match:
        summary = summary_match.group(1).strip()
        # Clean up markdown
        summary = re.sub(r"\*+", "", summary)
        return summary[:500]  # Limit length

    # Fallback: extract first meaningful paragraph
    lines = [line.strip() for line in response_text.split("\n") if line.strip()]
    for line in lines:
        # Skip greetings and short lines
        if len(line) > 30 and not line.lower().startswith(("thank you", "hello", "hi ")):
            return line[:300]

    # Last resort: truncate response
    return response_text[:200] if response_text else "Escalation requested"


async def _send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    reply_markup: dict | None = None,
    parse_mode: str | None = None,
) -> int | None:
    """Send a simple message to Telegram.

    Delegates to the shared helper in shared/utils/telegram_send.py.
    """
    from shared.utils.telegram_send import send_telegram_message

    return await send_telegram_message(
        bot_token, chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode
    )


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


class _TypingIndicator:
    """Async context manager that refreshes the typing indicator every 8 seconds.

    Telegram's "typing..." expires after ~5 seconds. This keeps it alive for
    the entire duration of processing. The background task is always cancelled
    in __aexit__ (even on error), and sendChatAction is stateless on Telegram's
    side so there's no risk of a stuck indicator.

    Usage:
        async with _TypingIndicator(chat_id, topic_id):
            await long_running_processing()
    """

    def __init__(self, chat_id: str, topic_id: str | None, interval: float = 8.0):
        self.chat_id = chat_id
        self.topic_id = topic_id
        self.interval = interval
        self._task: Optional[asyncio.Task] = None

    async def __aenter__(self) -> "_TypingIndicator":
        self._task = asyncio.create_task(self._refresh_loop())
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _refresh_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.interval)
                await _send_telegram_typing_indicator(self.chat_id, self.topic_id)
        except asyncio.CancelledError:
            raise


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
                        await _send_telegram_photo(
                            bot_token=bot_token,
                            chat_id=chat_id,
                            topic_id=topic_id,
                            photo_data=image_data,
                            caption=caption,
                            reply_to_message_id=reply_to_message_id,
                        )
                        LOGGER.info(f"Sent image from tool {result.name} to Telegram")
                    except Exception as e:
                        LOGGER.error(f"Failed to send image from tool {result.name}: {e}")


async def _send_telegram_photo(
    bot_token: str,
    chat_id: str,
    topic_id: str | None,
    photo_data: str,
    caption: str | None = None,
    reply_to_message_id: int | None = None,
) -> None:
    """Send photo to Telegram via Bot API.

    Args:
        bot_token: Telegram bot token
        chat_id: Original Telegram chat ID (with -100 prefix if present)
        topic_id: Optional topic/thread ID for forum groups
        photo_data: Base64-encoded photo data
        caption: Optional caption for the photo
        reply_to_message_id: Optional message ID to reply to (tags the original message)
    """
    import base64

    try:
        webhook_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"

        # Decode base64 to bytes
        photo_bytes = base64.b64decode(photo_data)

        # Build form data payload
        data = aiohttp.FormData()
        data.add_field("chat_id", chat_id)
        data.add_field("photo", photo_bytes, filename="image.png", content_type="image/png")

        if caption:
            data.add_field("caption", caption)

        if topic_id:
            data.add_field("message_thread_id", topic_id)

        if reply_to_message_id:
            data.add_field("reply_to_message_id", str(reply_to_message_id))

        LOGGER.info(
            f"Sending photo to Telegram: chat_id={chat_id}, "
            f"topic_id={topic_id}, size={len(photo_bytes)} bytes"
        )

        async with aiohttp.ClientSession() as session:
            async with session.post(
                webhook_url,
                data=data,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    LOGGER.error(
                        f"Failed to send photo: status={response.status}, error={error_text}"
                    )
                else:
                    LOGGER.info("Successfully sent photo to Telegram")

    except Exception as e:
        LOGGER.exception(f"Error sending telegram photo: {e}")
        raise


async def _store_buttons_message_id(
    sent_message_id: int | None,
    reply_markup: Dict[str, Any] | None,
    chat_id: str,
    session_id: str | None,
) -> None:
    """Store the Telegram message_id of a buttons message in packet state.

    This allows buttons to be removed later when the workflow resumes.
    """
    if not sent_message_id or not reply_markup:
        return
    packet_id = _extract_packet_id_from_reply_markup(reply_markup)
    if not packet_id:
        return
    try:
        from orchestrator.services.work_packet_service import WorkPacketService

        pkt_svc = WorkPacketService()
        await pkt_svc.update_state(
            packet_id,
            {
                "buttons_message_id": sent_message_id,
                "buttons_chat_id": chat_id,
            },
            session_id,
        )
        LOGGER.debug(f"Stored buttons_message_id={sent_message_id} for packet {packet_id}")
    except Exception as store_err:
        LOGGER.warning(f"Failed to store buttons message_id: {store_err}")


async def _backfill_bot_message_id(session_id: str, telegram_message_id: int) -> None:
    """Back-fill telegram_message_id on the latest bot message in a session.

    Messages are saved by save_history BEFORE we get the Telegram message_id
    back from sendMessage, so bot messages are missing this field. We update
    the most recent model message in the session with the id returned by
    Telegram so the admin UI can delete it.

    Non-fatal — never blocks the response flow.
    """
    try:
        from orchestrator.services.supabase_client import get_supabase_client

        supabase = get_supabase_client()._get_client()

        # Look up session UUID from session_id
        session_result = (
            supabase.table("chat_sessions")
            .select("id")
            .eq("session_id", session_id)
            .limit(1)
            .execute()
        )
        if not session_result.data:
            return
        session_uuid = session_result.data[0]["id"]

        # Find the latest model message without a telegram_message_id
        msg_result = (
            supabase.table("chat_messages")
            .select("id")
            .eq("session_id", str(session_uuid))
            .eq("role", "model")
            .is_("telegram_message_id", "null")
            .order("message_index", desc=True)
            .limit(1)
            .execute()
        )
        if not msg_result.data:
            return

        # Update it with the Telegram message_id
        supabase.table("chat_messages").update({"telegram_message_id": telegram_message_id}).eq(
            "id", msg_result.data[0]["id"]
        ).execute()

        LOGGER.debug(
            f"Back-filled telegram_message_id={telegram_message_id} on bot message "
            f"in session {session_id}"
        )
    except Exception as e:
        LOGGER.debug(f"Failed to back-fill bot message_id (non-fatal): {e}")


def _extract_message_id(response_json: dict) -> int | None:
    """Extract message_id from a Telegram Bot API sendMessage response."""
    try:
        if response_json.get("ok"):
            msg_id: int = response_json["result"]["message_id"]
            return msg_id
    except (KeyError, TypeError):
        pass
    return None


def _extract_packet_id_from_reply_markup(reply_markup: Dict[str, Any] | None) -> str | None:
    """Extract packet_id from a webapp-button reply_markup (Edit Parameters / View State).

    Returns the packet_id if the reply_markup contains a web_app button with packet_id param.
    """
    if not reply_markup:
        return None
    try:
        for row in reply_markup.get("inline_keyboard", []):
            for button in row:
                url = button.get("web_app", {}).get("url", "")
                if "packet_id=" in url:
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(url)
                    params = parse_qs(parsed.query)
                    pids = params.get("packet_id", [])
                    if pids:
                        pid: str = pids[0]
                        return pid
    except Exception as e:
        LOGGER.debug(f"Failed to extract packet_id from reply_markup: {e}")
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


def _get_settings() -> AppSettings:
    """Get application settings from environment."""
    # Get tools service URL from environment
    bridge_url = os.getenv("TOOLS_SERVICE_URL", "")

    # Validate the key for the selected LLM provider.
    llm_provider = os.getenv("LLM_PROVIDER", "gemini").strip().lower()
    google_api_key = os.getenv("GOOGLE_API_KEY", "")
    openrouter_api_key = os.getenv("OPENROUTER_API_KEY", "")
    if llm_provider in {"gemini", ""} and not google_api_key:
        LOGGER.error("GOOGLE_API_KEY environment variable is not set!")
        raise ValueError(
            "GOOGLE_API_KEY is required. Please set it in your .env file. "
            "Get your API key from https://makersuite.google.com/app/apikey"
        )
    if llm_provider in {"openrouter", "open-router"} and not openrouter_api_key:
        LOGGER.error("OPENROUTER_API_KEY environment variable is not set!")
        raise ValueError("OPENROUTER_API_KEY is required when LLM_PROVIDER=openrouter.")
    if llm_provider not in {"gemini", "openrouter", "open-router", ""}:
        raise ValueError(
            f"Unsupported LLM_PROVIDER={llm_provider!r}; expected 'gemini' or 'openrouter'"
        )

    # Parse optional temperature (None = use model default, recommended for Gemini 3+)
    temp_str = os.getenv("GEMINI_TEMPERATURE")
    temperature = None
    if temp_str and temp_str.lower() not in ("", "auto", "none", "default"):
        temperature = float(temp_str)

    settings = AppSettings(  # type: ignore[call-arg]
        llm_provider=llm_provider or "gemini",
        google_api_key=google_api_key,
        openrouter_api_key=openrouter_api_key,
        debug=os.getenv("DEBUG", "false").lower() == "true",
        gemini=GeminiModelConfig(
            model=os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
            fallback_model=os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-2.5-flash-lite"),
            temperature=temperature,
        ),
        allow_parallel_calls=os.getenv("ALLOW_PARALLEL_CALLS", "true").lower() == "true",
        max_tool_rounds=int(os.getenv("MAX_TOOL_ROUNDS", "3")),
    )

    # Store bridge URL in settings for use by permissions service
    settings.bridge_url = bridge_url

    return settings
