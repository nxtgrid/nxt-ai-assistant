from types import SimpleNamespace

import pytest

from shared.llm import GenerationOptions, LLMMessage, ToolResult, ToolSpec, Usage
from shared.llm.gemini import GeminiGateway


class FakeModels:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def generate_content(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeClient:
    def __init__(self, responses):
        self.models = FakeModels(responses)
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
