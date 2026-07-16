from typing import Any

import pytest

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import AppSettings, GeminiModelConfig
from orchestrator.models.schemas import (
    ConversationMessage,
    FunctionCall,
    MediaAttachment,
    ToolCallResult,
)
from shared.llm.types import GenerateResult, GenerationOptions, LLMMessage, ToolCall, Usage


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def generate_content(self, payload, *, model=None):
        self.calls.append((payload, model))
        return {"text": "ok"}


class FakeOpenRouterGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[Any] | None = None,
        tool_results: list[Any] | None = None,
        conversation_state: Any | None = None,
    ) -> GenerateResult:
        self.calls.append(
            {
                "messages": messages,
                "options": options,
                "tools": tools,
                "tool_results": tool_results,
                "conversation_state": conversation_state,
            }
        )
        return GenerateResult(
            text="",
            tool_calls=[
                ToolCall(
                    id="call_123",
                    name="lookup_customer",
                    args={"customer_id": "C-1"},
                )
            ],
            usage=Usage(input_tokens=11, output_tokens=3),
            finish_reason="tool_calls",
            raw={"choices": []},
        )


@pytest.mark.asyncio
async def test_gemini_client_delegates_raw_payload_to_gateway():
    gateway = FakeGateway()
    client = GeminiClient(
        api_key="test-key",
        model_config=GeminiModelConfig(
            model="primary-model",
            fallback_model="fallback-model",
        ),
        gateway=gateway,
    )
    payload = {"contents": [{"role": "user", "parts": [{"text": "hello"}]}]}

    result = await client.generate_content(payload)

    assert result == {"text": "ok"}
    assert gateway.calls == [(payload, "primary-model")]


@pytest.mark.asyncio
async def test_gemini_client_generates_from_conversation_messages():
    gateway = FakeGateway()
    gateway.generate_content = _async_returning(
        gateway,
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {"text": "The meter is healthy."},
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 11,
                "candidatesTokenCount": 7,
            },
        },
    )
    client = GeminiClient(
        api_key="test-key",
        model_config=GeminiModelConfig(
            model="primary-model",
            fallback_model="fallback-model",
            max_output_tokens=512,
            thinking_budget=-1,
        ),
        gateway=gateway,
    )

    result = await client.generate_messages(
        [
            ConversationMessage(role="user", content="Check meter M1"),
            ConversationMessage(
                role="model",
                function_call=FunctionCall(
                    name="lookup_meter",
                    arguments={"meter_id": "M1"},
                    thought_signature="opaque-signature",
                ),
            ),
            ConversationMessage(
                role="tool",
                tool_result=ToolCallResult(
                    name="lookup_meter",
                    success=True,
                    output={"status": "ok"},
                ),
            ),
        ],
        system_instructions="Answer concisely.",
        tools_payload=[
            {
                "name": "lookup_meter",
                "description": "Look up a meter",
                "parameters": {"type": "OBJECT"},
            }
        ],
    )

    assert result.text == "The meter is healthy."
    assert result.tool_calls == []
    assert result.finish_reason == "STOP"
    assert result.input_tokens == 11
    assert result.output_tokens == 7
    assert gateway.calls == [
        (
            {
                "generationConfig": {
                    "candidateCount": 1,
                    "topK": 40,
                    "topP": 0.95,
                    "maxOutputTokens": 512,
                    "temperature": 0.2,
                },
                "systemInstruction": {"parts": [{"text": "Answer concisely."}]},
                "tools": [
                    {
                        "functionDeclarations": [
                            {
                                "name": "lookup_meter",
                                "description": "Look up a meter",
                                "parameters": {"type": "OBJECT"},
                            }
                        ]
                    }
                ],
                "contents": [
                    {"role": "user", "parts": [{"text": "Check meter M1"}]},
                    {
                        "role": "model",
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "lookup_meter",
                                    "args": {"meter_id": "M1"},
                                },
                                "thoughtSignature": "opaque-signature",
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": "lookup_meter",
                                    "response": {"status": "ok"},
                                }
                            }
                        ],
                    },
                ],
            },
            "primary-model",
        )
    ]


@pytest.mark.asyncio
async def test_gemini_client_extracts_tool_calls_from_response():
    gateway = FakeGateway()
    gateway.generate_content = _async_returning(
        gateway,
        {
            "candidates": [
                {
                    "finishReason": "STOP",
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "lookup_meter",
                                    "args": {"meter_id": "M1"},
                                },
                                "thoughtSignature": "opaque-signature",
                            }
                        ]
                    },
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 5,
                "candidatesTokenCount": 3,
            },
        },
    )
    client = GeminiClient(
        api_key="test-key",
        model_config=GeminiModelConfig(model="primary-model", fallback_model="fallback-model"),
        gateway=gateway,
    )

    result = await client.generate_messages([ConversationMessage(role="user", content="Check M1")])

    assert result.text == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0] == FunctionCall(
        name="lookup_meter",
        arguments={"meter_id": "M1"},
        thought_signature="opaque-signature",
    )


@pytest.mark.asyncio
async def test_gemini_client_converts_media_inside_client_boundary():
    gateway = FakeGateway()
    gateway.generate_content = _async_returning(
        gateway,
        {
            "candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {},
        },
    )
    client = GeminiClient(
        api_key="test-key",
        model_config=GeminiModelConfig(model="primary-model", fallback_model="fallback-model"),
        gateway=gateway,
    )

    await client.generate_messages(
        [
            ConversationMessage(
                role="user",
                content="Inspect this image",
                media=[
                    MediaAttachment(
                        type="image",
                        data="base64-image",
                        mime_type="image/png",
                    )
                ],
            )
        ]
    )

    assert gateway.calls[0][0]["contents"] == [
        {
            "role": "user",
            "parts": [
                {"text": "Inspect this image"},
                {
                    "inline_data": {
                        "mime_type": "image/png",
                        "data": "base64-image",
                    }
                },
            ],
        }
    ]


