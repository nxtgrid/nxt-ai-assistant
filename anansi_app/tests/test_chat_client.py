"""Tests for the admin-app chat HTTP client.

Pins the request shape POST /chat needs (source, identity-assertion header,
the per-tab user_id that keeps sessions from collapsing into one), and the
response shape handler.py's direct-API path returns.
"""

from __future__ import annotations

import pytest
import requests
from nicegui_app import chat_client


# ── base url ──────────────────────────────────────────────────────────────────
def test_base_url_strips_a_trailing_slash(monkeypatch):
    monkeypatch.setenv("CHAT_ORCHESTRATOR_URL", "http://orchestrator:8000/")
    assert chat_client.orchestrator_base_url() == "http://orchestrator:8000"


def test_base_url_tolerates_the_env_example_value_ending_in_chat(monkeypatch):
    """anansi_app/.env.example ships this ending in /chat; the DO spec sets the
    bare service URL. Both must produce one /chat, not /chat/chat."""
    monkeypatch.setenv("CHAT_ORCHESTRATOR_URL", "http://localhost:8000/chat")
    assert chat_client.orchestrator_base_url() == "http://localhost:8000"


def test_base_url_defaults_to_localhost(monkeypatch):
    monkeypatch.delenv("CHAT_ORCHESTRATOR_URL", raising=False)
    assert chat_client.orchestrator_base_url() == "http://localhost:8000"


# ── headers ───────────────────────────────────────────────────────────────────
def test_headers_carry_both_secrets(monkeypatch):
    monkeypatch.setenv("API_KEY", "k1")
    monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "k2")
    headers = chat_client.orchestrator_headers()
    assert headers["X-Api-Key"] == "k1"
    assert headers["X-Identity-Assertion-Key"] == "k2"


def test_identity_configured_is_false_without_the_key(monkeypatch):
    monkeypatch.delenv("IDENTITY_ASSERTION_KEY", raising=False)
    assert chat_client.identity_configured() is False


# ── payload ───────────────────────────────────────────────────────────────────
def _payload(**kwargs):
    base = {
        "message": "hello",
        "user_email": "admin@example.com",
        "tab_nonce": "abc123",
        "is_bot_admin": True,
    }
    base.update(kwargs)
    return chat_client.build_payload(**base)


def test_payload_user_id_is_unique_per_tab_nonce():
    """generate_session_id hashes user_id for DMs, so a bare email would make
    every tab share one never-ending session."""
    assert _payload()["user_id"] == "anansi-app:admin@example.com:abc123"
    assert _payload(tab_nonce="zzz")["user_id"] != _payload()["user_id"]


def test_payload_declares_the_web_source():
    assert _payload()["source"] == "web"


def test_payload_opts_into_admin_app_auth():
    metadata = _payload()["metadata"]
    assert metadata["admin_app_auth"] is True
    assert metadata["admin_app_bot_admin"] is True


def test_payload_titles_the_session_so_it_is_identifiable_in_the_chats_viewer():
    assert _payload()["metadata"]["chat_title"] == "Anansi App — admin@example.com"


def test_payload_omits_entity_context_when_nothing_is_attached():
    assert "entity_context" not in _payload(entity_context=None)


def test_payload_includes_entity_context_when_attached():
    ctx = {"grid_id": "17", "additional_context": {"Page": "Grid: Alpha"}}
    assert _payload(entity_context=ctx)["entity_context"] == ctx


# ── response parsing ──────────────────────────────────────────────────────────
def test_parse_response_reads_the_envelope():
    turn = chat_client.parse_response(
        {
            "success": True,
            "message": "**Grid Alpha** is online.",
            "session_id": "web_dm_abcd",
            "attachments": [{"kind": "image", "data_b64": "iVBOR", "mime_type": "image/png"}],
            "choices": [{"label": "Yes", "value": "decision:1:yes"}],
            "tool_calls": ["customer_get_all_grids_status"],
            "scope": {"is_staff": True, "organization": "yourorg"},
        }
    )
    assert turn.text == "**Grid Alpha** is online."
    assert turn.session_id == "web_dm_abcd"
    assert turn.attachments[0]["data_b64"] == "iVBOR"
    assert turn.choices[0]["label"] == "Yes"
    assert turn.tool_calls == ["customer_get_all_grids_status"]
    assert turn.scope_label() == "Staff · yourorg"


def test_parse_response_tolerates_a_bare_body():
    turn = chat_client.parse_response({"success": True, "message": "hi"})
    assert turn.attachments == [] and turn.choices == [] and turn.tool_calls == []
    assert turn.scope_label() == ""


def test_parse_response_raises_on_an_unsuccessful_body():
    """BOT_ENABLED=false returns HTTP 200 with success=False."""
    with pytest.raises(chat_client.ChatTurnError) as exc:
        chat_client.parse_response({"success": False, "message": "Bot is currently disabled."})
    assert "disabled" in str(exc.value)


def test_scope_label_falls_back_to_the_org_when_not_staff():
    turn = chat_client.parse_response(
        {"success": True, "message": "x", "scope": {"is_staff": False, "organization": "7"}}
    )
    assert turn.scope_label() == "7"


# ── send_turn wire contract ──────────────────────────────────────────────────
class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._body


def test_send_turn_posts_to_the_chat_endpoint(monkeypatch):
    monkeypatch.setenv("CHAT_ORCHESTRATOR_URL", "http://orchestrator:8000")
    monkeypatch.setenv("API_KEY", "k1")
    monkeypatch.setenv("IDENTITY_ASSERTION_KEY", "k2")
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json, timeout=timeout)
        return _FakeResponse({"success": True, "message": "ok", "session_id": "web_dm_1"})

    monkeypatch.setattr(chat_client.requests, "post", fake_post)

    turn = chat_client.send_turn(
        message="status?", user_email="admin@example.com", tab_nonce="n1", is_bot_admin=True
    )

    assert captured["url"] == "http://orchestrator:8000/chat"
    assert captured["headers"]["X-Identity-Assertion-Key"] == "k2"
    assert captured["json"]["source"] == "web"
    assert captured["timeout"] == chat_client.DEFAULT_TIMEOUT_SECONDS
    assert turn.session_id == "web_dm_1"


def test_error_detail_prefers_the_orchestrator_message(monkeypatch):
    exc = requests.HTTPError(response=_FakeResponse({"message": "not registered"}, status=403))
    assert chat_client.error_detail(exc) == "not registered"
