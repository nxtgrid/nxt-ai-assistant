"""Tests for resolve_auth's staff-group auth bypass branch.

handler.py's "STAFF GROUP + UNKNOWN GROUP PRE-CHECK" block verifies a
Telegram group message's sender against public.accounts and, for a
confirmed staff member in a registered staff group, sets
metadata.staff_group_auth (+ staff_group_organization_id) so this node can
grant staff permissions without re-deriving the organization from the
group's own chat-based mapping (which -- by design, see
get_organization_from_chat's docstring -- resolves to whichever
organization *owns the grid the group discusses*, not to the sender's own
organization; that's correct for a genuine shared customer O&M group, but
wrong for an internal-staff monitoring group whose topics happen to be
named after customer sites).

Regression test: a staff-group message is ALSO a plain Telegram chat
message with a chat_id -- meaning it always satisfies the generic
"source == telegram and chat_id" branch that comes first in the function's
if/elif chain. That generic branch unconditionally won for every Telegram
message regardless of the staff_group_auth flag, making this elif branch
dead code for its own intended use case: it could never run for the one
transport (Telegram) it exists to serve. Proving
resolve_permissions_from_chat was never awaited is what proves this branch
(not the generic one) actually ran.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.graphs.nodes.resolve_auth import resolve_auth
from orchestrator.models.schemas import UserContext
from shared.auth.auth_service import STAFF_ORG_ID


def _staff_group_state(**metadata_overrides) -> dict:
    metadata = {
        "staff_group_auth": True,
        "staff_group_organization_id": STAFF_ORG_ID,
        **metadata_overrides,
    }
    return {
        "session_id": "session_xyz",
        "metadata": metadata,
        "user_context": UserContext(
            user_id="111",
            user_email="staffer@example.com",
            source="telegram",
            chat_id="-1009999",
            topic_id="7",
        ),
    }


class TestStaffGroupAuthBranch:
    @pytest.mark.asyncio
    async def test_grants_staff_without_the_generic_chat_lookup(self):
        """The defining behavior: the generic Telegram chat-based branch
        above this one in the if/elif chain is never reached."""
        fake_auth = AsyncMock()
        state = _staff_group_state()

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].is_staff is True
        assert result["user_context"].organization_ids == [str(STAFF_ORG_ID)]
        fake_auth.resolve_permissions_from_chat.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_preserves_the_telegram_user_id(self):
        fake_auth = AsyncMock()
        state = _staff_group_state()

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].user_id == "111"

    @pytest.mark.asyncio
    async def test_honors_an_explicit_staff_group_organization_id(self):
        """staff_group_organization_id lets handler.py's pre-check pass
        through a non-default staff org id; falls back to STAFF_ORG_ID
        (asserted above) only when the key is absent."""
        fake_auth = AsyncMock()
        state = _staff_group_state(staff_group_organization_id=99)

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].organization_ids == ["99"]
