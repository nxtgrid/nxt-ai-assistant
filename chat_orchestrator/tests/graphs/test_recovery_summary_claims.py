"""The budget-recovery summary must not claim an escalation that never happened.

2026-10-19: a turn ran out of tool budget and the summary the model wrote
ended "I have escalated this to our technical team for investigation ...
#NXTAction". Nothing had been escalated. The system prompt pushes hard on
escalating, and the summary call (tools disabled) simply wrote the sentence the
prompt trained it to write. The safety net then filed a backup escalation for the
false claim, and the claim stayed in the history, so on the user's next message
("Continue.") the model repeated it and a second backup was filed.

Of 18 distinct recovery summaries since July this was the only one with such a
claim, and most of the others report real, tool-backed work ("Closed OPS-123"),
so the fix removes only unbacked escalation claims and action tags and leaves
everything else alone.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.graphs.execution_limit_recovery import ExecutionLimitReason
from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult

INCIDENT_SUMMARY = (
    "* **Confirmed Completed Work:** None. The system is unable to retrieve live data.\n"
    "* **Remaining Issues:** All three DCUs remain offline.\n"
    "\n"
    "I have escalated this to our technical team for investigation, as the system is "
    "currently not providing any data to diagnose the cause of the offline status. #NXTAction"
)


@pytest.fixture
def builder():
    settings = SimpleNamespace(
        allow_parallel_calls=False,
        max_tool_rounds=2,
        gemini=SimpleNamespace(
            candidate_count=1,
            top_k=40,
            top_p=0.95,
            max_output_tokens=8192,
            get_effective_temperature=lambda: None,
        ),
    )
    return ConversationGraphBuilder(
        gemini_client=MagicMock(),
        registry=MagicMock(),
        executor=MagicMock(),
        settings=settings,
    )


def _say(builder, text):
    builder._gemini.generate_messages = AsyncMock(
        return_value=GeminiTurnResult(
            text=text,
            tool_calls=[],
            finish_reason="STOP",
            input_tokens=0,
            output_tokens=0,
            raw_response={},
        )
    )


def _state(**extra):
    return {
        "current_round": 2,
        "max_rounds": 2,
        "llm_messages": [ConversationMessage(role="user", content="check the grid")],
        "history_messages": [],
        "accumulated_tool_calls": [FunctionCall(name="customer_meter_information", arguments={})],
        "accumulated_tool_results": [
            ToolCallResult(name="customer_meter_information", success=True, output="ok")
        ],
        "pending_tool_calls": [],
        "system_instructions": None,
        **extra,
    }


async def _recover(builder, text, **extra):
    _say(builder, text)
    result = await builder._call_gemini_node(_state(**extra))
    assert result["graceful_limit_recovery"] is True
    return result["final_response"]


@pytest.mark.asyncio
async def test_an_unbacked_escalation_claim_and_its_tag_are_removed(builder):
    response = await _recover(builder, INCIDENT_SUMMARY)

    assert "All three DCUs remain offline" in response
    assert "escalated" not in response
    assert "technical team" not in response
    assert "#NXTAction" not in response


@pytest.mark.asyncio
async def test_a_claim_backed_by_a_real_escalation_is_kept(builder):
    response = await _recover(builder, INCIDENT_SUMMARY, escalation_triggered=True)

    assert "I have escalated this to our technical team" in response
    assert "#NXTAction" in response


@pytest.mark.asyncio
async def test_only_the_claim_sentence_goes_not_the_rest_of_its_line(builder):
    response = await _recover(
        builder, "* All DCUs are offline. I have escalated this to staff. Power is stable."
    )

    assert "All DCUs are offline." in response
    assert "Power is stable." in response
    assert "escalated" not in response


@pytest.mark.asyncio
async def test_a_summary_that_is_only_a_claim_falls_back_to_the_no_action_line(builder):
    response = await _recover(builder, "I've escalated this to our support team. #NXTAction")

    assert "No additional action was confirmed" in response
    assert "escalated" not in response


@pytest.mark.asyncio
async def test_tool_backed_work_is_left_alone(builder):
    summary = "- Closed OPS-123\n- Updated the meter date for 47000000001\n- Commented on OPS-456"

    response = await _recover(builder, summary)

    assert "Closed OPS-123" in response
    assert "Updated the meter date" in response
    assert "Commented on OPS-456" in response


@pytest.mark.asyncio
async def test_action_tags_go_but_a_ticket_number_sign_stays(builder):
    response = await _recover(
        builder, "- Reviewed ticket #123\n- Waiting on the customer #CustomerAction\n#OperatorAction"
    )

    assert "ticket #123" in response
    assert "#CustomerAction" not in response
    assert "#OperatorAction" not in response


@pytest.mark.asyncio
async def test_the_output_limit_path_is_guarded_too(builder):
    _say(builder, INCIDENT_SUMMARY)

    result = await builder._recover_execution_limit(
        _state(), ExecutionLimitReason.OUTPUT_LIMIT, partial_response="I have escalated this."
    )

    assert "All three DCUs remain offline" in result["final_response"]
    assert "escalated" not in result["final_response"]


# --- what the summary call is told ------------------------------------------------------


async def _synthesis_prompt(builder, **extra):
    _say(builder, "- nothing")
    await builder._synthesize_partial_answer(_state(**extra))
    return builder._gemini.generate_messages.call_args.args[0][-1].content


@pytest.mark.asyncio
async def test_the_summary_call_is_told_what_actually_happened(builder):
    failed = ToolCallResult(name="jira_search_issues", success=False, output=None, error="HTTP 502")
    prompt = await _synthesis_prompt(
        builder,
        accumulated_tool_calls=[
            FunctionCall(name="customer_meter_information", arguments={}),
            FunctionCall(name="jira_search_issues", arguments={}),
        ],
        accumulated_tool_results=[
            ToolCallResult(name="customer_meter_information", success=True, output="ok"),
            failed,
        ],
    )

    assert "FACTS" in prompt
    assert "- customer_meter_information: succeeded" in prompt
    assert "- jira_search_issues: failed (HTTP 502)" in prompt
    assert "Escalation to staff: NOT filed this turn" in prompt


@pytest.mark.asyncio
async def test_the_facts_say_when_an_escalation_was_filed(builder):
    prompt = await _synthesis_prompt(builder, escalation_triggered=True)

    assert "Escalation to staff: filed this turn" in prompt


@pytest.mark.asyncio
async def test_the_summary_call_is_forbidden_from_claiming_escalations_or_adding_tags(builder):
    prompt = await _synthesis_prompt(builder)

    assert "escalated, forwarded, notified, reported or passed to staff" in prompt
    assert "unless the FACTS" in prompt
    assert "hashtags" in prompt
    assert "tool-call budget" in prompt  # the original instruction is still there


@pytest.mark.asyncio
async def test_repeated_identical_tool_lines_are_collapsed(builder):
    rejected = ToolCallResult(name="no_such_tool", success=False, output=None, error="not available")
    prompt = await _synthesis_prompt(
        builder,
        accumulated_tool_calls=[FunctionCall(name="no_such_tool", arguments={})] * 19,
        accumulated_tool_results=[rejected] * 19,
    )

    assert prompt.count("- no_such_tool: failed (not available)") == 1
    assert "x19" in prompt


@pytest.mark.asyncio
async def test_a_long_fact_list_is_capped(builder):
    names = [f"tool_{i}" for i in range(60)]
    prompt = await _synthesis_prompt(
        builder,
        accumulated_tool_calls=[FunctionCall(name=n, arguments={}) for n in names],
        accumulated_tool_results=[ToolCallResult(name=n, success=True, output="ok") for n in names],
    )

    assert "tool_0: succeeded" in prompt
    assert "tool_59: succeeded" not in prompt
    assert "more not listed" in prompt
