"""Google GenAI SDK implementation of the provider-neutral LLM gateway."""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from typing import Any, Callable

from shared.llm.types import GenerateResult, GenerationOptions, LLMMessage, ToolSpec, Usage
from shared.utils.langfuse_utils import update_generation
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

_API_KEY_PATTERN = re.compile(r"(\?|&)key=[^&\s]+", re.IGNORECASE)


def _sanitize_text(value: object, api_key: str | None = None) -> str:
    text = _API_KEY_PATTERN.sub(r"\1key=***REDACTED***", str(value))
    if api_key:
        text = text.replace(api_key, "***REDACTED***")
    return text


def _get_value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _status_code(exc: Exception) -> int | None:
    status = _get_value(exc, "status_code", "status", "code")
    if status is None and hasattr(exc, "response"):
        status = _get_value(exc.response, "status_code", "status")
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_quota_exhausted(error_text: str) -> bool:
    try:
        data = json.loads(error_text)
    except Exception:
        data = {}
    details = data.get("error", {}).get("details", []) if isinstance(data, dict) else []
    for detail in details:
        if detail.get("@type") != "type.googleapis.com/google.rpc.QuotaFailure":
            continue
        for violation in detail.get("violations", []):
            metric = violation.get("quotaMetric", "")
            if "tier_requests" in metric and "_per_minute" not in metric:
                return True
    return False


class GeminiGateway:
    """Provider-neutral generation gateway backed by the Google GenAI SDK."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        default_model: str | None = None,
        fallback_model: str | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Any] | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY")
        self._client = client
        self._default_model = default_model or os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
        self._fallback_model = fallback_model or os.getenv("GEMINI_FALLBACK_MODEL")
        self._max_retries = max_retries
        self._sleep = sleep or asyncio.sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("GOOGLE_API_KEY is not set; cannot create Gemini client")
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def generate(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None = None,
    ) -> GenerateResult:
        model = options.model or self._default_model
        try:
            return await self._generate_for_model(model, messages, options, tools)
        except Exception as exc:
            if self._fallback_model and _status_code(exc) == 429:
                LOGGER.warning(
                    "Gemini model %s rate-limited; falling back to %s",
                    model,
                    self._fallback_model,
                )
                fallback_options = GenerationOptions(
                    model=self._fallback_model,
                    temperature=options.temperature,
                    max_output_tokens=options.max_output_tokens,
                    response_format=options.response_format,
                    thinking=options.thinking,
                )
                return await self._generate_for_model(
                    self._fallback_model,
                    messages,
                    fallback_options,
                    tools,
                    retry=False,
                )
            raise RuntimeError(_sanitize_text(exc, self._api_key)) from exc

    async def _generate_for_model(
        self,
        model: str,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None,
        *,
        retry: bool = True,
    ) -> GenerateResult:
        attempts = self._max_retries + 1 if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return await self._call_once(model, messages, options, tools)
            except Exception as exc:
                last_exc = exc
                status = _status_code(exc)
                if status == 429 and _is_quota_exhausted(str(exc)):
                    break
                if status != 429 or attempt >= attempts - 1:
                    break
                delay = min(2.0 * (2**attempt), 30.0)
                delay += random.uniform(0, delay * 0.3)
                LOGGER.warning(
                    "Gemini rate limit for %s on attempt %s/%s; retrying in %.1fs",
                    model,
                    attempt + 1,
                    attempts,
                    delay,
                )
                await self._sleep(delay)
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No response received from Gemini API")

    async def _call_once(
        self,
        model: str,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None,
    ) -> GenerateResult:
        contents, system_instruction = self._convert_messages(messages)
        config = self._build_config(options, system_instruction, tools)
        t0 = time.monotonic()
        response = await self.client.aio.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )
        duration_ms = int((time.monotonic() - t0) * 1000)
        result = self._convert_response(response)
        self._log_metrics(model, duration_ms, result.usage)
        self._update_langfuse(model, result.usage)
        return result

    @staticmethod
    def _convert_messages(messages: list[LLMMessage]) -> tuple[list[dict[str, Any]], str | None]:
        contents: list[dict[str, Any]] = []
        system_parts: list[str] = []
        for message in messages:
            if message.role == "system":
                if message.text:
                    system_parts.append(message.text)
                continue
            role = "model" if message.role == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": message.text or ""}]})
        system_instruction = "\n\n".join(system_parts) if system_parts else None
        return contents, system_instruction

    @staticmethod
    def _build_config(
        options: GenerationOptions,
        system_instruction: str | None,
        tools: list[ToolSpec] | None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if system_instruction:
            config["system_instruction"] = system_instruction
        if options.temperature is not None:
            config["temperature"] = options.temperature
        if options.max_output_tokens is not None:
            config["max_output_tokens"] = options.max_output_tokens
        if options.response_format == "json":
            config["response_mime_type"] = "application/json"
        if options.thinking == "off":
            config["thinking_config"] = {"thinking_budget": 0}
        elif options.thinking in ("medium", "high"):
            config["thinking_config"] = {"thinking_level": options.thinking}
        if tools:
            config["tools"] = [
                {
                    "function_declarations": [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters_json_schema,
                        }
                        for tool in tools
                    ]
                }
            ]
        return config

    @staticmethod
    def _convert_response(response: Any) -> GenerateResult:
        usage = _get_value(response, "usage_metadata", "usageMetadata", default={})
        candidates = _get_value(response, "candidates", default=[]) or []
        first_candidate = candidates[0] if candidates else None
        finish_reason = _get_value(first_candidate, "finish_reason", "finishReason")
        if finish_reason is not None:
            finish_reason = str(finish_reason)
        return GenerateResult(
            text=str(_get_value(response, "text", default="") or ""),
            usage=Usage(
                input_tokens=int(_get_value(usage, "prompt_token_count", "promptTokenCount", default=0) or 0),
                output_tokens=int(
                    _get_value(usage, "candidates_token_count", "candidatesTokenCount", default=0)
                    or 0
                ),
                thinking_tokens=int(
                    _get_value(usage, "thoughts_token_count", "thoughtsTokenCount", default=0) or 0
                ),
                cached_tokens=int(
                    _get_value(
                        usage,
                        "cached_content_token_count",
                        "cachedContentTokenCount",
                        default=0,
                    )
                    or 0
                ),
            ),
            finish_reason=finish_reason,
            raw=response,
        )

    @staticmethod
    def _log_metrics(model: str, duration_ms: int, usage: Usage) -> None:
        LOGGER.info(
            "Gemini %s: %sms | tokens in=%s out=%s thinking=%s cached=%s",
            model,
            duration_ms,
            usage.input_tokens,
            usage.output_tokens,
            usage.thinking_tokens,
            usage.cached_tokens,
        )

    @staticmethod
    def _update_langfuse(model: str, usage: Usage) -> None:
        try:
            update_generation(
                model=model,
                usage_details={
                    "input": usage.input_tokens,
                    "output": usage.output_tokens,
                },
            )
        except Exception:
            LOGGER.debug("Skipping Langfuse generation update", exc_info=True)
