"""Two self-contained signed values, both HMAC-signed JWTs so they need no
server-side storage to validate — only the authorization code additionally
needs a single-use check, which is a DB row, not the whole session.
"""

import sys
import time
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth_codes import (
    CorrelationState,
    CorrelationStateInvalid,
    IssuedCodeInvalid,
    decode_correlation_state,
    decode_issued_code,
    encode_correlation_state,
    issue_authorization_code,
)

SECRET = "test-secret-not-a-real-key"


# --- correlation state (the Google-leg `state` parameter) ------------------


def test_correlation_state_round_trips():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="client-chosen-opaque-value",
        code_challenge="abc123",
        secret=SECRET,
    )
    decoded = decode_correlation_state(encoded, SECRET)
    assert decoded == CorrelationState(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="client-chosen-opaque-value",
        code_challenge="abc123",
    )


def test_correlation_state_tampering_is_rejected():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )
    with pytest.raises(CorrelationStateInvalid):
        decode_correlation_state(encoded, "different-secret")


def test_expired_correlation_state_is_rejected():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
        issued_at=time.time() - 10_000,
        ttl_seconds=60,
    )
    with pytest.raises(CorrelationStateInvalid):
        decode_correlation_state(encoded, SECRET)


# --- the gateway's own authorization code -----------------------------------


def test_issued_code_round_trips():
    issued = issue_authorization_code(
        email="user@example.com",
        code_challenge="abc123",
        secret=SECRET,
    )
    decoded = decode_issued_code(issued.code, SECRET)
    assert decoded.email == "user@example.com"
    assert decoded.code_challenge == "abc123"
    assert decoded.code_id == issued.code_id


def test_issued_code_carries_a_stable_code_id_for_single_use_tracking():
    issued = issue_authorization_code(email="a@example.com", code_challenge="c", secret=SECRET)
    decoded = decode_issued_code(issued.code, SECRET)
    assert decoded.code_id == issued.code_id
    assert len(issued.code_id) >= 16  # enough entropy to be a real PK, not a guessable counter


def test_issued_code_tampering_is_rejected():
    issued = issue_authorization_code(email="a@example.com", code_challenge="c", secret=SECRET)
    with pytest.raises(IssuedCodeInvalid):
        decode_issued_code(issued.code, "different-secret")


def test_expired_issued_code_is_rejected():
    issued = issue_authorization_code(
        email="a@example.com",
        code_challenge="c",
        secret=SECRET,
        issued_at=time.time() - 10_000,
        ttl_seconds=60,
    )
    with pytest.raises(IssuedCodeInvalid):
        decode_issued_code(issued.code, SECRET)
