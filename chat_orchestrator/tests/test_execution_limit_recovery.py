"""Regression tests for graceful orchestration execution-limit recovery."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.graphs.execution_limit_recovery import (
    ExecutionLimitReason,
    format_execution_limit_response,
    graph_recursion_limit,
)
from orchestrator.graphs.full_conversation_graph import FullConversationGraphBuilder
from orchestrator.graphs.nodes.save_history import _determine_message_type
from orchestrator.models.schemas import ConversationMessage, ToolCallResult
from orchestrator.services import webhook_processor


def test_recursion_budget_allows_configured_tool_rounds_to_reach_the_soft_limit():
    assert graph_recursion_limit(20) > 50
    assert graph_recursion_limit(50) >= 130


def test_recovery_message_records_work_and_continuation_instruction():
    response = format_execution_limit_response(
        ExecutionLimitReason.TOOL_BUDGET,
        "- Updated OPS-123\n- Commented on OPS-456",
    )

    assert "**Completed**" in response
    assert "Updated OPS-123" in response
    assert "**Remaining**" in response
    assert "continue" in response.lower()


def test_output_limit_message_does_not_claim_unknown_work_succeeded():
    response = format_execution_limit_response(ExecutionLimitReason.OUTPUT_LIMIT, None)

    assert "response-size limit" in response
    assert "No additional action was confirmed" in response


@pytest.fixture
def builder():
    gemini = MagicMock()
    settings = SimpleNamespace(
        allow_parallel_calls=False,
        max_tool_rounds=2,
        gemini=SimpleNamespace(
            candidate_count=1,
            top_k=40,
            top_p=0.95,
            max_output_tokens=8192,
            get_effective_temperature=lambda: None,
        ),
    )
    return ConversationGraphBuilder(
        gemini_client=gemini,
        registry=MagicMock(),
        executor=MagicMock(),
        settings=settings,
    )


@pytest.mark.asyncio
async def test_tool_budget_synthesizes_without_tools_and_marks_terminal_recovery(builder):
    builder._gemini.generate_messages = AsyncMock(
        return_value=GeminiTurnResult(
            text="- Closed OPS-123",
            tool_calls=[],
            finish_reason="STOP",
            input_tokens=0,
            output_tokens=0,
            raw_response={},
        )
    )
    state = {
        "current_round": 2,
        "max_rounds": 2,
        "llm_messages": [ConversationMessage(role="user", content="close stale tickets")],
        "history_messages": [],
        "accumulated_tool_results": [
            ToolCallResult(name="jira_change_status", success=True, output={})
        ],
        "pending_tool_calls": [],
        "system_instructions": None,
    }

    result = await builder._call_gemini_node(state)

    assert result["graceful_limit_recovery"] is True
    assert result["execution_limit_reason"] == "tool_budget"
    assert "Closed OPS-123" in result["final_response"]
    assert builder._gemini.generate_messages.call_args.kwargs["tools_payload"] is None


def test_graceful_recovery_bypasses_response_verification():
    full_builder = FullConversationGraphBuilder.__new__(FullConversationGraphBuilder)

    assert full_builder._route_after_gemini(
        {
            "graceful_limit_recovery": True,
            "final_response": "summary",
            "verification_enabled": True,
        }
    ) == "safety_check"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("metadata", "message_type"),
    [({}, "interactive"), ({"scheduled_execution": True}, "scheduled")],
)
async def test_limit_recovery_is_persisted_for_regular_and_scheduled_runs(metadata, message_type):
    full_builder = FullConversationGraphBuilder.__new__(FullConversationGraphBuilder)
    recovery_response = format_execution_limit_response(
        ExecutionLimitReason.TOOL_BUDGET,
        "- Updated OPS-123",
    )
    state = {
        "final_response": recovery_response,
        "history_messages": [ConversationMessage(role="user", content="update OPS-123")],
        "gemini_history": [],
        "metadata": metadata,
        "current_round": 2,
        "total_input_tokens": 12,
        "total_output_tokens": 8,
    }

    result = await full_builder._respond_node(state)

    assert result["history_messages"][-1].role == "model"
    assert result["history_messages"][-1].content == recovery_response
    assert _determine_message_type(state) == message_type


@pytest.mark.asyncio
async def test_output_limit_returns_continuation_summary_without_executing_tools(builder):
    builder._gemini.generate_messages = AsyncMock(
        side_effect=[
            GeminiTurnResult(
                text="The ticket review is in progress",
                tool_calls=[],
                finish_reason="MAX_TOKENS",
                input_tokens=0,
                output_tokens=0,
                raw_response={},
            ),
            GeminiTurnResult(
                text="- No completed actions were confirmed.",
                tool_calls=[],
                finish_reason="STOP",
                input_tokens=0,
                output_tokens=0,
                raw_response={},
            ),
        ]
    )
    state = {
        "current_round": 0,
        "max_rounds": 2,
        "llm_messages": [ConversationMessage(role="user", content="summarize ticket status")],
        "history_messages": [],
        "accumulated_tool_results": [],
        "pending_tool_calls": [],
        "system_instructions": None,
        "tools_payload": [{"name": "jira_change_status"}],
    }

    result = await builder._call_gemini_node(state)

    assert result["execution_limit_reason"] == "output_limit"
    assert "**Completed**" in result["final_response"]
    assert builder._gemini.generate_messages.call_args.kwargs["tools_payload"] is None


@pytest.mark.asyncio
async def test_unexpected_graph_recursion_returns_continuation_contract(monkeypatch):
    from langgraph.errors import GraphRecursionError

    user_context = SimpleNamespace(email="user@example.com", organization_id=2, mode="staff")
    monkeypatch.setattr(
        "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
        lambda: object(),
    )
    monkeypatch.setattr(
        "orchestrator.graphs.full_conversation_graph.invoke_full_graph",
        AsyncMock(side_effect=GraphRecursionError("limit")),
    )
    persist_fallback = AsyncMock()
    monkeypatch.setattr(webhook_processor, "_persist_execution_limit_fallback", persist_fallback)

    response, tool_results, markup = await webhook_processor.process_webhook_with_graph(
        "status",
        user_context,
        session_id="scheduled-session",
        metadata={"scheduled_execution": True},
    )

    assert "**Completed**" in response
    assert "continue" in response.lower()
    assert tool_results == []
    assert markup is None
    persist_fallback.assert_awaited_once_with(
        response=response,
        session_id="scheduled-session",
        user_context=user_context,
        metadata={"scheduled_execution": True},
    )
