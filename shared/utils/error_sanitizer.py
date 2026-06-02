"""
Error Sanitizer Utility

Converts technical error messages to user-friendly messages before they are
sent to the LLM or displayed to end users via Telegram.

Technical details are logged for debugging but NEVER exposed to users.
"""

import logging
import re
from typing import Dict, Optional, Tuple

from shared.utils.error_messages import ErrorCategory, get_user_message

logger = logging.getLogger(__name__)

# Patterns for technical errors and their user-friendly replacements
# Format: (regex_pattern, user_friendly_message, log_level)
ERROR_PATTERNS: list[Tuple[str, str, int]] = [
    # Module/import errors
    (
        r"No module named ['\"]?(\w+)['\"]?",
        "A required component is temporarily unavailable. Please try again later.",
        logging.ERROR,
    ),
    (
        r"ModuleNotFoundError",
        "A required component is temporarily unavailable. Please try again later.",
        logging.ERROR,
    ),
    (
        r"ImportError",
        "A required component is temporarily unavailable. Please try again later.",
        logging.ERROR,
    ),
    # Database errors
    (
        r"(connection refused|connection reset|connection timed out)",
        "Unable to connect to the database. Please try again in a few moments.",
        logging.ERROR,
    ),
    (
        r"(asyncpg|psycopg|postgres|postgresql).*error",
        "A database error occurred. Please try again later.",
        logging.ERROR,
    ),
    (
        r"relation ['\"]?\w+['\"]? does not exist",
        "A database configuration issue occurred. The team has been notified.",
        logging.ERROR,
    ),
    (
        r"duplicate key value violates unique constraint",
        "This item already exists.",
        logging.WARNING,
    ),
    # HTTP/Network errors
    (
        r"(HTTPError|ConnectError|TimeoutException)",
        "A network error occurred. Please try again later.",
        logging.ERROR,
    ),
    (
        r"(timed out|timeout|ETIMEDOUT)",
        "The request took too long. Please try again.",
        logging.WARNING,
    ),
    (
        r"(ECONNREFUSED|ENOTFOUND|EHOSTUNREACH)",
        "Unable to reach the service. Please try again later.",
        logging.ERROR,
    ),
    # Authentication/Authorization errors (keep these somewhat specific for users)
    (
        r"(401|Unauthorized|invalid.*token|expired.*token)",
        "Authentication failed. Please contact support if this persists.",
        logging.WARNING,
    ),
    (
        r"(403|Forbidden|permission denied|access denied)",
        "You don't have permission to perform this action.",
        logging.WARNING,
    ),
    # Rate limiting
    (
        r"(429|rate.?limit|too many requests)",
        "Too many requests. Please wait a moment and try again.",
        logging.WARNING,
    ),
    # Internal server errors
    (
        r"(500|Internal Server Error)",
        "An internal error occurred. Please try again later.",
        logging.ERROR,
    ),
    (
        r"(502|Bad Gateway)",
        "A service is temporarily unavailable. Please try again.",
        logging.ERROR,
    ),
    (
        r"(503|Service Unavailable)",
        "The service is temporarily unavailable. Please try again later.",
        logging.ERROR,
    ),
    # Python/Runtime errors
    (
        r"(TypeError|ValueError|KeyError|AttributeError|IndexError)",
        "An unexpected error occurred. Please try again or rephrase your request.",
        logging.ERROR,
    ),
    (
        r"(NoneType|'None'|null|undefined)",
        "Some required information is missing. Please try again.",
        logging.WARNING,
    ),
    (
        r"(stack trace|traceback|at line \d+)",
        "An internal error occurred. Please try again later.",
        logging.ERROR,
    ),
    # File system errors
    (
        r"(FileNotFoundError|No such file|ENOENT)",
        "A required resource could not be found.",
        logging.ERROR,
    ),
    (
        r"(PermissionError|EACCES)",
        "A permission error occurred.",
        logging.ERROR,
    ),
    # JSON/Parsing errors
    (
        r"(JSONDecodeError|json.decoder|Expecting value)",
        "Failed to process the response. Please try again.",
        logging.ERROR,
    ),
    # Generic bridge/tool errors
    (
        r"Bridge returned \d+:",
        "The tool encountered an error. Please try again.",
        logging.ERROR,
    ),
    (
        r"Error calling tool:",
        "The tool encountered an error. Please try again.",
        logging.ERROR,
    ),
    # Memory errors
    (
        r"(MemoryError|out of memory|OOM)",
        "The system is under heavy load. Please try again later.",
        logging.CRITICAL,
    ),
]

# Default message for unmatched errors
DEFAULT_USER_MESSAGE = "An unexpected error occurred. Please try again later."


def sanitize_error(
    error: str,
    context: Optional[str] = None,
    include_ref_id: bool = True,
) -> str:
    """
    Sanitize a technical error message for end-user display.

    Args:
        error: The original technical error message
        context: Optional context about where the error occurred (e.g., "schedule tool")
        include_ref_id: Whether to include a reference ID for support tickets

    Returns:
        A user-friendly error message safe for display
    """
    if not error:
        return DEFAULT_USER_MESSAGE

    # Log the original error for debugging
    log_context = f" [{context}]" if context else ""
    logger.error(f"Original error{log_context}: {error}")

    # Check against known patterns
    for pattern, user_message, log_level in ERROR_PATTERNS:
        if re.search(pattern, error, re.IGNORECASE):
            logger.log(log_level, f"Matched error pattern '{pattern}' -> '{user_message}'")
            return user_message

    # For unmatched errors, return generic message
    logger.warning(f"Unmatched error pattern, using default message: {error[:200]}")
    return DEFAULT_USER_MESSAGE


