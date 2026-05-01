"""Response sanitization utilities for MCP tool results."""

from __future__ import annotations

import decimal
import json
import math
from typing import Any, Set

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Fields to strip from tool results before sending to LLM
# These contain internal identifiers or sensitive data that shouldn't be exposed
SENSITIVE_FIELDS: Set[str] = {
    # Organization/tenant identifiers (internal)
    "rls_organization_id",
    "organization_id",
    "rls_grid_id",
    # Auth/security related
    "api_key",
    "secret",
    "token",
    "password",
    "credential",
    "auth_token",
    "access_token",
    "refresh_token",
    # Internal database IDs that shouldn't be exposed
    "internal_id",
    "db_id",
    # PII fields
    "telegram_id",
    "phone_number",
    "ssn",
    "social_security",
}


def sanitize_tool_response(response: Any, depth: int = 0) -> Any:
    """
    Recursively sanitize tool response, removing sensitive fields.

    Args:
        response: Tool response (dict, list, or primitive)
        depth: Current recursion depth (max 10 to prevent infinite loops)

    Returns:
        Sanitized response with sensitive fields removed
    """
    if depth > 10:
        return response

    if isinstance(response, dict):
        sanitized = {}
        for key, value in response.items():
            key_lower = key.lower()

            # Skip sensitive fields entirely
            if key_lower in SENSITIVE_FIELDS or any(
                sensitive in key_lower for sensitive in SENSITIVE_FIELDS
            ):
                LOGGER.debug(f"Sanitized field from tool response: {key}")
                continue

            # Recursively sanitize nested structures
            sanitized[key] = sanitize_tool_response(value, depth + 1)

        return sanitized

    elif isinstance(response, list):
        return [sanitize_tool_response(item, depth + 1) for item in response]

    elif isinstance(response, float):
        if not math.isfinite(response):
            return None
        return response

    elif isinstance(response, decimal.Decimal):
        return float(response)

    elif isinstance(response, str):
        # Check for JSON strings that might contain sensitive data
        try:
            parsed = json.loads(response)
            if isinstance(parsed, (dict, list)):
                sanitized = sanitize_tool_response(parsed, depth + 1)
                return json.dumps(sanitized)
        except (json.JSONDecodeError, ValueError):
            pass
        return response

    else:
        return response


__all__ = ["sanitize_tool_response", "SENSITIVE_FIELDS"]
