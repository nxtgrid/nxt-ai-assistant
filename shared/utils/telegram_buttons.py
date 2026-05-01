"""Telegram inline keyboard button utilities for decision prompts.

Provides helper functions for building inline keyboards and parsing callback data
for four types of flows:

1. Expert Workflow Decisions (pd: prefix)
   - Stored in pending_decisions table
   - Fixed options (run new, resume, cancel)
   - Format: pd:{decision_id_prefix}:{action}

2. Procedure-Based Choices (pc: prefix)
   - Dynamic options from LLM response
   - No database storage (LLM maintains context)
   - Format: pc:{choice_number}

3. Step Input Choices (si: prefix)
   - Step handler needs_user_input prompts with numbered options
   - Sends choice NUMBER (not text) as user_input
   - Format: si:{choice_number}

4. Escalation Tracking (es: prefix)
   - Track escalation as JIRA ticket and close
   - Format: es:{mapping_uuid}

Telegram Limits:
- callback_data: max 64 bytes

Usage:
    from shared.utils.telegram_buttons import (
        build_decision_keyboard,
        parse_callback_data,
        parse_procedure_buttons,
        build_step_input_keyboard,
        parse_numbered_options,
        DUPLICATE_OPTIONS,
    )

    # Build keyboard for a decision
    keyboard = build_decision_keyboard(decision_id, DUPLICATE_OPTIONS)

    # Parse [BUTTONS] block from LLM response
    clean_text, keyboard = parse_procedure_buttons(llm_response)

    # Build keyboard for step handler numbered options
    options = parse_numbered_options(user_prompt)
    keyboard = build_step_input_keyboard(options)

    # Parse callback data from button click
    result = parse_callback_data("pd:abc12345:run_new")
    # Returns: {"type": "pd", "id_prefix": "abc12345", "action": "run_new"}
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Callback data prefix for pending decisions (expert workflows)
CALLBACK_PREFIX = "pd"

# Callback data prefix for procedure choices (LLM-generated)
PROCEDURE_CALLBACK_PREFIX = "pc"

# Callback data prefix for step input choices (step handler needs_user_input)
STEP_INPUT_CALLBACK_PREFIX = "si"

# Callback data prefix for escalation tracking (es:{mapping_uuid})
ESCALATION_TRACK_CALLBACK_PREFIX = "es"

# Callback data prefix for close escalation silently (ec:{mapping_uuid})
ESCALATION_CLOSE_SILENT_PREFIX = "ec"

# Callback data prefix for close escalation and notify customer (en:{mapping_uuid})
ESCALATION_CLOSE_NOTIFY_PREFIX = "en"

# Callback data prefix for proactive escalation offer to customer (eo:{session_id})
ESCALATION_OFFER_PREFIX = "eo"

# Maximum length for Telegram callback_data (64 bytes)
MAX_CALLBACK_DATA_LENGTH = 64

# Decision ID prefix length (first N characters of UUID)
# UUID = 36 chars, we use first 8 for prefix
# Full callback: "pd:abc12345:run_new" = ~20 chars (well under 64)
DECISION_ID_PREFIX_LENGTH = 8


# =============================================================================
# Pre-defined option sets for decision types
# =============================================================================

# Duplicate detection options (non-resumable expert)
DUPLICATE_OPTIONS: List[Dict[str, str]] = [
    {"label": "1. Run new", "action": "run_new"},
    {"label": "2. Cancel", "action": "cancel"},
]

# Duplicate detection options (resumable expert)
DUPLICATE_OPTIONS_RESUMABLE: List[Dict[str, str]] = [
    {"label": "1. Run new", "action": "run_new"},
    {"label": "2. Resume", "action": "resume"},
    {"label": "3. Cancel", "action": "cancel"},
]

# Resume failed/blocked packet options
RESUME_OPTIONS: List[Dict[str, str]] = [
    {"label": "1. Resume", "action": "resume"},
    {"label": "2. Start fresh", "action": "start_fresh"},
    {"label": "3. Abandon", "action": "abandon"},
]


def is_inline_buttons_enabled() -> bool:
    """Check if inline buttons feature is enabled.

    Returns:
        True if INLINE_BUTTONS_ENABLED env var is "true" (case-insensitive)
    """
    return os.getenv("INLINE_BUTTONS_ENABLED", "false").lower() == "true"


def build_decision_keyboard(
    decision_id: str,
    options: List[Dict[str, str]],
) -> Dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup for a decision prompt.

    Args:
        decision_id: Full decision UUID from pending_decisions table
        options: List of option dicts with 'label' and 'action' keys

    Returns:
        Telegram InlineKeyboardMarkup structure:
        {
            "inline_keyboard": [
                [{"text": "1️⃣ Run new", "callback_data": "pd:abc12345:run_new"}],
                [{"text": "2️⃣ Cancel", "callback_data": "pd:abc12345:cancel"}],
            ]
        }
    """
    # Use full decision_id (UUID = 36 chars, fits within 64-byte callback limit)
    id_prefix = decision_id

    buttons = []
    for option in options:
        callback_data = f"{CALLBACK_PREFIX}:{id_prefix}:{option['action']}"

        # Validate callback_data length
        if len(callback_data.encode("utf-8")) > MAX_CALLBACK_DATA_LENGTH:
            LOGGER.error(f"Callback data too long ({len(callback_data)} bytes): {callback_data}")
            # Truncate action if needed (shouldn't happen with our short actions)
            max_action_len = MAX_CALLBACK_DATA_LENGTH - len(f"{CALLBACK_PREFIX}:{id_prefix}:") - 1
            truncated_action = option["action"][:max_action_len]
            callback_data = f"{CALLBACK_PREFIX}:{id_prefix}:{truncated_action}"

        # Each button on its own row for better tap targets
        buttons.append([{"text": option["label"], "callback_data": callback_data}])

    return {"inline_keyboard": buttons}


