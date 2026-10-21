"""A rejected tool call is answered, and repeated rejections end the turn.

2026-10-19: the model called a tool name that was not offered to it. The tools
node logged "terminating to avoid recursion limit" but returned without
answering the call, and the router always sends the graph back to the model, so
the model saw its own call unanswered and issued it again: 19 times, until the
tool budget ran out and the budget summary claimed an escalation that never
happened. (When a rejected call shares a batch with a valid one it does get an
error answer, and the model corrected itself on the next round -- the loop only
ever ran on all-rejected batches.)
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import FunctionCall, ToolCallResult
from shared.utils.error_messages import ErrorCategory, get_user_message

ALLOWED = [
    "customer_customer_get_grid_status",
    "customer_meter_information",
    "jira_search_issues",
]
INTERNAL_ERROR = get_user_message(ErrorCategory.SYSTEM, "internal_error")


def _builder():
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    return object.__new__(ConversationGraphBuilder)


def _state(calls, **extra):
    return {
        "pending_tool_calls": calls,
        "allowed_tool_names": ALLOWED,
        "history_messages": [],
        "llm_messages": [],
        "accumulated_tool_calls": [],
        "accumulated_tool_results": [],
        "metadata": {},
        **extra,
    }


def _bad(name="no_such_tool", **extra):
    return FunctionCall(name=name, arguments={}, **extra)


@pytest.mark.asyncio
async def test_the_first_rejected_round_answers_the_call_and_goes_back_to_the_model():
    result = await _builder()._execute_tools_node(_state([_bad(tool_call_id="call-1")]))

    answer = result["llm_messages"][-1]
    assert answer.role == "tool"
    assert answer.tool_result.success is False
    assert answer.tool_result.tool_call_id == "call-1"
    assert "not available" in answer.tool_result.output["error"]
    assert result["history_messages"][-1] == answer
    # The turn continues: the model gets to read the error and correct itself.
    assert not result.get("tool_loop_aborted")
    assert "final_response" not in result
    assert result["pending_tool_calls"] == []
    assert result["rejected_tool_rounds"] == 1


@pytest.mark.asyncio
async def test_calls_and_results_stay_aligned_after_a_rejected_round():
    result = await _builder()._execute_tools_node(_state([_bad("a_tool"), _bad("b_tool")]))

    assert [c.name for c in result["accumulated_tool_calls"]] == ["a_tool", "b_tool"]
    assert [r.name for r in result["accumulated_tool_results"]] == ["a_tool", "b_tool"]


@pytest.mark.asyncio
async def test_the_error_names_the_closest_available_tool():
    result = await _builder()._execute_tools_node(_state([_bad("customer_customer_get_grid_statuss")]))

    message = result["accumulated_tool_results"][0].output["error"]
    assert "Closest available tools: customer_customer_get_grid_status" in message


@pytest.mark.asyncio
async def test_the_error_suggests_nothing_when_nothing_is_close():
    result = await _builder()._execute_tools_node(_state([_bad("zzz_qqq")]))

    message = result["accumulated_tool_results"][0].output["error"]
    assert "not available" in message
    assert "Closest" not in message


@pytest.mark.asyncio
async def test_a_second_rejected_round_in_a_row_ends_the_turn():
    builder = _builder()
    builder._make_escalation_offer_markup = lambda state: {"inline_keyboard": [["Escalate"]]}

    result = await builder._execute_tools_node(_state([_bad()], rejected_tool_rounds=1))

    assert result["tool_loop_aborted"] is True
    assert result["should_continue"] is False
    assert result["pending_tool_calls"] == []
    assert result["final_response"] == INTERNAL_ERROR
    assert result["error_category"] == ErrorCategory.SYSTEM.value
    assert result["reply_markup"] == {"inline_keyboard": [["Escalate"]]}
    # Even the round that ends the turn answers its call.
    assert result["llm_messages"][-1].role == "tool"


@pytest.mark.asyncio
async def test_a_round_that_runs_a_tool_resets_the_count():
    builder = _builder()
    builder._execute_tool_calls = AsyncMock(
        return_value=[ToolCallResult(name="customer_meter_information", success=True, output="ok")]
    )

    result = await builder._execute_tools_node(
        _state([FunctionCall(name="customer_meter_information", arguments={})], rejected_tool_rounds=1)
    )

    assert result["rejected_tool_rounds"] == 0
    assert not result.get("tool_loop_aborted")


@pytest.mark.asyncio
async def test_a_mixed_round_counts_as_progress_not_as_a_rejection():
    builder = _builder()
    builder._execute_tool_calls = AsyncMock(
        return_value=[ToolCallResult(name="customer_meter_information", success=True, output="ok")]
    )
    calls = [FunctionCall(name="customer_meter_information", arguments={}), _bad()]

    result = await builder._execute_tools_node(_state(calls, rejected_tool_rounds=1))

    assert result["rejected_tool_rounds"] == 0
    assert not result.get("tool_loop_aborted")
    assert [r.success for r in result["accumulated_tool_results"]] == [True, False]


def test_the_router_ends_the_turn_after_an_abort():
    from orchestrator.graphs.full_conversation_graph import FullConversationGraphBuilder

    router = object.__new__(FullConversationGraphBuilder)

    assert router._route_after_tools({"tool_loop_aborted": True}) == "safety_check"
    assert router._route_after_tools({}) == "call_gemini"
    assert router._route_after_tools({"nl_expert_reroute": True}) == "expert_router"


def test_the_real_graph_can_route_from_the_tools_node_to_the_safety_check():
    from orchestrator.graphs.full_conversation_graph import FullConversationGraphBuilder

    graph = FullConversationGraphBuilder().build().get_graph()

    targets = {edge.target for edge in graph.edges if edge.source == "execute_tools"}
    assert {"call_gemini", "expert_router", "safety_check"} <= targets


@pytest.mark.asyncio
async def test_a_model_that_keeps_calling_a_missing_tool_is_stopped_after_two_rounds():
    """The real tools node and the real router, with a model that never learns."""
    from langgraph.graph import END, START, StateGraph

    from orchestrator.graphs.full_conversation_graph import FullConversationGraphBuilder
    from orchestrator.graphs.state import ConversationState

    builder = _builder()
    builder._make_escalation_offer_markup = lambda state: None
    router = object.__new__(FullConversationGraphBuilder)
    model_calls = []

    async def always_calls_a_missing_tool(state):
        model_calls.append(state.get("current_round", 0))
        return {
            "pending_tool_calls": [_bad()],
            "current_round": state.get("current_round", 0) + 1,
        }

    async def safety_check(state):
        return {}

    graph = StateGraph(ConversationState)
    graph.add_node("call_gemini", always_calls_a_missing_tool)
    graph.add_node("execute_tools", builder._execute_tools_node)
    graph.add_node("safety_check", safety_check)
    graph.add_edge(START, "call_gemini")
    graph.add_edge("call_gemini", "execute_tools")
    graph.add_conditional_edges(
        "execute_tools",
        router._route_after_tools,
        {"call_gemini": "call_gemini", "expert_router": END, "safety_check": "safety_check"},
    )
    graph.add_edge("safety_check", END)

    final = await graph.compile().ainvoke(_state([]), config={"recursion_limit": 40})

    assert len(model_calls) == 2
    assert final["final_response"] == INTERNAL_ERROR
    assert final["tool_loop_aborted"] is True


@pytest.mark.asyncio
async def test_a_text_answer_resets_the_count():
    """"In a row" means in a row: a later regeneration must not inherit a stale count."""
    from unittest.mock import MagicMock

    from orchestrator.clients.gemini import GeminiTurnResult
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
    from orchestrator.models.schemas import ConversationMessage

    settings = MagicMock()
    settings.max_tool_rounds = 10
    settings.gemini.get_effective_temperature.return_value = None
    builder = ConversationGraphBuilder(
        gemini_client=MagicMock(), registry=MagicMock(), executor=MagicMock(), settings=settings
    )
    builder._gemini.generate_messages = AsyncMock(
        return_value=GeminiTurnResult(
            text="Here you go.",
            tool_calls=[],
            finish_reason="STOP",
            input_tokens=1,
            output_tokens=1,
            raw_response={},
        )
    )
    state = {
        "current_round": 1,
        "max_rounds": 5,
        "llm_messages": [ConversationMessage(role="user", content="hello")],
        "tools_payload": [{"name": "tool_a"}],
        "system_instructions": "system",
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "raw_gemini_responses": [],
        "accumulated_tool_calls": [],
        "rejected_tool_rounds": 1,
    }

    result = await builder._call_gemini_node(state)

    assert result["final_response"] == "Here you go."
    assert result["rejected_tool_rounds"] == 0
