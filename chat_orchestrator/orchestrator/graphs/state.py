"""State definitions for LangGraph conversation orchestration.

This module defines the TypedDict state schema used by the conversation graph.
The state captures all context needed to process a conversation turn through
the Gemini API with tool calling.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, TypedDict

from orchestrator.models.schemas import (
    ConversationMessage,
    EntityContext,
    FunctionCall,
    MediaAttachment,
    ToolCallResult,
    UserContext,
)


class ConversationState(TypedDict, total=False):
    """State maintained across the conversation graph.

    This TypedDict captures all the context needed to process a conversation
    turn through Gemini with tool calling. Fields marked as required (no default)
    must be provided when initializing the state.

    Attributes:
        # Core request data
        user_input: The current user message text
        user_context: User identity and permissions context
        entity_context: Optional domain-specific entity context
        media: Media attachments from the user
        metadata: Arbitrary metadata propagated to tool executors

        # Conversation context
        conversation_history: Prior conversation messages
        gemini_history: Legacy Gemini-formatted history retained for old checkpoints
        llm_messages: Provider-neutral message history for active LLM calls
        history_messages: Messages accumulated during this turn

        # Instructions and context
        system_instructions: Dynamic system instructions from Google Docs
        context_message: Context from artifacts provider (QnA, examples, etc.)

        # Tool configuration
        tools_payload: Provider-neutral tool declarations
        unlocked_tools: Tools unlocked by /command (command-gated tools)

        # Execution state
        current_round: Current tool execution round (0-indexed)
        max_rounds: Maximum tool rounds allowed
        pending_tool_calls: Tool calls requested by Gemini
        accumulated_tool_calls: All tool calls made this turn
        accumulated_tool_results: All tool results from this turn
        raw_gemini_responses: Raw Gemini API responses for debugging

        # Token tracking
        total_input_tokens: Cumulative input tokens across all rounds
        total_output_tokens: Cumulative output tokens across all rounds

        # Output
        final_response: The final text response to return

        # Verification state (Phase 2)
        verification_enabled: Whether to verify responses before sending
        verification_instructions: Criteria from Google Docs for verification
        verification_attempt: Current verification attempt (0, 1, 2)
        max_verification_attempts: Maximum verification attempts (default 2)
        verification_passed: Whether verification succeeded
        verification_feedback: Feedback from failed verification
        verification_categories: Categories that failed verification

        # Escalation state (Phase 2)
        session_id: Session identifier for escalation routing
        escalation_triggered: Whether escalation was triggered
        escalation_result: Result from escalation service

        # Control flow
        should_continue: Whether to continue the tool loop
        error: Error message if processing failed
        error_category: Error category for user-facing message
    """

    # Core request data (required)
    user_input: str
    user_context: UserContext

    # Optional request data
    entity_context: Optional[EntityContext]
    media: List[MediaAttachment]
    metadata: Dict[str, Any]

    # Conversation context
    conversation_history: List[ConversationMessage]
    gemini_history: List[Dict[str, Any]]
    llm_messages: List[ConversationMessage]
    history_messages: List[ConversationMessage]

    # Instructions and context
    system_instructions: Optional[str]
    context_message: Optional[str]

    # Tool configuration
    tools_payload: Optional[List[Dict[str, Any]]]
    unlocked_tools: List[str]
    allowed_tool_names: List[str]  # Tools Gemini can call (hallucination guard)

    # Execution state
    current_round: int
    max_rounds: int
    pending_tool_calls: List[FunctionCall]
    accumulated_tool_calls: List[FunctionCall]
    accumulated_tool_results: List[ToolCallResult]
    raw_gemini_responses: List[Dict[str, Any]]

    # Token tracking
    total_input_tokens: int
    total_output_tokens: int

    # Output
    final_response: Optional[str]

    # Verification state (Phase 2)
    verification_enabled: bool
    verification_instructions: Optional[str]
    verification_attempt: int
    max_verification_attempts: int
    verification_passed: Optional[bool]
    verification_feedback: Optional[str]
    verification_categories: List[str]

    # Escalation state (Phase 2)
    session_id: Optional[str]
    escalation_triggered: bool
    escalation_result: Optional[Dict[str, Any]]

    # Control flow
    should_continue: bool
    error: Optional[str]
    error_category: Optional[str]
    finish_reason: Optional[str]  # Gemini finishReason (STOP, SAFETY, RECITATION, etc.)

    # Phase 3: Services
    # NOTE: Service objects are NOT stored in state (causes checkpointer serialization errors).
    # Use singleton accessors instead:
    #   - get_supabase_client() from orchestrator.services.supabase_client
    #   - get_auth_service() from shared.auth
    #   - get_settings() from orchestrator.config.settings
    #   - get_permissions_service() from orchestrator.services.user_permissions
    # These fields are kept for backwards compatibility but should not be used.

    # Phase 3: Authentication state
    user_permissions: Optional[Dict[str, Any]]
    is_escalated_session: bool
    escalation_forward_result: Optional[str]

    # Phase 3: Context preparation
    available_tools: Optional[List[Dict[str, Any]]]
    rag_context: Optional[List[str]]

    # Phase 3: Command parsing
    original_input: str
    parsed_command: Optional[str]

    # Phase 3: Response handling
    tool_images: List[Dict[str, Any]]
    tool_calls: List[FunctionCall]
    safety_escalation_needed: bool
    loaded_message_count: int
    user_message_saved: bool  # True if save_user_message persisted input early
    command_model_override: str  # Model override from command (e.g., gemini-3.1-pro)
    messages: List[ConversationMessage]

    # Phase 3: Debug/notification
    tele_debug: Optional[Any]  # Debug notification function

    # Phase 4: Tool executor - NOT stored in state (checkpointer serialization issue).
    # Declared here for typing only; expert_handler.py builds it locally when needed
    # and never writes it back into graph state, so it always reads back as None.
    tool_executor: Optional[Any]

    # Phase 4: Expert routing
    expert_routing_decision: Optional[str]  # "expert" | "continue"
    active_work_packet: Optional[Dict[str, Any]]  # Current work packet if any
    matched_expert_id: Optional[str]  # Expert ID if routing to expert
    expert_command: Optional[str]  # Original command (e.g., /analyze)
    expert_packet_type: Optional[str]  # Packet type to create
    expert_key_entity: Optional[str]  # Site/entity name extracted from command

    # Phase 4: NL expert routing (virtual tool interception)
    nl_expert_reroute: bool  # Whether to reroute to expert_router after tool execution

    # Phase 4: Expert execution
    expert_executed: bool  # Whether expert handler ran
    expert_awaiting_input: bool  # Whether expert is waiting for user input
    expert_error: Optional[str]  # Error from expert execution

    # Phase 4: Resumable packet handling
    resumable_packet: Optional[Dict[str, Any]]  # Failed/blocked packet that can be resumed
    awaiting_resume_decision: bool  # Whether waiting for user to decide on resume

    # Phase 4: Duplicate work detection
    similar_work_packet: Optional[Dict[str, Any]]  # Similar completed work found
    awaiting_duplicate_decision: bool  # Whether waiting for user to decide on duplicate

    # Decision handling state
    user_input_consumed: bool  # Whether user_input was consumed by decision handling

    # Telegram inline buttons (for decision prompts)
    reply_markup: Optional[Dict[str, Any]]  # InlineKeyboardMarkup for Telegram

    # Phase 4: Expert redirect to main LLM
    redirect_to_main_llm: bool  # Whether expert step wants to redirect to main LLM
    redirect_reason: Optional[str]  # Reason for redirect (for logging)

    # User preferences
    user_preferences: Optional[List[Dict[str, Any]]]  # Per-user response preferences

    # Context management
    conversation_summary: Optional[str]  # Progressive summary of older messages

    # Thread disentanglement
    thread_id: Optional[str]  # Assigned thread for this message
    thread_filtered_history: Optional[List[ConversationMessage]]  # Thread-scoped history
    thread_assignment_method: Optional[str]  # How the thread was selected
    thread_assignment_confidence: Optional[float]  # Confidence from thread assignment
    thread_is_new: bool  # Whether this turn created a new thread
    sender_telegram_id: Optional[str]  # Telegram user ID of the sender
    telegram_message_id: Optional[int]  # Telegram message ID for reply chains
    reply_to_telegram_message_id: Optional[int]  # Telegram message ID being replied to

    # Conversation direction planning
    conversation_direction: Optional[str]  # normal_chat | new_expert_workflow | active_workflow
    conversation_context_scope: Optional[str]  # session | thread | packet
    conversation_direction_method: Optional[str]  # deterministic | model
    conversation_issue_type: Optional[str]  # lpp | kpi | support taxonomy | other
    planned_expert_route: Optional[Dict[str, str]]  # Precomputed natural-language expert route


def create_initial_state(
    user_input: str,
    user_context: UserContext,
    conversation_history: Optional[List[ConversationMessage]] = None,
    entity_context: Optional[EntityContext] = None,
    media: Optional[List[MediaAttachment]] = None,
    metadata: Optional[Dict[str, Any]] = None,
    system_instructions: Optional[str] = None,
    context_message: Optional[str] = None,
    tools_payload: Optional[List[Dict[str, Any]]] = None,
    unlocked_tools: Optional[List[str]] = None,
    max_rounds: int = 3,
    verification_enabled: bool = False,
    verification_instructions: Optional[str] = None,
    session_id: Optional[str] = None,
) -> ConversationState:
    """Create an initial conversation state with sensible defaults.

    Args:
        user_input: The current user message text
        user_context: User identity and permissions context
        conversation_history: Prior conversation messages
        entity_context: Optional domain-specific entity context
        media: Media attachments from the user
        metadata: Arbitrary metadata propagated to tool executors
        system_instructions: Dynamic system instructions from Google Docs
        context_message: Context from artifacts provider
        tools_payload: Provider-neutral tool declarations
        unlocked_tools: Tools unlocked by /command
        max_rounds: Maximum tool rounds allowed
        verification_enabled: Whether to verify responses before sending
        verification_instructions: Criteria from Google Docs for verification
        session_id: Session identifier for escalation routing

    Returns:
        A fully initialized ConversationState ready for graph execution
    """
    return ConversationState(
        # Core request data
        user_input=user_input,
        user_context=user_context,
        entity_context=entity_context,
        media=media or [],
        metadata=metadata or {},
        # Conversation context
        conversation_history=conversation_history or [],
        gemini_history=[],
        llm_messages=[],
        history_messages=[],
        # Instructions and context
        system_instructions=system_instructions,
        context_message=context_message,
        # Tool configuration
        tools_payload=tools_payload,
        unlocked_tools=unlocked_tools or [],
        # Execution state
        current_round=0,
        max_rounds=max_rounds,
        pending_tool_calls=[],
        accumulated_tool_calls=[],
        accumulated_tool_results=[],
        raw_gemini_responses=[],
        # Token tracking
        total_input_tokens=0,
        total_output_tokens=0,
        # Output
        final_response=None,
        # Verification state (Phase 2)
        verification_enabled=verification_enabled,
        verification_instructions=verification_instructions,
        verification_attempt=0,
        max_verification_attempts=2,
        verification_passed=None,
        verification_feedback=None,
        verification_categories=[],
        # Escalation state (Phase 2)
        session_id=session_id,
        escalation_triggered=False,
        escalation_result=None,
        # Control flow
        should_continue=True,
        error=None,
        error_category=None,
    )


__all__ = ["ConversationState", "create_initial_state"]
