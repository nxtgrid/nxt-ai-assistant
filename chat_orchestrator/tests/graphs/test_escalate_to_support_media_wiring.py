"""Verifies every escalate_to_support call site forwards media_file_ids
extracted from the turn's metadata."""

from __future__ import annotations

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.graphs.conversation_graph import ConversationGraphBuilder


def _escalation_service_mock() -> MagicMock:
    mock = MagicMock()
    mock.is_enabled.return_value = True
    mock.escalate_to_support = AsyncMock(return_value={"success": True})
    return mock


@pytest.mark.asyncio
async def test_direct_tool_call_path_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._escalation_handler = None  # forces the direct-EscalationService path
    from orchestrator.models.schemas import FunctionCall

    call = FunctionCall(
        name="escalate_to_support",
        arguments={"question_summary": "Help, see photo"},
    )
    metadata = {"photo_file_id": "abc123", "session_id": "telegram_1"}

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._execute_tool_calls([call], metadata)

    escalation_service = service_cls.return_value
    escalation_service.escalate_to_support.assert_awaited_once()
    kwargs = escalation_service.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "image", "file_id": "abc123"}]


@pytest.mark.asyncio
async def test_content_block_escalation_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._extract_org_id = MagicMock(return_value=None)
    state: Dict[str, Any] = {
        "session_id": "telegram_1",
        "user_context": None,
        "metadata": {"video_file_id": "vid123"},
        "user_input": "hello",
    }

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._escalate_for_blocked_content(state, "SAFETY", "hello")

    kwargs = service_cls.return_value.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "video", "file_id": "vid123"}]


@pytest.mark.asyncio
async def test_loop_escalation_forwards_media_file_ids() -> None:
    graph = ConversationGraphBuilder.__new__(ConversationGraphBuilder)
    graph._extract_org_id = MagicMock(return_value=None)
    state: Dict[str, Any] = {
        "session_id": "telegram_1",
        "user_context": None,
        "metadata": {"audio_file_id": "aud123"},
        "user_input": "hello",
    }
    loop_result = MagicMock(consecutive_similar_turns=3)

    with patch(
        "orchestrator.services.escalation_service.EscalationService",
        return_value=_escalation_service_mock(),
    ) as service_cls:
        await graph._escalate_for_loop(state, loop_result)

    kwargs = service_cls.return_value.escalate_to_support.await_args.kwargs
    assert kwargs["media_file_ids"] == [{"type": "audio", "file_id": "aud123"}]