def _async_returning(gateway, response):
    async def generate_content(payload, *, model=None):
        gateway.calls.append((payload, model))
        return response

    return generate_content


@pytest.mark.asyncio
async def test_openrouter_client_preserves_tool_call_ids_and_normalizes_finish_reason():
    from orchestrator.clients.openrouter import OpenRouterClient

    gateway = FakeOpenRouterGateway()
    client = OpenRouterClient(
        api_key="sk-or-test",
        model_config=GeminiModelConfig(model="gemini-2.5-flash"),
        gateway=gateway,
    )

    result = await client.generate_messages([ConversationMessage(role="user", content="hi")])

    assert result.finish_reason == "FUNCTION_CALL"
    assert result.input_tokens == 11
    assert result.output_tokens == 3
    assert result.tool_calls == [
        FunctionCall(
            name="lookup_customer",
            arguments={"customer_id": "C-1"},
            tool_call_id="call_123",
        )
    ]
    assert gateway.calls[0]["options"].model == "google/gemini-2.5-flash"


@pytest.mark.asyncio
async def test_openrouter_client_uses_role_model_not_openrouter_model_env(monkeypatch):
    from orchestrator.clients.openrouter import OpenRouterClient

    monkeypatch.setenv("OPENROUTER_MODEL", "openai/gpt-4o")
    gateway = FakeOpenRouterGateway()
    client = OpenRouterClient(
        api_key="sk-or-test",
        model_config=GeminiModelConfig(model="gemini-2.5-flash-lite"),
        gateway=gateway,
    )

    await client.generate_messages([ConversationMessage(role="user", content="hi")])

    assert gateway.calls[0]["options"].model == "google/gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_openrouter_client_sends_prior_tool_result_with_matching_id():
    from orchestrator.clients.openrouter import OpenRouterClient

    gateway = FakeOpenRouterGateway()
    client = OpenRouterClient(
        api_key="sk-or-test",
        model_config=GeminiModelConfig(model="gemini-2.5-flash"),
        gateway=gateway,
    )
    messages = [
        ConversationMessage(
            role="model",
            function_call=FunctionCall(
                name="lookup_customer",
                arguments={"customer_id": "C-1"},
                tool_call_id="call_123",
            ),
        ),
        ConversationMessage(
            role="tool",
            tool_result=ToolCallResult(
                name="lookup_customer",
                success=True,
                output={"name": "Ada"},
                tool_call_id="call_123",
            ),
        ),
    ]

    await client.generate_messages(messages)

    assert gateway.calls[0]["messages"][0].provider_state["openrouter_message"][
        "tool_calls"
    ][0]["id"] == "call_123"
    assert gateway.calls[0]["tool_results"][0].call_id == "call_123"


@pytest.mark.asyncio
async def test_openrouter_client_preserves_inline_image_media():
    from orchestrator.clients.openrouter import OpenRouterClient

    gateway = FakeOpenRouterGateway()
    client = OpenRouterClient(
        api_key="sk-or-test",
        model_config=GeminiModelConfig(model="gemini-2.5-flash"),
        gateway=gateway,
    )

    await client.generate_messages(
        [
            ConversationMessage(
                role="user",
                content="Inspect this image",
                media=[
                    MediaAttachment(type="image", data="base64-image", mime_type="image/png")
                ],
            )
        ]
    )

    assert gateway.calls[0]["messages"][0].provider_state["openrouter_message"][
        "content"
    ] == [
        {"type": "text", "text": "Inspect this image"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,base64-image"},
        },
    ]


def test_chat_client_factory_selects_openrouter_without_google_key():
    from orchestrator.clients.factory import create_chat_llm_client
    from orchestrator.clients.openrouter import OpenRouterClient

    settings = AppSettings(
        llm_provider="openrouter",
        openrouter_api_key="sk-or-test",
        google_api_key="",
        gemini=GeminiModelConfig(model="gemini-2.5-flash"),
    )

    assert isinstance(create_chat_llm_client(settings), OpenRouterClient)


def test_chat_client_factory_keeps_legacy_gemini_path():
    from orchestrator.clients.factory import create_chat_llm_client

    settings = AppSettings(
        llm_provider="gemini",
        openrouter_api_key="",
        google_api_key="google-test",
        gemini=GeminiModelConfig(model="gemini-2.5-flash"),
    )

    assert isinstance(create_chat_llm_client(settings), GeminiClient)


def test_gemini_payload_ignores_openrouter_tool_call_id():
    message = ConversationMessage(
        role="model",
        function_call=FunctionCall(
            name="lookup_customer",
            arguments={"customer_id": "C-1"},
            thought_signature="sig",
            tool_call_id="call_123",
        ),
    )

    payload = GeminiClient._to_gemini_message(message)

    assert payload["parts"][0]["functionCall"] == {
        "name": "lookup_customer",
        "args": {"customer_id": "C-1"},
    }
    assert payload["parts"][0]["thoughtSignature"] == "sig"
    assert "tool_call_id" not in str(payload)


def test_get_settings_accepts_openrouter_without_google_key(monkeypatch):
    from chat_orchestrator import handler

    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    settings = handler._get_settings()

    assert settings.llm_provider == "openrouter"
    assert settings.openrouter_api_key == "sk-or-test"
    assert settings.google_api_key == ""


def test_get_settings_keeps_gemini_key_required(monkeypatch):
    from chat_orchestrator import handler

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    with pytest.raises(ValueError, match="GOOGLE_API_KEY is required"):
        handler._get_settings()
