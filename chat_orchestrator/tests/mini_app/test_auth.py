"""Tests for Telegram Mini App initData HMAC validation."""

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from orchestrator.mini_app.auth import (
    MAX_AUTH_AGE_SECONDS,
    MAX_FUTURE_SKEW_SECONDS,
    validate_init_data,
)

BOT_TOKEN = "1234567890:ABCDefGhIjKlMnOpQrStUvWxYz"


def _build_init_data(
    user: dict | None = None,
    auth_date: int | None = None,
    query_id: str = "test_query_123",
    extra: dict | None = None,
    bot_token: str = BOT_TOKEN,
) -> str:
    """Build a valid initData string with correct HMAC signature."""
    if auth_date is None:
        auth_date = int(time.time())
    if user is None:
        user = {"id": 12345, "first_name": "Test", "username": "testuser"}

    data = {
        "user": json.dumps(user),
        "auth_date": str(auth_date),
        "query_id": query_id,
    }
    if extra:
        data.update(extra)

    # Build data check string (sorted, \n-joined)
    data_check_parts = sorted(f"{key}={value}" for key, value in data.items())
    data_check_string = "\n".join(data_check_parts)

    # Compute HMAC
    secret_key = hmac.new(
        key=b"WebAppData",
        msg=bot_token.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    hash_value = hmac.new(
        key=secret_key,
        msg=data_check_string.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).hexdigest()

    data["hash"] = hash_value
    return urlencode(data)


class TestValidateInitData:
    """Tests for validate_init_data function."""

    def test_valid_init_data(self):
        """Valid initData should parse successfully."""
        init_data = _build_init_data()
        result = validate_init_data(init_data, BOT_TOKEN)

        assert result["user"]["id"] == 12345
        assert result["user"]["first_name"] == "Test"
        assert result["query_id"] == "test_query_123"
        assert "hash" not in result  # hash should be removed

    def test_empty_init_data(self):
        """Empty string should raise ValueError."""
        with pytest.raises(ValueError, match="Empty initData"):
            validate_init_data("", BOT_TOKEN)

    def test_missing_hash(self):
        """initData without hash should raise ValueError."""
        data = urlencode({"auth_date": str(int(time.time())), "query_id": "test"})
        with pytest.raises(ValueError, match="Missing hash"):
            validate_init_data(data, BOT_TOKEN)

    def test_tampered_data(self):
        """Modified initData should fail signature check."""
        init_data = _build_init_data()
        # Tamper with the query_id
        tampered = init_data.replace("test_query_123", "tampered_query")
        with pytest.raises(ValueError, match="Invalid initData signature"):
            validate_init_data(tampered, BOT_TOKEN)

    def test_wrong_bot_token(self):
        """Wrong bot token should fail signature check."""
        init_data = _build_init_data()
        with pytest.raises(ValueError, match="Invalid initData signature"):
            validate_init_data(init_data, "wrong:token")

    def test_expired_init_data(self):
        """initData older than MAX_AUTH_AGE_SECONDS should expire."""
        old_auth_date = int(time.time()) - MAX_AUTH_AGE_SECONDS - 10
        init_data = _build_init_data(auth_date=old_auth_date)
        with pytest.raises(ValueError, match="initData expired"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_fresh_init_data(self):
        """initData within the age window should be accepted."""
        recent = int(time.time()) - 60  # 1 minute ago
        init_data = _build_init_data(auth_date=recent)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result["auth_date"] == str(recent)

    def test_user_json_parsed(self):
        """User field should be parsed from JSON string to dict."""
        user = {"id": 99999, "first_name": "Alice", "last_name": "Smith"}
        init_data = _build_init_data(user=user)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result["user"] == user

    def test_extra_fields_preserved(self):
        """Extra fields in initData should be preserved."""
        init_data = _build_init_data(extra={"chat_instance": "abc123"})
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result["chat_instance"] == "abc123"

    def test_auth_date_boundary(self):
        """initData exactly at the expiry boundary should still be valid."""
        # Just under the limit
        boundary = int(time.time()) - MAX_AUTH_AGE_SECONDS + 1
        init_data = _build_init_data(auth_date=boundary)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result is not None

    def test_non_numeric_auth_date(self):
        """Non-numeric auth_date should raise ValueError."""
        _build_init_data(extra={"auth_date": "not_a_number"})
        # Override the auth_date that _build_init_data sets
        # We need to build this manually to get a non-numeric auth_date signed
        user = {"id": 12345, "first_name": "Test", "username": "testuser"}
        data = {
            "user": json.dumps(user),
            "auth_date": "abc",
            "query_id": "test_query_123",
        }
        data_check_parts = sorted(f"{key}={value}" for key, value in data.items())
        data_check_string = "\n".join(data_check_parts)
        secret_key = hmac.new(
            key=b"WebAppData", msg=BOT_TOKEN.encode("utf-8"), digestmod=hashlib.sha256
        ).digest()
        hash_value = hmac.new(
            key=secret_key, msg=data_check_string.encode("utf-8"), digestmod=hashlib.sha256
        ).hexdigest()
        data["hash"] = hash_value
        raw = urlencode(data)

        with pytest.raises(ValueError, match="Invalid auth_date"):
            validate_init_data(raw, BOT_TOKEN)

    def test_future_auth_date_rejected(self):
        """auth_date far in the future should be rejected."""
        future = int(time.time()) + MAX_FUTURE_SKEW_SECONDS + 60
        init_data = _build_init_data(auth_date=future)
        with pytest.raises(ValueError, match="auth_date is in the future"):
            validate_init_data(init_data, BOT_TOKEN)

    def test_slight_future_auth_date_accepted(self):
        """auth_date slightly in the future (within skew) should be accepted."""
        slight_future = int(time.time()) + 30  # 30s ahead, within 60s tolerance
        init_data = _build_init_data(auth_date=slight_future)
        result = validate_init_data(init_data, BOT_TOKEN)
        assert result is not None
