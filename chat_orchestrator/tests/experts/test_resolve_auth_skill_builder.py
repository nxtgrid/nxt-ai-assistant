"""Tests for resolve_auth's skill-builder staff-auth branch.

The skill builder (anansi_app/nicegui_app/pages/skill_builder.py) is a
bot-admin-only NiceGUI surface (RBAC-gated by perms.can_view_bot_admin, a
Google-OAuth allowlist -- a wholly different identity system) that sends
chat turns through POST /chat as an identity-trusted "api" caller, asserting
the logged-in admin's own email as user_email.

Without this branch, that email falls into the generic "User-based
authentication via email" branch below, which looks it up in
public.accounts -- the bot's OWN auth DB, populated by Telegram/bot
onboarding, not by NiceGUI-app logins. Most bot-admin emails were never
added there, so the lookup misses and the caller gets empty
organization_ids and is_staff=False: the whole builder session silently
runs under customer.system instructions with no org scope, and any
org-scoped tool (e.g. customer_get_all_grids_status, which requires an
injected organization_id) gets nothing to work with -- see
mcp_servers/servers/customer_server/customer_mcp_server.py's
_tool_customer_get_all_grids_status, which returns a plain error string
the model then apologizes around instead of raising.

This branch trusts an explicit "skill_builder_staff_auth" metadata flag,
but ONLY when paired with "_identity_trusted" -- the same server-verified
(caller-unforgeable) signal that already exclusively guards user_email
assertion for this exact caller (see app.py's is_identity_trusted_caller
and handler.py's _resolve_email_lookup_fallback). A caller holding only the
shared API_KEY -- every "api" auth_method caller, not just the skill
builder -- cannot set _identity_trusted itself (it's computed server-side
from a header and merged into metadata after the caller's own metadata is
spread, see handler.py's _handle_webhook_async), so this cannot become a
new self-escalation path for any other "api" caller.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.graphs.nodes.resolve_auth import resolve_auth
from orchestrator.models.schemas import UserContext
from shared.auth.auth_service import STAFF_ORG_ID, UserPermissions


def _builder_state(**metadata_overrides) -> dict:
    metadata = {
        "skill_builder_staff_auth": True,
        "_identity_trusted": True,
        **metadata_overrides,
    }
    return {
        "session_id": "session_abc",
        "metadata": metadata,
        "user_context": UserContext(
            user_id="admin@example.com:uuid-1",
            user_email="admin@example.com",
            source="api",
        ),
    }


class TestSkillBuilderStaffAuthBranch:
    @pytest.mark.asyncio
    async def test_grants_staff_with_no_accounts_table_lookup(self):
        """The defining behavior: neither accounts-table lookup method is
        touched -- proves this branch, not the generic email-lookup
        fallback, is what ran."""
        fake_auth = AsyncMock()
        state = _builder_state()

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].is_staff is True
        assert result["user_context"].organization_ids == [str(STAFF_ORG_ID)]
        fake_auth.get_user_permissions.assert_not_awaited()
        fake_auth.resolve_permissions_from_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preserves_the_asserted_email(self):
        fake_auth = AsyncMock()
        state = _builder_state()

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].user_email == "admin@example.com"

    @pytest.mark.asyncio
    async def test_the_opt_in_flag_alone_is_not_enough(self):
        """Without server-verified _identity_trusted, a caller-supplied
        skill_builder_staff_auth flag must be ignored -- otherwise any
        caller holding only the shared API_KEY could self-grant staff by
        setting this one metadata key."""
        fake_auth = AsyncMock()
        fake_auth.get_user_permissions = AsyncMock(
            return_value=UserPermissions(
                user_id="admin@example.com", email="admin@example.com", organization_ids=[]
            )
        )
        state = _builder_state(_identity_trusted=False)

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        fake_auth.get_user_permissions.assert_awaited_once()
        assert result["user_context"].is_staff is False

    @pytest.mark.asyncio
    async def test_identity_trust_alone_is_not_enough(self):
        """Without the explicit opt-in flag, a merely identity-trusted
        request must not auto-become staff -- IDENTITY_ASSERTION_KEY only
        proves the caller may assert an email, not that the session should
        run as staff."""
        fake_auth = AsyncMock()
        fake_auth.get_user_permissions = AsyncMock(
            return_value=UserPermissions(
                user_id="admin@example.com", email="admin@example.com", organization_ids=[]
            )
        )
        state = _builder_state(skill_builder_staff_auth=False)

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        fake_auth.get_user_permissions.assert_awaited_once()
        assert result["user_context"].is_staff is False
