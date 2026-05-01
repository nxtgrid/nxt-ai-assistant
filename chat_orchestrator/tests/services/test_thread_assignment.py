"""Tests for ThreadAssignmentService and filter_history_by_thread."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult
from orchestrator.services.thread_assignment import (
    ThreadAssignment,
    ThreadAssignmentService,
    _find_by_telegram_msg_id,
    _get_active_thread_ids,
    _new_thread_id,
    filter_history_by_thread,
    is_thread_disentanglement_enabled,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _msg(
    role="user",
    content="hello",
    thread_id=None,
    telegram_message_id=None,
    reply_to_telegram_message_id=None,
    sender_id=None,
    timestamp=None,
    function_call=None,
    tool_result=None,
):
    return ConversationMessage(
        role=role,
        content=content,
        thread_id=thread_id,
        telegram_message_id=telegram_message_id,
        reply_to_telegram_message_id=reply_to_telegram_message_id,
        sender_id=sender_id,
        timestamp=timestamp,
        function_call=function_call,
        tool_result=tool_result,
    )


def _fc(name="get_grid_status"):
    return FunctionCall(name=name, arguments={"grid": "ExampleGrid"})


def _tr(name="get_grid_status"):
    return ToolCallResult(name=name, success=True, output="OK")


# ---------------------------------------------------------------------------
# _new_thread_id
# ---------------------------------------------------------------------------


class TestNewThreadId:
    def test_format(self):
        tid = _new_thread_id()
        assert tid.startswith("thr_")
        assert len(tid) == 16  # "thr_" + 12 hex chars

    def test_uniqueness(self):
        ids = {_new_thread_id() for _ in range(100)}
        assert len(ids) == 100


# ---------------------------------------------------------------------------
# _find_by_telegram_msg_id
# ---------------------------------------------------------------------------


class TestFindByTelegramMsgId:
    def test_found(self):
        history = [
            _msg(telegram_message_id=100, thread_id="thr_aaa"),
            _msg(telegram_message_id=200, thread_id="thr_bbb"),
        ]
        result = _find_by_telegram_msg_id(history, 200)
        assert result is not None
        assert result.thread_id == "thr_bbb"

    def test_not_found(self):
        history = [_msg(telegram_message_id=100)]
        result = _find_by_telegram_msg_id(history, 999)
        assert result is None

    def test_prefers_last_occurrence(self):
        history = [
            _msg(telegram_message_id=100, thread_id="thr_old"),
            _msg(telegram_message_id=100, thread_id="thr_new"),
        ]
        result = _find_by_telegram_msg_id(history, 100)
        assert result.thread_id == "thr_new"


# ---------------------------------------------------------------------------
# _get_active_thread_ids
# ---------------------------------------------------------------------------


class TestGetActiveThreadIds:
    def test_empty_history(self):
        assert _get_active_thread_ids([]) == []

    def test_ignores_null_thread_id(self):
        history = [_msg(content="old", thread_id=None)]
        assert _get_active_thread_ids(history) == []

    def test_returns_distinct_threads(self):
        # Without timestamps, all messages are included (timestamp parsing fails gracefully)
        history = [
            _msg(thread_id="thr_aaa"),
            _msg(thread_id="thr_bbb"),
            _msg(thread_id="thr_aaa"),
        ]
        result = _get_active_thread_ids(history)
        # Most recent first (reversed iteration), deduped
        assert result == ["thr_aaa", "thr_bbb"]


# ---------------------------------------------------------------------------
# filter_history_by_thread
# ---------------------------------------------------------------------------


class TestFilterHistoryByThread:
    def test_empty_thread_id_returns_all(self):
        history = [_msg(content="a"), _msg(content="b")]
        result = filter_history_by_thread(history, "")
        assert len(result) == 2

    def test_matches_thread_and_null(self):
        history = [
            _msg(content="old", thread_id=None),
            _msg(content="thread_a", thread_id="thr_aaa"),
            _msg(content="thread_b", thread_id="thr_bbb"),
        ]
        result = filter_history_by_thread(history, "thr_aaa")
        assert len(result) == 2
        assert result[0].content == "old"
        assert result[1].content == "thread_a"

    def test_null_thread_id_included_in_all_threads(self):
        history = [
            _msg(content="shared", thread_id=None),
            _msg(content="a1", thread_id="thr_aaa"),
            _msg(content="b1", thread_id="thr_bbb"),
        ]
        # Both threads include the shared (NULL) message
        result_a = filter_history_by_thread(history, "thr_aaa")
        result_b = filter_history_by_thread(history, "thr_bbb")
        assert any(m.content == "shared" for m in result_a)
        assert any(m.content == "shared" for m in result_b)

    def test_tool_pairs_preserved(self):
        """If a function_call is in-thread, its tool_result stays too."""
        history = [
            _msg(content="user asks", thread_id="thr_aaa"),
            _msg(role="model", function_call=_fc(), thread_id="thr_aaa"),
            _msg(role="tool", tool_result=_tr(), thread_id="thr_bbb"),  # Wrong thread
            _msg(content="next", thread_id="thr_aaa"),
        ]
        result = filter_history_by_thread(history, "thr_aaa")
        # The tool_result at index 2 should be pulled in because the function_call at 1 is included
        assert len(result) == 4  # all included due to pair enforcement

    def test_tool_pairs_reverse_direction(self):
        """If a tool_result is in-thread, its function_call stays too."""
        history = [
            _msg(role="model", function_call=_fc(), thread_id="thr_bbb"),  # Wrong thread
            _msg(role="tool", tool_result=_tr(), thread_id=None),  # NULL = included
        ]
        result = filter_history_by_thread(history, "thr_aaa")
        # tool_result (NULL) is included, so function_call at index 0 should be pulled in
        assert len(result) == 2


# ---------------------------------------------------------------------------
# ThreadAssignmentService — Path A
# ---------------------------------------------------------------------------


class TestPathADeterministic:
    @pytest.mark.asyncio
    async def test_reply_chain(self):
        """Reply-to → follow parent's thread."""
        history = [
            _msg(telegram_message_id=100, thread_id="thr_parent"),
        ]
        service = ThreadAssignmentService()
        result = await service.assign_thread(
            user_input="yes",
            conversation_history=history,
            reply_to_telegram_message_id=100,
        )
        assert result is not None
        assert result.thread_id == "thr_parent"
        assert result.method == "reply_chain"

    @pytest.mark.asyncio
    async def test_slash_command_new_thread(self):
        """Slash commands always get a new thread."""
        service = ThreadAssignmentService()
        result = await service.assign_thread(
            user_input="/grid ExampleGrid",
            conversation_history=[],
        )
        assert result is not None
        assert result.is_new is True
        assert result.method == "command"
        assert result.thread_id.startswith("thr_")

    @pytest.mark.asyncio
    async def test_active_expert_workflow(self):
        """Active expert packet → use its thread."""
        service = ThreadAssignmentService()
        result = await service.assign_thread(
            user_input="proceed",
            conversation_history=[],
            active_work_packet={"state": {"thread_id": "thr_expert_abc"}},
        )
        assert result is not None
        assert result.thread_id == "thr_expert_abc"
        assert result.method == "active_expert"

    @pytest.mark.asyncio
    async def test_zero_active_threads_new_thread(self):
        """No active threads → new thread."""
        service = ThreadAssignmentService()
        result = await service.assign_thread(
            user_input="hello",
            conversation_history=[],
        )
        assert result is not None
        assert result.is_new is True
        assert result.method == "first_message"

    @pytest.mark.asyncio
    async def test_single_active_thread(self):
        """One active thread → assign to it."""
        history = [_msg(thread_id="thr_only_one")]
        service = ThreadAssignmentService()
        result = await service.assign_thread(
            user_input="what's the status?",
            conversation_history=history,
        )
        assert result is not None
        assert result.thread_id == "thr_only_one"
        assert result.method == "single_active"


