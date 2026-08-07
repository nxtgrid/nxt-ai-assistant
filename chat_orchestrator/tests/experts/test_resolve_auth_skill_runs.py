"""Tests for resolve_auth's skill-run branch (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 2).

The generic is_scheduled_execution branch (already existed) trusts a
permissions snapshot captured when a schedule was created. Skill runs
deliberately do NOT use that path -- they re-resolve permissions from the
live chat every time, so a staff-authored skill behaves correctly if run in
a customer group, and a chat's current (not creation-time) membership
governs. This is the one place that distinction is implemented; get it
wrong here and every downstream per-run authorization check inherits the
mistake.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.graphs.nodes.resolve_auth import resolve_auth
from orchestrator.models.schemas import UserContext
from shared.auth.auth_service import UserPermissions


def _skill_run_state(**metadata_overrides) -> dict:
    metadata = {
        "scheduled_execution": True,
        "skill_id": "11111111-1111-1111-1111-111111111111",
        **metadata_overrides,
    }
    return {
        "session_id": "session_abc",
        "metadata": metadata,
        "user_context": UserContext(
            user_id="scheduled",
            user_email="",
            source="telegram",
            chat_id="-100999",
            topic_id="42",
        ),
    }


class TestSkillRunBranch:
    @pytest.mark.asyncio
    async def test_resolves_from_the_live_chat_not_a_stored_snapshot(self):
        """The defining behavior: calls resolve_permissions_from_chat with
        the CURRENT chat_id/topic_id, never touching a
        scheduled_organization_id-style stored snapshot even if one is
        present in metadata -- proving the skill branch, not the generic
        scheduled-command branch, is what ran."""
        fake_auth = AsyncMock()
        fake_auth.resolve_permissions_from_chat = AsyncMock(
            return_value=UserPermissions(
                user_id="scheduled",
                email="ops@example.com",
                organization_ids=["7"],
                grid_ids=["grid-1"],
                is_staff=True,
            )
        )
        state = _skill_run_state(
            # Present but must be ignored -- proves this isn't the stored-
            # snapshot generic scheduled-command branch.
            scheduled_organization_id=999,
            scheduled_is_staff=False,
        )

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        fake_auth.resolve_permissions_from_chat.assert_awaited_once_with(
            chat_id="-100999",
            topic_id="42",
            user_id="scheduled",
            telegram_id="scheduled",
        )
        assert result["user_context"].organization_ids == ["7"]
        assert result["user_context"].is_staff is True
        assert result["user_context"].grid_ids == ["grid-1"]

    @pytest.mark.asyncio
    async def test_unresolvable_chat_does_not_raise(self):
        """Unlike the live (non-scheduled) Telegram branch, which raises
        PermissionError on an unresolvable chat, a skill run must return
        normally with empty organization_ids -- there is no live user to
        see a raised error, and skill_runner.py is responsible for treating
        this as a silent per-chat skip (Phase 5, item 3)."""
        fake_auth = AsyncMock()
        fake_auth.resolve_permissions_from_chat = AsyncMock(
            return_value=UserPermissions(user_id="scheduled", email=None, organization_ids=[])
        )
        state = _skill_run_state()

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        assert result["user_context"].organization_ids == []

    @pytest.mark.asyncio
    async def test_skill_id_without_scheduled_execution_does_not_take_this_branch(self):
        """metadata.skill_id alone (e.g. a stray field on an unrelated
        request) must not trigger live chat resolution -- both conditions
        are required. Falls through to the generic is_scheduled_execution
        check, which is False here, so it lands in the plain user-based
        auth branch."""
        fake_auth = AsyncMock()
        fake_auth.resolve_permissions_from_chat = AsyncMock()
        fake_auth.get_user_permissions = AsyncMock(
            return_value=UserPermissions(user_id="u1", email="a@example.com", organization_ids=[])
        )
        state = {
            "session_id": "session_abc",
            "metadata": {"skill_id": "11111111-1111-1111-1111-111111111111"},
            "user_context": UserContext(
                user_id="u1", user_email="a@example.com", source="api", chat_id=None
            ),
        }

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            await resolve_auth(state)

        fake_auth.resolve_permissions_from_chat.assert_not_awaited()
        fake_auth.get_user_permissions.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_generic_scheduled_command_without_skill_id_is_unaffected(self):
        """Regression guard: an ordinary scheduled command (no skill_id)
        must still take the pre-existing stored-snapshot branch, not this
        new one."""
        fake_auth = AsyncMock()
        fake_auth.resolve_permissions_from_chat = AsyncMock()
        state = {
            "session_id": "session_abc",
            "metadata": {
                "scheduled_execution": True,
                "scheduled_organization_id": 3,
                "scheduled_is_staff": True,
            },
            "user_context": UserContext(
                user_id="scheduled", user_email="a@example.com", source="telegram", chat_id="-1001"
            ),
        }

        with patch(
            "orchestrator.graphs.nodes.resolve_auth.get_auth_service", return_value=fake_auth
        ):
            result = await resolve_auth(state)

        fake_auth.resolve_permissions_from_chat.assert_not_awaited()
        assert result["user_context"].organization_ids == ["3"]
        assert result["user_context"].is_staff is True
