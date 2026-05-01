"""Client for Google Gemini generateContent API."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from typing import Any, Dict, Optional

import httpx

from orchestrator.config.settings import GeminiModelConfig
from shared.utils.langfuse_utils import langfuse_observe, update_generation
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Pattern to match API keys in URLs (key=xxx query parameter)
_API_KEY_PATTERN = re.compile(r"(\?|&)key=[^&\s]+", re.IGNORECASE)


def _sanitize_url(url: str) -> str:
    """Remove API key from URL for safe logging/error messages."""
    return _API_KEY_PATTERN.sub(r"\1key=***REDACTED***", str(url))


def _sanitize_error(error: Exception) -> str:
    """Sanitize error message to remove API keys."""
    return _API_KEY_PATTERN.sub(r"\1key=***REDACTED***", str(error))


def _is_quota_exhausted(error_body: str) -> bool:
    """Check if 429 error is daily quota exhausted (non-retryable).

    Daily quota errors have quotaMetric containing 'free_tier_requests'
    or 'paid_tier_requests' without a time-based suffix.

    RPM/TPM limits have 'per_minute' in the metric and are retryable.
    """
    try:
        import json

        error_data = json.loads(error_body)
        details = error_data.get("error", {}).get("details", [])

        for detail in details:
            if detail.get("@type") == "type.googleapis.com/google.rpc.QuotaFailure":
                for violation in detail.get("violations", []):
                    quota_metric = violation.get("quotaMetric", "")
                    # Daily quotas: 'free_tier_requests', 'paid_tier_requests'
                    # NOT containing '_per_minute' or '_per_day'
                    if "tier_requests" in quota_metric:
                        if "_per_minute" not in quota_metric:
                            return True  # Daily quota exhausted
    except Exception:
        pass

    # Default: assume retryable (RPM limit)
    return False


class GeminiClient:
    """Thin asynchronous wrapper around the Gemini REST API."""

    def __init__(
        self,
        api_key: str,
        model_config: GeminiModelConfig,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self._api_key = api_key
        self._model_config = model_config
        default_timeout = float(os.getenv("GEMINI_HTTP_TIMEOUT", "180"))
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(default_timeout))
        self._closed = False

    @staticmethod
    def _log_response_metrics(
        model_label: str, duration_ms: int, response_json: Dict[str, Any]
    ) -> None:
        """Log Gemini response timing and token usage."""
        usage = response_json.get("usageMetadata", {})
        LOGGER.info(
            f"Gemini {model_label}: {duration_ms}ms | "
            f"tokens in={usage.get('promptTokenCount', 0)} "
            f"out={usage.get('candidatesTokenCount', 0)} "
            f"thinking={usage.get('thoughtsTokenCount', 0)} "
            f"cached={usage.get('cachedContentTokenCount', 0)}"
        )

    @staticmethod
    def _update_langfuse_generation(model: str, result: Dict[str, Any]) -> None:
        """Update Langfuse generation with model and token usage."""
        usage = result.get("usageMetadata", {})
        update_generation(
            model=model,
            usage_details={
                "input": usage.get("promptTokenCount", 0),
                "output": usage.get("candidatesTokenCount", 0),
            },
        )

    @langfuse_observe(as_type="generation", name="gemini-generation")
    async def generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Call the Gemini generateContent endpoint with the provided payload.

        Implements exponential backoff with jitter for 429 rate limit errors.
        Falls back to the configured fallback model if rate limits are exhausted.
        AI Studio free tier limits:
        - Gemini Flash: 10 RPM (1 request per 6 seconds)
        - Gemini Pro: 5 RPM (1 request per 12 seconds)
        """

        if not self._api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set; cannot call Gemini")

        endpoint = self._model_config.endpoint()
        # Log endpoint without API key for security
        LOGGER.info(
            f"Gemini API call using model: {self._model_config.model}, "
            f"endpoint: {_sanitize_url(endpoint)}"
        )
        max_retries = 3
        base_delay = 2.0  # Start with 2 second delay
        used_fallback = False
        last_response = None

        for attempt in range(max_retries + 1):
            try:
                LOGGER.debug(f"Calling Gemini endpoint (attempt {attempt + 1}/{max_retries + 1})")

                # Log full payload at DEBUG level
                import json

                LOGGER.debug(f"Gemini payload: {json.dumps(payload, indent=2)}")

                t0 = time.monotonic()
                response = await self._client.post(
                    endpoint,
                    params={"key": self._api_key},
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                duration_ms = int((time.monotonic() - t0) * 1000)
                last_response = response

                if response.status_code == 200:
                    result = dict(response.json())
                    self._log_response_metrics(self._model_config.model, duration_ms, result)
                    self._update_langfuse_generation(self._model_config.model, result)
                    if used_fallback:
                        LOGGER.info(
                            f"Successfully completed request using {self._model_config.fallback_model} fallback"
                        )
                    return result

                # Handle rate limit (429) with exponential backoff
                if response.status_code == 429:
                    error_body = response.text

                    # Check if this is a quota exhausted error (daily limit)
                    # Don't retry - immediately fall back to alternate model
                    if _is_quota_exhausted(error_body):
                        LOGGER.warning(
                            f"Gemini daily quota exhausted - skipping retries, "
                            f"falling back immediately. Error: {error_body[:200]}"
                        )
                        break  # Exit loop, will trigger fallback

                    # Transient rate limit (RPM) - retry with backoff
                    if attempt < max_retries:
                        # Calculate exponential backoff with jitter
                        # For AI Studio: 10 RPM = 1 request per 6 seconds minimum
                        delay = min(base_delay * (2**attempt), 30.0)  # Cap at 30 seconds
                        jitter = random.uniform(0, delay * 0.3)  # Add up to 30% jitter
                        total_delay = delay + jitter

                        LOGGER.warning(
                            f"Gemini rate limit (429) hit on attempt {attempt + 1}. "
                            f"Retrying in {total_delay:.1f}s... "
                            f"Error: {error_body[:200]}"
                        )

                        await asyncio.sleep(total_delay)
                        continue
                    else:
                        # Exhausted retries with 429, will try fallback after loop
                        LOGGER.warning(
                            f"Rate limit (429) exhausted after {max_retries + 1} attempts"
                        )
                        break

                # For other non-429 errors, log and raise with sanitized message
                error_body = response.text
                LOGGER.error(f"Gemini API error {response.status_code}: {error_body}")
                # Raise custom error without exposing API key in URL
                raise RuntimeError(f"Gemini API error {response.status_code}: {error_body[:500]}")

            except httpx.HTTPStatusError as e:
                # Sanitize and re-raise HTTP errors
                raise RuntimeError(_sanitize_error(e)) from None
            except (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                # Transient network errors - retry with backoff
                LOGGER.error(f"Gemini API transient error: {_sanitize_error(e)}")
                if attempt < max_retries:
                    delay = base_delay * (2**attempt)
                    LOGGER.warning(f"Retrying after {delay}s...")
                    await asyncio.sleep(delay)
                    continue
                raise
            except Exception as e:
                # Non-transient errors (JSON parse, key errors, etc.) - fail immediately
                LOGGER.error(f"Gemini API request failed (non-retryable): {_sanitize_error(e)}")
                raise

        # If we exhausted all retries with 429, try fallback model
        if last_response and last_response.status_code == 429:
            LOGGER.warning(
                f"Rate limit exhausted for {self._model_config.model}. "
                f"Falling back to {self._model_config.fallback_model}..."
            )
            fallback_endpoint = self._model_config.fallback_endpoint()
            used_fallback = True

            try:
                LOGGER.debug("Calling fallback endpoint")
                t0 = time.monotonic()
                response = await self._client.post(
                    fallback_endpoint,
                    params={"key": self._api_key},
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                if response.status_code == 200:
                    result = dict(response.json())
                    self._log_response_metrics(
                        f"{self._model_config.fallback_model} (fallback)",
                        duration_ms,
                        result,
                    )
                    self._update_langfuse_generation(self._model_config.fallback_model, result)
                    return result

                error_body = response.text
                LOGGER.error(f"Fallback also failed with {response.status_code}: {error_body}")
                raise RuntimeError(
                    f"Gemini fallback API error {response.status_code}: {error_body[:500]}"
                )

            except Exception as e:
                LOGGER.error(f"Fallback request failed: {_sanitize_error(e)}")
                raise RuntimeError(_sanitize_error(e)) from None

        # If we get here, raise error without exposing API key
        if last_response:
            raise RuntimeError(
                f"Gemini API error {last_response.status_code}: {last_response.text[:500]}"
            )
        else:
            raise RuntimeError("No response received from Gemini API")

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""

        if not self._closed:
            await self._client.aclose()
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
        super().__init__(
            api_key="test", model_config=GeminiModelConfig(), client=httpx.AsyncClient()
        )

    async def generate_content(self, payload: Dict[str, Any]) -> Dict[str, Any]:  # type: ignore[override]
        self.recorded_payloads.append(payload)
        if not self._responses:
            raise RuntimeError("No mock responses queued for MockGeminiClient")
        await asyncio.sleep(0)
        return self._responses.pop(0)


__all__ = ["GeminiClient", "MockGeminiClient"]
