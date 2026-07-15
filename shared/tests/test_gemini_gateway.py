from types import SimpleNamespace

import pytest

from shared.llm import (
    EmbeddingOptions,
    GenerationOptions,
    LLMMessage,
    ToolResult,
    ToolSpec,
    Usage,
)
from shared.llm.gemini import GeminiGateway


class FakeModels:
    def __init__(self, responses, embed_responses=None):
        self.responses = list(responses)
        self.embed_responses = list(embed_responses or [])
        self.calls = []
        self.embed_calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def embed_content(self, **kwargs):
        self.embed_calls.append(kwargs)
        return self.embed_responses.pop(0)


class FakeClient:
    def __init__(self, responses, embed_responses=None):
        self.models = FakeModels(responses, embed_responses)
        self.aio = SimpleNamespace(models=self.models)


class FakeRateLimitError(Exception):
    status_code = 429


def fake_response(text="ok", prompt_tokens=10, output_tokens=4, finish_reason="STOP"):
    return SimpleNamespace(
        text=text,
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt_tokens,
            candidates_token_count=output_tokens,
            thoughts_token_count=2,
            cached_content_token_count=1,
        ),
        candidates=[SimpleNamespace(finish_reason=finish_reason)],
    )


def fake_embedding_response(*vectors):
    return SimpleNamespace(
        embeddings=[SimpleNamespace(values=vector) for vector in vectors],
    )


@pytest.mark.asyncio
async def test_generate_text_uses_model_contents_and_omits_none_temperature():
    client = FakeClient([fake_response(text="hello")])
    gateway = GeminiGateway(api_key="test-key", client=client)

    result = await gateway.generate(
        [LLMMessage(role="user", text="Say hello")],
        GenerationOptions(model="gemini-test", max_output_tokens=128),
    )

    assert result.text == "hello"
    assert result.usage == Usage(
        input_tokens=10,
        output_tokens=4,
        thinking_tokens=2,
        cached_tokens=1,
    )
    assert result.finish_reason == "STOP"
    assert client.models.calls == [
        {
            "model": "gemini-test",
            "contents": [{"role": "user", "parts": [{"text": "Say hello"}]}],
            "config": {"max_output_tokens": 128},
        }
    ]


@pytest.mark.asyncio
async def test_generate_json_sets_response_mime_type_and_system_instruction():
    client = FakeClient([fake_response(text='{"ok": true}')])
    gateway = GeminiGateway(api_key="test-key", client=client)

    result = await gateway.generate(
        [
            LLMMessage(role="system", text="Return strict JSON."),
            LLMMessage(role="user", text="Classify this."),
        ],
        GenerationOptions(
            model="gemini-json",
            temperature=0.0,
            max_output_tokens=64,
            response_format="json",
        ),
    )

    assert result.text == '{"ok": true}'
    assert client.models.calls[0] == {
        "model": "gemini-json",
        "contents": [{"role": "user", "parts": [{"text": "Classify this."}]}],
        "config": {
            "system_instruction": "Return strict JSON.",
            "temperature": 0.0,
            "max_output_tokens": 64,
            "response_mime_type": "application/json",
        },
    }


@pytest.mark.asyncio
async def test_generate_falls_back_after_primary_rate_limit():
    client = FakeClient([FakeRateLimitError("rate limited"), fake_response(text="fallback")])
    gateway = GeminiGateway(
        api_key="test-key",
        client=client,
        default_model="primary-model",
        fallback_model="fallback-model",
        max_retries=0,
    )

    result = await gateway.generate(
        [LLMMessage(role="user", text="Hello")],
        GenerationOptions(max_output_tokens=32),
    )

    assert result.text == "fallback"
    assert [call["model"] for call in client.models.calls] == [
        "primary-model",
        "fallback-model",
    ]


@pytest.mark.asyncio
async def test_generate_content_accepts_legacy_payload_and_returns_raw_dict():
    client = FakeClient([fake_response(text='{"ok": true}')])
    gateway = GeminiGateway(api_key="test-key", client=client)

    result = await gateway.generate_content(
        {
            "contents": [{"role": "user", "parts": [{"text": "Classify"}]}],
            "systemInstruction": {"parts": [{"text": "Return JSON"}]},
            "generationConfig": {
                "maxOutputTokens": 128,
                "responseMimeType": "application/json",
            },
            "tools": [
                {
                    "functionDeclarations": [
                        {
                            "name": "lookup_meter",
                            "description": "Look up a meter.",
                            "parameters": {"type": "object"},
                        }
                    ]
                }
            ],
        },
        model="gemini-legacy",
    )

    assert result["text"] == '{"ok": true}'
    assert result["usageMetadata"]["promptTokenCount"] == 10
    assert client.models.calls == [
        {
            "model": "gemini-legacy",
            "contents": [{"role": "user", "parts": [{"text": "Classify"}]}],
            "config": {
                "max_output_tokens": 128,
                "response_mime_type": "application/json",
                "system_instruction": "Return JSON",
                "tools": [
                    {
                        "function_declarations": [
                            {
                                "name": "lookup_meter",
                                "description": "Look up a meter.",
                                "parameters": {"type": "object"},
                            }
                        ]
                    }
                ],
            },
        }
    ]


def test_raw_payload_conversion_preserves_thought_signatures():
    contents, _ = GeminiGateway._convert_raw_payload(
        {
            "contents": [
                {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {"name": "lookup_meter", "args": {"id": "M1"}},
                            "thoughtSignature": "opaque-signature",
                        }
                    ],
                }
            ]
        }
    )

    assert contents == [
        {
            "role": "model",
            "parts": [
                {
                    "function_call": {"name": "lookup_meter", "args": {"id": "M1"}},
                    "thought_signature": "opaque-signature",
                }
            ],
        }
    ]


