"""The escalation node must not promise an escalation that did not happen.

2026-08-27 incident: a customer asking for grid status got "Let me check on
that and get back to you. I've notified our support team who will respond
shortly." _escalate_node emitted that text unconditionally -- it computed
``result["success"]`` from the escalation attempt, stored it on
``escalation_triggered``, and then ignored it when choosing the customer-facing
message. With ESCALATION_TELEGRAM_CHAT_ID unset, the bot token missing, or the
Telegram send failing, the customer was told support had been notified when no
escalation existed and nobody was coming.

This is the same defect class 47009cf2 fixed in safety_check ("a promise that
support had been notified. No escalation was created"), in a different node.
"""

import pytest

from orchestrator.models.schemas import ConversationMessage
from shared.utils.error_messages import ErrorCategory, get_user_message

PROMISE = get_user_message(ErrorCategory.ESCALATION, "verification_failed")
NO_PROMISE = get_user_message(ErrorCategory.ESCALATION, "failed")


def _builder():
    """_escalate_node touches no constructor state beyond _extract_org_id."""
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    return object.__new__(ConversationGraphBuilder)


def _state():
    return {
        "user_input": "what is the current status of gridv",
        "final_response": "GridV is currently operating in HPS mode.",
        "verification_feedback": "Claims are not supported by the tool output.",
        "verification_categories": ["accuracy"],
        "session_id": "sess-gridv-1",
        "user_context": None,
        "metadata": {},
        "history_messages": [ConversationMessage(role="model", content="GridV is...")],
    }


def _stub_escalation_service(monkeypatch, *, enabled=True, result=None, raises=None):
    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def is_enabled(self):
            return enabled

        async def escalate_verification_failure(self, **kwargs):
            if raises is not None:
                raise raises
            return result

    monkeypatch.setattr("orchestrator.services.escalation_service.EscalationService", _Stub)


@pytest.mark.asyncio
async def test_no_promise_when_the_escalation_service_is_not_configured(monkeypatch):
    """is_enabled() False -- nothing was sent, so promise nothing."""
    _stub_escalation_service(monkeypatch, enabled=False)

    result = await _builder()._escalate_node(_state())

    assert result["final_response"] == NO_PROMISE
    assert result["final_response"] != PROMISE
    assert result["escalation_triggered"] is False


@pytest.mark.asyncio
async def test_no_promise_when_the_telegram_send_fails(monkeypatch):
    """The service is configured but the send failed -- still nobody notified."""
    _stub_escalation_service(
        monkeypatch,
        enabled=True,
        result={"success": False, "error": "Failed to escalate: chat not found"},
    )

    result = await _builder()._escalate_node(_state())

    assert result["final_response"] == NO_PROMISE
    assert result["escalation_triggered"] is False


@pytest.mark.asyncio
async def test_no_promise_when_the_escalation_raises(monkeypatch):
    _stub_escalation_service(monkeypatch, raises=RuntimeError("supabase unreachable"))

    result = await _builder()._escalate_node(_state())

    assert result["final_response"] == NO_PROMISE
    assert result["escalation_triggered"] is False


@pytest.mark.asyncio
async def test_promise_is_kept_when_the_escalation_actually_succeeds(monkeypatch):
    """The fix must not over-correct: a real escalation still promises support."""
    _stub_escalation_service(
        monkeypatch,
        enabled=True,
        result={"success": True, "escalation_message_id": 4242, "is_escalated": True},
    )

    result = await _builder()._escalate_node(_state())

    assert result["final_response"] == PROMISE
    assert result["escalation_triggered"] is True


@pytest.mark.asyncio
async def test_transcript_does_not_record_an_escalation_that_did_not_happen(monkeypatch):
    """metadata['escalated'] is read back as history; it must not lie either."""
    _stub_escalation_service(monkeypatch, enabled=False)
    state = _state()

    result = await _builder()._escalate_node(state)

    assert result["history_messages"][-1].metadata["escalated"] is False


@pytest.mark.asyncio
async def test_transcript_records_a_real_escalation(monkeypatch):
    _stub_escalation_service(monkeypatch, result={"success": True})
    state = _state()

    result = await _builder()._escalate_node(state)

    assert result["history_messages"][-1].metadata["escalated"] is True
    assert (
        result["history_messages"][-1].metadata["verification_feedback"]
        == "Claims are not supported by the tool output."
    )
