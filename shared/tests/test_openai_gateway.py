from types import SimpleNamespace

import pytest

from shared.llm import EmbeddingOptions, OpenAIEmbeddingGateway


class FakeEmbeddings:
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            data=[
                SimpleNamespace(embedding=[0.1, 0.2]),
                SimpleNamespace(embedding=[0.3, 0.4]),
            ]
        )


class FakeClient:
    def __init__(self):
        self.embeddings = FakeEmbeddings()


@pytest.mark.asyncio
async def test_openai_embedding_gateway_uses_configured_model():
    client = FakeClient()
    gateway = OpenAIEmbeddingGateway(client=client, default_model="default-embedding")

    embeddings = await gateway.embed_texts(
        ["hello", "world"],
        EmbeddingOptions(task_type="CODE_SEARCH"),
    )

    assert [embedding.values for embedding in embeddings] == [[0.1, 0.2], [0.3, 0.4]]
    assert [embedding.model for embedding in embeddings] == [
        "default-embedding",
        "default-embedding",
    ]
    assert [embedding.task_type for embedding in embeddings] == [
        "CODE_SEARCH",
        "CODE_SEARCH",
    ]
    assert client.embeddings.calls == [
        {
            "model": "default-embedding",
            "input": ["hello", "world"],
        }
    ]
