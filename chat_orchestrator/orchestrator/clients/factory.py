"""Provider-aware chat client factory."""

from __future__ import annotations

from orchestrator.clients.gemini import GeminiClient
from orchestrator.clients.openrouter import OpenRouterClient
from orchestrator.config.settings import AppSettings, GeminiModelConfig


def create_chat_llm_client(
    settings: AppSettings,
    model_config: GeminiModelConfig | None = None,
) -> GeminiClient | OpenRouterClient:
    """Create the chat client selected by ``LLM_PROVIDER``."""

    config = model_config or settings.gemini
    provider = (settings.llm_provider or "gemini").strip().lower()
    if provider in {"openrouter", "open-router"}:
        return OpenRouterClient(
            api_key=settings.openrouter_api_key,
            model_config=config,
        )
    if provider != "gemini":
        raise ValueError(
            f"Unsupported LLM_PROVIDER={provider!r}; expected 'gemini' or 'openrouter'"
        )
    return GeminiClient(api_key=settings.google_api_key, model_config=config)


__all__ = ["create_chat_llm_client"]
