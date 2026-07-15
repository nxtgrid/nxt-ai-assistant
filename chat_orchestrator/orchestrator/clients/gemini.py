"""Compatibility client for Gemini generateContent calls."""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from orchestrator.config.settings import GeminiModelConfig
from shared.llm import GeminiGateway
from shared.utils.langfuse_utils import langfuse_observe
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class GeminiClient:
    """Legacy raw-payload wrapper backed by the shared GenAI SDK gateway."""

    def __init__(
        self,
        api_key: str,
        model_config: GeminiModelConfig,
        client: Optional[Any] = None,
        gateway: Optional[GeminiGateway] = None,
    ) -> None:
        self._api_key = api_key
        self._model_config = model_config
        self._gateway = gateway or GeminiGateway(
            api_key=api_key,
            client=client if client is not None and hasattr(client, "aio") else None,
            default_model=model_config.model,
            fallback_model=model_config.fallback_model,
        )
        self._client_to_close = client if client is not None and hasattr(client, "aclose") else None
        self._closed = False

    @langfuse_observe(as_type="generation", name="gemini-generation")
    async def generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Generate content from a legacy Gemini REST-style payload."""

        if not self._api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set; cannot call Gemini")

        LOGGER.info(f"Gemini API call using model: {self._model_config.model}")
        return await self._gateway.generate_content(payload, model=self._model_config.model)

    async def aclose(self) -> None:
        """Close an injected SDK client when it exposes an async close hook."""

        if not self._closed:
            if self._client_to_close is not None:
                await self._client_to_close.aclose()
            self._closed = True

    async def __aenter__(self) -> "GeminiClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


class MockGeminiClient(GeminiClient):
    """Testing helper that replays queued responses instead of hitting the API."""

    def __init__(self, responses: Optional[list[Dict[str, Any]]] = None):  # type: ignore[override]
        self._responses = responses or []
        self.recorded_payloads: list[Dict[str, Any]] = []
        super().__init__(api_key="test", model_config=GeminiModelConfig())

    async def generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        self.recorded_payloads.append(payload)
        if not self._responses:
            raise RuntimeError("No mock responses queued for MockGeminiClient")
        await asyncio.sleep(0)
        return self._responses.pop(0)


__all__ = ["GeminiClient", "MockGeminiClient"]
