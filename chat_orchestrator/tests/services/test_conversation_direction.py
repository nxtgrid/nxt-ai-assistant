from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.models.schemas import ConversationMessage
from orchestrator.services.conversation_direction import ConversationDirectionService
from orchestrator.services.thread_assignment import ThreadAssignment


def _msg(content="hello", thread_id=None):
    return ConversationMessage(role="user", content=content, thread_id=thread_id)


@pytest.mark.asyncio
async def test_direction_planner_returns_thread_context_and_expert_route():
    service = ConversationDirectionService()

    with (
        patch(
            "orchestrator.services.conversation_direction.ThreadAssignmentService"
        ) as mock_thread_class,
        patch(
            "orchestrator.services.conversation_direction.route_expert_intent"
        ) as mock_route_intent,
    ):
        mock_thread_service = MagicMock()
        mock_thread_service.assign_thread = AsyncMock(
            return_value=ThreadAssignment(
                thread_id="thr_lpp",
                is_new=True,
                method="llm_new",
                confidence=0.83,
                issue_type=None,
            )
        )
        mock_thread_class.return_value = mock_thread_service
        mock_route_intent.return_value = {
            "command": "/lpp",
            "packet_type": "light_preliminary_package",
            "key_entity": "9.3947551,9.3176320",
            "args": "9.3947551,9.3176320",
        }

        result = await service.plan(
            user_input="Create an LPP at 9.3947551,9.3176320",
            conversation_history=[
                _msg("old shared"),
                _msg("threaded", thread_id="thr_lpp"),
                _msg("other", thread_id="thr_other"),
            ],
            thread_disentanglement_enabled=True,
        )

    assert result.thread_id == "thr_lpp"
    assert result.context_scope == "thread"
    assert [m.content for m in result.thread_filtered_history] == ["old shared", "threaded"]
    assert result.expert_route["command"] == "/lpp"
    assert result.direction == "new_expert_workflow"
    assert result.issue_type == "lpp"


@pytest.mark.asyncio
async def test_direction_planner_uses_session_scope_when_threading_disabled():
    service = ConversationDirectionService()

    with patch(
        "orchestrator.services.conversation_direction.route_expert_intent"
    ) as mock_route_intent:
        mock_route_intent.return_value = None

        result = await service.plan(
            user_input="Hello",
            conversation_history=[_msg("previous", thread_id="thr_existing")],
            thread_disentanglement_enabled=False,
        )

    assert result.thread_id is None
    assert result.thread_filtered_history is None
    assert result.context_scope == "session"
    assert result.direction == "normal_chat"
