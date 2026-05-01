"""Session ID generation utilities with security hashing."""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from typing import Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@lru_cache(maxsize=1)
def _get_session_secret() -> str:
    """Get session ID secret from environment."""
    secret = os.getenv("SESSION_ID_SECRET", "")
    if not secret:
        LOGGER.warning(
            "SESSION_ID_SECRET not set - using fallback. "
            "Set this in production for session ID unpredictability."
        )
        secret = "dev-fallback-not-for-production"  # pragma: allowlist secret
    return secret


def hash_session_component(component: str) -> str:
    """
    Hash a session component (chat_id, topic_id, user_id) for unpredictability.

    Args:
        component: The raw component to hash (e.g., "1234567890")

    Returns:
        First 16 characters of hex SHA-256 hash
    """
    secret = _get_session_secret()
    combined = f"{component}:{secret}"
    hash_bytes = hashlib.sha256(combined.encode()).hexdigest()
    return hash_bytes[:16]


def generate_session_id(
    source: str,
    chat_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> str:
    """
    Generate a session ID with hashed components for unpredictability.

    Session ID hierarchy:
    - For topic threads: {source}_{hash(chat_id:topic_id)}
    - For chats/groups: {source}_{hash(chat_id)}
    - For DMs: {source}_dm_{hash(user_id)}

    Args:
        source: Message source (telegram, roam, web, api)
        chat_id: Chat/group ID (optional)
        topic_id: Topic/thread ID within chat (optional)
        user_id: User ID for DMs (optional)

    Returns:
        Unpredictable session ID string
    """
    secret = _get_session_secret()

    if topic_id and chat_id:
        combined = f"{chat_id}:{topic_id}:{secret}"
        session_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]
        return f"{source}_{session_hash}"
    elif chat_id:
        combined = f"{chat_id}:{secret}"
        session_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]
        return f"{source}_{session_hash}"
    else:
        combined = f"dm:{user_id or 'unknown'}:{secret}"
        session_hash = hashlib.sha256(combined.encode()).hexdigest()[:16]
        return f"{source}_dm_{session_hash}"


def generate_parent_session_id(
    source: str,
    chat_id: Optional[str] = None,
    topic_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Optional[str]:
    """Return the chat-level session ID if the current session is topic-scoped.

    In Telegram groups with topics/threads, the session_id includes the topic_id
    in the hash.  When a user sends a message without replying (no topic_id),
    a different session_id is generated.  This helper returns that chat-level
    session_id so it can be stored in ``sessions_involved`` alongside the
    topic-level one, allowing packet lookups from either context.

    Returns None if there is no parent (i.e. no topic_id, or no chat_id).
    """
    if not topic_id or not chat_id:
        return None
    # The parent is the chat-level session (same call without topic_id)
    return generate_session_id(source=source, chat_id=chat_id, user_id=user_id)


__all__ = ["generate_session_id", "generate_parent_session_id", "hash_session_component"]
