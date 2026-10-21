"""The response judge sees what the tools returned, not just that they ran.

2026-10-20: a customer asked for the delivery success rate of Full Service
commands. The model called the grid status tool, whose result held those
figures, and quoted them. The judge was told only that the tool "succeeded",
never what it returned, so it called the figures fabricated -- twice. The reply
was replaced by the canned "I've notified our support team" message and a
verification-failed escalation was filed, for an answer that was correct.
"""

import pytest

from orchestrator.models.schemas import FunctionCall, ToolCallResult
from orchestrator.services.verification_service import VerificationResult

GRID_STATUS = "customer_customer_get_grid_status"
FS_OUTPUT = (
    '{"fs_detail": {"last_fs_command": '
    '{"command": "ON", "delivery_pct": 82.4, "successful": 61, "total": 74}}}'
)


@pytest.fixture
def judge(monkeypatch):
    """Capture what _verify_node hands the judge."""
    seen: dict = {}

    class _Judge:
        def __init__(self, *args, **kwargs):
            pass

        async def verify_response(self, **kwargs):
            seen.update(kwargs)
            return VerificationResult(passed=True, feedback="", categories=[])

        async def aclose(self):
            pass

    monkeypatch.setattr(
        "orchestrator.services.verification_service.ResponseVerificationService", _Judge
    )
    return seen


def _builder():
    from orchestrator.graphs.conversation_graph import ConversationGraphBuilder

    return object.__new__(ConversationGraphBuilder)


def _state(*tool_runs: tuple[str, ToolCallResult]) -> dict:
    return {
        "final_response": "The last Full Service command was delivered to 82.4% of meters.",
        "verification_instructions": "Judge accuracy.",
        "user_input": "What is the Full Service delivery rate?",
        "verification_attempt": 0,
        "history_messages": [],
        "accumulated_tool_calls": [FunctionCall(name=name, arguments={}) for name, _ in tool_runs],
        "accumulated_tool_results": [result for _, result in tool_runs],
    }


def _ok(name: str, output) -> ToolCallResult:
    return ToolCallResult(name=name, success=True, output=output)


@pytest.mark.asyncio
async def test_judge_is_shown_what_the_tool_returned(judge):
    await _builder()._verify_node(_state((GRID_STATUS, _ok(GRID_STATUS, FS_OUTPUT))))

    context = judge["conversation_context"]
    assert "TOOL OUTPUTS" in context
    assert '"delivery_pct": 82.4' in context


@pytest.mark.asyncio
async def test_the_tool_activity_summary_is_kept(judge):
    # The judge's hand-off rule reads "the tool activity above" to know whether an
    # escalation succeeded this turn, so these lines must stay as they were.
    await _builder()._verify_node(_state((GRID_STATUS, _ok(GRID_STATUS, FS_OUTPUT))))

    context = judge["conversation_context"]
    assert "TOOLS CALLED THIS TURN:" in context
    assert f"- {GRID_STATUS}: succeeded" in context


@pytest.mark.asyncio
async def test_structured_outputs_are_shown_as_json(judge):
    output = {"fs_detail": {"last_fs_command": {"delivery_pct": 82.4}}}

    await _builder()._verify_node(_state((GRID_STATUS, _ok(GRID_STATUS, output))))

    assert '"delivery_pct": 82.4' in judge["conversation_context"]


@pytest.mark.asyncio
async def test_a_huge_output_is_cut_and_says_so(judge):
    huge = "x" * 50_000

    await _builder()._verify_node(_state(("big_tool", _ok("big_tool", huge))))

    context = judge["conversation_context"]
    assert "x" * 10_000 in context
    assert "x" * 10_001 not in context
    assert "truncated" in context


@pytest.mark.asyncio
async def test_total_size_is_bounded_and_dropped_outputs_are_noted(judge):
    runs = [(f"tool_{i}", _ok(f"tool_{i}", "y" * 9_000)) for i in range(6)]

    await _builder()._verify_node(_state(*runs))

    context = judge["conversation_context"]
    assert len(context) < 30_000
    assert "tool_0" in context
    assert "omitted" in context


@pytest.mark.asyncio
async def test_a_failed_tool_shows_its_error(judge):
    failed = ToolCallResult(name="flaky_tool", success=False, output=None, error="upstream 502")

    await _builder()._verify_node(_state(("flaky_tool", failed)))

    context = judge["conversation_context"]
    assert "upstream 502" in context
    assert "- flaky_tool: failed" in context


@pytest.mark.asyncio
async def test_no_tool_calls_means_no_tool_output_block(judge):
    await _builder()._verify_node(_state())

    assert "TOOL OUTPUTS" not in (judge.get("conversation_context") or "")
