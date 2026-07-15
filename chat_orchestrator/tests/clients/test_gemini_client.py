import pytest

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import GeminiModelConfig
from orchestrator.models.schemas import (
    ConversationMessage,
    FunctionCall,
    MediaAttachment,
    ToolCallResult,
)


class FakeGateway:
    def __init__(self):
        self.calls = []

    async def generate_content(self, payload, *, model=None):
        self.calls.append((payload, model))
        return {"text": "ok"}


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
                "functionDeclarations": [
                    {
                        "name": "lookup_meter",
                        "description": "Look up a meter",
                        "parameters": {"type": "OBJECT"},
                    }
                ]
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