# ---------------------------------------------------------------------------
# ThreadAssignmentService — Path B (mocked LLM)
# ---------------------------------------------------------------------------


class TestPathBLLM:
    @pytest.mark.asyncio
    async def test_llm_continues_existing_thread(self):
        """LLM identifies the correct thread."""
        history = [
            _msg(thread_id="thr_aaa", content="grid status"),
            _msg(thread_id="thr_bbb", content="jira ticket"),
        ]
        llm_result = ThreadAssignment(thread_id="thr_aaa", method="llm", confidence=0.9)
        service = ThreadAssignmentService()
        with patch.object(service, "_classify_with_llm", return_value=llm_result):
            result = await service.assign_thread(
                user_input="how is ExampleGrid doing?",
                conversation_history=history,
            )

        assert result is not None
        assert result.thread_id == "thr_aaa"
        assert result.method == "llm"
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_llm_low_confidence_new_thread(self):
        """Low LLM confidence → service returns new thread."""
        history = [
            _msg(thread_id="thr_aaa"),
            _msg(thread_id="thr_bbb"),
        ]
        # Simulate LLM returning a new thread due to low confidence
        llm_result = ThreadAssignment(
            thread_id="thr_new123456", is_new=True, method="llm_new", confidence=0.3
        )
        service = ThreadAssignmentService()
        with patch.object(service, "_classify_with_llm", return_value=llm_result):
            result = await service.assign_thread(
                user_input="something random",
                conversation_history=history,
            )

        assert result is not None
        assert result.is_new is True
        assert result.method == "llm_new"

    @pytest.mark.asyncio
    async def test_llm_returns_new(self):
        """LLM returns NEW → new thread."""
        history = [
            _msg(thread_id="thr_aaa"),
            _msg(thread_id="thr_bbb"),
        ]
        llm_result = ThreadAssignment(
            thread_id="thr_brand_new12", is_new=True, method="llm_new", confidence=0.8
        )
        service = ThreadAssignmentService()
        with patch.object(service, "_classify_with_llm", return_value=llm_result):
            result = await service.assign_thread(
                user_input="completely unrelated",
                conversation_history=history,
            )

        assert result is not None
        assert result.is_new is True

    @pytest.mark.asyncio
    async def test_llm_error_creates_new_thread(self):
        """LLM error → _assign raises → fail-open returns None."""
        history = [
            _msg(thread_id="thr_aaa"),
            _msg(thread_id="thr_bbb"),
        ]
        service = ThreadAssignmentService()
        with patch.object(service, "_assign", side_effect=RuntimeError("LLM boom")):
            result = await service.assign_thread(
                user_input="hello",
                conversation_history=history,
            )
        assert result is None  # fail-open


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


