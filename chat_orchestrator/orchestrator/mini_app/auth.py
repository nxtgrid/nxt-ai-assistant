"""Telegram Mini App initData HMAC validation.

Validates the initData payload sent by Telegram Mini Apps to ensure
requests are authentic and not tampered with. Uses HMAC-SHA256 as
specified by the Telegram Bot API documentation.

See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs

from fastapi import HTTPException, Request

from shared.auth.auth_service import get_auth_service
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Maximum age of initData before considered stale (seconds)
MAX_AUTH_AGE_SECONDS = 300  # 5 minutes

# Maximum clock skew tolerance for future-dated auth_date (seconds)
MAX_FUTURE_SKEW_SECONDS = 60


def validate_init_data(
    init_data_raw: str,
    bot_token: str,
    max_age_seconds: int = MAX_AUTH_AGE_SECONDS,
) -> Dict[str, Any]:
    """Validate Telegram Mini App initData using HMAC-SHA256.

    Args:
        init_data_raw: Raw query string from Telegram.WebApp.initData
        bot_token: Telegram bot token for HMAC key derivation
        max_age_seconds: Maximum acceptable age of auth_date (default: 5 min)

    Returns:
        Parsed and validated data dict with 'user' as parsed JSON

    Raises:
        ValueError: If validation fails (missing hash, bad signature, expired)
    """
    if not init_data_raw:
        raise ValueError("Empty initData")

    # Parse query string into key-value pairs
    parsed = parse_qs(init_data_raw, keep_blank_values=True)
    data = {k: v[0] for k, v in parsed.items()}

    # Extract hash
    received_hash = data.pop("hash", None)
    if not received_hash:
        raise ValueError("Missing hash in initData")

    # Build data check string: sorted key=value pairs joined by \n
    data_check_parts = sorted(f"{key}={value}" for key, value in data.items())
    data_check_string = "\n".join(data_check_parts)

    # secret_key = HMAC-SHA256("WebAppData", bot_token)
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()

    # check_hash = HMAC-SHA256(secret_key, data_check_string)
    computed_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    # Constant-time comparison
    if not hmac.compare_digest(computed_hash, received_hash):
        raise ValueError("Invalid initData signature")

    # Check auth_date freshness
    try:
        auth_date = int(data.get("auth_date", "0"))
    except (TypeError, ValueError):
        raise ValueError("Invalid auth_date")

    now = time.time()
    if auth_date > now + MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("auth_date is in the future")
    if now - auth_date > max_age_seconds:
        raise ValueError("initData expired")

    # Parse user JSON if present
    if "user" in data:
        data["user"] = json.loads(data["user"])

    return data


async def resolve_user_from_telegram_id(
    telegram_id: str,
) -> Optional[Dict[str, Any]]:
    """Look up a user in the Auth DB by their Telegram ID.

    Uses the shared AuthService connection pool instead of per-request connections.

    Args:
        telegram_id: Telegram user ID (as string)

    Returns:
        Dict with id, organization_id, email if found, else None
    """
    auth_service = get_auth_service()
    try:
        pool = await auth_service._get_db_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, organization_id, email
                FROM accounts
                WHERE telegram_id = $1 AND deleted_at IS NULL
                LIMIT 1
                """,
                str(telegram_id),
            )
            if row:
                return dict(row)
            return None
    except Exception:
        LOGGER.exception("Failed to resolve user from telegram_id=%s", telegram_id)
        return None


# In-memory cache: telegram_id -> (record_dict, timestamp)
_user_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}
_USER_CACHE_TTL = 300  # 5 minutes — matches MAX_AUTH_AGE_SECONDS


def _get_cached_user(telegram_id: str) -> Optional[Dict[str, Any]]:
    """Return cached user record if still valid, else None."""
    entry = _user_cache.get(telegram_id)
    if entry is None:
        return None
    record, ts = entry
    if time.time() - ts > _USER_CACHE_TTL:
        del _user_cache[telegram_id]
        return None
    return record


def _set_cached_user(telegram_id: str, record: Dict[str, Any]) -> None:
    """Store user record in cache with current timestamp."""
    _user_cache[telegram_id] = (record, time.time())


async def get_validated_user(request: Request) -> Dict[str, Any]:
    """FastAPI dependency: extract and validate Telegram user from request.

    Expects Authorization header: "tma <initData>"

    Returns:
        Dict with validated initData fields including parsed 'user' object
        and resolved 'organization_id' from Auth DB.

    Raises:
        HTTPException: 401 if validation fails
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("tma "):
        raise HTTPException(status_code=401, detail="Authentication failed")

    init_data_raw = auth_header[4:]
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        LOGGER.error("TELEGRAM_BOT_TOKEN not configured")
        raise HTTPException(status_code=500, detail="Server configuration error")

    try:
        data = validate_init_data(init_data_raw, bot_token)
    except ValueError as e:
        LOGGER.warning("Mini App auth failed: %s", e)
        raise HTTPException(status_code=401, detail="Authentication failed")

    # Resolve organization from Telegram user ID (with in-memory cache)
    user = data.get("user", {})
    telegram_id = str(user.get("id", ""))
    if not telegram_id:
        raise HTTPException(status_code=401, detail="Authentication failed")

    user_record = _get_cached_user(telegram_id)
    if not user_record:
        user_record = await resolve_user_from_telegram_id(telegram_id)
        if not user_record:
            raise HTTPException(status_code=401, detail="Authentication failed")
        _set_cached_user(telegram_id, user_record)

    data["account_id"] = user_record["id"]
    data["organization_id"] = user_record["organization_id"]
    data["email"] = user_record.get("email")

    return data
