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


# ---------------------------------------------------------------------------
# Generic leak detection (2026-08-24 incident)
#
# The original guard only matched `escalate_to_support(` — a tool name followed
# immediately by an opening paren. Production hit a different shape: the model
# wrote the tool name on one line and the arguments as a JSON object on the
# next, which sailed past the paren check and reached the customer verbatim.
# The guard must key on call *structure* for any declared tool, not on one
# tool name in one syntax.
# ---------------------------------------------------------------------------

JSON_BLOCK_LEAK = """Call Tool: escalate_to_support
{
  "reasoning": "The user is asking for a data download feature that is not available in the current toolset.",
  "question_summary": "User is requesting to download 1 year of dashboard data.",
  "reason": "staff_action_required",
  "action_type": "other_action",
  "conversation_context": "User is asking how to export dashboard data."
}

I'm sorry, I don't have the ability to download data directly from that dashboard. I've escalated your request to our support team."""


@pytest.mark.asyncio
async def test_json_block_tool_call_leak_is_never_sent_to_customer(fake_escalation_service):
    """The exact production shape: tool name, then a JSON argument object."""
    state = _make_state(final_response=JSON_BLOCK_LEAK)

    result = await safety_check(state)

    assert "escalate_to_support" not in result["final_response"]
    assert "Call Tool" not in result["final_response"]
    assert "question_summary" not in result["final_response"]
    assert result["safety_escalation_needed"] is True


@pytest.mark.asyncio
async def test_json_block_leak_recovers_summary_from_json_arguments(fake_escalation_service):
    state = _make_state(final_response=JSON_BLOCK_LEAK)

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    assert kwargs["question_summary"] == "User is requesting to download 1 year of dashboard data."


@pytest.mark.parametrize(
    "leaked",
    [
        pytest.param('Call Tool: escalate_to_support\n{"reason": "x"}', id="json-block"),
        pytest.param("escalate_to_support(reason='x')", id="paren"),
        pytest.param("Tool Call: fetch_training_image\n{}", id="other-tool-name"),
        pytest.param("print(default_api.escalate_to_support(reason='x'))", id="default-api"),
        pytest.param('```tool_code\nescalate_to_support(reason="x")\n```', id="fenced-tool-code"),
        pytest.param('{"tool": "escalate_to_support", "args": {"reason": "x"}}', id="json-tool-key"),
        pytest.param('{"name": "expert_run_steps", "arguments": {}}', id="json-name-key"),
        pytest.param("functionCall: escalate_to_support", id="function-call-marker"),
    ],
)
def test_detect_raw_tool_call_leak_covers_every_leak_shape(leaked):
    assert safety_check_module._detect_raw_tool_call_leak(leaked) is True


@pytest.mark.parametrize(
    "clean",
    [
        pytest.param("I will escalate this for you.", id="plain-prose"),
        pytest.param("", id="empty"),
        pytest.param(
            "I used the escalate_to_support process to notify the team.",
            id="bare-name-mention",
        ),
        pytest.param(
            "Your meter reading is 4231 (recorded yesterday).", id="prose-with-paren"
        ),
    ],
)
def test_detect_raw_tool_call_leak_has_no_false_positives(clean):
    assert safety_check_module._detect_raw_tool_call_leak(clean) is False


def test_detect_raw_tool_call_leak_uses_declared_tool_names_from_payload():
    """A tool this node has never heard of is still caught when it was declared."""
    leaked = "some_brand_new_mcp_tool(grid='x')"

    assert safety_check_module._detect_raw_tool_call_leak(leaked) is False
    assert (
        safety_check_module._detect_raw_tool_call_leak(
            leaked, known_tool_names={"some_brand_new_mcp_tool"}
        )
        is True
    )