def parse_callback_data(callback_data: str) -> Optional[Dict[str, str]]:
    """Parse callback data from button click into components.

    Supports two formats:
    - Decision callbacks: "pd:{id_prefix}:{action}"
    - Procedure callbacks: "pc:{choice_number}"

    Args:
        callback_data: String from Telegram callback_query.data

    Returns:
        Dict with parsed components or None if invalid:
        - For pd: {"type": "pd", "id_prefix": "abc12345", "action": "run_new"}
        - For pc: {"type": "pc", "choice": "1"}
    """
    if not callback_data:
        return None

    parts = callback_data.split(":", 2)  # Split into at most 3 parts

    if len(parts) < 2:
        LOGGER.warning(f"Invalid callback_data format: {callback_data}")
        return None

    callback_type = parts[0]

    # Handle pending decision callbacks (pd:id_prefix:action)
    if callback_type == CALLBACK_PREFIX:
        if len(parts) != 3:
            LOGGER.warning(f"Invalid pd callback_data format: {callback_data}")
            return None
        return {
            "type": callback_type,
            "id_prefix": parts[1],
            "action": parts[2],
        }

    # Handle procedure choice callbacks (pc:choice_number)
    if callback_type == PROCEDURE_CALLBACK_PREFIX:
        return {
            "type": callback_type,
            "choice": parts[1],
        }

    # Handle step input callbacks (si:choice_number)
    if callback_type == STEP_INPUT_CALLBACK_PREFIX:
        return {
            "type": callback_type,
            "choice": parts[1],
        }

    # Handle escalation callbacks (es/ec/en:mapping_uuid)
    if callback_type in (
        ESCALATION_TRACK_CALLBACK_PREFIX,
        ESCALATION_CLOSE_SILENT_PREFIX,
        ESCALATION_CLOSE_NOTIFY_PREFIX,
    ):
        return {
            "type": callback_type,
            "mapping_id": parts[1],
        }

    # Handle escalation offer callback (eo:session_id)
    if callback_type == ESCALATION_OFFER_PREFIX:
        return {
            "type": callback_type,
            "session_id": parts[1] if len(parts) > 1 else "",
        }

    LOGGER.warning(f"Unknown callback type: {callback_type}")
    return None


def get_options_for_duplicate_decision(is_resumable: bool) -> List[Dict[str, str]]:
    """Get the appropriate options list for a duplicate decision.

    Args:
        is_resumable: Whether the expert supports resuming existing work

    Returns:
        List of option dicts for build_decision_keyboard
    """
    return DUPLICATE_OPTIONS_RESUMABLE if is_resumable else DUPLICATE_OPTIONS


def get_options_for_resume_decision() -> List[Dict[str, str]]:
    """Get options list for a resume failed/blocked decision.

    Returns:
        List of option dicts for build_decision_keyboard
    """
    return RESUME_OPTIONS


# =============================================================================
# Procedure-Based Buttons (LLM-generated choices)
# =============================================================================

# Regex to match BUTTONS blocks - resilient to bracket variations
# Branch 1: [BUTTONS]...[/BUTTONS] (with or without brackets, closed)
# Branch 2: [BUTTONS]... (unclosed tag — captures content to end of string)
BUTTONS_BLOCK_PATTERN = re.compile(
    r"\[?BUTTONS\]?\s*(.*?)\s*\[?/BUTTONS\]?" r"|\[BUTTONS\]\s*(.*)",
    re.DOTALL | re.IGNORECASE,
)

# Regex to match numbered options within a buttons block
# Matches: "1. Option text" or "1) Option text" or "1 Option text"
BUTTON_OPTION_PATTERN = re.compile(
    r"^\s*(\d+)[.\)]\s*(.+?)\s*$",
    re.MULTILINE,
)


def is_procedure_buttons_enabled() -> bool:
    """Check if procedure buttons feature is enabled for customer support.

    Returns:
        True if PROCEDURE_BUTTONS_ENABLED env var is "true" (case-insensitive)
    """
    return os.getenv("PROCEDURE_BUTTONS_ENABLED", "false").lower() == "true"


def parse_procedure_buttons(
    response_text: str,
) -> Tuple[str, Optional[Dict[str, Any]], Optional[List[str]]]:
    """Parse [BUTTONS] block from LLM response and build inline keyboard.

    Extracts a [BUTTONS]...[/BUTTONS] block from the response, parses
    numbered options, and creates an inline keyboard.

    Format expected:
        Some text here...

        [BUTTONS]
        1. First option
        2. Second option
        3. Third option
        [/BUTTONS]

        More text here...

    Args:
        response_text: Full LLM response text

    Returns:
        Tuple of:
        - Clean text with [BUTTONS] block removed
        - InlineKeyboardMarkup dict (or None if no valid buttons found)
        - List of choice texts for mapping (or None)

    Example:
        >>> text, keyboard, choices = parse_procedure_buttons(response)
        >>> if keyboard:
        ...     # Send message with keyboard
        ...     choices  # ["Check meter status", "Create ticket", "Talk to human"]
    """
    if not response_text:
        return response_text, None, None

    # Find [BUTTONS] block
    match = BUTTONS_BLOCK_PATTERN.search(response_text)
    if not match:
        return response_text, None, None

    buttons_content = match.group(1) or match.group(2) or ""

    # Always strip BUTTONS tags from text, even if we can't build a keyboard.
    # This prevents raw tags like "BUTTONS ... /BUTTONS" from reaching the user.
    clean_text = BUTTONS_BLOCK_PATTERN.sub("", response_text).strip()
    clean_text = re.sub(r"\n{3,}", "\n\n", clean_text)

    # Try parsing numbered options first (e.g., "1. Option text")
    options = BUTTON_OPTION_PATTERN.findall(buttons_content)

    # Build inline keyboard
    buttons = []
    choice_texts = []

    if options and 2 <= len(options) <= 4:
        # Numbered options found
        for number, label in options:
            display_label = label[:50] + "..." if len(label) > 50 else label
            callback_data = f"{PROCEDURE_CALLBACK_PREFIX}:{number}"
            buttons.append([{"text": f"{number}. {display_label}", "callback_data": callback_data}])
            choice_texts.append(label.strip())
    else:
        # Fallback: parse non-empty lines as unnumbered options
        lines = [line.strip() for line in buttons_content.strip().split("\n") if line.strip()]
        # Strip leading bullet/dash markers (e.g., "- Option" or "• Option")
        lines = [re.sub(r"^[-•*]\s*", "", line) for line in lines]
        lines = [line for line in lines if line]

        if not lines or len(lines) < 2 or len(lines) > 4:
            LOGGER.warning(
                f"Invalid button count ({len(lines) if lines else 0}), must be 2-4. "
                f"Skipping buttons but tags stripped from response."
            )
            return clean_text, None, None

        for i, label in enumerate(lines, start=1):
            display_label = label[:50] + "..." if len(label) > 50 else label
            callback_data = f"{PROCEDURE_CALLBACK_PREFIX}:{i}"
            buttons.append([{"text": f"{i}. {display_label}", "callback_data": callback_data}])
            choice_texts.append(label.strip())
        LOGGER.info("Used fallback parsing for unnumbered button options")

    keyboard = {"inline_keyboard": buttons}

    LOGGER.info(f"Parsed {len(buttons)} procedure buttons from response")

    return clean_text, keyboard, choice_texts


