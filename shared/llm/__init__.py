"""Provider-neutral LLM gateway interfaces and data types."""

from shared.llm.factory import get_default_embedding_gateway, get_default_generation_gateway
from shared.llm.gateway import EmbeddingGateway, GenerationGateway
from shared.llm.gemini import GeminiGateway
from shared.llm.openai import OpenAIEmbeddingGateway
from shared.llm.types import (
    EmbeddingOptions,
    EmbeddingVector,
    GenerateResult,
    GenerationOptions,
    LLMConversationState,
    LLMMessage,
    ToolCall,
    ToolResult,
    ToolSpec,
    Usage,
)

__all__ = [
    "EmbeddingGateway",
    "EmbeddingOptions",
    "EmbeddingVector",
    "GeminiGateway",
    "GenerateResult",
    "GenerationGateway",
    "GenerationOptions",
    "LLMConversationState",
    "LLMMessage",
    "OpenAIEmbeddingGateway",
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "Usage",
    "get_default_embedding_gateway",
    "get_default_generation_gateway",
]
