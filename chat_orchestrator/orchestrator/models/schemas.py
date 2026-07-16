"""Data models used throughout the Anansi Chat Orchestrator service."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

RoleLiteral = Literal["user", "model", "tool", "system"]
MessageSourceLiteral = Literal["telegram", "roam", "web", "api"]


class MediaAttachment(BaseModel):
    """Media attachment (image, video, etc.)."""

    type: Literal["image", "video", "audio", "document"] = "image"
    url: Optional[str] = None
    data: Optional[str] = Field(default=None, description="Base64-encoded media data")
    mime_type: Optional[str] = None
    caption: Optional[str] = None


class UserContext(BaseModel):
    """User identity and permissions context."""

    user_id: str = Field(description="Unique user identifier (telegram_id, email, etc.)")
    user_email: str = Field(
        description="User email (resolved from telegram_id/etc via auth service)"
    )
    username: Optional[str] = Field(default=None, description="Display name")
    source: MessageSourceLiteral = Field(
        default="api", description="Source platform (telegram, roam, web, api)"
    )
    chat_id: Optional[str] = Field(
        default=None,
        description="Chat/group ID for conversation tracking",
    )
    topic_id: Optional[str] = Field(
        default=None,
        description="Optional topic/thread ID within a chat (for forum groups)",
    )
    is_group: bool = Field(
        default=False, description="Whether this is a group/channel chat (vs direct message)"
    )
    roles: List[str] = Field(default_factory=list, description="User roles for permission checking")
    # Permissions resolved from auth database
    organization_ids: List[str] = Field(
        default_factory=list, description="Organizations user has access to"
    )
    grid_ids: List[str] = Field(default_factory=list, description="Grid IDs user has access to")
    meter_ids: List[str] = Field(default_factory=list, description="Meter IDs user has access to")
    is_admin: bool = Field(default=False, description="Whether user is an admin")
    is_staff: bool = Field(
        default=False,
        description="Whether user is staff (organization_id matches STAFF_ORG_ID env var)",
    )
    organization_name: Optional[str] = Field(
        default=None, description="Display name of user's primary organization"
    )
    chat_title: Optional[str] = Field(
        default=None,
        description="Chat/group title from Telegram (used for session naming)",
    )


class EntityContext(BaseModel):
    """Optional entity context for domain-specific queries."""

    customer_id: Optional[str] = None
    meter_id: Optional[str] = None
    grid_id: Optional[str] = None
    site_id: Optional[str] = None
    installation_id: Optional[str] = None
    additional_context: Dict[str, Any] = Field(
        default_factory=dict, description="Additional entity metadata"
    )


class FunctionCall(BaseModel):
    """Representation of an LLM tool invocation request."""

    name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    tool_call_id: Optional[str] = Field(
        default=None,
        description="Provider tool-call id required by OpenRouter/OpenAI-style tool loops.",
    )
    thought_signature: Optional[str] = Field(
        default=None,
        description="Gemini 3 thought signature - must be passed back with function response",
    )


class ToolCallResult(BaseModel):
    """Result returned after executing a tool."""

    name: str
    success: bool
    output: Any
    tool_call_id: Optional[str] = Field(
        default=None,
        description="Provider tool-call id this result answers, when required.",
    )
    status_code: Optional[int] = None
    raw_response: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class ConversationMessage(BaseModel):
    """Generic chat message exchanged with Gemini."""

    role: RoleLiteral
    content: Optional[str] = None
    media: List[MediaAttachment] = Field(
        default_factory=list, description="Media attachments (images, etc.)"
    )
    function_call: Optional[FunctionCall] = None
    tool_result: Optional[ToolCallResult] = None
    timestamp: Optional[str] = Field(
        default=None, description="ISO timestamp of when message was created"
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Additional metadata (e.g., token counts, model used)",
    )
    # Thread disentanglement fields
    sender_id: Optional[str] = Field(default=None, description="Telegram user ID of the sender")
    telegram_message_id: Optional[int] = Field(
        default=None, description="Telegram message ID for reply chain tracking"
    )
    reply_to_telegram_message_id: Optional[int] = Field(
        default=None, description="Telegram message ID this message replies to"
    )
    thread_id: Optional[str] = Field(
        default=None, description="Assigned conversation thread identifier"
    )


class ChatRequest(BaseModel):
    """Payload accepted by the orchestration API."""

    user_input: str = Field(description="Latest user utterance to process")
    user_context: UserContext = Field(description="User identity and permissions context")
    entity_context: Optional[EntityContext] = Field(
        default=None,
        description="Optional entity context (customer, meter, grid, etc.)",
    )
    media: List[MediaAttachment] = Field(
        default_factory=list, description="Media attachments from the user"
    )
    context: Optional[str] = Field(
        default=None,
        description="Optional context to prepend to the user input, such as RAG snippets.",
    )
    conversation: List[ConversationMessage] = Field(
        default_factory=list,
        description="Prior conversation history to include when calling Gemini.",
    )
    tool_overrides: Optional[List[Dict[str, Any]]] = Field(
        default=None,
        description="Optional list of tool definitions to override the configured services.",
    )
    unlocked_tools: Optional[List[str]] = Field(
        default=None,
        description="Tools unlocked by /command (command-gated tools).",
    )
    metadata: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary metadata propagated to tool executors.",
    )


class ChatResponse(BaseModel):
    """Response returned from the orchestration API."""

    final_text: str
    tool_calls: List[FunctionCall]
    tool_results: List[ToolCallResult]
    raw_responses: List[Dict[str, Any]]
    history: List[ConversationMessage]


class WebhookRequest(BaseModel):
    """Webhook request from Telegram or other platforms."""

    message: str = Field(description="User message text")
    user_id: str = Field(description="Platform-specific user ID")
    user_email: Optional[str] = None
    username: Optional[str] = None
    source: MessageSourceLiteral = "telegram"
    chat_id: Optional[str] = None
    topic_id: Optional[str] = Field(
        default=None, description="Optional topic/thread ID within a chat (for forum groups)"
    )
    outgoing_webhook_url: Optional[str] = Field(
        default=None, description="Webhook URL to send async response (Telegram Bot API format)"
    )
    media: List[MediaAttachment] = Field(default_factory=list)
    entity_context: Optional[EntityContext] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WebhookResponse(BaseModel):
    """Response to webhook request."""

    success: bool
    message: str = Field(description="Response text to send back to user")
    error: Optional[str] = None
    session_id: Optional[str] = None


__all__ = [
    "ChatRequest",
    "ChatResponse",
    "ConversationMessage",
    "FunctionCall",
    "ToolCallResult",
    "UserContext",
    "EntityContext",
    "MediaAttachment",
    "WebhookRequest",
    "WebhookResponse",
]
