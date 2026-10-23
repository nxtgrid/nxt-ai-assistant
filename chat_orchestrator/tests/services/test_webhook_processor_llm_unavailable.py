"""A provider billing block reaches the customer as a temporary issue, never as
billing, and never with a promise of an escalation that did not happen.

When the LLM provider refuses service over billing (HTTP 402) every turn fails,
so a live customer message is escalated to support instead of dropped. Only live
customer messages: not staff (they are support), not button callbacks, not
scheduled runs. The customer is told it was escalated only when the escalation
actually exists -- the same promise-honesty rule _escalate_node follows.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orchestrator.services import webhook_processor
from shared.utils.error_messages import ErrorCategory, get_user_message

GEMINI_402 = RuntimeError(
    "402 RESOURCE_EXHAUSTED. {'error': {'code': 402, 'message': "
    "'Your prepayment credits are depleted. Please go to AI Studio at "
    "https://ai.studio/projects to manage your project and billing.', "
    "'status': 'RESOURCE_EXHAUSTED'}}"
)

ESCALATED = get_user_message(ErrorCategory.ESCALATION, "llm_unavailable")
PLAIN = get_user_message(ErrorCategory.TRANSIENT, "llm_unavailable")
GENERIC = get_user_message(ErrorCategory.SYSTEM, "internal_error")

# A real inbound Telegram message carries its message id; button callbacks and
# scheduled runs build their own metadata without one.
LIVE_MESSAGE = {"telegram_message_id": 4242}

_BILLING_WORDS = ("402", "prepay", "billing", "credit", "balance", "ai studio")


def _assert_no_billing_language(response):
    leaked = [word for word in _BILLING_WORDS if word in response.lower()]
    assert not leaked, f"{leaked} leaked into: {response}"


def _user(**overrides):
    fields = {
        "email": "customer@example.test",
        "user_email": "customer@example.test",
        "username": "Test Customer",
        "chat_id": "chat-test-1",
        "topic_id": None,
        "organization_ids": ["7"],
        "organization_name": "TestOrg",
        "is_staff": False,
        "mode": "customer",
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.fixture
def failing_graph(monkeypatch):
    def _fail_with(error):
        monkeypatch.setattr(
            "orchestrator.graphs.full_conversation_graph.build_full_conversation_graph",
            lambda: object(),
        )
        monkeypatch.setattr(
            "orchestrator.graphs.full_conversation_graph.invoke_full_graph",
            AsyncMock(side_effect=error),
        )

    return _fail_with


@pytest.fixture
def escalations(monkeypatch):
    """Stand-in EscalationService; ``.calls`` records every escalate_to_support call."""
    state = SimpleNamespace(calls=[], enabled=True, result={"success": True}, raises=None)

    class _Stub:
        def __init__(self, *args, **kwargs):
            pass

        def is_enabled(self):
            return state.enabled

        async def escalate_to_support(self, **kwargs):
            state.calls.append(kwargs)
            if state.raises is not None:
                raise state.raises
            return state.result

    monkeypatch.setattr("orchestrator.services.escalation_service.EscalationService", _Stub)
    return state


async def _turn(user=None, metadata=LIVE_MESSAGE, session_id="sess-1", text="status of my meter"):
    return await webhook_processor.process_webhook_with_graph(
        text,
        user or _user(),
        session_id=session_id,
        metadata=dict(metadata) if metadata is not None else None,
    )


@pytest.mark.asyncio
async def test_customer_is_told_it_was_escalated_when_it_really_was(failing_graph, escalations):
    failing_graph(GEMINI_402)

    response, tool_results, reply_markup, tokens = await _turn()

    assert response == ESCALATED
    _assert_no_billing_language(response)
    assert (tool_results, reply_markup, tokens) == ([], None, {})
    assert len(escalations.calls) == 1


@pytest.mark.asyncio
async def test_escalation_carries_the_customer_message_and_who_sent_it(failing_graph, escalations):
    failing_graph(GEMINI_402)

    await _turn()

    call = escalations.calls[0]
    assert call["reason"] == "llm_unavailable"
    assert call["session_id"] == "sess-1"
    assert call["customer_chat_id"] == "chat-test-1"
    assert call["customer_username"] == "Test Customer"
    assert call["customer_email"] == "customer@example.test"
    assert call["organization_id"] == 7
    assert call["organization_short_name"] == "TestOrg"
    assert "status of my meter" in call["question_summary"]


@pytest.mark.asyncio
async def test_escalation_tells_support_why_the_bot_did_not_answer(failing_graph, escalations):
    failing_graph(GEMINI_402)

    await _turn()

    assert "unavailable" in escalations.calls[0]["question_summary"].lower()


@pytest.mark.asyncio
async def test_no_promise_when_escalation_is_not_configured(failing_graph, escalations):
    failing_graph(GEMINI_402)
    escalations.enabled = False

    response, *_ = await _turn()

    assert response == PLAIN
    _assert_no_billing_language(response)
    assert escalations.calls == []


@pytest.mark.asyncio
async def test_no_promise_when_the_escalation_fails(failing_graph, escalations):
    failing_graph(GEMINI_402)
    escalations.result = {"success": False, "error": "Failed to escalate: chat not found"}

    response, *_ = await _turn()

    assert response == PLAIN


@pytest.mark.asyncio
async def test_no_promise_when_the_escalation_raises(failing_graph, escalations):
    failing_graph(GEMINI_402)
    escalations.raises = RuntimeError("supabase unreachable")

    response, *_ = await _turn()

    assert response == PLAIN


@pytest.mark.asyncio
async def test_staff_are_told_to_retry_not_escalated_to_themselves(failing_graph, escalations):
    failing_graph(GEMINI_402)

    response, *_ = await _turn(user=_user(is_staff=True))

    assert response == PLAIN
    assert escalations.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [{}, {"from_decision_button": True}, {"scheduled_execution": True}, None],
    ids=["no-message-id", "button-callback", "scheduled-run", "no-metadata"],
)
async def test_turns_that_are_not_a_live_message_are_not_escalated(
    failing_graph, escalations, metadata
):
    failing_graph(GEMINI_402)

    response, *_ = await _turn(metadata=metadata)

    assert response == PLAIN
    assert escalations.calls == []


@pytest.mark.asyncio
async def test_a_turn_with_no_session_cannot_be_escalated_and_promises_nothing(
    failing_graph, escalations
):
    failing_graph(GEMINI_402)

    response, *_ = await _turn(session_id=None)

    assert response == PLAIN
    assert escalations.calls == []


@pytest.mark.asyncio
async def test_other_errors_keep_the_generic_message_and_are_never_escalated(
    failing_graph, escalations
):
    failing_graph(ValueError("boom"))

    response, *_ = await _turn()

    assert response == GENERIC
    assert escalations.calls == []
