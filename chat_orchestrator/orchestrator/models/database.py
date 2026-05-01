"""Database models for Supabase integration."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class UserModel(BaseModel):
    """User model for database operations."""

    id: Optional[UUID] = None
    external_user_id: Optional[str] = None
    username: Optional[str] = None
    email: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ChatSessionModel(BaseModel):
    """Chat session model for database operations."""

    id: Optional[UUID] = None
    session_id: str
    user_id: Optional[UUID] = None
    title: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # Security: Multi-tenant isolation
    organization_id: Optional[int] = None
    # Preserve original Telegram IDs for admin UI lookups (session_id is hashed)
    telegram_chat_id: Optional[str] = None
    telegram_topic_id: Optional[str] = None


class ChatMessageModel(BaseModel):
    """Chat message model for database operations."""

    id: Optional[UUID] = None
    session_id: UUID
    role: str  # user, model, tool, system
    content: Optional[str] = None
    function_call: Optional[Dict[str, Any]] = None
    tool_result: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None
    message_index: int
    from_chat_id: Optional[str] = None  # Telegram chat ID where message originated
    group_id: Optional[str] = None  # Telegram group ID if from group, null for 1-on-1
    # Thread disentanglement fields
    telegram_message_id: Optional[int] = None
    reply_to_telegram_message_id: Optional[int] = None
    sender_telegram_id: Optional[str] = None
    thread_id: Optional[str] = None


class ToolCallModel(BaseModel):
    """Tool call model for database operations."""

    id: Optional[UUID] = None
    session_id: UUID
    message_id: Optional[UUID] = None
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    result: Optional[Dict[str, Any]] = None
    success: Optional[bool] = None
    error_message: Optional[str] = None
    execution_time_ms: Optional[int] = None
    status_code: Optional[int] = None
    raw_response: Optional[Dict[str, Any]] = None
    created_at: Optional[datetime] = None


class TokenUsageModel(BaseModel):
    """Token usage model for database operations."""

    id: Optional[UUID] = None
    session_id: UUID
    message_id: Optional[UUID] = None
    model_name: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: Optional[int] = None  # Computed column
    cost_usd: Optional[float] = None
    created_at: Optional[datetime] = None


class UserFeedbackModel(BaseModel):
    """User feedback model for database operations."""

    id: Optional[UUID] = None
    session_id: UUID
    message_id: UUID
    user_id: UUID
    feedback_type: str  # thumbs_up, thumbs_down, rating, text
    rating: Optional[int] = Field(None, ge=1, le=5)
    feedback_text: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[datetime] = None


class DocumentChunkModel(BaseModel):
    """Document chunk model for RAG operations."""

    id: Optional[UUID] = None
    document_id: str
    chunk_index: int
    content: str
    embedding: Optional[List[float]] = None  # Vector will be serialized as list
    metadata: Dict[str, Any] = Field(default_factory=dict)
    source_url: Optional[str] = None
    source_type: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ConversationSummary(BaseModel):
    """Summary of conversation metrics."""

    session_id: str
    message_count: int
    tool_calls_count: int
    total_tokens: int
    total_cost_usd: float
    duration_minutes: Optional[float] = None
    user_feedback_count: int
    last_activity: datetime


class RAGSearchResult(BaseModel):
    """Result from RAG similarity search."""

    chunk_id: UUID
    document_id: str
    content: str
    similarity_score: float
    metadata: Dict[str, Any]
    source_url: Optional[str] = None
    source_type: Optional[str] = None


__all__ = [
    "UserModel",
    "ChatSessionModel",
    "ChatMessageModel",
    "ToolCallModel",
    "TokenUsageModel",
    "UserFeedbackModel",
    "DocumentChunkModel",
    "ConversationSummary",
    "RAGSearchResult",
]
