"""Tests for resolve_auth's admin-app branch.

The anansi_app chat widget is a bot-admin-only NiceGUI surface, RBAC-gated by
perms.can_view_bot_admin -- a Google-OAuth allowlist, a wholly different
identity system from public.accounts (the bot's own Telegram/onboarding auth
DB). It sends chat turns through POST /chat as an identity-trusted "api"
caller asserting the logged-in admin's own email.

Unlike the skill_builder_staff_auth branch, which forces STAFF_ORG_ID
unconditionally, this branch tries public.accounts FIRST and uses whatever it
finds verbatim. That is the point: a real staff account already carries
organization_id == STAFF_ORG_ID, so staff land on the staff org for free,
while a non-staff viewer added to the allowlist later is scoped to their own
org instead of silently inheriting ours.

The fallback is deliberately narrow and loud. Without it, an admin email
absent from public.accounts falls into the generic email branch, which returns
empty organization_ids with is_staff=False -- an unscoped customer session
that looks like it worked. That silent degradation is the leak risk.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.graphs.nodes.resolve_auth import resolve_auth
from orchestrator.models.schemas import UserContext
from shared.auth.auth_service import STAFF_ORG_ID, UserPermissions


def _state(**metadata_overrides) -> dict:
    metadata = {
        "admin_app_auth": True,
        "admin_app_bot_admin": True,
        "_identity_trusted": True,
        **metadata_overrides,
    }
    return {
        "session_id": "web_dm_abc",
        "metadata": metadata,
        "user_context": UserContext(
            user_id="anansi-app:admin@example.com:n1",
            user_email="admin@example.com",
            source="web",
        ),
    }


def _auth_returning(permissions: UserPermissions) -> AsyncMock:
    fake = AsyncMock()
    fake.get_user_permissions = AsyncMock(return_value=permissions)
    return fake


class TestAdminAppAuthBranch:
    @pytest.mark.asyncio
    async def test_a_known_customer_account_keeps_its_own_org(self):
        """The defining behaviour: the account is authoritative, not the fallback."""
        fake_auth = _auth_returning(
            UserPermissions(
                user_id="7",
                email="admin@example.com",
                organization_ids=["9"],
                organization_short_name="acme",
                is_staff=False,
            )
        )
        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(_state())

        assert result["user_context"].organization_ids == ["9"]
        assert result["user_context"].is_staff is False
        assert result["user_context"].organization_name == "acme"

    @pytest.mark.asyncio
    async def test_a_real_staff_account_gets_the_staff_org_with_no_fallback(self):
        fake_auth = _auth_returning(
            UserPermissions(
                user_id="1",
                email="admin@example.com",
                organization_ids=[str(STAFF_ORG_ID)],
                is_staff=True,
            )
        )
        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(_state())

        assert result["user_context"].is_staff is True
        assert result["user_context"].organization_ids == [str(STAFF_ORG_ID)]
        fake_auth.get_user_permissions.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_unknown_bot_admin_email_falls_back_to_staff_loudly(self):
        fake_auth = _auth_returning(
            UserPermissions(user_id="admin@example.com", email="admin@example.com")
        )
        with (
            patch(
                "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
            ),
            patch("orchestrator.graphs.nodes.resolve_auth.LOGGER") as mock_logger,
        ):
            result = await resolve_auth(_state())

        assert result["user_context"].is_staff is True
        assert result["user_context"].organization_ids == [str(STAFF_ORG_ID)]
        mock_logger.warning.assert_called_once()
        assert "admin@example.com" in mock_logger.warning.call_args.args[0]

    @pytest.mark.asyncio
    async def test_an_unknown_non_admin_email_is_refused_not_run_unscoped(self):
        fake_auth = _auth_returning(
            UserPermissions(user_id="someone@example.com", email="someone@example.com")
        )
        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            with pytest.raises(PermissionError):
                await resolve_auth(_state(admin_app_bot_admin=False))

    @pytest.mark.asyncio
    async def test_the_opt_in_flag_alone_is_not_enough(self):
        """Without server-verified _identity_trusted, any holder of the shared
        API_KEY could self-grant by setting one metadata key."""
        fake_auth = _auth_returning(
            UserPermissions(user_id="x", email="admin@example.com", organization_ids=[])
        )
        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(_state(_identity_trusted=False))

        # Generic email branch ran: no staff grant, no PermissionError.
        assert result["user_context"].is_staff is False
        assert result["user_context"].organization_ids == []

    @pytest.mark.asyncio
    async def test_identity_trust_alone_is_not_enough(self):
        fake_auth = _auth_returning(
            UserPermissions(user_id="x", email="admin@example.com", organization_ids=[])
        )
        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(_state(admin_app_auth=False))

        assert result["user_context"].is_staff is False
