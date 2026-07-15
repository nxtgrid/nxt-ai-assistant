"""Provider-neutral types for LLM generation, tool use, and embeddings."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

LLMRole = Literal["system", "user", "assistant", "tool"]
ResponseFormat = Literal["text", "json"]
ThinkingMode = Literal["default", "off", "medium", "high"]


@dataclass(frozen=True)
class LLMMessage:
    role: LLMRole
    text: str | None = None
    tool_call_id: str | None = None
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMConversationState:
    messages: list[LLMMessage] = field(default_factory=list)
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters_json_schema: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any] = field(default_factory=dict)
    provider_state: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    call_id: str
    name: str
    result: Any
    is_error: bool = False


@dataclass(frozen=True)
class GenerationOptions:
    model: str | None = None
    temperature: float | None = None
    max_output_tokens: int | None = None
    response_format: ResponseFormat = "text"
    thinking: ThinkingMode = "default"
    thinking_budget: int | None = None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True)
class GenerateResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str | None = None
    conversation_state: LLMConversationState | None = None
    raw: Any | None = None


@dataclass(frozen=True)
class EmbeddingOptions:
    model: str | None = None
    task_type: str | None = None
    output_dimensionality: int = 768


@dataclass(frozen=True)
class EmbeddingVector:
    values: list[float]
    model: str
    task_type: str | None = None
