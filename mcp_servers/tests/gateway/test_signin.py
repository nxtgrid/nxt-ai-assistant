"""Turning a Google-verified email into a gateway bearer token.

Two gates, both required: the shared RBAC whitelist (may this person log in at
all) and session resolution (do they map to an organization). The second is not
redundant — AuthService returns empty organization_ids rather than raising for
an email with no accounts row.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.signin import SignInRejected, mint_token_for_email
from gateway.tokens import verify_token

SECRET = "test-secret-not-a-real-key"


class _Auth:
    def __init__(self, organization_ids):
        self._organization_ids = organization_ids

    async def get_user_permissions(self, email, user_id=None):
        class _P:
            organization_ids = self._organization_ids
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _P()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return ["Alpha Site"]


@pytest.mark.asyncio
async def test_authorized_user_receives_a_usable_token():
    token = await mint_token_for_email(
        "user@example.com", SECRET, _Auth(["4"]), is_authorized=lambda e: True
    )
    assert verify_token(token, SECRET) == "user@example.com"


@pytest.mark.asyncio
async def test_user_outside_the_whitelist_is_rejected():
    with pytest.raises(SignInRejected):
        await mint_token_for_email(
            "stranger@example.com", SECRET, _Auth(["4"]), is_authorized=lambda e: False
        )


@pytest.mark.asyncio
async def test_whitelisted_user_with_no_organization_is_rejected():
    with pytest.raises(SignInRejected):
        await mint_token_for_email(
            "ghost@example.com", SECRET, _Auth([]), is_authorized=lambda e: True
        )
