from __future__ import annotations

from types import SimpleNamespace

from nicegui_app.pages import settings as settings_page
from services.settings_service import SettingsService


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
        {"LLM_PROVIDER": "gemini", "MODEL_FAST": "gemini-2.5-flash"},
    )

    assert opts["LLM_PROVIDER"] == {
        "gemini": "Gemini (Google direct)",
        "openrouter": "OpenRouter",
    }
    assert opts["MODEL_FAST"] == ["gemini-2.5-flash"]
    assert opts["FALLBACK_MODEL"] == ["gemini-2.5-flash"]
    assert "OPENROUTER_MODEL" not in opts
    assert opts["OPENROUTER_PROVIDER_ORDER"] == {}


def test_model_setting_options_use_openrouter_models_for_role_fields():
    opts = settings_page._model_select_options(
        SimpleNamespace(
            get_llm_provider_options=lambda: {},
            get_gemini_models=lambda: ["gemini-2.5-flash"],
            get_openrouter_models=lambda: ["google/gemini-2.5-flash"],
            get_openrouter_provider_routes=lambda model: {
                "google-vertex": "Google Vertex",
                "google-ai-studio": "Google AI Studio",
            },
        ),
        {"LLM_PROVIDER": "openrouter", "MODEL_FAST": "google/gemini-2.5-flash"},
    )

    for key in (
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
        "FALLBACK_MODEL",
    ):
        assert opts[key] == ["google/gemini-2.5-flash"]
    assert "OPENROUTER_MODEL" not in opts
    assert opts["OPENROUTER_PROVIDER_ORDER"] == {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    }


def test_model_setting_options_use_role_model_for_provider_routes():
    opts = settings_page._model_select_options(
        SimpleNamespace(
            get_llm_provider_options=lambda: {},
            get_gemini_models=lambda: [],
            get_openrouter_models=lambda: ["google/gemini-2.5-flash"],
            get_openrouter_provider_routes=lambda model: {"seen": model},
        ),
        {"LLM_PROVIDER": "openrouter", "MODEL_FAST": "google/gemini-3.1-flash-lite"},
    )

    assert opts["OPENROUTER_PROVIDER_ORDER"] == {"seen": "google/gemini-3.1-flash-lite"}


def test_provider_change_normalizes_role_model_values():
    pending = {
        "LLM_PROVIDER": "gemini",
        "MODEL_THINKING": "gemini-pro-latest",
        "MODEL_FAST": "gemini-2.5-flash",
        "MODEL_LITE": "gemini-3.1-flash-lite",
        "FALLBACK_MODEL": "",
    }

    changes = settings_page._apply_llm_provider_change(pending, "openrouter")

    assert pending["LLM_PROVIDER"] == "openrouter"
    assert pending["MODEL_THINKING"] == "google/gemini-pro-latest"
    assert pending["MODEL_FAST"] == "google/gemini-2.5-flash"
    assert pending["MODEL_LITE"] == "google/gemini-3.1-flash-lite"
    assert pending["FALLBACK_MODEL"] == ""
    assert changes == {
        "LLM_PROVIDER",
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
    }

    changes = settings_page._apply_llm_provider_change(pending, "gemini")

    assert pending["LLM_PROVIDER"] == "gemini"
    assert pending["MODEL_THINKING"] == "gemini-pro-latest"
    assert pending["MODEL_FAST"] == "gemini-2.5-flash"
    assert pending["MODEL_LITE"] == "gemini-3.1-flash-lite"
    assert changes == {
        "LLM_PROVIDER",
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
    }


def test_model_section_visibility_is_provider_specific():
    names = [
        "LLM_PROVIDER",
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
        "FALLBACK_MODEL",
        "OPENROUTER_MODEL",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    ]
    gemini_plan = settings_page._model_section_plan(names, {"LLM_PROVIDER": "gemini"})
    openrouter_plan = settings_page._model_section_plan(names, {"LLM_PROVIDER": "openrouter"})

    assert gemini_plan.show_openrouter_routes is False
    assert gemini_plan.primary_keys == [
        "LLM_PROVIDER",
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
        "FALLBACK_MODEL",
    ]
    assert "OPENROUTER_MODEL" not in gemini_plan.primary_keys + gemini_plan.remaining_keys
    assert "OPENROUTER_PROVIDER_ORDER" not in gemini_plan.primary_keys + gemini_plan.remaining_keys
    assert "OPENROUTER_ALLOW_FALLBACKS" not in gemini_plan.primary_keys + gemini_plan.remaining_keys

    assert openrouter_plan.show_openrouter_routes is True
    assert openrouter_plan.primary_keys == [
        "LLM_PROVIDER",
        "MODEL_THINKING",
        "MODEL_FAST",
        "MODEL_LITE",
        "FALLBACK_MODEL",
        "OPENROUTER_PROVIDER_ORDER",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    ]
    assert "OPENROUTER_MODEL" not in openrouter_plan.primary_keys + openrouter_plan.remaining_keys


def test_openrouter_route_fallbacks_follow_selected_main_model_provider():
    assert settings_page._route_fallbacks_for_model("google/gemini-2.5-flash") == {
        "google-vertex": "Google Vertex",
        "google-ai-studio": "Google AI Studio",
    }
    assert settings_page._route_fallbacks_for_model("openai/gpt-4o") == {
        "openai": "OpenAI"
    }
    assert settings_page._route_fallbacks_for_model("unknown/model") == {}


def test_common_generation_controls_have_provider_neutral_labels():
    # Labels moved from a page-local _MODEL_LABELS table onto the registry
    # (Flag.label / display_label) in the settings-ux-redesign, and read-only
    # status is now conveyed by a set_via hint under RenderMode.READ_ONLY
    # rather than a "(read-only)" suffix baked into the label itself.
    from shared.config import flag_registry as fr

    assert fr.FLAGS["MODEL_THINKING"].display_label == "Thinking-tier model"
    assert fr.FLAGS["MODEL_FAST"].display_label == "Fast-tier model"
    assert fr.FLAGS["MODEL_LITE"].display_label == "Lite-tier model"
    assert fr.FLAGS["FALLBACK_MODEL"].display_label == "Fallback model"
    assert fr.FLAGS["GEMINI_TEMPERATURE"].display_label == "Temperature"
    assert fr.FLAGS["GEMINI_MAX_OUTPUT_TOKENS"].display_label == "Main model max output tokens"
    assert fr.FLAGS["GEMINI_MAX_OUTPUT_TOKENS"].editable is True
    assert fr.FLAGS["GEMINI_LITE_MAX_OUTPUT_TOKENS"].display_label == (
        "Lite model max output tokens"
    )
    assert fr.FLAGS["GEMINI_LITE_MAX_OUTPUT_TOKENS"].editable is False
    assert fr.FLAGS["GEMINI_LITE_MAX_OUTPUT_TOKENS"].set_via
