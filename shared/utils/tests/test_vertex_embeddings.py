import pytest

from shared.llm import EmbeddingVector
from shared.utils import vertex_embeddings


class FakeEmbeddingGateway:
    def __init__(self):
        self.calls = []

    async def embed_texts(self, texts, options):
        self.calls.append((texts, options))
        return [
            EmbeddingVector(
                values=[0.1, 0.2],
                model=options.model,
                task_type=options.task_type,
            )
        ]


@pytest.mark.asyncio
async def test_get_embeddings_delegates_to_default_gateway(monkeypatch):
    gateway = FakeEmbeddingGateway()
    monkeypatch.setattr(
        vertex_embeddings,
        "get_default_embedding_gateway",
        lambda: gateway,
    )

    result = await vertex_embeddings.get_embeddings(
        ["hello"],
        task_type="RETRIEVAL_QUERY",
        model_name="embedding-test",
        output_dimensionality=2,
    )

    assert result == [[0.1, 0.2]]
    assert len(gateway.calls) == 1
    texts, options = gateway.calls[0]
    assert texts == ["hello"]
    assert options.model == "embedding-test"
    assert options.task_type == "RETRIEVAL_QUERY"
    assert options.output_dimensionality == 2
