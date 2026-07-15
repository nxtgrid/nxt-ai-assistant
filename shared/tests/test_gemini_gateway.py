from types import SimpleNamespace

import pytest

from shared.llm import GenerationOptions, LLMMessage, Usage
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
