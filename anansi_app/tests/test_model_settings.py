from __future__ import annotations

import sys
from types import SimpleNamespace

from services.settings_service import SettingsService

sys.modules.setdefault(
    "nicegui",
    SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()),
)
from nicegui_app.pages import settings as settings_page


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_gemini_models_are_fetched_and_normalized(monkeypatch):
    def fake_get(url, *, params=None, timeout=None, headers=None):
        assert "generativelanguage.googleapis.com" in url
        assert params == {"key": "google-key"}
        return FakeResponse(
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-flash",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/text-embedding-004",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            }
        )

    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.setattr("requests.get", fake_get)

    assert SettingsService().get_gemini_models() == ["gemini-2.5-flash"]


def test_openrouter_models_are_fetched_and_sorted(monkeypatch):
    def fake_get(url, *, timeout=None, headers=None):
        assert url == "https://openrouter.ai/api/v1/models"
        return FakeResponse(
            {
                "data": [
                    {"id": "openai/gpt-oss-20b:free", "name": "GPT OSS"},
                    {"id": "google/gemini-2.5-flash", "name": "Gemini Flash"},
                    {"id": "", "name": "bad"},
                ]
            }
        )

    monkeypatch.setattr("requests.get", fake_get)

    assert SettingsService().get_openrouter_models() == [
        "google/gemini-2.5-flash",
        "openai/gpt-oss-20b:free",
    ]


def test_openrouter_model_provider_routes_are_fetched_for_selected_model(monkeypatch):
    def fake_get(url, *, timeout=None, headers=None):
        assert url == "https://openrouter.ai/api/v1/models/google/gemini-2.5-flash/endpoints"
        assert headers == {"Authorization": "Bearer openrouter-key"}
        return FakeResponse(
            {
                "data": {
                    "endpoints": [
                        {
                            "tag": "google-vertex",
                            "provider_name": "Google",
                            "name": "Google | google/gemini-2.5-flash",
                        },
                        {
                            "tag": "google-ai-studio/flex",
                            "provider_name": "Google AI Studio",
                            "name": "Google AI Studio | google/gemini-2.5-flash",
                        },
                        {"tag": "", "provider_name": "bad"},
                    ]
                }
            }
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setattr("requests.get", fake_get)

    assert SettingsService().get_openrouter_provider_routes("google/gemini-2.5-flash") == {
        "google-ai-studio/flex": "Google AI Studio | google/gemini-2.5-flash",
        "google-vertex": "Google | google/gemini-2.5-flash",
    }


def test_openrouter_model_provider_routes_fall_back_to_common_slugs(monkeypatch):
    def fake_get(url, *, timeout=None, headers=None):
        raise RuntimeError("offline")

    monkeypatch.setattr("requests.get", fake_get)

    assert SettingsService().get_openrouter_provider_routes("google/gemini-2.5-flash") == {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    }


def test_openrouter_provider_route_parser_accepts_legacy_list_payload():
    assert SettingsService._parse_openrouter_provider_routes(
        {
            "data": [
                    {
                    "tag": "openai",
                    "provider_name": "OpenAI",
                    "name": "OpenAI | gpt-4o",
                    },
            ]
        }
    ) == {"openai": "OpenAI | gpt-4o"}


def test_model_setting_options_keep_provider_contexts_separate():
    opts = settings_page._model_select_options(
        SimpleNamespace(
            get_llm_provider_options=lambda: {
                "gemini": "Gemini (Google direct)",
                "openrouter": "OpenRouter",
            },
            get_gemini_models=lambda: ["gemini-2.5-flash"],
            get_openrouter_models=lambda: ["google/gemini-2.5-flash"],
            get_openrouter_provider_routes=lambda model: {
                "google-vertex": "Google Vertex",
                "google-ai-studio": "Google AI Studio",
            },
        ),
        {"OPENROUTER_MODEL": "google/gemini-2.5-flash"},
    )

    assert opts["LLM_PROVIDER"] == {
        "gemini": "Gemini (Google direct)",
        "openrouter": "OpenRouter",
    }
    assert opts["GEMINI_MODEL"] == ["gemini-2.5-flash"]
    assert opts["GEMINI_FALLBACK_MODEL"] == ["gemini-2.5-flash"]
    assert opts["OPENROUTER_MODEL"] == ["google/gemini-2.5-flash"]
    assert opts["OPENROUTER_PROVIDER_ORDER"] == {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    }


def test_model_setting_options_use_fallback_model_for_provider_routes():
    opts = settings_page._model_select_options(
        SimpleNamespace(
            get_llm_provider_options=lambda: {},
            get_gemini_models=lambda: [],
            get_openrouter_models=lambda: ["google/gemini-2.5-flash"],
            get_openrouter_provider_routes=lambda model: {"seen": model},
        ),
        {},
    )

    assert opts["OPENROUTER_PROVIDER_ORDER"] == {"seen": "google/gemini-2.5-flash"}
