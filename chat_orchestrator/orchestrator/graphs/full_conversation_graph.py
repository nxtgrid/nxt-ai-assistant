"""Full conversation graph for LangGraph (Phase 3 + Phase 4 Experts).

This module provides a complete webhook-to-response StateGraph that handles:
- Service initialization
- Authentication resolution
- Escalation checking
- Media download
- Tools and context preparation
- Expert routing (Phase 4)
- Command parsing
- Gemini conversation loop
- Safety checking
- History persistence

This replaces the 900-line _process_webhook_async function in handler.py.

Graph flow:
    [START]
        ↓
    [init_services] → Initialize supabase, auth, settings
        ↓
    [resolve_auth] → Permission resolution (chat vs user based)
        ↓
    [check_escalation] → Check if session has active escalation
        ↓ (conditional: if escalated → [respond] → [END])
    [prepare_media] → Download Telegram photos/audio
        ↓
    [prepare_tools] → Get available tools + escalation/training tools
        ↓
    [assign_thread] → Thread disentanglement (filter history to current thread)
        ↓
    [prepare_context] → RAG, instructions, enrichment, date context
        ↓
    [expert_router] → Check for active work packets or expert commands
        ↓ (conditional: if expert → [expert_handler])
    [expert_handler]? → Execute expert workflow (if routed)
        ↓
    [parse_command] → Process /slash commands
        ↓
    [prepare] → Build Gemini history, create request
        ↓
    [call_gemini] → Call Gemini API
        ↓ ←─────────────────────────┐
    [execute_tools]? → Execute tool calls
        ↓ ────────────────────────────┘ (loop back)
    [verify]? → LLM-as-judge verification (if enabled)
        ↓ (conditional: escalate if max failures)
    [safety_check] → Detect false escalation claims
        ↓
    [respond] → Finalize response with token metadata
        ↓
    [save_history] → Persist to Supabase (includes token counts)
        ↓
    [END]

State management:
    Multi-turn decision state (e.g., awaiting_duplicate_decision) is persisted
    via the pending_decisions database table, not LangGraph checkpoints.
    The graph runs stateless (single-turn per webhook request).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Literal, Optional

from langgraph.graph import END, START, StateGraph

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

from orchestrator.clients.gemini import GeminiClient
from orchestrator.config.settings import AppSettings, get_settings
from orchestrator.graphs.conversation_graph import ConversationGraphBuilder
from orchestrator.graphs.nodes import (
    ask_about_duplicate,
    ask_resume_failed,
    assign_thread,
    check_escalation,
    expert_handler,
    expert_router,
    init_services,
    parse_command,
    prepare_context,
    prepare_media,
    prepare_tools,
    resolve_auth,
    safety_check,
    save_history,
)
from orchestrator.graphs.nodes.save_user_message import save_user_message
from orchestrator.graphs.state import ConversationState
from orchestrator.models.schemas import FunctionCall
from orchestrator.services.tool_executor import ToolExecutor
from orchestrator.services.tool_registry import ToolRegistry

# Type alias for escalation/training image handlers
EscalationHandler = Callable[[FunctionCall, Dict[str, Any]], Any]


class FullConversationGraphBuilder:
    """Builder for the complete webhook-to-response StateGraph.

    This class extends the base conversation graph with Phase 3 nodes
    for service initialization, authentication, context preparation, etc.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        escalation_handler: Optional[EscalationHandler] = None,
        training_image_handler: Optional[EscalationHandler] = None,
    ) -> None:
        """Initialize the full graph builder.

        Args:
            settings: Application settings. If None, will be loaded from env.
            escalation_handler: Handler for escalation tool calls.
            training_image_handler: Handler for training image tool calls.
        """
        self._settings = settings or get_settings()
        self._escalation_handler = escalation_handler
        self._training_image_handler = training_image_handler

        # These will be created lazily in prepare node
        self._gemini: Optional[GeminiClient] = None
        self._registry: Optional[ToolRegistry] = None
        self._executor: Optional[ToolExecutor] = None
        # Cached inner builder — reused across all node invocations to avoid
        # creating a new ConversationGraphBuilder per node call
        self._inner_builder: Optional[ConversationGraphBuilder] = None

    def build(self) -> StateGraph:
        """Build and return the compiled full conversation graph.

        Returns:
            A compiled StateGraph ready for invocation
        """
        builder = StateGraph(ConversationState)

        # Phase 3: Pre-conversation nodes
        builder.add_node("init_services", init_services)
        builder.add_node("resolve_auth", resolve_auth)
        builder.add_node("check_escalation", check_escalation)
        builder.add_node("prepare_media", prepare_media)
        builder.add_node("prepare_tools", prepare_tools)
        builder.add_node("prepare_context", prepare_context)

        # Early user message persistence (before processing)
        builder.add_node("save_user_message", save_user_message)

        # Thread disentanglement
        builder.add_node("assign_thread", assign_thread)

        # Phase 4: Expert routing nodes
        builder.add_node("expert_router", expert_router)
        builder.add_node("expert_handler", expert_handler)
        builder.add_node("ask_resume_failed", ask_resume_failed)
        builder.add_node("ask_about_duplicate", ask_about_duplicate)

        builder.add_node("parse_command", parse_command)

        # Core conversation nodes (from Phase 1/2)
        builder.add_node("prepare", self._prepare_node)
        builder.add_node("call_gemini", self._call_gemini_node)
        builder.add_node("execute_tools", self._execute_tools_node)
        builder.add_node("verify", self._verify_node)
        builder.add_node("escalate", self._escalate_node)

        # Phase 3: Post-conversation nodes
        builder.add_node("safety_check", safety_check)
        builder.add_node("save_history", save_history)
        builder.add_node("respond", self._respond_node)

        # Phase 3: Entry flow edges
        builder.add_edge(START, "init_services")
        builder.add_edge("init_services", "resolve_auth")
        builder.add_edge("resolve_auth", "check_escalation")
        builder.add_conditional_edges(
            "check_escalation",
            self._route_after_escalation_check,
            {
                "respond": "respond",  # Early exit for escalated sessions
                "prepare_media": "prepare_media",
            },
        )
        builder.add_edge("prepare_media", "prepare_tools")

        # Early save: persist user message before any processing
        builder.add_edge("prepare_tools", "save_user_message")
        builder.add_edge("save_user_message", "assign_thread")
        builder.add_edge("assign_thread", "prepare_context")
        builder.add_edge("prepare_context", "expert_router")
        builder.add_conditional_edges(
            "expert_router",
            self._route_after_expert_check,
            {
                "expert": "expert_handler",
                "ask_resume": "ask_resume_failed",
                "ask_duplicate": "ask_about_duplicate",
                "continue": "parse_command",
                "skip_gemini": "safety_check",  # Skip Gemini when final_response already set
            },
        )
        # Expert handler: conditional - may redirect to Gemini or continue to safety
        builder.add_conditional_edges(
            "expert_handler",
            self._route_after_expert_handler,
            {
                "safety_check": "safety_check",  # Normal: expert finished
                "redirect_to_gemini": "parse_command",  # Redirect: user input for main LLM
            },
        )
        # User interaction nodes go directly to respond (wait for user decision)
        builder.add_edge("ask_resume_failed", "save_history")
        builder.add_edge("ask_about_duplicate", "save_history")

        builder.add_edge("parse_command", "prepare")

        # Core conversation loop edges
        builder.add_edge("prepare", "call_gemini")
        builder.add_conditional_edges(
            "call_gemini",
            self._route_after_gemini,
            {
                "execute_tools": "execute_tools",
                "verify": "verify",
                "safety_check": "safety_check",  # Skip verify for non-verified flows
            },
        )
        builder.add_conditional_edges(
            "execute_tools",
            self._route_after_tools,
            {
                "call_gemini": "call_gemini",
                "expert_router": "expert_router",
            },
        )

        # Verification routing
        builder.add_conditional_edges(
            "verify",
            self._route_after_verify,
            {
                "safety_check": "safety_check",
                "regenerate": "prepare",  # Loop back with feedback
                "escalate": "escalate",
            },
        )
        builder.add_edge("escalate", "safety_check")

        # Phase 3: Exit flow edges
        builder.add_edge("safety_check", "respond")
        builder.add_edge("respond", "save_history")
        builder.add_edge("save_history", END)

        return builder.compile()

    def _route_after_escalation_check(
        self, state: ConversationState
    ) -> Literal["respond", "prepare_media"]:
        """Route after escalation check.

        If session is escalated and message was forwarded, go to respond.
        Otherwise continue with normal flow.
        """
        is_escalated = state.get("is_escalated_session", False)
        forward_result = state.get("escalation_forward_result")

        if is_escalated and forward_result:
            LOGGER.info("Session is escalated, skipping to respond")
            return "respond"

        return "prepare_media"

    def _route_after_expert_check(
        self, state: ConversationState
    ) -> Literal["expert", "ask_resume", "ask_duplicate", "continue", "skip_gemini"]:
        """Route based on expert router decision.

        Routing priority:
        1. "expert" - Active packet or new expert command -> expert_handler
        2. "ask_resume" - Found resumable failed/blocked packet -> ask user
        3. "ask_duplicate" - Found similar completed work -> ask user
        4. "skip_gemini" - final_response already set (e.g., workflow cancelled) -> skip to safety_check
        5. "continue" - No expert routing needed -> normal Gemini flow
        """
        decision = state.get("expert_routing_decision", "continue")

        # Check if final_response is already set (e.g., workflow was cancelled)
        # In this case, skip Gemini entirely to avoid tool pollution
        if state.get("final_response"):
            LOGGER.info("final_response already set, skipping Gemini")
            return "skip_gemini"

        if decision == "expert":
            expert_id = state.get("matched_expert_id")
            packet = state.get("active_work_packet")

            if packet:
                LOGGER.info(
                    f"Routing to expert: {expert_id} (resuming packet: {packet.get('packet_id')})"
                )
            else:
                LOGGER.info(f"Routing to expert: {expert_id} (new packet)")

            return "expert"

        if decision == "ask_resume":
            packet = state.get("resumable_packet")
            LOGGER.info(
                f"Found resumable packet: {packet.get('packet_id') if packet else 'unknown'} "
                f"- asking user for decision"
            )
            return "ask_resume"

        if decision == "ask_duplicate":
            packet = state.get("similar_work_packet")
            LOGGER.info(
                f"Found similar completed work: {packet.get('packet_id') if packet else 'unknown'} "
                f"- asking user for decision"
            )
            return "ask_duplicate"

        return "continue"

    def _route_after_expert_handler(
        self, state: ConversationState
    ) -> Literal["safety_check", "redirect_to_gemini"]:
        """Route after expert handler execution.

        If the expert step detected unrelated user input and wants to redirect
        to the main LLM, route to parse_command for normal Gemini processing.
        Otherwise continue to safety_check.
        """
        if state.get("redirect_to_main_llm"):
            reason = state.get("redirect_reason", "unrelated input detected")
            LOGGER.info(f"Expert handler requesting redirect to main LLM: {reason}")
            return "redirect_to_gemini"

        return "safety_check"

    def _route_after_tools(
        self, state: ConversationState
    ) -> Literal["call_gemini", "expert_router"]:
        """Route after tool execution.

        Normally loops back to call_gemini. If NL expert routing intercepted
        a start_expert_workflow call, reroute to expert_router with the
        synthetic slash command set as user_input.
        """
        if state.get("nl_expert_reroute"):
            LOGGER.info("NL expert reroute: sending synthetic command to expert_router")
            return "expert_router"
        return "call_gemini"

    def _route_after_gemini(
        self, state: ConversationState
    ) -> Literal["execute_tools", "verify", "safety_check"]:
        """Route after Gemini call based on state."""
        if state.get("pending_tool_calls"):
            return "execute_tools"

        # Check if verification is enabled
        verification_enabled = state.get("verification_enabled", False)
        user_context = state.get("user_context")
        is_staff = user_context.is_staff if user_context else False

        if verification_enabled and not is_staff and state.get("final_response"):
            return "verify"

        return "safety_check"

    def _route_after_verify(
        self, state: ConversationState
    ) -> Literal["safety_check", "regenerate", "escalate"]:
        """Route after verification based on result."""
        verification_passed = state.get("verification_passed")

        if verification_passed:
            return "safety_check"

        # Check if we can retry
        attempt = state.get("verification_attempt", 0)
        max_attempts = state.get("max_verification_attempts", 2)

        if attempt < max_attempts:
            LOGGER.info(f"Verification failed (attempt {attempt}/{max_attempts}), regenerating")
            return "regenerate"

        LOGGER.warning(f"Verification failed {attempt} times, escalating")
        return "escalate"

    def _get_inner_builder(self, state: ConversationState) -> ConversationGraphBuilder:
        """Get or create the cached inner ConversationGraphBuilder.

        Creates Gemini client, registry, and executor lazily on first call,
        then reuses the same inner builder across all node invocations.
        This avoids creating ~8+ builder instances per request.
        """
        settings = state.get("settings") or self._settings

        # Create Gemini client if needed
        if not self._gemini:
            self._gemini = GeminiClient(
                api_key=settings.google_api_key,
                model_config=settings.gemini,
            )

        # Create tool registry and executor if needed
        if not self._registry:
            self._registry = ToolRegistry(settings)
        if not self._executor:
            self._executor = ToolExecutor(self._registry, settings)

        # Create inner builder once and reuse
        if not self._inner_builder:
            self._inner_builder = ConversationGraphBuilder(
                gemini_client=self._gemini,
                registry=self._registry,
                executor=self._executor,
                settings=settings,
                escalation_handler=self._escalation_handler,
                training_image_handler=self._training_image_handler,
            )

        return self._inner_builder

    async def _prepare_node(self, state: ConversationState) -> Dict[str, Any]:
        """Prepare Gemini history and context.

        This is a wrapper around the Phase 1 prepare logic, but uses
        state values from Phase 3 nodes.
        """
        inner_builder = self._get_inner_builder(state)
        result: Dict[str, Any] = await inner_builder._prepare_node(state)
        return result

    async def _call_gemini_node(self, state: ConversationState) -> Dict[str, Any]:
        """Call Gemini API. Delegates to inner builder."""
        inner_builder = self._get_inner_builder(state)
        result: Dict[str, Any] = await inner_builder._call_gemini_node(state)
        return result

    async def _execute_tools_node(self, state: ConversationState) -> Dict[str, Any]:
        """Execute tool calls. Intercepts NL expert routing, delegates rest to inner builder."""
        from orchestrator.graphs.nodes.prepare_tools import (
            NL_EXPERT_TOOL_NAME,
            PACKET_TYPE_TO_COMMAND,
        )

        # Check for start_expert_workflow tool call — intercept before execution
        function_calls = state.get("pending_tool_calls", [])
        nl_expert_call = next((fc for fc in function_calls if fc.name == NL_EXPERT_TOOL_NAME), None)

        if nl_expert_call:
            args = nl_expert_call.arguments or {}
            packet_type = args.get("packet_type", "")
            key_entity = args.get("key_entity", "")
            command = PACKET_TYPE_TO_COMMAND.get(packet_type)

            if command:
                # Sanitize key_entity: strip newlines, limit length, remove leading /
                if key_entity:
                    key_entity = key_entity.replace("\n", " ").replace("\r", " ").strip()
                    key_entity = key_entity.lstrip("/")[:200]
                # Construct synthetic slash command (e.g., "/lpp ExampleGrid")
                synthetic_input = f"{command} {key_entity}".strip() if key_entity else command
                LOGGER.info(
                    f"NL expert routing: intercepted start_expert_workflow("
                    f"packet_type={packet_type}, key_entity={key_entity}) "
                    f"→ synthetic command '{synthetic_input}'"
                )
                return {
                    "user_input": synthetic_input,
                    "nl_expert_reroute": True,
                    "pending_tool_calls": [],
                }
            else:
                LOGGER.warning(
                    f"NL expert routing: unknown packet_type '{packet_type}', "
                    f"falling through to normal execution"
                )

        inner_builder = self._get_inner_builder(state)
        result: Dict[str, Any] = await inner_builder._execute_tools_node(state)
        # Always clear NL reroute flag on normal tool execution to prevent
        # stale flag from causing a loop if expert_router falls through to "continue"
        result["nl_expert_reroute"] = False
        return result

    async def _verify_node(self, state: ConversationState) -> Dict[str, Any]:
        """Verify response. Delegates to inner builder."""
        inner_builder = self._get_inner_builder(state)
        result: Dict[str, Any] = await inner_builder._verify_node(state)
        return result

    async def _escalate_node(self, state: ConversationState) -> Dict[str, Any]:
        """Escalate verification failure. Delegates to inner builder."""
        inner_builder = self._get_inner_builder(state)
        result: Dict[str, Any] = await inner_builder._escalate_node(state)
        return result

    async def _respond_node(self, state: ConversationState) -> Dict[str, Any]:
        """Finalize the response."""
        final_text = state.get("final_response", "")

        # If this is an escalation forward, use that message
        escalation_forward_result = state.get("escalation_forward_result")
        if escalation_forward_result:
            final_text = escalation_forward_result

        # Add final message to history if we have history_messages
        history_messages = list(state.get("history_messages", []))
        gemini_history = list(state.get("gemini_history", []))

        # If history_messages is empty (e.g., skip_gemini path where _prepare_node
        # was never called), seed from conversation_history and add the user
        # message so both user input and bot response are persisted for debugging.
        if not history_messages:
            from orchestrator.models.schemas import ConversationMessage

            history_messages = list(state.get("conversation_history", []))
            user_input = state.get("user_input", "")
            if user_input:
                history_messages.append(ConversationMessage(role="user", content=user_input))

        if final_text and not escalation_forward_result:
            from orchestrator.models.schemas import ConversationMessage

            token_metadata = {
                "input_tokens": state.get("total_input_tokens", 0),
                "output_tokens": state.get("total_output_tokens", 0),
                "total_tokens": state.get("total_input_tokens", 0)
                + state.get("total_output_tokens", 0),
                "gemini_rounds": state.get("current_round", 0),
            }

            final_message = ConversationMessage(
                role="model", content=final_text, metadata=token_metadata
            )
            history_messages.append(final_message)

        return {
            "final_response": final_text,
            "history_messages": history_messages,
            "gemini_history": gemini_history,
            "messages": history_messages,  # Legacy alias (save_history now uses history_messages)
        }


