"""OpenRouter implementation of the provider-neutral generation gateway."""

from __future__ import annotations

import json
import os
from typing import Any

import httpx

from shared.llm.types import (
    GenerateResult,
    GenerationOptions,
    LLMConversationState,
    LLMMessage,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from shared.utils.langfuse_utils import update_generation
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class OpenRouterGateway:
    """Provider-neutral generation gateway backed by OpenRouter chat completions."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        async_client: Any | None = None,
        default_model: str | None = None,
        base_url: str | None = None,
        http_referer: str | None = None,
        app_title: str | None = None,
        provider_order: list[str] | None = None,
        allow_fallbacks: bool | None = None,
        require_parameters: bool | None = None,
    ) -> None:
        self._api_key = (
            api_key
            or os.getenv("OPENROUTER_API_KEY")
            or os.getenv("OPEN_ROUTER_BEARER_TOKEN")
        )
        self._client = client
        self._async_client = async_client
        self._default_model = normalize_openrouter_model(
            default_model or os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
        )
        self._base_url = (
            base_url
            or os.getenv("OPENROUTER_BASE_URL")
            or "https://openrouter.ai/api/v1"
        ).rstrip("/")
        self._http_referer = http_referer or os.getenv("OPENROUTER_HTTP_REFERER")
        self._app_title = app_title or os.getenv("OPENROUTER_APP_TITLE", "Anansi")
        self._provider_order = provider_order or _csv_env("OPENROUTER_PROVIDER_ORDER")
        self._allow_fallbacks = (
            allow_fallbacks
            if allow_fallbacks is not None
            else _optional_bool_env("OPENROUTER_ALLOW_FALLBACKS")
        )
        self._require_parameters = (
            require_parameters
            if require_parameters is not None
            else _optional_bool_env("OPENROUTER_REQUIRE_PARAMETERS")
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = httpx.Client(timeout=120.0)
        return self._client

    @property
    def async_client(self) -> Any:
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(timeout=120.0)
        return self._async_client

    async def generate(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None = None,
        tool_results: list[ToolResult] | None = None,
        conversation_state: LLMConversationState | None = None,
    ) -> GenerateResult:
        payload = self._build_payload(
            messages, options, tools, tool_results, conversation_state
        )
        response = await self.async_client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        parsed = self._convert_response(result)
        self._log_metrics(parsed)
        self._update_langfuse(parsed)
        return parsed

    def generate_sync(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None = None,
        tool_results: list[ToolResult] | None = None,
        conversation_state: LLMConversationState | None = None,
    ) -> GenerateResult:
        payload = self._build_payload(
            messages, options, tools, tool_results, conversation_state
        )
        response = self.client.post(
            f"{self._base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
        )
        response.raise_for_status()
        result = response.json()
        parsed = self._convert_response(result)
        self._log_metrics(parsed)
        self._update_langfuse(parsed)
        return parsed

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set; cannot call OpenRouter")
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._http_referer:
            headers["HTTP-Referer"] = self._http_referer
        if self._app_title:
            headers["X-OpenRouter-Title"] = self._app_title
        return headers

    def _build_payload(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None,
        tool_results: list[ToolResult] | None,
        conversation_state: LLMConversationState | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": normalize_openrouter_model(options.model or self._default_model),
            "messages": self._convert_messages(
                messages, tool_results, conversation_state
            ),
        }
        if options.temperature is not None:
            payload["temperature"] = options.temperature
        if options.max_output_tokens is not None:
            payload["max_tokens"] = options.max_output_tokens
        if options.response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        if tools:
            payload["tools"] = [self._convert_tool(tool) for tool in tools]
        provider = self._provider_preferences()
        if provider:
            payload["provider"] = provider
        return payload

    def _provider_preferences(self) -> dict[str, Any]:
        provider: dict[str, Any] = {}
        if self._provider_order:
            provider["order"] = self._provider_order
        if self._allow_fallbacks is not None:
            provider["allow_fallbacks"] = self._allow_fallbacks
        if self._require_parameters is not None:
            provider["require_parameters"] = self._require_parameters
        return provider

    @staticmethod
    def _convert_messages(
        messages: list[LLMMessage],
        tool_results: list[ToolResult] | None,
        conversation_state: LLMConversationState | None,
    ) -> list[dict[str, Any]]:
        converted = [OpenRouterGateway._convert_message(message) for message in messages]
        state_messages = conversation_state.messages if conversation_state else []
        for state_message in state_messages:
            openrouter_message = state_message.provider_state.get("openrouter_message")
            if openrouter_message:
                converted.append(openrouter_message)
            elif state_message.text:
                converted.append({"role": "assistant", "content": state_message.text})
        for tool_result in tool_results or []:
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_result.call_id,
                    "name": tool_result.name,
                    "content": OpenRouterGateway._stringify_tool_result(tool_result.result),
                }
            )
        return converted

    @staticmethod
    def _convert_message(message: LLMMessage) -> dict[str, Any]:
        openrouter_message = message.provider_state.get("openrouter_message")
        if openrouter_message:
            return openrouter_message
        if message.role == "tool":
            return {
                "role": "tool",
                "tool_call_id": message.tool_call_id or "",
                "content": message.text or "",
            }
        role = "assistant" if message.role == "assistant" else message.role
        return {"role": role, "content": message.text or ""}

    @staticmethod
    def _convert_tool(tool: ToolSpec) -> dict[str, Any]:
        parameters = tool.parameters_json_schema or {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": parameters,
            },
        }

    @staticmethod
    def _convert_response(response: dict[str, Any]) -> GenerateResult:
        choices = response.get("choices") or []
        first_choice = choices[0] if choices else {}
        message = first_choice.get("message") or {}
        text = message.get("content") or ""
        tool_calls = OpenRouterGateway._extract_tool_calls(
            message.get("tool_calls") or []
        )
        usage = OpenRouterGateway._extract_usage(response.get("usage") or {})
        return GenerateResult(
            text=str(text),
            tool_calls=tool_calls,
            usage=usage,
            finish_reason=first_choice.get("finish_reason"),
            conversation_state=OpenRouterGateway._extract_conversation_state(message),
            raw=response,
        )

    @staticmethod
    def _extract_tool_calls(raw_tool_calls: list[dict[str, Any]]) -> list[ToolCall]:
        tool_calls: list[ToolCall] = []
        for index, raw_call in enumerate(raw_tool_calls):
            function = raw_call.get("function") or {}
            raw_args = function.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (TypeError, ValueError):
                args = {}
            tool_calls.append(
                ToolCall(
                    id=str(raw_call.get("id") or f"tool-call-{index}"),
                    name=str(function.get("name") or ""),
                    args=args,
                    provider_state={"provider": "openrouter"},
                )
            )
        return tool_calls

    @staticmethod
    def _extract_usage(usage: dict[str, Any]) -> Usage:
        prompt_details = usage.get("prompt_tokens_details") or {}
        completion_details = usage.get("completion_tokens_details") or {}
        return Usage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            thinking_tokens=int(completion_details.get("reasoning_tokens") or 0),
            cached_tokens=int(prompt_details.get("cached_tokens") or 0),
        )

    @staticmethod
    def _extract_conversation_state(message: dict[str, Any]) -> LLMConversationState | None:
        if not message:
            return None
        state_message = {"role": "assistant", "content": message.get("content")}
        if "tool_calls" in message:
            state_message["tool_calls"] = message["tool_calls"]
        return LLMConversationState(
            messages=[
                LLMMessage(
                    role="assistant",
                    text=message.get("content") or None,
                    provider_state={"openrouter_message": state_message},
                )
            ],
            provider_state={"provider": "openrouter"},
        )

    @staticmethod
    def _stringify_tool_result(result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, default=str)

    @staticmethod
    def _log_metrics(result: GenerateResult) -> None:
        LOGGER.info(
            "OpenRouter generation: tokens in={} out={} reasoning={} cached={}",
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.thinking_tokens,
            result.usage.cached_tokens,
        )

    @staticmethod
    def _update_langfuse(result: GenerateResult) -> None:
        try:
            update_generation(
                usage_details={
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                },
            )
        except Exception:
            LOGGER.debug("Skipping Langfuse generation update", exc_info=True)


def _csv_env(name: str) -> list[str]:
    return [part.strip() for part in os.getenv(name, "").split(",") if part.strip()]


def _optional_bool_env(name: str) -> bool | None:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return None
    return raw.strip().lower() in {"true", "1", "yes", "on"}


def normalize_openrouter_model(model: str | None) -> str:
    """Convert native Gemini ids to OpenRouter's Google provider slugs."""

    if not model:
        return "google/gemini-2.5-flash"
    model = model.strip()
    if "/" in model:
        return model
    if model.startswith("gemini-"):
        return f"google/{model}"
    return model
