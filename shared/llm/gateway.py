"""Provider-neutral gateway protocols for generation and embeddings."""

from __future__ import annotations

from typing import Protocol

from shared.llm.types import (
    EmbeddingOptions,
    EmbeddingVector,
    GenerateResult,
    GenerationOptions,
    LLMConversationState,
    LLMMessage,
    ToolResult,
    ToolSpec,
)


class GenerationGateway(Protocol):
    async def generate(
        self,
        messages: list[LLMMessage],
        options: GenerationOptions,
        tools: list[ToolSpec] | None = None,
        tool_results: list[ToolResult] | None = None,
        conversation_state: LLMConversationState | None = None,
    ) -> GenerateResult:
        """Generate text, JSON, or tool calls from provider-neutral messages."""


class EmbeddingGateway(Protocol):
    async def embed_texts(
        self,
        texts: list[str],
        options: EmbeddingOptions,
    ) -> list[EmbeddingVector]:
        """Embed one or more texts."""
