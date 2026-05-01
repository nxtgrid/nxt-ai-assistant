"""RAG Provider protocol for conversation context enrichment.

This module defines the protocol that RAG providers should implement.
The actual orchestration is now handled by LangGraph in:
- orchestrator/graphs/full_conversation_graph.py
- orchestrator/graphs/conversation_graph.py
"""

from __future__ import annotations

from typing import List, Protocol


class RagProvider(Protocol):
    """Protocol that optional RAG implementations should follow."""

    async def retrieve(self, query: str, limit: int = 3) -> List[str]: ...
