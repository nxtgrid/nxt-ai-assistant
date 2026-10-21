"""The judge is told how to use the tool outputs it is shown.

Showing it the outputs is not enough on its own: without a rule it still has no
basis for treating a very specific figure as supported, which is how a correct
Full Service delivery rate got called fabricated (2026-10-20).
"""

from orchestrator.services.verification_service import ResponseVerificationService

WITH_OUTPUTS = "TOOLS CALLED THIS TURN:\n- grid_status: succeeded\n\nTOOL OUTPUTS THIS TURN:\n### grid_status\n{}"


def _prompt(context, mode="reply"):
    service = ResponseVerificationService(api_key="test-key")
    return service._build_verification_prompt(
        original_message="What is the delivery rate?",
        response_text="82.4%.",
        conversation_context=context,
        available_tools=None,
        mode=mode,
    )


def test_reply_prompt_says_figures_found_in_the_tool_outputs_are_supported():
    prompt = _prompt(WITH_OUTPUTS)

    assert "EVIDENCE:" in prompt
    assert "appears there is supported" in prompt


def test_reply_prompt_still_lets_the_judge_fail_unsupported_specifics():
    prompt = _prompt(WITH_OUTPUTS)

    assert "neither in those outputs nor in the conversation" in prompt


def test_no_evidence_rule_when_no_tool_outputs_were_shown():
    assert "EVIDENCE:" not in _prompt("TOOLS CALLED THIS TURN:\n- grid_status: succeeded")
    assert "EVIDENCE:" not in _prompt(None)


def test_broadcast_prompt_never_gets_the_evidence_rule():
    assert "EVIDENCE:" not in _prompt(WITH_OUTPUTS, mode="broadcast")
