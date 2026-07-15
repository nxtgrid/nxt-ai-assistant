"""Tests for the tool hallucination guard in conversation_graph._execute_tools_node."""

from unittest.mock import MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult


@pytest.fixture
def builder():
    """Create a ConversationGraphBuilder with mocked dependencies."""
    gemini_client = MagicMock()
    settings = MagicMock()
    settings.allow_parallel_calls = False
    settings.max_tool_rounds = 10
    settings.gemini = MagicMock()
    settings.gemini.candidate_count = 1
    settings.gemini.top_k = 40
    settings.gemini.top_p = 0.95
    settings.gemini.max_output_tokens = 8192
    settings.gemini.get_effective_temperature.return_value = None
    registry = MagicMock()
    executor = MagicMock()
    return ConversationGraphBuilder(
        gemini_client=gemini_client,
        registry=registry,
        executor=executor,
        settings=settings,
    )


def _make_state(**overrides):
    """Create a minimal ConversationState dict for testing."""
    base = {
        "pending_tool_calls": [],
        "gemini_history": [],
        "history_messages": [],
        "llm_messages": [],
        "accumulated_tool_calls": [],
        "accumulated_tool_results": [],
        "metadata": {},
        "allowed_tool_names": [],
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_hallucinated_tool_blocked(builder):
    """A tool call not in allowed_tool_names gets blocked with an error result."""
    valid_call = FunctionCall(name="jira_jira_search_issues", arguments={"query": "test"})
    hallucinated_call = FunctionCall(name="nonexistent_tool", arguments={"x": 1})

    state = _make_state(
        pending_tool_calls=[valid_call, hallucinated_call],
        allowed_tool_names=["jira_jira_search_issues", "customer_get_grid_status"],
    )

    valid_result = ToolCallResult(name="jira_jira_search_issues", success=True, output={"ok": True})

    # Mock _execute_tool_calls to only receive the valid call
    async def mock_execute(calls, metadata):
        assert len(calls) == 1
        assert calls[0].name == "jira_jira_search_issues"
        return [valid_result]

    builder._execute_tool_calls = mock_execute

    result = await builder._execute_tools_node(state)

    # Should have 2 results total (1 executed + 1 blocked)
    all_results = result["accumulated_tool_results"]
    assert len(all_results) == 2

    # First result should be the valid execution
    assert all_results[0].name == "jira_jira_search_issues"
    assert all_results[0].success is True

    # Second result should be the blocked hallucination
    assert all_results[1].name == "nonexistent_tool"
    assert all_results[1].success is False
    assert "not available" in all_results[1].output["error"]


@pytest.mark.asyncio
async def test_all_tools_allowed_passes_through(builder):
    """When all tool calls are in allowed_tool_names, all execute normally."""
    call_a = FunctionCall(name="tool_a", arguments={})
    call_b = FunctionCall(name="tool_b", arguments={})

    state = _make_state(
        pending_tool_calls=[call_a, call_b],
        allowed_tool_names=["tool_a", "tool_b", "tool_c"],
    )

    result_a = ToolCallResult(name="tool_a", success=True, output={"r": "a"})
    result_b = ToolCallResult(name="tool_b", success=True, output={"r": "b"})

    async def mock_execute(calls, metadata):
        assert len(calls) == 2
        return [result_a, result_b]

    builder._execute_tool_calls = mock_execute

    result = await builder._execute_tools_node(state)
    all_results = result["accumulated_tool_results"]
    assert len(all_results) == 2
    assert all(r.success for r in all_results)
    assert result["llm_messages"][-4:] == [
        ConversationMessage(role="model", function_call=call_a),
        ConversationMessage(role="model", function_call=call_b),
        ConversationMessage(role="tool", tool_result=result_a),
        ConversationMessage(role="tool", tool_result=result_b),
    ]


@pytest.mark.asyncio
async def test_empty_allowlist_skips_guard(builder):
    """When allowed_tool_names is empty (no tools scenario), guard is skipped."""
    call = FunctionCall(name="any_tool", arguments={})

    state = _make_state(
        pending_tool_calls=[call],
        allowed_tool_names=[],
    )

    expected_result = ToolCallResult(name="any_tool", success=True, output={"ok": True})

    async def mock_execute(calls, metadata):
        assert len(calls) == 1
        return [expected_result]

    builder._execute_tool_calls = mock_execute

    result = await builder._execute_tools_node(state)
    all_results = result["accumulated_tool_results"]
    assert len(all_results) == 1
    assert all_results[0].success is True


@pytest.mark.asyncio
async def test_missing_allowlist_key_skips_guard(builder):
    """Old checkpoints without allowed_tool_names field skip the guard."""
    call = FunctionCall(name="any_tool", arguments={})

    state = _make_state(pending_tool_calls=[call])
    del state["allowed_tool_names"]  # Simulate old checkpoint

    expected_result = ToolCallResult(name="any_tool", success=True, output={"ok": True})

    async def mock_execute(calls, metadata):
        assert len(calls) == 1
        return [expected_result]

    builder._execute_tool_calls = mock_execute

    result = await builder._execute_tools_node(state)
    all_results = result["accumulated_tool_results"]
    assert len(all_results) == 1
    assert all_results[0].success is True


@pytest.mark.asyncio
async def test_result_ordering_preserved(builder):
    """Results appear in the same order as the original function_calls list."""
    # Create calls: valid, hallucinated, valid, hallucinated
    calls = [
        FunctionCall(name="good_1", arguments={}),
        FunctionCall(name="bad_1", arguments={}),
        FunctionCall(name="good_2", arguments={}),
        FunctionCall(name="bad_2", arguments={}),
    ]

    state = _make_state(
        pending_tool_calls=calls,
        allowed_tool_names=["good_1", "good_2"],
    )

    good_result_1 = ToolCallResult(name="good_1", success=True, output={"v": 1})
    good_result_2 = ToolCallResult(name="good_2", success=True, output={"v": 2})

    async def mock_execute(exec_calls, metadata):
        assert len(exec_calls) == 2
        assert exec_calls[0].name == "good_1"
        assert exec_calls[1].name == "good_2"
        return [good_result_1, good_result_2]

    builder._execute_tool_calls = mock_execute

    result = await builder._execute_tools_node(state)
    all_results = result["accumulated_tool_results"]
    assert len(all_results) == 4

    # Check ordering matches original calls
    assert all_results[0].name == "good_1"
    assert all_results[0].success is True
    assert all_results[1].name == "bad_1"
    assert all_results[1].success is False
    assert all_results[2].name == "good_2"
    assert all_results[2].success is True
    assert all_results[3].name == "bad_2"
    assert all_results[3].success is False


@pytest.mark.asyncio
async def test_prepare_node_populates_allowlist(builder):
    """_prepare_node correctly populates allowed_tool_names from tools_payload."""
    tools_payload = [
        {"name": "tool_alpha", "description": "Alpha"},
        {"name": "tool_beta", "description": "Beta"},
    ]

    state = _make_state(
        user_input="hello",
        user_context=MagicMock(
            username="test",
            user_email="test@example.com",
            source="telegram",
            organization_name="TestOrg",
            is_group=False,
            roles=[],
            is_staff=False,
            chat_id="123",
            topic_id=None,
        ),
        tools_payload=tools_payload,
        unlocked_tools=[],
        conversation_history=[],
        context_message=None,
        verification_feedback=None,
        parsed_command=None,
        media=[],
    )

    result = await builder._prepare_node(state)
    assert sorted(result["allowed_tool_names"]) == ["tool_alpha", "tool_beta"]


@pytest.mark.asyncio
async def test_prepare_node_populates_neutral_llm_messages(builder):
    """_prepare_node prepares model-facing messages without Gemini payload shapes."""
    prior = ConversationMessage(role="user", content="previous question")
    state = _make_state(
        user_input="current question",
        user_context=MagicMock(
            username="test",
            user_email="test@example.com",
            source="telegram",
            organization_name="TestOrg",
            is_group=False,
            roles=[],
            is_staff=True,
            chat_id="123",
            topic_id=None,
        ),
        tools_payload=None,
        unlocked_tools=[],
        conversation_history=[prior],
        context_message="retrieved context",
        verification_feedback=None,
        parsed_command=None,
        media=[],
    )

    builder._registry.tools_payload.return_value = None

    result = await builder._prepare_node(state)

    llm_messages = result["llm_messages"]
    assert llm_messages[0] == ConversationMessage(role="user", content="retrieved context")
    assert llm_messages[1] == prior
    assert llm_messages[-1].role == "user"
    assert "[User Context]" in llm_messages[-1].content
    assert "Mode: Staff" in llm_messages[-1].content
    assert "current question" in llm_messages[-1].content


@pytest.mark.asyncio
async def test_prepare_node_empty_tools_empty_allowlist(builder):
    """_prepare_node sets empty allowlist when no tools are available."""
    state = _make_state(
        user_input="hello",
        user_context=MagicMock(
            username="test",
            user_email="test@example.com",
            source="telegram",
            organization_name="TestOrg",
            is_group=False,
            roles=[],
            is_staff=False,
            chat_id="123",
            topic_id=None,
        ),
        tools_payload=None,
        unlocked_tools=[],
        conversation_history=[],
        context_message=None,
        verification_feedback=None,
        parsed_command=None,
        media=[],
    )

    # Mock registry to return None/empty
    builder._registry.tools_payload.return_value = None

    result = await builder._prepare_node(state)
    assert result["allowed_tool_names"] == []


@pytest.mark.asyncio
async def test_call_gemini_node_uses_message_adapter(builder):
    """The graph delegates Gemini payload construction/parsing to GeminiClient."""
    result = GeminiTurnResult(
        text="final answer",
        tool_calls=[],
        finish_reason="STOP",
        input_tokens=13,
        output_tokens=5,
        raw_response={"raw": "response"},
    )
    builder._gemini.generate_messages = MagicMock()
    builder._gemini.generate_messages.side_effect = _async_returning(result)
    builder._gemini.generate_content.side_effect = AssertionError(
        "graph must not use raw generate_content for the main call path"
    )

    state = _make_state(
        current_round=0,
        max_rounds=3,
        llm_messages=[ConversationMessage(role="user", content="hello")],
        tools_payload=[{"name": "tool_a"}],
        system_instructions="system",
        total_input_tokens=0,
        total_output_tokens=0,
        raw_gemini_responses=[],
        accumulated_tool_calls=[],
    )

    node_result = await builder._call_gemini_node(state)

    assert node_result["final_response"] == "final answer"
    assert node_result["raw_gemini_responses"] == [{"raw": "response"}]
    assert node_result["total_input_tokens"] == 13
    assert node_result["total_output_tokens"] == 5
    builder._gemini.generate_messages.assert_called_once_with(
        [ConversationMessage(role="user", content="hello")],
        system_instructions="system",
        tools_payload=[{"name": "tool_a"}],
    )


@pytest.mark.asyncio
async def test_synthesize_partial_answer_uses_message_adapter(builder):
    """Partial synthesis also avoids raw Gemini payload calls."""
    result = GeminiTurnResult(
        text="partial answer",
        tool_calls=[],
        finish_reason="STOP",
        input_tokens=3,
        output_tokens=2,
        raw_response={"raw": "response"},
    )
    builder._gemini.generate_messages = MagicMock()
    builder._gemini.generate_messages.side_effect = _async_returning(result)
    builder._gemini.generate_content.side_effect = AssertionError(
        "partial synthesis must not use raw generate_content"
    )

    answer = await builder._synthesize_partial_answer(
        {
            "llm_messages": [ConversationMessage(role="user", content="hello")],
            "system_instructions": "system",
        }
    )

    assert answer == "partial answer"
    sent_messages = builder._gemini.generate_messages.call_args.args[0]
    assert sent_messages[0] == ConversationMessage(role="user", content="hello")
    assert sent_messages[-1].role == "user"
    assert "tool-call budget" in sent_messages[-1].content
    assert builder._gemini.generate_messages.call_args.kwargs == {
        "system_instructions": "system",
        "tools_payload": None,
    }


def _async_returning(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner
