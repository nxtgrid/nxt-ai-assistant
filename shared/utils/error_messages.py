"""Centralized user-facing error messages.

This module provides consistent, user-friendly error messages across the application.
Errors are categorized by the appropriate user action:

Categories:
- TRANSIENT: User should try again (rate limits, timeouts, temp unavailable)
- REPHRASE: User should rephrase their input (empty response, ambiguous)
- PERMISSION: User needs to contact support (auth failures)
- SYSTEM: Internal error, user should try again or contact support (500s, tool failures)
- ESCALATION: Request has been escalated to support

Usage:
    from shared.utils.error_messages import ErrorCategory, get_user_message

    category, message = categorize_error(exception)
    # or
    message = get_user_message(ErrorCategory.TRANSIENT, "rate_limit")
"""

from enum import Enum
from typing import Optional, Tuple


class ErrorCategory(Enum):
    """Error categories based on appropriate user action."""

    TRANSIENT = "transient"  # User should try again later
    REPHRASE = "rephrase"  # User should rephrase their request
    PERMISSION = "permission"  # User needs to contact support
    SYSTEM = "system"  # Internal error, user should try again or contact support
    ESCALATION = "escalation"  # Request escalated to support


# Error messages organized by category and subtype
ERROR_MESSAGES = {
    ErrorCategory.TRANSIENT: {
        "rate_limit": "I'm experiencing high traffic. Please try again in a few moments.",
        "timeout": "That request took too long. Please try again.",
        "service_unavailable": "I'm temporarily unavailable. Please try again in a minute.",
        "connection_error": "I'm having trouble connecting. Please try again in a moment.",
    },
    ErrorCategory.REPHRASE: {
        "empty_response": "I wasn't able to understand that. Could you try rephrasing?",
        "ambiguous": "I'm not sure what you mean. Could you be more specific?",
        "parse_error": "I had trouble understanding that format. Could you rephrase it?",
    },
    ErrorCategory.PERMISSION: {
        "not_authorized": "This chat isn't registered. Please contact support to get set up.",
        "user_not_found": "I don't recognize your account. Please contact support.",
        "access_denied": "You don't have permission for that action. Please contact support.",
    },
    ErrorCategory.SYSTEM: {
        "internal_error": "Something went wrong on our end. Please try again or contact support.",
        "tool_unavailable": "One of my capabilities isn't working right now. Please try again.",
        "database_error": "I'm having trouble accessing data right now. Please try again.",
        "safety_blocked": "I can't respond to that request. If you believe this is an error, please contact support.",
        "content_blocked": "I'm not able to help with that particular request.",
        "recitation_blocked": "I can't provide that information due to content restrictions.",
    },
    ErrorCategory.ESCALATION: {
        "success": "I've escalated your request to our support team. They'll respond shortly.",
        "failed": "I tried to get help but ran into an issue. Please contact support directly.",
        "verification_failed": (
            "Let me check on that and get back to you. "
            "I've notified our support team who will respond shortly."
        ),
    },
}

# Default fallback message
DEFAULT_MESSAGE = "I encountered an issue. Please try again or contact support if this continues."


def get_user_message(category: ErrorCategory, subtype: str) -> str:
    """Get user-facing message for error category and subtype.

    Args:
        category: The error category
        subtype: Specific error subtype within the category

    Returns:
        User-friendly error message
    """
    return ERROR_MESSAGES.get(category, {}).get(subtype, DEFAULT_MESSAGE)


