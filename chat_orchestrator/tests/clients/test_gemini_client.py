import pytest

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import GeminiModelConfig


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