class TestFailOpen:
    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        """Any internal exception → returns None (fail-open)."""
        service = ThreadAssignmentService()

        # Patch _assign to raise
        with patch.object(service, "_assign", side_effect=RuntimeError("boom")):
            result = await service.assign_thread(
                user_input="hello",
                conversation_history=[],
            )
        assert result is None


# ---------------------------------------------------------------------------
# Feature flag
# ---------------------------------------------------------------------------


class TestFeatureFlag:
    def test_disabled_by_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert is_thread_disentanglement_enabled() is False

    def test_enabled(self):
        with patch.dict("os.environ", {"THREAD_DISENTANGLEMENT_ENABLED": "true"}):
            assert is_thread_disentanglement_enabled() is True

    def test_case_insensitive(self):
        with patch.dict("os.environ", {"THREAD_DISENTANGLEMENT_ENABLED": "True"}):
            assert is_thread_disentanglement_enabled() is True


# ---------------------------------------------------------------------------
# assign_thread node
# ---------------------------------------------------------------------------


class TestAssignThreadNode:
    @pytest.mark.asyncio
    async def test_disabled_returns_empty(self):
        """When feature flag is off, node is pass-through."""
        from orchestrator.graphs.nodes.assign_thread import assign_thread

        with patch.dict("os.environ", {"THREAD_DISENTANGLEMENT_ENABLED": "false"}):
            result = await assign_thread({"user_input": "hello"})
        assert result == {}

    @pytest.mark.asyncio
    async def test_enabled_assigns_thread(self):
        """When enabled, node assigns thread and filters history."""
        from orchestrator.graphs.nodes.assign_thread import assign_thread

        assignment = ThreadAssignment(thread_id="thr_test123", is_new=True, method="first_message")

        with patch.dict("os.environ", {"THREAD_DISENTANGLEMENT_ENABLED": "true"}):
            with patch(
                "orchestrator.graphs.nodes.assign_thread.ThreadAssignmentService"
            ) as MockService:
                instance = MockService.return_value
                instance.assign_thread = AsyncMock(return_value=assignment)

                result = await assign_thread(
                    {
                        "user_input": "hello",
                        "conversation_history": [_msg(content="old", thread_id=None)],
                    }
                )

        assert result["thread_id"] == "thr_test123"
        assert result["thread_filtered_history"] is not None
        assert len(result["thread_filtered_history"]) == 1  # NULL thread_id included