def strip_buttons_tags(text: str) -> str:
    """Strip [BUTTONS]...[/BUTTONS] tags from text without creating a keyboard.

    Use this for contexts where inline buttons cannot be used (e.g. scheduled
    messages) but the LLM may still emit BUTTONS markup.

    Args:
        text: Response text that may contain [BUTTONS] blocks

    Returns:
        Text with BUTTONS blocks removed
    """
    if not text:
        return text
    cleaned = BUTTONS_BLOCK_PATTERN.sub("", text).strip()
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def build_procedure_keyboard(options: List[str]) -> Dict[str, Any]:
    """Build inline keyboard from a list of option strings.

    This is a simpler alternative to parse_procedure_buttons when you
    already have the options extracted.

    Args:
        options: List of option labels (2-4 items)

    Returns:
        InlineKeyboardMarkup dict
    """
    if len(options) < 2 or len(options) > 4:
        raise ValueError(f"Must have 2-4 options, got {len(options)}")

    buttons = []
    for i, label in enumerate(options, start=1):
        display_label = label[:50] + "..." if len(label) > 50 else label
        callback_data = f"{PROCEDURE_CALLBACK_PREFIX}:{i}"
        buttons.append([{"text": f"{i}. {display_label}", "callback_data": callback_data}])

    return {"inline_keyboard": buttons}


# =============================================================================
# Step Input Buttons (step handler needs_user_input)
# =============================================================================

# Number emoji mapping for step input buttons
_NUMBER_PREFIXES = {1: "1.", 2: "2.", 3: "3.", 4: "4.", 5: "5.", 6: "6.", 7: "7.", 8: "8.", 9: "9."}

# Regex to match numbered options with emoji or digit prefixes in step handler prompts
# Matches: "1️⃣ Chat about this review" or "1. Option text" or "1) Option text"
STEP_OPTION_PATTERN = re.compile(
    r"^\s*(?:(\d)️⃣\s*(.+)|(\d)[.\)]\s*(.+))\s*$",
    re.MULTILINE,
)


def _number_prefix(n: int) -> str:
    """Get number prefix for a digit (1-9)."""
    return _NUMBER_PREFIXES.get(n, f"{n}.")


