"""Tests for the raw tool-call leak guard in orchestrator.graphs.nodes.safety_check.

Regression coverage for a production incident: the fallback model emitted a
tool invocation as plain text (e.g. "Call Tool: escalate_to_support(...)")
instead of a native function call, and that raw text was sent to the customer
verbatim over Telegram.
"""

import importlib
from unittest.mock import AsyncMock, MagicMock

import pytest

# orchestrator.graphs.nodes.__init__ re-exports the `safety_check` function under
# the same name as this submodule, shadowing it on the package — import via
# importlib to get the actual module (for its private helpers) rather than the
# function.
safety_check_module = importlib.import_module("orchestrator.graphs.nodes.safety_check")
safety_check = safety_check_module.safety_check


def _make_state(**overrides):
    base = {
        "final_response": "",
        "accumulated_tool_calls": [],
        "accumulated_tool_results": [],
        "user_context": None,
        "session_id": "session-123",
        "user_input": "Purchasing new meter for customer at Kudi, drop me the designated account",
        "metadata": {},
    }
    base.update(overrides)
    return base


LEAKED_TEXT = (
    "Call Tool: escalate_to_support(action_type='other_action', "
    "conversation_context='A customer is requesting the designated bank "
    "account details for a new meter purchase at Kudi.', "
    "question_summary='Request for designated bank account for meter purchase at Kudi', "
    "reason='staff_action_required', "
    "reasoning='I do not have access to bank account details for meter purchases')"
)


@pytest.fixture(autouse=True)
def fake_escalation_service(monkeypatch):
    """Stub EscalationService and AuthService so no real Telegram/DB calls happen in tests."""
    instance = MagicMock()
    instance.is_enabled.return_value = True
    instance.escalate_to_support = AsyncMock(return_value={"success": True})

    def _factory(*args, **kwargs):
        return instance

    monkeypatch.setattr(
        "orchestrator.services.escalation_service.EscalationService", _factory
    )
    monkeypatch.setattr(safety_check_module, "get_auth_service", lambda: AsyncMock())
    return instance


@pytest.mark.asyncio
async def test_raw_tool_call_leak_is_never_sent_to_customer(fake_escalation_service):
    state = _make_state(final_response=LEAKED_TEXT)

    result = await safety_check(state)

    assert "escalate_to_support(" not in result["final_response"]
    assert "Call Tool" not in result["final_response"]


@pytest.mark.asyncio
async def test_raw_tool_call_leak_triggers_backup_escalation(fake_escalation_service):
    state = _make_state(final_response=LEAKED_TEXT)

    result = await safety_check(state)

    assert result["safety_escalation_needed"] is True
    fake_escalation_service.escalate_to_support.assert_awaited_once()


@pytest.mark.asyncio
async def test_raw_tool_call_leak_uses_model_summary_not_raw_syntax(fake_escalation_service):
    state = _make_state(final_response=LEAKED_TEXT)

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    summary = kwargs["question_summary"]
    assert summary == "Request for designated bank account for meter purchase at Kudi"
    assert "escalate_to_support(" not in summary


@pytest.mark.asyncio
async def test_normal_response_is_untouched(fake_escalation_service):
    state = _make_state(final_response="Sure, here is the grid status you asked for.")

    result = await safety_check(state)

    assert result.get("final_response") is None  # no override needed
    assert result["safety_escalation_needed"] is False
    fake_escalation_service.escalate_to_support.assert_not_awaited()


@pytest.mark.asyncio
async def test_response_mentioning_tool_name_without_call_syntax_is_untouched(
    fake_escalation_service,
):
    # Should not false-positive on prose that happens to mention the tool name
    # without the call syntax (no opening paren immediately after the name).
    state = _make_state(
        final_response="I used the escalate_to_support process to notify the team."
    )

    result = await safety_check(state)

    assert result.get("final_response") is None
    assert result["safety_escalation_needed"] is False


def test_detect_raw_tool_call_leak_matches_leaked_syntax():
    assert safety_check_module._detect_raw_tool_call_leak(LEAKED_TEXT) is True


def test_detect_raw_tool_call_leak_ignores_normal_text():
    assert safety_check_module._detect_raw_tool_call_leak("I will escalate this for you.") is False
    assert safety_check_module._detect_raw_tool_call_leak("") is False


def test_extract_kwargs_from_tool_call_text_recovers_arguments():
    kwargs = safety_check_module._extract_kwargs_from_tool_call_text(
        LEAKED_TEXT, "escalate_to_support"
    )
    assert kwargs["question_summary"] == (
        "Request for designated bank account for meter purchase at Kudi"
    )
    assert kwargs["reason"] == "staff_action_required"
    assert kwargs["conversation_context"].startswith("A customer is requesting")


def test_extract_kwargs_from_tool_call_text_returns_empty_on_no_match():
    assert safety_check_module._extract_kwargs_from_tool_call_text("no call here", "escalate_to_support") == {}


@pytest.mark.asyncio
async def test_raw_tool_call_leak_forwards_media_file_ids(fake_escalation_service):
    state = _make_state(final_response=LEAKED_TEXT, metadata={"photo_file_id": "photo1"})

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    assert kwargs["media_file_ids"] == [{"type": "image", "file_id": "photo1"}]
