"""Compatibility client for OpenRouter chat completions."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from orchestrator.clients.gemini import GeminiTurnResult
from orchestrator.config.settings import GeminiModelConfig
from orchestrator.models.schemas import ConversationMessage, FunctionCall
from shared.llm import GenerationOptions, LLMMessage, OpenRouterGateway, ToolResult, ToolSpec
from shared.llm.openrouter import normalize_openrouter_model
from shared.utils.langfuse_utils import langfuse_observe
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


_FINISH_REASON_MAP = {
    "tool_calls": "FUNCTION_CALL",
    "function_call": "FUNCTION_CALL",
    "stop": "STOP",
    "length": "MAX_TOKENS",
    "content_filter": "SAFETY",
}


def normalize_finish_reason(reason: str | None) -> str | None:
    """Return Gemini-style finish reasons at the graph boundary."""

    if reason is None:
        return None
    return _FINISH_REASON_MAP.get(reason.lower(), reason)


class OpenRouterClient:
    """GeminiClient-compatible wrapper backed by OpenRouter chat completions."""

    def __init__(
        self,
        api_key: str,
        model_config: GeminiModelConfig,
        gateway: Optional[OpenRouterGateway] = None,
    ) -> None:
        self._api_key = api_key
        self._model_config = model_config
        self._default_model = normalize_openrouter_model(model_config.model)
        self._gateway = gateway or OpenRouterGateway(
            api_key=api_key,
            default_model=self._default_model,
        )

    @langfuse_observe(as_type="generation", name="openrouter-generation")
    async def generate_messages(
        self,
        messages: list[ConversationMessage],
        *,
        system_instructions: Optional[str] = None,
        tools_payload: Optional[list[Dict[str, Any]]] = None,
    ) -> GeminiTurnResult:
        """Generate from graph-level conversation messages."""

        if not self._api_key:
            raise RuntimeError("OPENROUTER_API_KEY is not set; cannot call OpenRouter")

        llm_messages, tool_results = self._convert_messages(messages, system_instructions)
        tool_specs = self._convert_tools_payload(tools_payload or [])
        options = GenerationOptions(
            model=self._default_model,
            temperature=self._model_config.get_effective_temperature(self._model_config.model),
            max_output_tokens=self._model_config.max_output_tokens,
            thinking_budget=(
                self._model_config.thinking_budget
                if self._model_config.thinking_budget >= 0
                else None
            ),
        )

        LOGGER.info(f"OpenRouter API call using model: {self._default_model}")
        result = await self._gateway.generate(
            llm_messages,
            options,
            tools=tool_specs,
            tool_results=tool_results,
        )
        return GeminiTurnResult(
            text=result.text or "",
            tool_calls=[
                FunctionCall(
                    name=call.name,
                    arguments=call.args,
                    tool_call_id=call.id,
                )
                for call in result.tool_calls
            ],
            finish_reason=normalize_finish_reason(result.finish_reason),
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            raw_response=result.raw if isinstance(result.raw, dict) else {"raw": result.raw},
        )

    @staticmethod
    def _convert_messages(
        messages: list[ConversationMessage],
        system_instructions: Optional[str],
    ) -> tuple[list[LLMMessage], list[ToolResult]]:
        llm_messages: list[LLMMessage] = []
        tool_results: list[ToolResult] = []
        if system_instructions:
            llm_messages.append(LLMMessage(role="system", text=system_instructions))

        for message in messages:
            if message.tool_result is not None:
                call_id = message.tool_result.tool_call_id or message.tool_result.name
                tool_results.append(
                    ToolResult(
                        call_id=call_id,
                        name=message.tool_result.name,
                        result=message.tool_result.output,
                        is_error=not message.tool_result.success,
                    )
                )
                continue

            if message.function_call is not None:
                call_id = message.function_call.tool_call_id or message.function_call.name
                llm_messages.append(
                    LLMMessage(
                        role="assistant",
                        provider_state={
                            "openrouter_message": {
                                "role": "assistant",
                                "content": message.content,
                                "tool_calls": [
                                    {
                                        "id": call_id,
                                        "type": "function",
                                        "function": {
                                            "name": message.function_call.name,
                                            "arguments": json.dumps(
                                                message.function_call.arguments,
                                                default=str,
                                            ),
                                        },
                                    }
                                ],
                            }
                        },
                    )
                )
                continue

            role = "assistant" if message.role == "model" else message.role
            text = message.content
            if message.timestamp and message.role == "user" and text is not None:
                text = f"[{message.timestamp}] {text}"
            if message.media:
                content_parts = _openrouter_content_parts(text, message.media)
                if content_parts:
                    llm_messages.append(
                        LLMMessage(
                            role=role,
                            provider_state={
                                "openrouter_message": {
                                    "role": role,
                                    "content": content_parts,
                                }
                            },
                        )
                    )
                continue
            if text is not None:
                llm_messages.append(LLMMessage(role=role, text=text))

        return llm_messages, tool_results

    @staticmethod
    def _convert_tools_payload(tools_payload: list[Dict[str, Any]]) -> list[ToolSpec]:
        declarations: list[Dict[str, Any]] = []
        for item in tools_payload:
            if "functionDeclarations" in item:
                declarations.extend(item.get("functionDeclarations") or [])
            elif "google_search" in item:
                continue
            else:
                declarations.append(item)

        return [
            ToolSpec(
                name=str(declaration.get("name") or ""),
                description=str(declaration.get("description") or ""),
                parameters_json_schema=_normalize_json_schema(
                    declaration.get("parameters") or {"type": "object", "properties": {}}
                ),
            )
            for declaration in declarations
            if declaration.get("name")
        ]


def _normalize_json_schema(value: Any) -> Any:
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if key == "type" and isinstance(item, str):
                normalized[key] = item.lower()
            else:
                normalized[key] = _normalize_json_schema(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_json_schema(item) for item in value]
    return value


def _openrouter_content_parts(text: str | None, media_items: list[Any]) -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    for media in media_items:
        if media.type == "image":
            if media.data:
                mime_type = media.mime_type or "image/jpeg"
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime_type};base64,{media.data}"},
                    }
                )
            elif media.url:
                parts.append({"type": "image_url", "image_url": {"url": media.url}})
        elif media.url:
            parts.append({"type": "text", "text": f"{media.type} attachment: {media.url}"})
    return parts


__all__ = ["OpenRouterClient", "normalize_finish_reason"]