def parse_numbered_options(text: str) -> List[str]:
    """Extract option labels from numbered prompts.

    Matches both emoji-numbered (1️⃣ Chat) and digit-numbered (1. Chat) lines.
    Returns labels in order, skipping non-option lines.

    Args:
        text: Prompt text containing numbered options

    Returns:
        List of option labels (may be empty if no numbered options found)
    """
    if not text:
        return []

    options: List[str] = []
    for match in STEP_OPTION_PATTERN.finditer(text):
        # Group 1+2 = emoji format, group 3+4 = digit format
        if match.group(1) is not None:
            label = match.group(2).strip()
        else:
            label = match.group(4).strip()
        if label:
            options.append(label)

    return options


def build_step_input_keyboard(options: List[str]) -> Optional[Dict[str, Any]]:
    """Build inline keyboard for step handler user input.

    Each button uses si:{number} callback (1-indexed), which sends the
    choice NUMBER back to the step handler (not the full text).

    Args:
        options: List of option labels to display as buttons

    Returns:
        InlineKeyboardMarkup dict, or None if options invalid (< 2 or > 6)
    """
    if not options or len(options) < 2 or len(options) > 6:
        if options:
            LOGGER.warning(
                f"Invalid step input option count ({len(options)}), must be 2-6. Skipping buttons."
            )
        return None

    buttons = []
    for i, label in enumerate(options, 1):
        display = label[:50] + "..." if len(label) > 50 else label
        callback_data = f"{STEP_INPUT_CALLBACK_PREFIX}:{i}"
        buttons.append([{"text": f"{_number_prefix(i)} {display}", "callback_data": callback_data}])

    return {"inline_keyboard": buttons}


# =============================================================================
# Escalation Tracking Buttons
# =============================================================================


def build_escalation_track_keyboard(mapping_id: str, include_track: bool = True) -> Dict[str, Any]:
    """Build inline keyboard with escalation action buttons.

    Three actions (when include_track=True):
    - Track as ticket & close: Creates JIRA ticket, notifies customer, closes escalation
    - Close silently: Closes escalation without any customer notification
    - Close & inform: Closes escalation and sends resolution message to customer

    When include_track=False (after-hours auto-Jira), the Track row is omitted.

    Args:
        mapping_id: Pre-generated UUID for the escalation mapping
        include_track: Include the "Track as ticket" button (default True)

    Returns:
        InlineKeyboardMarkup dict
    """
    rows = []
    if include_track:
        rows.append(
            [
                {
                    "text": "\U0001f4cb Track as ticket & close",
                    "callback_data": f"{ESCALATION_TRACK_CALLBACK_PREFIX}:{mapping_id}",
                }
            ]
        )
    rows.append(
        [
            {
                "text": "\U0001f507 Close silently",
                "callback_data": f"{ESCALATION_CLOSE_SILENT_PREFIX}:{mapping_id}",
            },
            {
                "text": "\u2705 Close & inform customer",
                "callback_data": f"{ESCALATION_CLOSE_NOTIFY_PREFIX}:{mapping_id}",
            },
        ]
    )
    return {"inline_keyboard": rows}


def build_escalation_offer_keyboard(session_id: str) -> Dict[str, Any]:
    """Build a single-button keyboard offering escalation to customer after a system error.

    The button disappears after the customer clicks it (handler calls remove_buttons_from_message).

    Args:
        session_id: Session identifier stored in callback_data for escalation lookup

    Returns:
        InlineKeyboardMarkup dict
    """
    # session_id truncated to 60 chars so "eo:" prefix fits within 64-byte Telegram limit
    callback_data = f"{ESCALATION_OFFER_PREFIX}:{session_id[:60]}"
    return {"inline_keyboard": [[{"text": "Contact support", "callback_data": callback_data}]]}


# =============================================================================
# Mini App (WebApp) Buttons
# =============================================================================