@pytest.mark.asyncio
async def test_generate_converts_tool_specs_to_function_declarations():
    client = FakeClient([fake_response(text="")])
    gateway = GeminiGateway(api_key="test-key", client=client)

    await gateway.generate(
        [LLMMessage(role="user", text="Check meter M1")],
        GenerationOptions(model="gemini-tools"),
        tools=[
            ToolSpec(
                name="lookup_meter",
                description="Look up a meter.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"meter_id": {"type": "string"}},
                    "required": ["meter_id"],
                },
            )
        ],
    )

    assert client.models.calls[0]["config"]["tools"] == [
        {
            "function_declarations": [
                {
                    "name": "lookup_meter",
                    "description": "Look up a meter.",
                    "parameters": {
                        "type": "object",
                        "properties": {"meter_id": {"type": "string"}},
                        "required": ["meter_id"],
                    },
                }
            ]
        }
    ]


@pytest.mark.asyncio
async def test_generate_extracts_function_calls_as_neutral_tool_calls():
    function_call = SimpleNamespace(name="lookup_meter", args={"meter_id": "M1"})
    response = SimpleNamespace(
        text="",
        usage_metadata={},
        candidates=[
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(parts=[SimpleNamespace(function_call=function_call)]),
            )
        ],
    )
    client = FakeClient([response])
    gateway = GeminiGateway(api_key="test-key", client=client)

    result = await gateway.generate(
        [LLMMessage(role="user", text="Check meter M1")],
        GenerationOptions(model="gemini-tools"),
    )

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "lookup_meter:0"
    assert result.tool_calls[0].name == "lookup_meter"
    assert result.tool_calls[0].args == {"meter_id": "M1"}


@pytest.mark.asyncio
async def test_generate_converts_tool_results_to_function_response_parts():
    client = FakeClient([fake_response(text="done")])
    gateway = GeminiGateway(api_key="test-key", client=client)

    await gateway.generate(
        [LLMMessage(role="user", text="Check meter M1")],
        GenerationOptions(model="gemini-tools"),
        tool_results=[
            ToolResult(
                call_id="lookup_meter:0",
                name="lookup_meter",
                result={"status": "ok"},
            )
        ],
    )

    assert client.models.calls[0]["contents"] == [
        {"role": "user", "parts": [{"text": "Check meter M1"}]},
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "lookup_meter",
                        "response": {"result": {"status": "ok"}},
                    }
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_generate_preserves_provider_state_for_tool_loop_continuation():
    function_call = SimpleNamespace(name="lookup_meter", args={"meter_id": "M1"})
    first_response = SimpleNamespace(
        text="",
        usage_metadata={},
        candidates=[
            SimpleNamespace(
                finish_reason="STOP",
                content=SimpleNamespace(
                    parts=[
                        SimpleNamespace(
                            function_call=function_call,
                            thought_signature="opaque-signature",
                        )
                    ]
                ),
            )
        ],
    )
    client = FakeClient([first_response, fake_response(text="Meter is OK")])
    gateway = GeminiGateway(api_key="test-key", client=client)

    first_result = await gateway.generate(
        [LLMMessage(role="user", text="Check meter M1")],
        GenerationOptions(model="gemini-tools"),
    )
    assert first_result.conversation_state is not None
    state_message = first_result.conversation_state.messages[0]
    assert state_message.provider_state["gemini_parts"][0]["thought_signature"] == (
        "opaque-signature"
    )

    await gateway.generate(
        [LLMMessage(role="user", text="Check meter M1")],
        GenerationOptions(model="gemini-tools"),
        conversation_state=first_result.conversation_state,
        tool_results=[
            ToolResult(
                call_id="lookup_meter:0",
                name="lookup_meter",
                result={"status": "ok"},
            )
        ],
    )

    assert client.models.calls[1]["contents"] == [
        {"role": "user", "parts": [{"text": "Check meter M1"}]},
        {
            "role": "model",
            "parts": [
                {
                    "function_call": {
                        "name": "lookup_meter",
                        "args": {"meter_id": "M1"},
                    },
                    "thought_signature": "opaque-signature",
                }
            ],
        },
        {
            "role": "user",
            "parts": [
                {
                    "function_response": {
                        "name": "lookup_meter",
                        "response": {"result": {"status": "ok"}},
                    }
                }
            ],
        },
    ]


@pytest.mark.asyncio
async def test_embed_texts_uses_embedding_model_task_type_and_dimensions():
    client = FakeClient(
        [],
        embed_responses=[fake_embedding_response([0.1, 0.2], [0.3, 0.4])],
    )
    gateway = GeminiGateway(
        api_key="test-key",
        client=client,
        default_embedding_model="embedding-default",
    )

    embeddings = await gateway.embed_texts(
        ["hello", "world"],
        options=EmbeddingOptions(task_type="RETRIEVAL_DOCUMENT", output_dimensionality=2),
    )

    assert [embedding.values for embedding in embeddings] == [[0.1, 0.2], [0.3, 0.4]]
    assert [embedding.model for embedding in embeddings] == [
        "embedding-default",
        "embedding-default",
    ]
    assert [embedding.task_type for embedding in embeddings] == [
        "RETRIEVAL_DOCUMENT",
        "RETRIEVAL_DOCUMENT",
    ]
    assert client.models.embed_calls == [
        {
            "model": "embedding-default",
            "contents": ["hello", "world"],
            "config": {
                "task_type": "RETRIEVAL_DOCUMENT",
                "output_dimensionality": 2,
            },
        }
    ]
