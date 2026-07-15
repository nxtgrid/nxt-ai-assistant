"""OpenAI implementation of provider-neutral embedding gateway."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from shared.llm.types import EmbeddingOptions, EmbeddingVector


class OpenAIEmbeddingGateway:
    """Provider-neutral embedding gateway backed by OpenAI embeddings."""

    def __init__(
        self,
        api_key: str | None = None,
        *,
        client: Any | None = None,
        default_model: str | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = client
        self._default_model = default_model or os.getenv(
            "OPENAI_EMBEDDING_MODEL",
            "text-embedding-ada-002",
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError("OPENAI_API_KEY is not set; cannot create OpenAI client")
            import openai

            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    async def embed_texts(
        self,
        texts: list[str],
        options: EmbeddingOptions,
    ) -> list[EmbeddingVector]:
        if not texts:
            return []
        model = options.model or self._default_model
        response = await asyncio.to_thread(
            self.client.embeddings.create,
            model=model,
            input=list(texts),
        )
        return [
            EmbeddingVector(
                values=list(item.embedding),
                model=model,
                task_type=options.task_type,
            )
            for item in response.data
        ]