def categorize_error(error: Exception) -> Tuple[ErrorCategory, str]:
    """Categorize an exception and return appropriate user message.

    Args:
        error: The exception to categorize

    Returns:
        Tuple of (ErrorCategory, user_message)
    """
    error_str = str(error).lower()

    # Permission errors - show the actual message (already user-friendly)
    if isinstance(error, PermissionError):
        return (ErrorCategory.PERMISSION, str(error))

    # Rate limiting - transient, user should try again
    if "429" in error_str or "rate limit" in error_str or "too many requests" in error_str:
        return (ErrorCategory.TRANSIENT, get_user_message(ErrorCategory.TRANSIENT, "rate_limit"))

    # Timeouts - transient
    if "timeout" in error_str or "timed out" in error_str:
        return (ErrorCategory.TRANSIENT, get_user_message(ErrorCategory.TRANSIENT, "timeout"))

    # Service unavailable - transient
    if "503" in error_str or "service unavailable" in error_str:
        return (
            ErrorCategory.TRANSIENT,
            get_user_message(ErrorCategory.TRANSIENT, "service_unavailable"),
        )

    # Connection errors - transient
    if "connection" in error_str and ("refused" in error_str or "error" in error_str):
        return (
            ErrorCategory.TRANSIENT,
            get_user_message(ErrorCategory.TRANSIENT, "connection_error"),
        )

    # Empty/no response - likely input issue, user should rephrase
    if "empty response" in error_str or "no response" in error_str:
        return (ErrorCategory.REPHRASE, get_user_message(ErrorCategory.REPHRASE, "empty_response"))

    # Parse/format errors - user should rephrase
    if "parse" in error_str or "format" in error_str or "invalid" in error_str:
        return (ErrorCategory.REPHRASE, get_user_message(ErrorCategory.REPHRASE, "parse_error"))

    # NOTE: We intentionally do NOT map raw "401"/"403"/"unauthorized" substrings
    # to ErrorCategory.PERMISSION. Those strings frequently come from upstream
    # services (Gemini billing/dunning, Jira API, etc.) and have nothing to do
    # with the end user's permissions. Genuine user-level denials raise
    # PermissionError and are caught by the isinstance check above; everything
    # else falls through to SYSTEM, which gives an honest "something went wrong
    # on our end" message instead of a misleading "contact support for access".

    # Database errors - system issue
    if "database" in error_str or "db " in error_str or "sql" in error_str:
        return (ErrorCategory.SYSTEM, get_user_message(ErrorCategory.SYSTEM, "database_error"))

    # Default to system error (don't tell user to "try again" for unknown errors)
    return (ErrorCategory.SYSTEM, get_user_message(ErrorCategory.SYSTEM, "internal_error"))


def is_transient_error(error: Exception) -> bool:
    """Check if error is transient (user should try again).

    Args:
        error: The exception to check

    Returns:
        True if error is transient and user should retry
    """
    category, _ = categorize_error(error)
    return category == ErrorCategory.TRANSIENT


def get_error_guidance(category: ErrorCategory) -> Optional[str]:
    """Get additional guidance for user based on error category.

    Args:
        category: The error category

    Returns:
        Additional guidance text, or None
    """
    guidance = {
        ErrorCategory.TRANSIENT: "This is usually temporary.",
        ErrorCategory.REPHRASE: "Try asking in a different way.",
        ErrorCategory.PERMISSION: "Contact support for assistance.",
        ErrorCategory.SYSTEM: "Please try again or contact support.",
        ErrorCategory.ESCALATION: "A team member will respond soon.",
    }
    return guidance.get(category)


def sanitize_error_for_user(error_text: str, context: str = "") -> str:
    """Sanitize error text for user display.

    Removes internal markers, step names, technical details, file paths,
    and sensitive data (API keys, tokens) that should not be exposed to users.

    Args:
        error_text: Raw error text (may contain internal details)
        context: Optional context description (e.g., "analysis", "processing")

    Returns:
        User-friendly error message
    """
    import re

    text = error_text

    # SECURITY: Remove API keys from URLs (key=xxx query parameters)
    # This catches Google API keys, Telegram tokens, and similar
    text = re.sub(r"(\?|&)key=[^&\s]+", r"\1key=***", text, flags=re.IGNORECASE)
    text = re.sub(r"(\?|&)token=[^&\s]+", r"\1token=***", text, flags=re.IGNORECASE)
    text = re.sub(r"(\?|&)secret=[^&\s]+", r"\1secret=***", text, flags=re.IGNORECASE)
    text = re.sub(r"(\?|&)api_key=[^&\s]+", r"\1api_key=***", text, flags=re.IGNORECASE)

    # SECURITY: Remove Bearer tokens from headers
    text = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer ***", text, flags=re.IGNORECASE)

    # Remove protection markers (both old and new format)
    text = re.sub(r"⟦CMD\d+⟧", "", text)
    text = re.sub(r"__PROTECTED_CMD_\d+__", "", text)
    text = re.sub(r"__PROTECTED\\_CMD\\_\d+__", "", text)
    text = re.sub(r"_\\_PROTECTED\\_CMD\\_\d+__", "", text)

    # Remove step names from error messages
    text = re.sub(r"\bstep\s+\w+\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[Error in step \w+:", "[Error:", text)
    text = re.sub(r"Failed at step \w+:", "Failed:", text)
    text = re.sub(r"No handler for step:\s*\w+", "Processing step unavailable", text)

    # Remove file paths
    text = re.sub(r"/[\w/.-]+\.py(:\d+)?", "", text)

    # Clean up whitespace
    text = re.sub(r"\s+", " ", text).strip()

    # If message is empty, too short, or too technical, use generic message
    if len(text) < 10 or text.startswith("Traceback"):
        if context:
            return f"I encountered an issue during {context}. Please try again."
        return DEFAULT_MESSAGE

    return text
