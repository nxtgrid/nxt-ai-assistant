"""Tests for the chat widget's pure helpers.

Only the helpers -- `mount()` builds NiceGUI elements, and anansi_app's
conftest fakes `nicegui`, so nothing that touches `ui` can be exercised here.
That split is why the widget module holds wiring and nothing else.
"""

from __future__ import annotations

from nicegui_app import chat_widget


# ── who gets the widget ───────────────────────────────────────────────────────
def test_a_bot_admin_gets_the_widget(monkeypatch):
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("ALLOWED_VIEWER_EMAILS", "admin@example.com")
    assert chat_widget.should_show_chat("admin@example.com") is True


def test_a_grid_only_viewer_does_not(monkeypatch):
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("ALLOWED_VIEWER_EMAILS", "admin@example.com")
    monkeypatch.setenv("GRID_DESIGN_ALLOWED_USERS", "viewer@example.com")
    assert chat_widget.should_show_chat("viewer@example.com") is False


def test_an_empty_email_does_not(monkeypatch):
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    monkeypatch.setenv("ALLOWED_VIEWER_EMAILS", "admin@example.com")
    assert chat_widget.should_show_chat("") is False


# ── session lifetime ──────────────────────────────────────────────────────────
def test_a_reload_starts_a_new_session():
    """The stated rule: each tab refresh starts a new session."""
    assert chat_widget.should_start_new_session("reload", {"nonce": "n1"}) is True


def test_navigating_within_the_tab_keeps_the_session():
    """ui.navigate.to() is a full page load, so without this the conversation
    would reset on every sidebar click."""
    assert chat_widget.should_start_new_session("navigate", {"nonce": "n1"}) is False


def test_an_absent_or_empty_tab_store_starts_a_new_session():
    assert chat_widget.should_start_new_session("navigate", None) is True
    assert chat_widget.should_start_new_session("navigate", {}) is True


def test_a_stored_entry_with_no_nonce_starts_a_new_session():
    assert chat_widget.should_start_new_session("navigate", {"messages": []}) is True


def test_an_unknown_navigation_type_keeps_the_session():
    """If the JS probe fails we fall back to 'navigate' -- fail toward
    continuity; the New chat button is always there."""
    assert chat_widget.should_start_new_session("", {"nonce": "n1"}) is False


def test_new_tab_session_is_unique_and_empty():
    first, second = chat_widget.new_tab_session(), chat_widget.new_tab_session()
    assert first["nonce"] != second["nonce"]
    assert first["session_id"] is None
    assert first["messages"] == []


# ── attachments ───────────────────────────────────────────────────────────────
def test_attachment_data_url_prefers_inline_bytes():
    url = chat_widget.attachment_data_url({"data_b64": "AAA", "mime_type": "image/jpeg"})
    assert url == "data:image/jpeg;base64,AAA"


def test_attachment_data_url_defaults_the_mime_type():
    assert chat_widget.attachment_data_url({"data_b64": "AAA"}) == "data:image/png;base64,AAA"


def test_attachment_data_url_falls_back_to_a_remote_url():
    assert (
        chat_widget.attachment_data_url({"url": "https://example.com/c.png"})
        == "https://example.com/c.png"
    )


def test_attachment_data_url_is_none_when_there_is_nothing_to_show():
    assert chat_widget.attachment_data_url({}) is None
