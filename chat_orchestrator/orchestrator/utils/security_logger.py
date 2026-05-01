"""Security event logging utilities."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger("security")


class SecurityEventType(Enum):
    """Types of security events to log."""

    CROSS_ORG_ACCESS_ATTEMPT = "cross_org_access"
    SESSION_ACCESS_DENIED = "session_access_denied"
    INVALID_SESSION_FORMAT = "invalid_session_format"
    UNAUTHORIZED_TOOL_CALL = "unauthorized_tool_call"


def log_security_event(
    event_type: SecurityEventType,
    session_id: Optional[str] = None,
    user_org_ids: Optional[List[int]] = None,
    target_org_id: Optional[int] = None,
    user_id: Optional[str] = None,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    """
    Log a security event with structured data for monitoring.

    Args:
        event_type: Type of security event
        session_id: Session ID involved (if any)
        user_org_ids: User's organization IDs
        target_org_id: Target organization ID being accessed
        user_id: User identifier
        details: Additional context
    """
    # Log as warning for security events that need attention
    LOGGER.warning(
        f"SECURITY_EVENT | type={event_type.value} | "
        f"session={session_id} | "
        f"user_orgs={user_org_ids} | "
        f"target_org={target_org_id} | "
        f"user={user_id}"
    )


__all__ = ["log_security_event", "SecurityEventType"]
