"""Tests for chat-database credential resolution (CHAT_DB_* with SUPABASE_* fallback)."""

import pytest

from shared.config.db_credentials import chat_db_service_key, chat_db_url

_URL_VARS = ["CHAT_DB_URL", "SUPABASE_URL"]
_KEY_VARS = ["CHAT_DB_SERVICE_KEY", "SUPABASE_KEY"]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _URL_VARS + _KEY_VARS:
        monkeypatch.delenv(var, raising=False)


def test_url_prefers_new_name(monkeypatch):
    monkeypatch.setenv("CHAT_DB_URL", "new")
    monkeypatch.setenv("SUPABASE_URL", "legacy")
    assert chat_db_url() == "new"


def test_url_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "legacy")
    assert chat_db_url() == "legacy"


def test_url_empty_new_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("CHAT_DB_URL", "")
    monkeypatch.setenv("SUPABASE_URL", "legacy")
    assert chat_db_url() == "legacy"


def test_url_returns_empty_string_when_unset():
    assert chat_db_url() == ""


def test_service_key_prefers_new_name(monkeypatch):
    monkeypatch.setenv("CHAT_DB_SERVICE_KEY", "new")
    monkeypatch.setenv("SUPABASE_KEY", "legacy")
    assert chat_db_service_key() == "new"


def test_service_key_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv("SUPABASE_KEY", "legacy")
    assert chat_db_service_key() == "legacy"


def test_service_key_returns_empty_string_when_unset():
    assert chat_db_service_key() == ""
