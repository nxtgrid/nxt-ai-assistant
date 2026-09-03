"""Bearer tokens carrying a Google-verified email."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import time

import pytest
from gateway.tokens import TokenInvalid, issue_token, verify_token

SECRET = "test-secret-not-a-real-key"


def test_round_trip_returns_the_email():
    token = issue_token("user@example.com", SECRET)
    assert verify_token(token, SECRET) == "user@example.com"


def test_token_signed_with_another_secret_is_rejected():
    token = issue_token("user@example.com", SECRET)
    with pytest.raises(TokenInvalid):
        verify_token(token, "different-secret")


def test_expired_token_is_rejected():
    token = issue_token("user@example.com", SECRET, issued_at=time.time() - 100_000, ttl_seconds=60)
    with pytest.raises(TokenInvalid):
        verify_token(token, SECRET)


def test_garbage_is_rejected():
    with pytest.raises(TokenInvalid):
        verify_token("not-a-token", SECRET)


def test_token_without_email_claim_is_rejected():
    import jwt

    token = jwt.encode({"exp": time.time() + 600}, SECRET, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        verify_token(token, SECRET)
