"""Selection rules for the Chats page's "delete a bot message" dialog.

Ported from the Streamlit ``_render_inline_delete_ui`` that the NiceGUI
migration dropped. Only bot messages that actually reached Telegram can be
deleted from it, and Telegram refuses group deletions past 48 hours.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from nicegui_app.pages import chat


def _msg(**overrides):
    msg = {
        "id": "msg-1",
        "role": "model",
        "content": "Your meter MTR-001 reads 42 kWh.",
        "created_at": datetime.utcnow().isoformat(),
        "telegram_message_id": 555,
        "metadata": {},
    }
    msg.update(overrides)
    return msg


def _hours_ago(hours: float) -> str:
    return (datetime.utcnow() - timedelta(hours=hours)).isoformat()


def test_deletable_includes_a_bot_message_that_reached_telegram():
    entries = chat._deletable_messages([_msg()], is_group=False)

    assert len(entries) == 1
    assert entries[0]["msg"]["id"] == "msg-1"


def test_deletable_excludes_user_messages():
    """The bot can only delete its own messages."""
    entries = chat._deletable_messages([_msg(role="user")], is_group=False)

    assert entries == []


def test_deletable_excludes_messages_never_sent_to_telegram():
    entries = chat._deletable_messages([_msg(telegram_message_id=None)], is_group=False)

    assert entries == []


def test_deletable_excludes_messages_with_no_content():
    entries = chat._deletable_messages([_msg(content="")], is_group=False)

    assert entries == []


def test_deletable_excludes_already_deleted_messages():
    """Re-deleting is a no-op that would only overwrite the deletion record."""
    entries = chat._deletable_messages([_msg(metadata={"deleted": True})], is_group=False)

    assert entries == []


def test_group_message_older_than_48h_is_expired():
    assert chat._is_expired_for_deletion(_msg(created_at=_hours_ago(49)), is_group=True) is True


def test_group_message_within_48h_is_not_expired():
    assert chat._is_expired_for_deletion(_msg(created_at=_hours_ago(47)), is_group=True) is False


def test_direct_message_is_never_expired():
    """Telegram's 48h limit applies to groups only; DMs can be deleted anytime."""
    assert (
        chat._is_expired_for_deletion(_msg(created_at=_hours_ago(500)), is_group=False) is False
    )


def test_expired_group_messages_are_listed_but_flagged():
    entries = chat._deletable_messages([_msg(created_at=_hours_ago(49))], is_group=True)

    assert len(entries) == 1
    assert entries[0]["expired"] is True
    assert "48h" in entries[0]["label"]


def test_label_carries_the_telegram_id_and_a_preview():
    entries = chat._deletable_messages([_msg()], is_group=False)

    assert "#555" in entries[0]["label"]
    assert "Your meter MTR-001" in entries[0]["label"]


def test_timezone_aware_timestamps_do_not_crash_expiry():
    """chat_messages timestamps come back tz-aware from Supabase."""
    aware = (datetime.utcnow() - timedelta(hours=49)).isoformat() + "+00:00"

    assert chat._is_expired_for_deletion(_msg(created_at=aware), is_group=True) is True
