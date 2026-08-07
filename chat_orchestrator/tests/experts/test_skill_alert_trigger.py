"""Tests for skill_schedule_dispatch's alert-trigger path (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md, item 6): waking
skills whose trigger is a notify alert, rather than a cron tick, scoped to
exactly the one grid the alert concerns.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.experts.skill_schedule_dispatch import (
    ALERT_TRIGGER_MIN_INTERVAL_SECONDS,
    _rate_limited,
    dispatch_skill_alert_trigger,
)


def _schedule(**overrides) -> Dict[str, Any]:
    base = {
        "id": "sched-1",
        "skill_id": "skill-1",
        "anchor_entity_type": "grid",
        "schedule_type": "notify_trigger",
        "skill_inputs": {},
    }
    base.update(overrides)
    return base


def _skill(**overrides) -> Dict[str, Any]:
    base = {"id": "skill-1", "title": "Grid Alert Skill", "status": "active", "created_by": "c@example.com"}
    base.update(overrides)
    return base


class TestRateLimited:
    @pytest.mark.asyncio
    async def test_never_run_before_is_not_rate_limited(self):
        mock_supabase = MagicMock()
        mock_supabase.get_last_skill_schedule_run_at = AsyncMock(return_value=None)

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            assert await _rate_limited("sched-1", "Example Grid") is False

    @pytest.mark.asyncio
    async def test_run_just_now_is_rate_limited(self):
        mock_supabase = MagicMock()
        mock_supabase.get_last_skill_schedule_run_at = AsyncMock(
            return_value=datetime.now(timezone.utc).isoformat()
        )

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            assert await _rate_limited("sched-1", "Example Grid") is True

    @pytest.mark.asyncio
    async def test_run_long_ago_is_not_rate_limited(self):
        mock_supabase = MagicMock()
        long_ago = datetime.now(timezone.utc) - timedelta(
            seconds=ALERT_TRIGGER_MIN_INTERVAL_SECONDS + 60
        )
        mock_supabase.get_last_skill_schedule_run_at = AsyncMock(return_value=long_ago.isoformat())

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            assert await _rate_limited("sched-1", "Example Grid") is False

    @pytest.mark.asyncio
    async def test_malformed_timestamp_fails_open(self):
        mock_supabase = MagicMock()
        mock_supabase.get_last_skill_schedule_run_at = AsyncMock(return_value="not-a-timestamp")

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            assert await _rate_limited("sched-1", "Example Grid") is False


class TestDispatchSkillAlertTrigger:
    @pytest.mark.asyncio
    async def test_no_notify_trigger_schedules_is_a_no_op(self):
        mock_supabase = MagicMock()
        mock_supabase.get_notify_trigger_schedules = AsyncMock(return_value=[])

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            result = await dispatch_skill_alert_trigger("Example Grid", "-100123", None)

        assert result == {"dispatched": 0, "skipped": 0, "failed": 0, "reason": None}

    @pytest.mark.asyncio
    async def test_rate_limited_schedule_is_skipped_without_dispatching(self):
        mock_supabase = MagicMock()
        mock_supabase.get_notify_trigger_schedules = AsyncMock(return_value=[_schedule()])

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._rate_limited",
                new_callable=AsyncMock,
                return_value=True,
            ),
        ):
            result = await dispatch_skill_alert_trigger("Example Grid", "-100123", None)

        assert result == {"dispatched": 0, "skipped": 1, "failed": 0, "reason": None}
        mock_supabase.get_skill.assert_not_called()

    @pytest.mark.asyncio
    async def test_dead_creator_marks_unusable_and_skips(self):
        mock_supabase = MagicMock()
        mock_supabase.get_notify_trigger_schedules = AsyncMock(return_value=[_schedule()])
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        mock_supabase.set_skill_status = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=False)

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
        ):
            result = await dispatch_skill_alert_trigger("Example Grid", "-100123", None)

        mock_supabase.set_skill_status.assert_awaited_once()
        assert result["skipped"] == 1

    @pytest.mark.asyncio
    async def test_happy_path_dispatches_to_the_one_resolved_grid(self):
        mock_supabase = MagicMock()
        mock_supabase.get_notify_trigger_schedules = AsyncMock(return_value=[_schedule()])
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=True)
        fake_auth.get_user_permissions = AsyncMock(
            return_value=MagicMock(organization_ids=["7"], is_staff=False)
        )

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._rate_limited",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._dispatch_to_one_entity",
                new_callable=AsyncMock,
                return_value="dispatched",
            ) as mock_dispatch_one,
        ):
            result = await dispatch_skill_alert_trigger("Example Grid", "-100123", "5")

        assert result == {"dispatched": 1, "skipped": 0, "failed": 0, "reason": None}
        anchor_metadata = mock_dispatch_one.call_args.kwargs["anchor_metadata"]
        assert anchor_metadata["grid_name"] == "Example Grid"
        assert anchor_metadata["telegram_chat_id"] == "-100123"
        assert anchor_metadata["telegram_topic_id"] == "5"
