"""Tests for user agent CRUD service."""

from orchestrator.services.user_agent_service import _paraphrase_to_check


class TestParaphraseGuard:
    """The stored check_prompt must be a question, not a creation instruction."""

    def test_strips_alert_me_language(self):
        result = _paraphrase_to_check(
            "alert me when connections hit 500",
            "Alert me when connections in ExampleGrid have reached 500",
        )
        assert "alert" not in result.lower()
        assert "?" in result

    def test_strips_let_me_know_language(self):
        result = _paraphrase_to_check(
            "let me know which customers cross 200W",
            "Let me know which customers in ExampleGrid have crossed a 200W threshold",
        )
        assert "let me know" not in result.lower()
        assert "?" in result

    def test_strips_create_agent_language(self):
        result = _paraphrase_to_check(
            "watch TestGrid power",
            "Create an agent to monitor TestGrid power output",
        )
        assert "create" not in result.lower()
        assert "agent" not in result.lower()
        assert "?" in result

    def test_preserves_meaningful_content(self):
        result = _paraphrase_to_check(
            "notify me when TestGrid goes offline",
            "Has TestGrid gone offline?",
        )
        assert "TestGrid" in result
        assert "offline" in result
        assert "?" in result

    def test_fallback_when_sanitization_destroys_prompt(self):
        result = _paraphrase_to_check(
            "alert me about TestGrid",
            "Alert me",  # Would be stripped to nearly nothing
        )
        assert len(result) >= 10
        assert "?" in result

    def test_capitalizes_first_letter(self):
        result = _paraphrase_to_check(
            "check power",
            "have connections reached 500?",
        )
        assert result[0].isupper()

    def test_response_prompt_also_sanitized(self):
        result = _paraphrase_to_check(
            "let me know which customers cross 200W",
            "Let me know which customers have crossed 200W showing meter ID and power",
        )
        assert "let me know" not in result.lower()
        assert "200W" in result
