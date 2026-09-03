"""The real single-use store, against the chat DB via PostgREST.

The first implementation connected with AUTH_DB_* - mirroring AuthService's
pattern - which was wrong twice over: that database is READ-ONLY, so the
insert could never have succeeded, and it is a different database from the
one db/migrations/ targets, so the 0032 table was never going to be there.
Production surfaced it as an UndefinedTableError 500 on the very last step
of an otherwise-complete OAuth flow.

CHAT_DB_URL + CHAT_DB_SERVICE_KEY are app-level env vars the gateway
already inherits, and are the same credentials the rest of the repo uses to
write to chat_db, so they are known-good in production rather than assumed.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth_store_chat_db import ChatDbSingleUseStore, SingleUseStoreError


class _FakeResponse:
    def __init__(self, status_code, text=""):
        self.status_code = status_code
        self.text = text


def _store(status_code, calls=None, text=""):
    def fake_post(url, json=None, headers=None, timeout=None):
        if calls is not None:
            calls.append({"url": url, "json": json, "headers": headers})
        return _FakeResponse(status_code, text)

    return ChatDbSingleUseStore(
        base_url="https://project.supabase.co",
        service_key="test-service-key",
        http_post=fake_post,
    )


@pytest.mark.asyncio
async def test_a_fresh_code_is_claimed():
    store = _store(201)
    assert await store.try_redeem("code-1") is True


@pytest.mark.asyncio
async def test_a_duplicate_code_is_reported_as_already_redeemed():
    # PostgREST surfaces the PRIMARY KEY violation as a 409. That conflict IS
    # the single-use guarantee: the uniqueness check happens inside one
    # INSERT, so two concurrent redemptions of the same code can never both
    # come back 201.
    store = _store(409)
    assert await store.try_redeem("code-1") is False


@pytest.mark.asyncio
async def test_an_unexpected_status_raises_rather_than_guessing():
    # Neither silent answer is safe here: returning False would report a
    # legitimate first redemption as a replay, and returning True would hand
    # out a token while the single-use row was never actually written.
    store = _store(500, text="Internal Server Error")
    with pytest.raises(SingleUseStoreError):
        await store.try_redeem("code-1")


@pytest.mark.asyncio
async def test_it_posts_to_the_right_table_with_service_credentials():
    calls = []
    store = _store(201, calls=calls)
    await store.try_redeem("code-1")

    call = calls[0]
    assert call["url"] == "https://project.supabase.co/rest/v1/mcp_gateway_oauth_codes"
    assert call["json"]["code_id"] == "code-1"
    assert call["json"]["expires_at"]
    assert call["headers"]["apikey"] == "test-service-key"
    assert call["headers"]["Authorization"] == "Bearer test-service-key"
