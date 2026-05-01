"""Tests for session ID generation, including parent session ID."""

from unittest.mock import patch

from orchestrator.utils.session_id import generate_parent_session_id, generate_session_id


class TestGenerateParentSessionId:
    """Tests for generate_parent_session_id."""

    @patch.dict("os.environ", {"SESSION_ID_SECRET": "test-secret"})
    def test_returns_chat_level_session_when_topic_present(self):
        """When topic_id is present, parent should be the chat-level session."""
        topic_session = generate_session_id(
            source="telegram", chat_id="-100123", topic_id="42", user_id="u1"
        )
        chat_session = generate_session_id(source="telegram", chat_id="-100123", user_id="u1")
        parent = generate_parent_session_id(
            source="telegram", chat_id="-100123", topic_id="42", user_id="u1"
        )

        # Parent should equal the chat-level session (no topic)
        assert parent == chat_session
        # And should differ from the topic-level session
        assert parent != topic_session

    def test_returns_none_without_topic(self):
        """No topic_id means no parent — session is already chat-level."""
        assert generate_parent_session_id(source="telegram", chat_id="-100123") is None

    def test_returns_none_without_chat_id(self):
        """No chat_id means no parent."""
        assert generate_parent_session_id(source="telegram", topic_id="42") is None

    @patch.dict("os.environ", {"SESSION_ID_SECRET": "test-secret"})
    def test_parent_is_findable_from_chat_level_message(self):
        """A message without topic_id should generate the same session_id as the parent."""
        # Simulate: workflow started in topic 42
        parent = generate_parent_session_id(
            source="telegram", chat_id="-100123", topic_id="42", user_id="u1"
        )
        # Simulate: user types "c" without topic_id (no reply)
        chat_session = generate_session_id(source="telegram", chat_id="-100123", user_id="u1")
        assert parent == chat_session
