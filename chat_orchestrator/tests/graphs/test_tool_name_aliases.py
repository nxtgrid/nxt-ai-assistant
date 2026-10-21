"""A tool name that is only missing its server prefix resolves to the tool meant.

2026-10-19: the customer prompt and several tool descriptions say
``customer_get_grid_status``, but the name actually offered to the model carries
the server prefix as well (``customer_customer_get_grid_status``). The model
followed the text, every call was rejected as a hallucinated tool, and because
the graph loops back to the model after a rejection, it repeated the call 19
times until the tool budget ran out. The budget-exhausted summary then claimed
an escalation that had not happened, and the safety net filed two backups.
"""

from unittest.mock import AsyncMock

import pytest

from orchestrator.models.schemas import FunctionCall, ToolCallResult

ALLOWED = {
    "customer_customer_get_grid_status",
    "customer_customer_get_all_grids_status",
    "customer_get_recent_production_errors_and_power",
    "customer_meter_information",
    "jira_search_issues",
    "escalate_to_support",
    "fetch_training_image",
}


def _resolve(calls, allowed=ALLOWED):
    from orchestrator.graphs.conversation_graph import resolve_tool_name_aliases

    return resolve_tool_name_aliases(calls, allowed)


def _call(name, **extra):
    return FunctionCall(name=name, arguments={"grid_name": "Example Grid"}, **extra)


def test_exact_names_are_left_alone():
    calls = [_call("customer_customer_get_grid_status"), _call("escalate_to_support")]

    assert _resolve(calls) is calls


def test_a_name_missing_its_server_prefix_is_resolved():
    [resolved] = _resolve([_call("customer_get_grid_status")])

    assert resolved.name == "customer_customer_get_grid_status"


def test_another_unprefixed_name_is_resolved():
    [resolved] = _resolve([_call("get_recent_production_errors_and_power")])

    assert resolved.name == "customer_get_recent_production_errors_and_power"


def test_everything_else_about_the_call_is_preserved():
    original = _call("customer_get_grid_status", tool_call_id="call-7", thought_signature="sig")

    [resolved] = _resolve([original])

    assert resolved.arguments == {"grid_name": "Example Grid"}
    assert resolved.tool_call_id == "call-7"
    assert resolved.thought_signature == "sig"
    assert original.name == "customer_get_grid_status"  # the input is not mutated


def test_only_the_near_miss_in_a_batch_is_rewritten():
    calls = [_call("customer_meter_information"), _call("customer_get_grid_status")]

    resolved = _resolve(calls)

    assert [c.name for c in resolved] == [
        "customer_meter_information",
        "customer_customer_get_grid_status",
    ]


def test_unknown_names_are_left_for_the_hallucination_guard():
    calls = [_call("customer_get_something_else")]

    assert _resolve(calls) is calls


def test_ambiguous_names_are_not_guessed():
    allowed = ALLOWED | {
        "grid_customer_get_grid_status",
        "grid_design_find_grid",
        "grid_design_list_options",
    }
    calls = [_call("customer_get_grid_status")]

    assert [c.name for c in _resolve(calls, allowed)] == ["customer_get_grid_status"]


@pytest.mark.parametrize("name", ["support", "to_support", "training_image", "image"])
def test_a_fragment_never_resolves_into_a_single_purpose_tool(name):
    # escalate_to_support and fetch_training_image are not behind a server
    # prefix, so no prefix can be "missing": resolving a fragment into them
    # would let a garbled name trigger an escalation.
    calls = [_call(name)]

    assert _resolve(calls) is calls


def test_nothing_is_resolved_when_there_is_no_allowlist():
    calls = [_call("customer_get_grid_status")]

    assert _resolve(calls, set()) is calls


@pytest.mark.asyncio
async def test_the_node_runs_and_records_the_resolved_name():
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    builder = object.__new__(ConversationGraphBuilder)
    builder._execute_tool_calls = AsyncMock(
        return_value=[
            ToolCallResult(name="customer_customer_get_grid_status", success=True, output="ok")
        ]
    )
    state = {
        "pending_tool_calls": [_call("customer_get_grid_status")],
        "allowed_tool_names": sorted(ALLOWED),
        "history_messages": [],
        "metadata": {},
    }

    result = await builder._execute_tools_node(state)

    executed = builder._execute_tool_calls.await_args.args[0]
    assert [c.name for c in executed] == ["customer_customer_get_grid_status"]
    assert [c.name for c in result["accumulated_tool_calls"]] == [
        "customer_customer_get_grid_status"
    ]
    assert "error" not in result
    assert result["accumulated_tool_results"][0].success is True
