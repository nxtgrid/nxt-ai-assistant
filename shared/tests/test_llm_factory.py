from shared.llm.factory import get_default_generation_gateway, is_generation_configured
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


# is_generation_configured() -- the provider-aware pre-flight check every
# GOOGLE_API_KEY-specific call site should have used instead of reading that
# var directly. See the grafana panel_description indexer incident,
# 2026-08-19, in factory.py's own docstring for why the direct-read pattern
# breaks silently under LLM_PROVIDER=openrouter.


def test_is_generation_configured_true_under_gemini_with_key_set(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    assert is_generation_configured() is True


def test_is_generation_configured_false_under_gemini_without_key(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert is_generation_configured() is False


def test_is_generation_configured_true_under_openrouter_with_key_set(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "openrouter-key")

    assert is_generation_configured() is True


def test_is_generation_configured_false_under_openrouter_without_key(monkeypatch):
    """The exact bug this helper exists to prevent: GOOGLE_API_KEY being set
    is NOT enough when the configured provider is openrouter -- checking the
    wrong provider's key would report "configured" when every real call
    would actually fail."""
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_API_KEY", "leftover-google-key")

    assert is_generation_configured() is False
