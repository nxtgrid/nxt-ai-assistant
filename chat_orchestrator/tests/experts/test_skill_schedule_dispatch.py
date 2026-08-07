"""Tests for orchestrator.experts.skill_schedule_dispatch (Phase 5 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md): fanning a
scheduled skill run out across every eligible entity, with per-run
authorization, run-history logging, and staff-vs-customer failure routing.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.experts.skill_schedule_dispatch import (
    _deliver_failure,
    _dispatch_to_one_entity,
    dispatch_skill_schedule,
)


def _schedule(**overrides) -> Dict[str, Any]:
    base = {
        "id": "sched-1",
        "skill_id": "skill-1",
        "anchor_entity_type": "grid",
        "skill_inputs": {},
        "created_by_email": "creator@example.com",
    }
    base.update(overrides)
    return base


def _skill(**overrides) -> Dict[str, Any]:
    base = {
        "id": "skill-1",
        "title": "Find Tickets",
        "status": "active",
        "created_by": "creator@example.com",
    }
    base.update(overrides)
    return base


def _permissions(organization_ids=None, is_staff=False) -> MagicMock:
    perms = MagicMock()
    perms.organization_ids = organization_ids or []
    perms.is_staff = is_staff
    return perms


class TestDispatchSkillSchedule:
    @pytest.mark.asyncio
    async def test_schedule_not_found(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=None)

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            result = await dispatch_skill_schedule("sched-1")

        assert result == {"dispatched": 0, "skipped": 0, "failed": 0, "reason": "schedule not found"}

    @pytest.mark.asyncio
    async def test_skill_not_found(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=None)

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            result = await dispatch_skill_schedule("sched-1")

        assert result["reason"] == "skill not found"

    @pytest.mark.asyncio
    async def test_inactive_skill(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=_skill(status="unusable"))

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            result = await dispatch_skill_schedule("sched-1")

        assert "unusable" in result["reason"]

    @pytest.mark.asyncio
    async def test_dead_creator_marks_skill_unusable_and_aborts(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        mock_supabase.set_skill_status = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=False)

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
        ):
            result = await dispatch_skill_schedule("sched-1")

        mock_supabase.set_skill_status.assert_awaited_once()
        assert mock_supabase.set_skill_status.call_args.args[:2] == ("skill-1", "unusable")
        assert result == {"dispatched": 0, "skipped": 0, "failed": 0, "reason": result["reason"]}
        assert "not live" in result["reason"]

    @pytest.mark.asyncio
    async def test_unknown_creator_liveness_skips_tick_without_marking_unusable(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        mock_supabase.set_skill_status = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=None)

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
        ):
            result = await dispatch_skill_schedule("sched-1")

        mock_supabase.set_skill_status.assert_not_awaited()
        assert "could not verify" in result["reason"]

    @pytest.mark.asyncio
    async def test_zero_eligible_entities_skips_the_tick(self):
        # Safety property from agent_worker.py's _reconcile_expert: 0 rows
        # means "Auth DB may be down", not "genuinely zero entities".
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=True)
        fake_auth.get_user_permissions = AsyncMock(return_value=_permissions())

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.experts.entity_fanout.get_eligible_entities",
                new_callable=AsyncMock,
                return_value=[],
            ),
        ):
            result = await dispatch_skill_schedule("sched-1")

        assert "0 eligible" in result["reason"]

    @pytest.mark.asyncio
    async def test_happy_path_dispatches_to_every_eligible_entity(self):
        mock_supabase = MagicMock()
        mock_supabase.get_user_schedule = AsyncMock(return_value=_schedule())
        mock_supabase.get_skill = AsyncMock(return_value=_skill())
        mock_supabase.log_skill_schedule_run = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.is_account_email_live = AsyncMock(return_value=True)
        fake_auth.get_user_permissions = AsyncMock(
            return_value=_permissions(organization_ids=["7"])
        )
        fake_auth.get_organization_from_chat = AsyncMock(return_value="7")

        entities = [{"name": "Grid A"}, {"name": "Grid B"}]

        async def fake_invoke(*_a, **_k):
            return {"final_response": "done", "expert_error": None}

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.experts.entity_fanout.get_eligible_entities",
                new_callable=AsyncMock,
                return_value=entities,
            ),
            patch(
                "orchestrator.experts.entity_fanout.build_anchor_metadata",
                side_effect=lambda _t, e: {
                    "grid_name": e["name"],
                    "telegram_chat_id": f"-100{e['name']}",
                    "telegram_topic_id": None,
                    "organization_id": 7,
                    "organization_name": "Acme",
                },
            ),
            patch(
                "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
                return_value=MagicMock(),
            ),
            patch(
                "orchestrator.graphs.full_conversation_graph.invoke_full_graph",
                side_effect=fake_invoke,
            ),
        ):
            result = await dispatch_skill_schedule("sched-1")

        assert result == {"dispatched": 2, "skipped": 0, "failed": 0, "reason": None}
        assert mock_supabase.log_skill_schedule_run.await_count == 2


class TestDispatchToOneEntity:
    def _anchor_metadata(self, **overrides) -> Dict[str, Any]:
        base = {
            "grid_name": "Example Grid",
            "telegram_chat_id": "-100123",
            "telegram_topic_id": None,
            "organization_id": 7,
            "organization_name": "Acme",
        }
        base.update(overrides)
        return base

    @pytest.mark.asyncio
    async def test_no_chat_id_is_skipped_and_logged(self):
        mock_supabase = MagicMock()
        mock_supabase.log_skill_schedule_run = AsyncMock()

        with patch(
            "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
            return_value=mock_supabase,
        ):
            outcome = await _dispatch_to_one_entity(
                schedule=_schedule(),
                skill=_skill(),
                creator_permissions=_permissions(),
                anchor_metadata=self._anchor_metadata(telegram_chat_id=None),
            )

        assert outcome == "skipped"
        mock_supabase.log_skill_schedule_run.assert_awaited_once()
        assert mock_supabase.log_skill_schedule_run.call_args.args[1] == "skipped"

    @pytest.mark.asyncio
    async def test_org_mismatch_non_staff_creator_is_skipped(self):
        mock_supabase = MagicMock()
        mock_supabase.log_skill_schedule_run = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.get_organization_from_chat = AsyncMock(return_value="99")  # different org

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
        ):
            outcome = await _dispatch_to_one_entity(
                schedule=_schedule(),
                skill=_skill(),
                creator_permissions=_permissions(organization_ids=["7"], is_staff=False),
                anchor_metadata=self._anchor_metadata(),
            )

        assert outcome == "skipped"
        logged_kwargs = mock_supabase.log_skill_schedule_run.call_args
        assert "does not match" in logged_kwargs.kwargs["error_message"]

    @pytest.mark.asyncio
    async def test_staff_creator_proceeds_despite_org_mismatch(self):
        mock_supabase = MagicMock()
        mock_supabase.log_skill_schedule_run = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.get_organization_from_chat = AsyncMock(return_value="99")

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
                return_value=MagicMock(),
            ),
            patch(
                "orchestrator.graphs.full_conversation_graph.invoke_full_graph",
                new_callable=AsyncMock,
                return_value={"final_response": "ok", "expert_error": None},
            ),
        ):
            outcome = await _dispatch_to_one_entity(
                schedule=_schedule(),
                skill=_skill(),
                creator_permissions=_permissions(organization_ids=["7"], is_staff=True),
                anchor_metadata=self._anchor_metadata(),
            )

        assert outcome == "dispatched"

    @pytest.mark.asyncio
    async def test_graph_exception_is_a_failure_with_delivery(self):
        mock_supabase = MagicMock()
        mock_supabase.log_skill_schedule_run = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.get_organization_from_chat = AsyncMock(return_value="7")

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._deliver_failure",
                new_callable=AsyncMock,
            ) as mock_deliver,
        ):
            outcome = await _dispatch_to_one_entity(
                schedule=_schedule(),
                skill=_skill(),
                creator_permissions=_permissions(organization_ids=["7"]),
                anchor_metadata=self._anchor_metadata(),
            )

        assert outcome == "failed"
        mock_deliver.assert_awaited_once()
        assert mock_supabase.log_skill_schedule_run.call_args.args[1] == "failed"

    @pytest.mark.asyncio
    async def test_expert_error_in_final_state_is_a_failure(self):
        mock_supabase = MagicMock()
        mock_supabase.log_skill_schedule_run = AsyncMock()
        fake_auth = MagicMock()
        fake_auth.get_organization_from_chat = AsyncMock(return_value="7")

        with (
            patch(
                "orchestrator.experts.skill_schedule_dispatch.get_supabase_client",
                return_value=mock_supabase,
            ),
            patch("shared.auth.auth_service.get_auth_service", return_value=fake_auth),
            patch(
                "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
                return_value=MagicMock(),
            ),
            patch(
                "orchestrator.graphs.full_conversation_graph.invoke_full_graph",
                new_callable=AsyncMock,
                return_value={"final_response": None, "expert_error": "skill has no steps"},
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch._deliver_failure",
                new_callable=AsyncMock,
            ) as mock_deliver,
        ):
            outcome = await _dispatch_to_one_entity(
                schedule=_schedule(),
                skill=_skill(),
                creator_permissions=_permissions(organization_ids=["7"]),
                anchor_metadata=self._anchor_metadata(),
            )

        assert outcome == "failed"
        mock_deliver.assert_awaited_once()


class TestDeliverFailure:
    @pytest.mark.asyncio
    async def test_staff_facing_chat_gets_the_message_directly(self):
        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "tok"}),
            patch(
                "orchestrator.experts.skill_schedule_dispatch.send_telegram_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            await _deliver_failure(True, "-100123", None, "Find Tickets", "boom")

        mock_send.assert_awaited_once()
        assert mock_send.call_args.args[1] == "-100123"

    @pytest.mark.asyncio
    async def test_non_staff_chat_never_gets_the_message_goes_to_escalation(self):
        with (
            patch.dict(
                "os.environ",
                {"TELEGRAM_BOT_TOKEN": "tok", "ESCALATION_TELEGRAM_CHAT_ID": "-100999"},
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch.send_telegram_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            await _deliver_failure(False, "-100123", None, "Find Tickets", "boom")

        mock_send.assert_awaited_once()
        # Must go to the escalation channel, never the customer chat.
        assert mock_send.call_args.args[1] == "-100999"
        assert mock_send.call_args.args[1] != "-100123"

    @pytest.mark.asyncio
    async def test_no_bot_token_sends_nothing(self):
        with (
            patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}),
            patch(
                "orchestrator.experts.skill_schedule_dispatch.send_telegram_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            await _deliver_failure(True, "-100123", None, "Find Tickets", "boom")

        mock_send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_escalation_chat_configured_sends_nothing_for_non_staff(self):
        with (
            patch.dict(
                "os.environ", {"TELEGRAM_BOT_TOKEN": "tok", "ESCALATION_TELEGRAM_CHAT_ID": ""}
            ),
            patch(
                "orchestrator.experts.skill_schedule_dispatch.send_telegram_message",
                new_callable=AsyncMock,
            ) as mock_send,
        ):
            await _deliver_failure(False, "-100123", None, "Find Tickets", "boom")

        mock_send.assert_not_awaited()
