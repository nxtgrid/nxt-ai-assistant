"""A session with an open escalation keeps getting answers.

2026-10-18: while a session had an open escalation, check_escalation forwarded
every new message into the escalation's support thread and sent back only
"Your message has been forwarded to the support team." -- the bot stopped
answering that chat until a human closed the escalation (median 5h, sometimes
days). Staff saw a grid manager's routine status questions pile up in the
support group, including one the bot had answered minutes before the
escalation. Follow-ups still go into the open thread; the bot now answers too.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.graphs.full_conversation_graph import FullConversationGraphBuilder

# orchestrator.graphs.nodes re-exports the check_escalation function under the
# submodule's name, shadowing it -- import the module itself.
check_escalation = importlib.import_module(
    "orchestrator.graphs.nodes.check_escalation"
).check_escalation

FORWARD_NOTICE = "Your message has been forwarded to the support team."


def _state(**overrides):
    base = {
        "session_id": "session-123",
        "user_input": "What is the battery percentage right now?",
        "user_context": None,
        "metadata": {},
    }
    base.update(overrides)
    return base


def _escalation_service(*, escalated: bool, forward_ok: bool = True) -> MagicMock:
    service = MagicMock()
    service.is_session_escalated = AsyncMock(return_value=escalated)
    service.forward_customer_message = AsyncMock(
        return_value={"success": True, "message": FORWARD_NOTICE}
        if forward_ok
        else {"success": False, "error": "Telegram send failed"}
    )
    return service


async def _run_check(service: MagicMock, **state_overrides):
    with patch(
        "orchestrator.services.escalation_service.EscalationService", return_value=service
    ):
        return await check_escalation(_state(**state_overrides))


@pytest.mark.asyncio
async def test_message_still_goes_into_the_open_escalation_thread():
    service = _escalation_service(escalated=True)

    result = await _run_check(service)

    service.forward_customer_message.assert_awaited_once()
    kwargs = service.forward_customer_message.await_args.kwargs
    assert kwargs["session_id"] == "session-123"
    assert kwargs["customer_message"] == "What is the battery percentage right now?"
    assert result["is_escalated_session"] is True
    assert result["escalation_followup_forwarded"] is True


@pytest.mark.asyncio
async def test_forwarding_does_not_end_the_turn():
    result = await _run_check(_escalation_service(escalated=True))

    assert "final_response" not in result
    assert result.get("should_continue", True) is not False


@pytest.mark.asyncio
async def test_failed_forward_is_not_reported_as_forwarded():
    result = await _run_check(_escalation_service(escalated=True, forward_ok=False))

    assert result["is_escalated_session"] is True
    assert result["escalation_followup_forwarded"] is False


@pytest.mark.asyncio
async def test_no_open_escalation_forwards_nothing():
    service = _escalation_service(escalated=False)

    result = await _run_check(service)

    service.forward_customer_message.assert_not_awaited()
    assert result["is_escalated_session"] is False
    assert result["escalation_followup_forwarded"] is False


@pytest.mark.asyncio
async def test_user_gets_the_bot_answer_not_a_forward_notice():
    forwarded = await _run_check(_escalation_service(escalated=True))
    state = {
        **_state(),
        **forwarded,
        "final_response": "The battery is at 64%.",
        "history_messages": [],
    }

    responded = await FullConversationGraphBuilder.__new__(
        FullConversationGraphBuilder
    )._respond_node(state)

    assert responded["final_response"] == "The battery is at 64%."
    assert responded["history_messages"][-1].content == "The battery is at 64%."


def test_graph_always_continues_past_the_escalation_check():
    graph = FullConversationGraphBuilder().build().get_graph()

    targets = {edge.target for edge in graph.edges if edge.source == "check_escalation"}

    assert targets == {"prepare_media"}


def test_model_is_told_the_message_already_reached_support():
    builder = ConversationGraphBuilder.__new__(ConversationGraphBuilder)

    message = builder._build_user_message(_state(escalation_followup_forwarded=True))

    assert "[Open Escalation]" in message.content
    assert message.content.endswith("What is the battery percentage right now?")


def test_no_open_escalation_note_when_nothing_was_forwarded():
    builder = ConversationGraphBuilder.__new__(ConversationGraphBuilder)

    message = builder._build_user_message(_state(escalation_followup_forwarded=False))

    assert "[Open Escalation]" not in message.content
