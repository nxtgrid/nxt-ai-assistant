"""One real escalation per turn -- repeat ``escalate_to_support`` calls are
answered without being re-sent.

2026-09-09 incident: verification failed, the graph regenerated, and the
regenerated model -- which does not carry this turn's tool history -- re-called
``escalate_to_support`` on every retry. Each call posted another follow-up: the
support topic got four messages (one real escalation + three duplicates) for a
single customer issue. The graph now tells ``_execute_tool_calls`` to suppress
repeat escalations once one has succeeded this turn, and a duplicate within a
single batch is suppressed too.
"""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.models.schemas import FunctionCall, ToolCallResult


def _graph() -> ConversationGraphBuilder:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._escalation_handler = None  # force the direct-EscalationService path
    return graph


def _escalation_service_mock() -> MagicMock:
    mock = MagicMock()
    mock.is_enabled.return_value = True
    mock.escalate_to_support = AsyncMock(return_value={"success": True})
    return mock


def _call() -> FunctionCall:
    return FunctionCall(name="escalate_to_support", arguments={"question_summary": "meter fault"})


@pytest.mark.asyncio
async def test_repeat_escalation_is_not_resent_when_one_already_succeeded():
    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        results = await _graph()._execute_tool_calls(
            [_call()], {"session_id": "s1"}, suppress_repeat_escalation=True
        )

    service_cls.return_value.escalate_to_support.assert_not_awaited()
    assert len(results) == 1
    assert results[0].success is True
    assert results[0].output["already_escalated"] is True
    assert "already open" in results[0].output["user_message"]


@pytest.mark.asyncio
async def test_first_escalation_still_sends_normally():
    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        results = await _graph()._execute_tool_calls(
            [_call()], {"session_id": "s1"}, suppress_repeat_escalation=False
        )

    service_cls.return_value.escalate_to_support.assert_awaited_once()
    assert results[0].success is True
    assert "already_escalated" not in results[0].output


@pytest.mark.asyncio
async def test_duplicate_escalation_within_one_batch_is_suppressed():
    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        results = await _graph()._execute_tool_calls(
            [_call(), _call()], {"session_id": "s1"}, suppress_repeat_escalation=False
        )

    # Exactly one real send; the second call is answered synthetically.
    service_cls.return_value.escalate_to_support.assert_awaited_once()
    assert len(results) == 2
    assert results[1].output["already_escalated"] is True


@pytest.mark.asyncio
async def test_execute_tools_node_passes_the_flag_when_already_escalated():
    graph = _graph()
    graph._execute_tool_calls = AsyncMock(
        return_value=[ToolCallResult(name="escalate_to_support", success=True, output={})]
    )
    state: Dict[str, Any] = {
        "pending_tool_calls": [_call()],
        "escalation_triggered": True,
    }

    await graph._execute_tools_node(state)

    assert graph._execute_tool_calls.await_args.kwargs["suppress_repeat_escalation"] is True


@pytest.mark.asyncio
async def test_execute_tools_node_does_not_set_the_flag_on_the_first_escalation():
    graph = _graph()
    graph._execute_tool_calls = AsyncMock(
        return_value=[ToolCallResult(name="escalate_to_support", success=True, output={})]
    )
    state: Dict[str, Any] = {"pending_tool_calls": [_call()]}

    await graph._execute_tools_node(state)

    assert graph._execute_tool_calls.await_args.kwargs["suppress_repeat_escalation"] is False
