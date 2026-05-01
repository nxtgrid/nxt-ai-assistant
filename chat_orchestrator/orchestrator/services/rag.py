"""Simple retrieval augmented generation helper."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import List

from orchestrator.config.settings import AppSettings, get_settings
from orchestrator.services.conversation import RagProvider
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class Document:
    """Lightweight document representation for local retrieval."""

    identifier: str
    text: str


class LocalFileRagProvider(RagProvider):
    """Loads a small knowledge base from disk and performs keyword retrieval."""

    def __init__(self, settings: AppSettings | None = None) -> None:
        self._settings = settings or get_settings()
        self._path = self._settings.rag.collection_path
        self._lock = asyncio.Lock()
        self._documents: List[Document] = []

    async def retrieve(self, query: str, limit: int = 3) -> List[str]:
        """Return up to ``limit`` snippets that match the query."""

        await self._ensure_loaded()
        query_tokens = {token.lower() for token in query.split() if token}
        scored: List[tuple[float, Document]] = []

        for document in self._documents:
            doc_tokens = {token.lower() for token in document.text.split()}
            if not doc_tokens:
                continue
            overlap = len(query_tokens & doc_tokens)
            if overlap:
                score = overlap / len(doc_tokens)
                scored.append((score, document))

        scored.sort(key=lambda item: item[0], reverse=True)
        top_docs = [doc for _, doc in scored[:limit]]
        LOGGER.debug("RAG retrieved %d documents", len(top_docs))
        return [doc.text for doc in top_docs]

    async def _ensure_loaded(self) -> None:
        """Load documents if not already cached."""

        if self._documents:
            return

        async with self._lock:
            if self._documents:
                return
            if not self._path.exists():
                LOGGER.warning("Knowledge base file %s not found", self._path)
                return
            with self._path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            self._documents = [
                Document(identifier=item.get("id", ""), text=item.get("text", ""))
                for item in payload
            ]
            LOGGER.info("Loaded %d documents for RAG", len(self._documents))


__all__ = ["LocalFileRagProvider"]