def build_webapp_keyboard(
    label: str,
    url: str,
    chat_id: Any = None,
) -> Dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup with a Mini App button.

    Always uses ``web_app`` button type for native Telegram Mini App popup.
    The ``chat_id`` parameter is accepted for future use but currently ignored.

    Args:
        label: Button text shown to user
        url: Full HTTPS URL of the Mini App page
        chat_id: Telegram chat ID (reserved for future use)

    Returns:
        InlineKeyboardMarkup dict
    """
    return {
        "inline_keyboard": [
            [{"text": label, "web_app": {"url": url}}],
        ]
    }


def build_url_keyboard(
    label: str,
    url: str,
) -> Dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup with a regular URL button.

    Opens the URL in the platform's browser/webview. Works on all
    Telegram platforms (mobile, desktop, web) without requiring the
    Telegram Mini App SDK.

    Args:
        label: Button text shown to user
        url: Full HTTPS URL to open

    Returns:
        InlineKeyboardMarkup dict with a url button
    """
    return {
        "inline_keyboard": [
            [{"text": label, "url": url}],
        ]
    }


def build_multi_webapp_keyboard(
    buttons: List[Tuple[str, str]],
    chat_id: Any = None,
) -> Dict[str, Any]:
    """Build Telegram InlineKeyboardMarkup with multiple Mini App buttons.

    Always uses ``web_app`` button type for native Telegram Mini App popup.

    Args:
        buttons: List of (label, url) tuples for each button
        chat_id: Telegram chat ID (reserved for future use)

    Returns:
        InlineKeyboardMarkup dict with buttons on a single row
    """
    row = [{"text": label, "web_app": {"url": url}} for label, url in buttons]
    return {"inline_keyboard": [row]}


async def remove_buttons_from_message(
    chat_id: str | int,
    message_id: int,
) -> None:
    """Remove inline buttons from a Telegram message by clearing its reply_markup.

    Args:
        chat_id: Telegram chat ID
        message_id: Message ID to edit
    """
    import aiohttp

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    if not isinstance(message_id, int) or not chat_id:
        LOGGER.warning(
            "Invalid args for remove_buttons_from_message: "
            f"chat_id={chat_id!r}, message_id={message_id!r}"
        )
        return

    try:
        url = f"https://api.telegram.org/bot{bot_token}/editMessageReplyMarkup"
        payload: Dict[str, Any] = {
            "chat_id": str(chat_id),
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    LOGGER.warning(f"Failed to remove buttons: {error_text}")
                else:
                    LOGGER.debug(f"Removed buttons from message {message_id}")
    except Exception as e:
        LOGGER.warning(f"Error removing buttons: {e}")


__all__ = [
    # Decision buttons (expert workflows)
    "build_decision_keyboard",
    "parse_callback_data",
    "is_inline_buttons_enabled",
    "get_options_for_duplicate_decision",
    "get_options_for_resume_decision",
    "DUPLICATE_OPTIONS",
    "DUPLICATE_OPTIONS_RESUMABLE",
    "RESUME_OPTIONS",
    "CALLBACK_PREFIX",
    "DECISION_ID_PREFIX_LENGTH",
    # Procedure buttons (LLM-generated)
    "parse_procedure_buttons",
    "build_procedure_keyboard",
    "is_procedure_buttons_enabled",
    "PROCEDURE_CALLBACK_PREFIX",
    # Step input buttons (step handler prompts)
    "build_step_input_keyboard",
    "parse_numbered_options",
    "STEP_INPUT_CALLBACK_PREFIX",
    # Escalation tracking
    "ESCALATION_TRACK_CALLBACK_PREFIX",
    "ESCALATION_CLOSE_SILENT_PREFIX",
    "ESCALATION_CLOSE_NOTIFY_PREFIX",
    "build_escalation_track_keyboard",
    # Escalation offer (customer-facing button after system error)
    "ESCALATION_OFFER_PREFIX",
    "build_escalation_offer_keyboard",
    # Mini App buttons
    "build_webapp_keyboard",
    "build_multi_webapp_keyboard",
    # Button removal
    "remove_buttons_from_message",
]
