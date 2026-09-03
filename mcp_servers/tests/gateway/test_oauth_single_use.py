"""Single-use enforcement for an issued authorization code's code_id.

A fake in-memory store stands in for db/migrations/0032's table - this
module never constructs a real DB connection itself, matching every other
piece of the gateway.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth_single_use import CodeAlreadyRedeemed, redeem_once


class _FakeStore:
    def __init__(self):
        self.redeemed: set[str] = set()

    async def try_redeem(self, code_id: str, expires_at) -> bool:
        """Mirrors an atomic UPDATE ... WHERE redeemed_at IS NULL RETURNING:
        True if this call claimed it, False if already claimed.
        """
        if code_id in self.redeemed:
            return False
        self.redeemed.add(code_id)
        return True


@pytest.mark.asyncio
async def test_first_redemption_succeeds():
    store = _FakeStore()
    await redeem_once("code-1", store)  # must not raise
    assert "code-1" in store.redeemed


@pytest.mark.asyncio
async def test_second_redemption_of_the_same_code_is_rejected():
    store = _FakeStore()
    await redeem_once("code-1", store)

    with pytest.raises(CodeAlreadyRedeemed):
        await redeem_once("code-1", store)


@pytest.mark.asyncio
async def test_different_codes_do_not_interfere():
    store = _FakeStore()
    await redeem_once("code-1", store)
    await redeem_once("code-2", store)  # must not raise
