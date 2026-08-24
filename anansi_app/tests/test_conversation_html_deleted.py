"""The Chats page must render ``metadata.deleted`` as a visible state.

Deleting a bot message keeps its text in chat_messages on purpose, so the
renderer -- not the database -- is what tells an operator the message is gone
from Telegram. Without this the row is indistinguishable from a live message.
"""

from __future__ import annotations

from rendering.conversation_html import render_message_html

BOT_TEXT = "Your meter MTR-001 reads 42 kWh."


def _bot_message(**metadata):
    return {
        "role": "model",
        "content": BOT_TEXT,
        "created_at": "2026-08-24T10:00:00",
        "telegram_message_id": 555,
        "metadata": metadata,
    }


def test_live_bot_message_has_no_deleted_marker():
    html = render_message_html(_bot_message(), {})

    assert "Deleted" not in html
    assert BOT_TEXT in html


def test_deleted_bot_message_is_flagged_as_deleted():
    html = render_message_html(
        _bot_message(deleted=True, deleted_at="2026-08-24T11:00:00", deleted_from_telegram=True),
        {},
    )

    assert "Deleted" in html
    assert "🗑️" in html


def test_deleted_bot_message_still_shows_its_text():
    """The whole point of keeping content: operators can still read it."""
    html = render_message_html(
        _bot_message(deleted=True, deleted_at="2026-08-24T11:00:00", deleted_from_telegram=True),
        {},
    )

    assert BOT_TEXT in html


def test_deleted_bot_message_shows_when_it_was_deleted():
    html = render_message_html(
        _bot_message(deleted=True, deleted_at="2026-08-24T11:00:00", deleted_from_telegram=True),
        {},
    )

    assert "2026-08-24T11:00:00" in html or "2026-08-24 11:00" in html


def test_message_telegram_refused_to_delete_is_called_out():
    """Flagged deleted but still live in Telegram -- the UI must not imply it's gone."""
    html = render_message_html(
        _bot_message(
            deleted=True,
            deleted_at="2026-08-24T11:00:00",
            deleted_from_telegram=False,
            telegram_delete_error="message to delete not found",
        ),
        {},
    )

    assert "still" in html.lower() or "not removed" in html.lower()
    assert "message to delete not found" in html


def test_deleted_message_html_escapes_its_content():
    message = _bot_message(deleted=True, deleted_at="2026-08-24T11:00:00")
    message["content"] = "<script>alert(1)</script>"

    html = render_message_html(message, {})

    assert "<script>alert(1)</script>" not in html