def sanitize_error_for_tool_result(
    error: str,
    tool_name: str,
) -> str:
    """
    Sanitize an error specifically for tool execution results.

    This version provides slightly more context for the LLM to work with
    while still hiding technical details from the end user.

    Args:
        error: The original technical error message
        tool_name: Name of the tool that failed

    Returns:
        A sanitized error message suitable for tool results
    """
    sanitized = sanitize_error(error, context=tool_name)

    # Extract a "friendly" tool name
    friendly_name = tool_name.replace("_", " ").replace("-", " ")
    if "_" in tool_name:
        parts = tool_name.split("_", 1)
        if len(parts) == 2:
            friendly_name = parts[1].replace("_", " ")

    return f"The {friendly_name} operation failed: {sanitized}"


def is_user_actionable_error(error: str) -> bool:
    """
    Determine if an error is something the user can take action on.

    Args:
        error: The error message

    Returns:
        True if the user might be able to fix the issue themselves
    """
    actionable_patterns = [
        r"(permission|access|forbidden|unauthorized)",
        r"(not found|does not exist)",
        r"(invalid|malformed|incorrect)",
        r"(rate.?limit|too many)",
        r"(already exists|duplicate)",
    ]

    error_lower = error.lower()
    return any(re.search(p, error_lower) for p in actionable_patterns)


# Mapping for specific error codes to messages (for APIs with numeric error codes)
ERROR_CODE_MESSAGES: Dict[int, str] = {
    400: "The request was invalid. Please check your input and try again.",
    401: "Authentication required. Please ensure you're properly logged in.",
    403: "You don't have permission to perform this action.",
    404: "The requested item was not found.",
    408: "The request timed out. Please try again.",
    409: "There was a conflict with the current state. Please refresh and try again.",
    429: "Too many requests. Please wait a moment and try again.",
    500: "An internal server error occurred. Please try again later.",
    502: "A service is temporarily unavailable. Please try again.",
    503: "The service is temporarily unavailable. Please try again later.",
    504: "The request timed out. Please try again.",
}


def get_user_message_for_status_code(status_code: int) -> str:
    """
    Get a user-friendly message for an HTTP status code.

    Args:
        status_code: HTTP status code

    Returns:
        User-friendly error message
    """
    return ERROR_CODE_MESSAGES.get(status_code, DEFAULT_USER_MESSAGE)


def categorize_and_sanitize_error(
    error: str,
    context: Optional[str] = None,
) -> Tuple[ErrorCategory, str]:
    """
    Categorize and sanitize an error message.

    Returns both the error category (for determining appropriate user action)
    and a sanitized user-friendly message.

    Args:
        error: The original technical error message
        context: Optional context about where the error occurred

    Returns:
        Tuple of (ErrorCategory, sanitized_message)
    """
    if not error:
        return (ErrorCategory.SYSTEM, get_user_message(ErrorCategory.SYSTEM, "internal_error"))

    error_lower = error.lower()

    # Log the original error
    log_context = f" [{context}]" if context else ""
    logger.error(f"Original error{log_context}: {error}")

    # Categorize based on error patterns

    # TRANSIENT errors - user should try again
    if re.search(r"(429|rate.?limit|too many requests)", error_lower):
        return (ErrorCategory.TRANSIENT, get_user_message(ErrorCategory.TRANSIENT, "rate_limit"))

    if re.search(r"(timed out|timeout|ETIMEDOUT)", error_lower, re.IGNORECASE):
        return (ErrorCategory.TRANSIENT, get_user_message(ErrorCategory.TRANSIENT, "timeout"))

    if re.search(r"(503|Service Unavailable)", error, re.IGNORECASE):
        return (
            ErrorCategory.TRANSIENT,
            get_user_message(ErrorCategory.TRANSIENT, "service_unavailable"),
        )

    if re.search(r"(connection refused|connection reset|ECONNREFUSED)", error_lower):
        return (
            ErrorCategory.TRANSIENT,
            get_user_message(ErrorCategory.TRANSIENT, "connection_error"),
        )

    # REPHRASE errors - user should rephrase input
    if re.search(r"(empty response|no response)", error_lower):
        return (ErrorCategory.REPHRASE, get_user_message(ErrorCategory.REPHRASE, "empty_response"))

    if re.search(r"(invalid|malformed|incorrect.*format)", error_lower):
        return (ErrorCategory.REPHRASE, get_user_message(ErrorCategory.REPHRASE, "parse_error"))

    # NOTE: Upstream 401/403 substrings (Gemini billing/dunning, Jira API, etc.)
    # are deliberately NOT mapped to ErrorCategory.PERMISSION. They have nothing
    # to do with the end user's permissions and showing "contact support for
    # access" misleads users when the real issue is on our side. Genuine user-
    # level denials should raise PermissionError and be handled upstream of
    # this function; everything else falls through to SYSTEM below.

    # SYSTEM errors - internal issues
    if re.search(r"(asyncpg|psycopg|postgres|database)", error_lower):
        return (ErrorCategory.SYSTEM, get_user_message(ErrorCategory.SYSTEM, "database_error"))

    # Default to SYSTEM error
    return (ErrorCategory.SYSTEM, get_user_message(ErrorCategory.SYSTEM, "internal_error"))


__all__ = [
    "sanitize_error",
    "sanitize_error_for_tool_result",
    "is_user_actionable_error",
    "get_user_message_for_status_code",
    "categorize_and_sanitize_error",
    "DEFAULT_USER_MESSAGE",
]
