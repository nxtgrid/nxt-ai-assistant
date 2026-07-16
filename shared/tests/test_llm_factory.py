from shared.llm.factory import get_default_generation_gateway
from shared.llm.gemini import GeminiGateway
from shared.llm.openrouter import OpenRouterGateway


def test_default_generation_gateway_uses_gemini_by_default(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")

    gateway = get_default_generation_gateway()

    assert isinstance(gateway, GeminiGateway)


def test_default_generation_gateway_can_select_openrouter(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o")
    monkeypatch.setenv("OPENROUTER_HTTP_REFERER", "https://example.com")
    monkeypatch.setenv("OPENROUTER_APP_TITLE", "Anansi")

    gateway = get_default_generation_gateway()

    assert isinstance(gateway, OpenRouterGateway)
    assert gateway._api_key == "openrouter-key"
    assert gateway._default_model == "openai/gpt-4o"
    assert gateway._http_referer == "https://example.com"
    assert gateway._app_title == "Anansi"


def test_explicit_api_key_and_model_override_openrouter_env(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "env-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "env/model")

    gateway = get_default_generation_gateway(
        api_key="explicit-key",
        default_model="explicit/model",
    )

    assert isinstance(gateway, OpenRouterGateway)
    assert gateway._api_key == "explicit-key"
    assert gateway._default_model == "explicit/model"


def test_openrouter_factory_accepts_bearer_token_alias(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER_BEARER_TOKEN", "alias-key")

    gateway = get_default_generation_gateway()

    assert isinstance(gateway, OpenRouterGateway)
    assert gateway._api_key == "alias-key"
