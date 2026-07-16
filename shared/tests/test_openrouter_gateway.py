import json

import pytest

from shared.llm import (
    GenerationOptions,
    LLMConversationState,
    LLMMessage,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)
from shared.llm.openrouter import OpenRouterGateway


class FakeAsyncResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.text)

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


class FakeSyncClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, *, headers=None, json=None):
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.responses.pop(0)


def completion_payload(
    *,
    content="hello",
    tool_calls=None,
    finish_reason="stop",
    usage=None,
):
    return {
        "id": "gen-test",
        "model": "openai/gpt-4o",
        "choices": [
            {
                "finish_reason": finish_reason,
                "message": {
                    "role": "assistant",
                    "content": content,
                    **({"tool_calls": tool_calls} if tool_calls is not None else {}),
                },
            }
        ],
        "usage": usage
        or {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "prompt_tokens_details": {"cached_tokens": 1},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    }


@pytest.mark.asyncio
async def test_generate_posts_openai_style_chat_completion():
    client = FakeAsyncClient([FakeAsyncResponse(completion_payload(content="ok"))])
    gateway = OpenRouterGateway(
        api_key="or-key",
        default_model="openai/gpt-4o",
        http_referer="https://example.com",
        app_title="Anansi Test",
        async_client=client,
    )

    result = await gateway.generate(
        [
            LLMMessage(role="system", text="Be concise."),
            LLMMessage(role="user", text="Say hi"),
        ],
        GenerationOptions(
            temperature=0.2,
            max_output_tokens=64,
            response_format="json",
        ),
    )

    assert result.text == "ok"
    assert result.usage == Usage(input_tokens=10, output_tokens=4, thinking_tokens=2, cached_tokens=1)
    assert result.finish_reason == "stop"
    assert result.conversation_state == LLMConversationState(
        messages=[
            LLMMessage(
                role="assistant",
                text="ok",
                provider_state={"openrouter_message": {"role": "assistant", "content": "ok"}},
            )
        ],
        provider_state={"provider": "openrouter"},
    )
    assert result.raw == completion_payload(content="ok")
    assert client.calls[0]["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert client.calls[0]["headers"]["Authorization"] == "Bearer or-key"
    assert client.calls[0]["headers"]["HTTP-Referer"] == "https://example.com"
    assert client.calls[0]["headers"]["X-OpenRouter-Title"] == "Anansi Test"
    assert client.calls[0]["json"] == {
        "model": "openai/gpt-4o",
        "messages": [
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "Say hi"},
        ],
        "temperature": 0.2,
        "max_tokens": 64,
        "response_format": {"type": "json_object"},
    }


@pytest.mark.asyncio
async def test_generate_maps_tool_specs_and_tool_calls():
    tool_calls = [
        {
            "id": "call-1",
            "type": "function",
            "function": {
                "name": "lookup_meter",
                "arguments": '{"meter_id": "M1"}',
            },
        }
    ]
    client = FakeAsyncClient(
        [
            FakeAsyncResponse(
                completion_payload(content=None, tool_calls=tool_calls, finish_reason="tool_calls")
            )
        ]
    )
    gateway = OpenRouterGateway(
        api_key="or-key", default_model="openai/gpt-4o", async_client=client
    )

    result = await gateway.generate(
        [LLMMessage(role="user", text="Check M1")],
        GenerationOptions(),
        tools=[
            ToolSpec(
                name="lookup_meter",
                description="Look up meter",
                parameters_json_schema={
                    "type": "object",
                    "properties": {"meter_id": {"type": "string"}},
                },
            )
        ],
    )

    assert result.text == ""
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls == [
        ToolCall(
            id="call-1",
            name="lookup_meter",
            args={"meter_id": "M1"},
            provider_state={"provider": "openrouter"},
        )
    ]
    assert client.calls[0]["json"]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "lookup_meter",
                "description": "Look up meter",
                "parameters": {
                    "type": "object",
                    "properties": {"meter_id": {"type": "string"}},
                },
            },
        }
    ]
    assert result.conversation_state is not None
    assert result.conversation_state.messages[0].provider_state["openrouter_message"][
        "tool_calls"
    ] == tool_calls


@pytest.mark.asyncio
async def test_generate_continues_with_tool_results_and_conversation_state():
    client = FakeAsyncClient([FakeAsyncResponse(completion_payload(content="done"))])
    gateway = OpenRouterGateway(
        api_key="or-key", default_model="openai/gpt-4o", async_client=client
    )

    state = LLMConversationState(
        messages=[
            LLMMessage(
                role="assistant",
                provider_state={
                    "openrouter_message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup_meter",
                                    "arguments": '{"meter_id": "M1"}',
                                },
                            }
                        ],
                    }
                },
            )
        ]
    )

    await gateway.generate(
        [LLMMessage(role="user", text="Check M1")],
        GenerationOptions(),
        tool_results=[ToolResult(call_id="call-1", name="lookup_meter", result={"status": "ok"})],
        conversation_state=state,
    )

    assert client.calls[0]["json"]["messages"] == [
        {"role": "user", "content": "Check M1"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "lookup_meter",
                        "arguments": '{"meter_id": "M1"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "lookup_meter",
            "content": '{"status": "ok"}',
        },
    ]


@pytest.mark.asyncio
async def test_generate_preserves_openrouter_provider_state_messages():
    client = FakeAsyncClient([FakeAsyncResponse(completion_payload(content="done"))])
    gateway = OpenRouterGateway(
        api_key="or-key", default_model="gemini-2.5-flash", async_client=client
    )

    await gateway.generate(
        [
            LLMMessage(role="user", text="Check M1"),
            LLMMessage(
                role="assistant",
                provider_state={
                    "openrouter_message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "lookup_meter",
                                    "arguments": '{"meter_id": "M1"}',
                                },
                            }
                        ],
                    }
                },
            ),
        ],
        GenerationOptions(),
        tool_results=[ToolResult(call_id="call-1", name="lookup_meter", result={"status": "ok"})],
    )

    assert client.calls[0]["json"]["model"] == "google/gemini-2.5-flash"
    assert client.calls[0]["json"]["messages"][1] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "lookup_meter",
                    "arguments": '{"meter_id": "M1"}',
                },
            }
        ],
    }


def test_generate_sync_uses_sync_client():
    client = FakeSyncClient([FakeAsyncResponse(completion_payload(content="sync ok"))])
    gateway = OpenRouterGateway(api_key="or-key", default_model="openai/gpt-4o", client=client)

    result = gateway.generate_sync(
        [LLMMessage(role="user", text="Say hi")],
        GenerationOptions(max_output_tokens=32),
    )

    assert result.text == "sync ok"
    assert client.calls[0]["json"]["max_tokens"] == 32


def test_accepts_open_router_bearer_token_alias(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("OPEN_ROUTER_BEARER_TOKEN", "alias-key")

    gateway = OpenRouterGateway()

    assert gateway._api_key == "alias-key"


def test_generate_sync_can_add_provider_routing():
    client = FakeSyncClient([FakeAsyncResponse(completion_payload(content="vertex ok"))])
    gateway = OpenRouterGateway(
        api_key="or-key",
        default_model="google/gemini-2.5-flash",
        client=client,
        provider_order=["google-vertex"],
        allow_fallbacks=False,
        require_parameters=True,
    )

    result = gateway.generate_sync(
        [LLMMessage(role="user", text="Say hi")],
        GenerationOptions(max_output_tokens=16),
    )

    assert result.text == "vertex ok"
    assert client.calls[0]["json"]["provider"] == {
        "order": ["google-vertex"],
        "allow_fallbacks": False,
        "require_parameters": True,
    }
