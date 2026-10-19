"""No verification-failure escalation on top of a real one from the same turn.

2026-10-18 audit: when the model had already escalated successfully this turn
(escalate_to_support) and the LLM judge then failed its hand-off reply twice,
_escalate_node filed a second escalation (reason=verification_failed) for the
same customer message -- 14 such pairs in two months, 4 of them after the
judge's hand-off carve-out (#194) shipped. Support was already handling the
turn, so the second escalation was pure duplication, and the customer got
"Let me check on that and get back to you..." -- a follow-up the bot never
makes. The twice-failed draft must still never reach the customer.
"""

import pytest

from orchestrator.models.schemas import ConversationMessage
from shared.utils.error_messages import ErrorCategory, get_user_message

ESCALATED = get_user_message(ErrorCategory.ESCALATION, "success")
VERIFICATION_FAILED_REPLY = get_user_message(ErrorCategory.ESCALATION, "verification_failed")
FAILED_DRAFT = "I've synced the meter clock and escalated the rest to our support team."


def _builder():
    """_escalate_node touches no constructor state beyond _extract_org_id."""
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    return object.__new__(ConversationGraphBuilder)


def _state(*, escalation_triggered: bool):
    return {
        "user_input": "My meter keeps tripping even though full service is on.",
        "final_response": FAILED_DRAFT,
        "verification_feedback": "Claims a fix the tool output doesn't show.",
        "verification_categories": ["accuracy"],
        "escalation_triggered": escalation_triggered,
        "session_id": "sess-1",
        "user_context": None,
        "metadata": {},
        "history_messages": [ConversationMessage(role="model", content=FAILED_DRAFT)],
    }


@pytest.fixture
def escalation_service(monkeypatch):
    calls = []

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def is_enabled(self):
            return True

        async def escalate_verification_failure(self, **kwargs):
            calls.append(kwargs)
            return {"success": True}

    monkeypatch.setattr("orchestrator.services.escalation_service.EscalationService", _Stub)
    return calls


@pytest.mark.asyncio
async def test_no_verification_escalation_after_a_real_one_this_turn(escalation_service):
    await _builder()._escalate_node(_state(escalation_triggered=True))

    assert escalation_service == []


@pytest.mark.asyncio
async def test_customer_gets_the_escalation_confirmation(escalation_service):
    result = await _builder()._escalate_node(_state(escalation_triggered=True))

    assert result["final_response"] == ESCALATED
    assert result["final_response"] not in (FAILED_DRAFT, VERIFICATION_FAILED_REPLY)


@pytest.mark.asyncio
async def test_turn_still_reports_the_escalation(escalation_service):
    # safety_check reads this to skip its own backup escalation.
    result = await _builder()._escalate_node(_state(escalation_triggered=True))

    assert result["escalation_triggered"] is True


@pytest.mark.asyncio
async def test_transcript_keeps_the_verification_feedback(escalation_service):
    result = await _builder()._escalate_node(_state(escalation_triggered=True))

    metadata = result["history_messages"][-1].metadata
    assert metadata["verification_feedback"] == "Claims a fix the tool output doesn't show."
    assert metadata["escalated"] is True


@pytest.mark.asyncio
async def test_verification_failure_without_a_prior_escalation_still_escalates(
    escalation_service,
):
    result = await _builder()._escalate_node(_state(escalation_triggered=False))

    assert len(escalation_service) == 1
    assert result["final_response"] == VERIFICATION_FAILED_REPLY