def build_full_conversation_graph(
    settings: Optional[AppSettings] = None,
    escalation_handler: Optional[EscalationHandler] = None,
    training_image_handler: Optional[EscalationHandler] = None,
) -> StateGraph:
    """Factory function to build the full conversation graph.

    This is the Phase 3 graph that handles the complete webhook flow,
    replacing handler.py's _process_webhook_async function.

    Args:
        settings: Application settings
        escalation_handler: Handler for escalation tool calls
        training_image_handler: Handler for training image tool calls

    Returns:
        A compiled StateGraph ready for invocation
    """
    builder = FullConversationGraphBuilder(
        settings=settings,
        escalation_handler=escalation_handler,
        training_image_handler=training_image_handler,
    )
    return builder.build()


async def invoke_full_graph(
    graph: StateGraph,
    user_input: str,
    user_context: Any,
    session_id: str,
    metadata: Optional[Dict[str, Any]] = None,
    entity_context: Optional[Any] = None,
) -> Dict[str, Any]:
    """Invoke the full conversation graph.

    Multi-turn decision flags (awaiting_duplicate_decision, awaiting_resume_decision,
    etc.) are managed via the pending_decisions database table, not graph checkpoints.

    Args:
        graph: Compiled StateGraph
        user_input: User's message
        user_context: UserContext object
        session_id: Session identifier
        metadata: Additional metadata
        entity_context: Optional entity context

    Returns:
        Final state with response
    """
    settings = get_settings()

    initial_state: ConversationState = {
        "user_input": user_input,
        "user_context": user_context,
        "session_id": session_id,
        "metadata": metadata or {},
        "entity_context": entity_context,
        # Initialize empty collections
        "messages": [],
        "media": [],
        "conversation_history": [],
        "gemini_history": [],
        "history_messages": [],
        "tool_calls": [],
        "pending_tool_calls": [],
        "accumulated_tool_calls": [],
        "accumulated_tool_results": [],
        "raw_gemini_responses": [],
        # Initialize flags
        "should_continue": True,
        "escalation_triggered": False,
        "verification_enabled": False,
        "verification_passed": None,
        "is_escalated_session": False,
        "safety_escalation_needed": False,
        # Initialize counters
        "current_round": 0,
        "max_rounds": settings.max_tool_rounds,
        "verification_attempt": 0,
        "max_verification_attempts": 2,
        "loaded_message_count": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        # Initialize optional fields
        "final_response": None,
        "error": None,
        "unlocked_tools": [],
        # Phase 4: Expert routing defaults
        "expert_routing_decision": None,
        "active_work_packet": None,
        "matched_expert_id": None,
        "expert_command": None,
        "expert_packet_type": None,
        "expert_executed": False,
        "expert_awaiting_input": False,
        "expert_error": None,
        # Phase 4: Resumable packet handling
        "resumable_packet": None,
        "awaiting_resume_decision": False,
        # Phase 4: Duplicate work detection
        "similar_work_packet": None,
        "awaiting_duplicate_decision": False,
        # Phase 4: Expert redirect to main LLM
        "redirect_to_main_llm": False,
        "redirect_reason": None,
        # Clear transient UI state from previous turn
        "reply_markup": None,
        # Thread disentanglement: extract Telegram signal from metadata
        "thread_id": None,
        "thread_filtered_history": None,
        "thread_assignment_method": None,
        "thread_assignment_confidence": None,
        "thread_is_new": False,
        # Conversation direction planning
        "conversation_direction": None,
        "conversation_context_scope": None,
        "conversation_direction_method": None,
        "conversation_issue_type": None,
        "planned_expert_route": None,
        "sender_telegram_id": (
            str(user_context.user_id)
            if user_context and user_context.user_id and "@" not in str(user_context.user_id)
            else None
        ),
        "telegram_message_id": (metadata or {}).get("telegram_message_id"),
        "reply_to_telegram_message_id": (
            ((metadata or {}).get("reply_to") or {}).get("message_id")
        ),
    }

    result: Dict[str, Any] = await graph.ainvoke(initial_state, config={"recursion_limit": 50})
    return result


__all__ = [
    "FullConversationGraphBuilder",
    "build_full_conversation_graph",
    "invoke_full_graph",
]
