"""
The reply-mode verification prompt must not fail a genuine hand-off response
for lacking a resolution time / ETA.

2026-09-09 incident: a customer meter question was correctly diagnosed, the
meter clock was synced, and the issue was escalated to support for the one
backend step the bot cannot do itself. The drafted reply said so. The
LLM-as-judge failed it twice -- "the response should provide the next steps or
expected resolution time from the support team" -- an ETA that cannot exist
before a human has looked at the escalation. Both regeneration passes were
therefore unsatisfiable, the turn hard-escalated, and the customer got the
content-free "I've notified our support team" line instead of the diagnosis.

The judge now gets an explicit carve-out for hand-off responses.
"""

from orchestrator.services.verification_service import ResponseVerificationService


def _reply_prompt(*, tools_called: str | None = None) -> str:
    service = ResponseVerificationService(api_key="test-key")
    context = "TOOLS CALLED THIS TURN:\n" + tools_called if tools_called else None
    return service._build_verification_prompt(
        original_message="meter 47 is tripping during FS hours",
        response_text=(
            "I've corrected the meter's clock and escalated the remaining backend "
            "step to our support team, who will follow up."
        ),
        conversation_context=context,
        mode="reply",
    )


class TestReplyPromptEscalationCarveOut:
    def test_reply_prompt_exempts_handoff_responses_from_needing_an_eta(self):
        prompt = _reply_prompt(tools_called="- escalate_to_support: succeeded")
        assert "HANDOFF RESPONSES" in prompt
        # The three things a hand-off reply must NOT be failed for lacking.
        assert "resolution time" in prompt
        assert "ETA" in prompt
        assert "next steps" in prompt

    def test_carve_out_is_scoped_to_a_real_successful_escalation(self):
        prompt = _reply_prompt(tools_called="- escalate_to_support: succeeded")
        assert "escalation actually succeeded" in prompt

    def test_broadcast_prompt_does_not_carry_the_reply_carve_out(self):
        service = ResponseVerificationService(api_key="test-key")
        prompt = service._build_verification_prompt(
            original_message="[One-way broadcast announcement]",
            response_text="Panels in the community are clean.",
            conversation_context=None,
            mode="broadcast",
        )
        assert "HANDOFF RESPONSES" not in prompt

    def test_reply_prompt_structure_is_otherwise_intact(self):
        prompt = _reply_prompt()
        assert "ORIGINAL USER MESSAGE:" in prompt
        assert "RESPONSE TO VERIFY:" in prompt
