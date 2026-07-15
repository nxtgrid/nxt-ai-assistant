"""Compatibility client for Gemini generateContent calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, Optional

from orchestrator.config.settings import GeminiModelConfig
from orchestrator.models.schemas import ConversationMessage, FunctionCall
from orchestrator.utils.response_sanitizer import sanitize_tool_response
from shared.llm import GeminiGateway
from shared.utils.langfuse_utils import langfuse_observe
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class GeminiTurnResult:
    """Provider-specific turn result with raw Gemini details hidden from graph code."""

    text: str
    tool_calls: list[FunctionCall]
    finish_reason: str | None
    input_tokens: int
    output_tokens: int
    raw_response: Dict[str, Any]


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

    @langfuse_observe(as_type="generation", name="gemini-generation")
    async def generate_messages(
        self,
        messages: list[ConversationMessage],
        *,
        system_instructions: Optional[str] = None,
        tools_payload: Optional[list[Dict[str, Any]]] = None,
    ) -> GeminiTurnResult:
        """Generate from graph-level conversation messages.

        This is the preferred orchestration boundary. The graph supplies its
        internal message and tool models; this client owns Gemini's
        ``contents``/``parts`` payload shape and raw response parsing.
        """

        if not self._api_key:
            raise RuntimeError("GOOGLE_API_KEY is not set; cannot call Gemini")

        payload = self._build_payload(
            messages=messages,
            system_instructions=system_instructions,
            tools_payload=tools_payload,
        )
        LOGGER.info(f"Gemini API call using model: {self._model_config.model}")
        response = await self._gateway.generate_content(payload, model=self._model_config.model)
        return self._parse_turn_response(response)

    def _build_payload(
        self,
        *,
        messages: list[ConversationMessage],
        system_instructions: Optional[str],
        tools_payload: Optional[list[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        generation_config: Dict[str, Any] = {
            "candidateCount": self._model_config.candidate_count,
            "topK": self._model_config.top_k,
            "topP": self._model_config.top_p,
            "maxOutputTokens": self._model_config.max_output_tokens,
        }

        effective_temp = self._model_config.get_effective_temperature()
        if effective_temp is not None:
            generation_config["temperature"] = effective_temp

        thinking_budget = self._model_config.thinking_budget
        model_lower = self._model_config.model.lower()
        is_pro_model = model_lower.endswith("-pro") or "-pro-" in model_lower
        if not is_pro_model and thinking_budget >= 0:
            if self._model_config._is_gemini_3_or_later(self._model_config.model):
                generation_config["thinkingConfig"] = {"thinkingLevel": "medium"}
            else:
                generation_config["thinkingConfig"] = {"thinkingBudget": thinking_budget}

        payload: Dict[str, Any] = {
            "generationConfig": generation_config,
            "contents": self._validate_conversation_structure(
                [self._to_gemini_message(message) for message in messages]
            ),
        }
        if system_instructions:
            payload["systemInstruction"] = {"parts": [{"text": system_instructions}]}
        if tools_payload:
            payload["tools"] = tools_payload
        return payload

    @staticmethod
    def _parse_turn_response(response: Dict[str, Any]) -> GeminiTurnResult:
        usage = response.get("usageMetadata", {}) or {}
        return GeminiTurnResult(
            text=GeminiClient._extract_text(response) or "",
            tool_calls=GeminiClient._extract_function_calls(response),
            finish_reason=GeminiClient._extract_finish_reason(response),
            input_tokens=int(usage.get("promptTokenCount", 0) or 0),
            output_tokens=int(usage.get("candidatesTokenCount", 0) or 0),
            raw_response=response,
        )

    @staticmethod
    def _extract_finish_reason(response: Dict[str, Any]) -> Optional[str]:
        candidates = response.get("candidates", [])
        if not candidates:
            prompt_feedback = response.get("promptFeedback", {})
            return prompt_feedback.get("blockReason")
        return candidates[0].get("finishReason")

    @staticmethod
    def _extract_text(response: Dict[str, Any]) -> Optional[str]:
        for candidate in response.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part and not part.get("thought"):
                    return str(part["text"])
        return None

    @staticmethod
    def _extract_function_calls(response: Dict[str, Any]) -> list[FunctionCall]:
        calls: list[FunctionCall] = []
        for candidate in response.get("candidates", []):
            content = candidate.get("content", {})
            parts = content.get("parts", [])
            for part in parts:
                if "functionCall" in part:
                    data = part["functionCall"]
                    calls.append(
                        FunctionCall(
                            name=data.get("name", ""),
                            arguments=data.get("args", {}),
                            thought_signature=part.get("thoughtSignature")
                            or data.get("thoughtSignature"),
                        )
                    )
                elif "functionCalls" in part:
                    for call in part["functionCalls"]:
                        calls.append(
                            FunctionCall(
                                name=call.get("name", ""),
                                arguments=call.get("args", {}),
                                thought_signature=call.get("thoughtSignature"),
                            )
                        )
        return calls

    @staticmethod
    def _to_gemini_message(message: ConversationMessage) -> Dict[str, Any]:
        parts: list[Dict[str, Any]] = []

        if message.content is not None:
            if message.timestamp and message.role == "user":
                text = f"[{message.timestamp}] {message.content}"
            else:
                text = message.content
            parts.append({"text": text})

        for media in message.media:
            media_part = GeminiClient._build_media_part(media)
            if media_part:
                parts.append(media_part)

        if message.function_call is not None:
            fc_data: Dict[str, Any] = {
                "name": message.function_call.name,
                "args": message.function_call.arguments,
            }
            part_data: Dict[str, Any] = {"functionCall": fc_data}
            if message.function_call.thought_signature:
                part_data["thoughtSignature"] = message.function_call.thought_signature
            parts.append(part_data)

        if message.tool_result is not None:
            response_output = message.tool_result.output
            if isinstance(response_output, str):
                try:
                    import json

                    response_output = json.loads(response_output)
                except (json.JSONDecodeError, ValueError):
                    pass

            response_output = sanitize_tool_response(response_output)

            if (
                isinstance(response_output, dict)
                and "error" in response_output
                and len(response_output) == 1
            ):
                response_output = {
                    "error_occurred": True,
                    "error_message": response_output["error"],
                    "details": "Tool execution failed",
                }

            if isinstance(response_output, str):
                response_output = {"result": response_output}
            elif isinstance(response_output, list):
                response_output = {"result": response_output}

            parts.append(
                {
                    "functionResponse": {
                        "name": message.tool_result.name,
                        "response": response_output,
                    }
                }
            )

        gemini_role = "user" if message.role == "tool" else message.role
        return {"role": gemini_role, "parts": parts}

    @staticmethod
    def _build_media_part(media: Any) -> Optional[Dict[str, Any]]:
        if media.type in ("image", "video", "audio"):
            if media.data:
                return {
                    "inline_data": {
                        "mime_type": media.mime_type or "image/jpeg",
                        "data": media.data,
                    }
                }
            if media.url:
                LOGGER.warning(f"Media URL not yet supported, skipping: {media.url}")
        return None

    @staticmethod
    def _validate_conversation_structure(history: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
        if not history:
            return history

        cleaned_history: list[Dict[str, Any]] = []
        i = 0
        while i < len(history):
            msg = history[i]
            parts = msg.get("parts", [])
            has_function_call = any("functionCall" in part for part in parts)

            if has_function_call:
                next_msg = history[i + 1] if i + 1 < len(history) else None
                if next_msg:
                    next_parts = next_msg.get("parts", [])
                    has_function_response = any(
                        "functionResponse" in part for part in next_parts
                    )
                    if has_function_response:
                        missing_sig = any(
                            "functionCall" in part and "thoughtSignature" not in part
                            for part in parts
                        )
                        if missing_sig:
                            i += 2
                            continue
                        cleaned_history.append(msg)
                        i += 1
                        continue
                    i += 1
                    continue
                i += 1
                continue

            cleaned_history.append(msg)
            i += 1

        return cleaned_history

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


__all__ = ["GeminiClient", "GeminiTurnResult", "MockGeminiClient"]