@pytest.mark.asyncio
async def test_leak_detection_reads_tool_names_from_state_payload(fake_escalation_service):
    state = _make_state(
        final_response="some_brand_new_mcp_tool(grid='x')",
        tools_payload=[{"name": "some_brand_new_mcp_tool"}],
    )

    result = await safety_check(state)

    assert "some_brand_new_mcp_tool" not in result["final_response"]
    assert result["safety_escalation_needed"] is True


def test_extract_kwargs_handles_json_argument_objects():
    kwargs = safety_check_module._extract_kwargs_from_tool_call_text(
        JSON_BLOCK_LEAK, "escalate_to_support"
    )

    assert kwargs["question_summary"] == "User is requesting to download 1 year of dashboard data."
    assert kwargs["reason"] == "staff_action_required"


# ---------------------------------------------------------------------------
# Escalation-claim detection must survive typographic apostrophes. Models emit
# U+2019 routinely; the ASCII-only patterns silently missed every such claim,
# disabling the backup escalation that is supposed to catch a bot promising an
# escalation it never made.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_detect_escalation_claim_handles_both_apostrophes(apostrophe):
    text = f"I{apostrophe}ve escalated your request to our support team."

    assert safety_check_module._detect_escalation_claim(text) is True


@pytest.mark.parametrize("apostrophe", ["'", "’"])
def test_detect_escalation_claim_negation_handles_both_apostrophes(apostrophe):
    text = f"I can{apostrophe}t escalate this, but here is what I found."

    assert safety_check_module._detect_escalation_claim(text) is False


# The 2026-08-24 Hardrock leak, verbatim in shape: the model wrapped the call
# in square brackets, so the text ends in ")]" rather than ")". It carried a
# conversation_context but no question_summary.
BRACKET_WRAPPED_LEAK = (
    "[Call Tool: escalate_to_support(action_type='other_action', "
    "conversation_context='Meter 47003334126. User reports burnt output "
    "terminals (second occurrence). User seeks clarification and "
    "recommendations on meter burning.')]"
)


def test_extract_kwargs_recovers_arguments_from_bracket_wrapped_call():
    # The end-anchored r"...\)\s*$" match this replaced silently returned {}
    # for any leak with a trailing character after the closing paren.
    kwargs = safety_check_module._extract_kwargs_from_tool_call_text(
        BRACKET_WRAPPED_LEAK, "escalate_to_support"
    )

    assert kwargs["action_type"] == "other_action"
    assert kwargs["conversation_context"].startswith("Meter 47003334126.")


def test_extract_kwargs_tolerates_parentheses_inside_quoted_arguments():
    leaked = (
        "escalate_to_support(question_summary='Burnt meter (output terminals)', "
        "reason='staff_action_required'). Support will follow up."
    )

    kwargs = safety_check_module._extract_kwargs_from_tool_call_text(
        leaked, "escalate_to_support"
    )

    assert kwargs["question_summary"] == "Burnt meter (output terminals)"
    assert kwargs["reason"] == "staff_action_required"


@pytest.mark.asyncio
async def test_bracket_wrapped_leak_escalates_with_model_context(fake_escalation_service):
    state = _make_state(final_response=BRACKET_WRAPPED_LEAK)

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    assert kwargs["question_summary"].startswith("Meter 47003334126.")


@pytest.mark.asyncio
async def test_escalation_question_is_never_the_customer_error_message(
    fake_escalation_service,
):
    # The card's "Question:" is what support staff read first. Extracting it
    # from `final_response` after the leak guard replaced that with the
    # generic "I tried to get help..." message made every safety escalation
    # arrive with a useless question (2026-08-24 Hardrock incident).
    state = _make_state(final_response="default_api.escalate_to_support")

    await safety_check(state)

    _, kwargs = fake_escalation_service.escalate_to_support.await_args
    summary = kwargs["question_summary"]
    assert "contact support directly" not in summary
    assert "ran into an issue" not in summary
    # Falls back to what the customer actually asked.
    assert summary == _make_state()["user_input"]
