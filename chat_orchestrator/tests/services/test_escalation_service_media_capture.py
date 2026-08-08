"""Tests for EscalationService's media-capture wiring at escalation time."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from orchestrator.services.escalation_service import EscalationService


def _make_service() -> EscalationService:
    service = EscalationService(
        escalation_chat_id="-100123",
        bot_token="test-token",
        supabase_url="https://example.supabase.co",
        supabase_key="test-key",
    )
    service._send_telegram_message = AsyncMock(
        return_value={"ok": True, "result": {"message_id": 555}}
    )
    service._get_supabase_client = MagicMock(return_value=MagicMock())
    service._get_raw_client = MagicMock(return_value=MagicMock())
    service.get_escalation_info = AsyncMock(return_value=None)
    service._get_or_create_escalation_topic = AsyncMock(return_value=None)
    return service


@pytest.mark.asyncio
async def test_escalate_to_support_captures_media_when_present() -> None:
    service = _make_service()
    service._record_canonical_escalation = AsyncMock()
    # escalate_to_support pre-generates the escalation's own id (no more
    # legacy save_escalation_mapping call to assign one on write) -- fix it
    # so the capture-call assertion below knows what to expect.
    mapping_id = "11111111-1111-1111-1111-111111111111"
    with (
        patch(
            "orchestrator.services.escalation_service.capture_escalation_media",
            new=AsyncMock(),
        ) as fake_capture,
        patch("orchestrator.services.escalation_service.uuid.uuid4", return_value=uuid.UUID(mapping_id)),
    ):
        result = await service.escalate_to_support(
            question_summary="Meter is sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )

    assert result["success"] is True
    fake_capture.assert_awaited_once()
    call_kwargs = fake_capture.await_args.kwargs
    assert call_kwargs["escalation_id"] == mapping_id
    assert call_kwargs["media_file_ids"] == [{"type": "image", "file_id": "photo1"}]


@pytest.mark.asyncio
async def test_escalate_to_support_skips_capture_when_no_media() -> None:
    service = _make_service()
    service._record_canonical_escalation = AsyncMock()
    with patch(
        "orchestrator.services.escalation_service.capture_escalation_media",
        new=AsyncMock(),
    ) as fake_capture:
        await service.escalate_to_support(
            question_summary="Just a question",
            session_id="telegram_abc",
            customer_chat_id="123",
        )
    fake_capture.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalation_still_succeeds_when_capture_raises() -> None:
    service = _make_service()
    service._record_canonical_escalation = AsyncMock()
    with patch(
        "orchestrator.services.escalation_service.capture_escalation_media",
        new=AsyncMock(side_effect=RuntimeError("storage down")),
    ):
        result = await service.escalate_to_support(
            question_summary="Meter is sparking, see photo",
            session_id="telegram_abc",
            customer_chat_id="123",
            media_file_ids=[{"type": "image", "file_id": "photo1"}],
        )
    assert result["success"] is True
