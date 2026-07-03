"""
Tests for the broadcast-mode verification prompt.

Broadcast messages are enriched (placeholders substituted with the recipient's
real org/grid names) BEFORE verification. The judge prompt must say so, or the
judge misreads real names as unrecognized placeholder tags and false-fails the
message (the criteria doc tells it to hunt for invalid <...> tags).
"""

from orchestrator.services.verification_service import ResponseVerificationService


def _build_prompt(mode: str) -> str:
    service = ResponseVerificationService(api_key="test-key")
    prompt: str = service._build_verification_prompt(
        original_message="[One-way broadcast announcement]",
        response_text="Good morning Demo Developer! Panels in Demo Community are clean.",
        conversation_context="This is a broadcast message being sent to: Demo Developer",
        mode=mode,
    )
    return prompt


class TestBroadcastPrompt:
    def test_broadcast_prompt_says_placeholders_already_substituted(self):
        prompt = _build_prompt("broadcast")
        assert "ALREADY been substituted" in prompt
        assert "NOT placeholder tags" in prompt

    def test_broadcast_prompt_limits_tag_rule_to_literal_angle_bracket_tokens(self):
        prompt = _build_prompt("broadcast")
        assert "literal angle-bracket token" in prompt

    def test_broadcast_prompt_frames_message_as_broadcast(self):
        prompt = _build_prompt("broadcast")
        assert "BROADCAST MESSAGE TO VERIFY:" in prompt
        assert "ORIGINAL USER MESSAGE:" not in prompt

    def test_reply_prompt_unchanged_by_broadcast_note(self):
        prompt = _build_prompt("reply")
        assert "ALREADY been substituted" not in prompt
        assert "ORIGINAL USER MESSAGE:" in prompt
        assert "RESPONSE TO VERIFY:" in prompt
