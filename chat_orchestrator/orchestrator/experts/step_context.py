"""Context and result types for expert step execution.

StepContext provides all the information a step handler needs to execute,
including packet data, workflow progress, auth context, and tool access.

StepResult is returned from step handlers to communicate results and state updates.

Usage:
    @register_step("fetch_metrics")
    async def fetch_metrics(context: StepContext) -> StepResult:
        # Access packet inputs
        grid_name = context.packet_inputs.get("grid", {}).get("grid_name")

        # Call MCP tools
        result = await context.mcp_executor.call_tool("grafana_query", {...})

        # Return results
        return StepResult(
            data={"metrics": result},
            state_updates={"metrics_fetched": True},
            progress_message="Fetched grid metrics",
        )
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from orchestrator.models.schemas import UserContext
    from orchestrator.services.tool_executor import ToolExecutor


@dataclass
class StepContext:
    """Full context passed to function step handlers.

    Auth context flows from the main orchestrator's resolve_auth node via
    ConversationState. The mcp_executor is pre-configured with user_context
    in metadata for tool calls.

    For multi-session work, both the current session's auth and the packet's
    original requester are available - use effective_email/effective_org_id
    for consistency within a packet's lifetime.
    """

    # Packet info
    packet_id: str
    packet_type: str
    packet_goal: str
    packet_inputs: Dict[str, Any]
    packet_state: Dict[str, Any]

    # Workflow progress
    current_step: str
    steps_completed: List[str]
    accumulated_results: Dict[str, Any] = field(default_factory=dict)

    # Session context
    session_id: str = ""
    user_email: Optional[str] = None
    organization_id: Optional[int] = None

    # Auth context (from main orchestrator's resolve_auth)
    user_context: Optional["UserContext"] = None

    # Packet's original auth (for multi-session consistency)
    packet_requester_email: Optional[str] = None
    packet_organization_id: Optional[int] = None

    # Tool access (pre-configured with auth)
    mcp_executor: Optional["ToolExecutor"] = None
    available_tools: List[str] = field(default_factory=list)

    # User interaction
    user_input: str = ""

    # Headless execution (when called by persistent agent)
    call_depth: int = 0  # 0 = user-invoked, 1 = agent-invoked, >1 = rejected

    # RAG access (for retrieval during steps)
    rag_context: Optional[List[str]] = None  # Pre-fetched RAG context from prepare_context
    context_message: Optional[str] = None  # Full context message (includes date, enrichment)
    rag_provider: Optional[Any] = None  # RAGProvider instance for on-demand queries

    @property
    def effective_email(self) -> Optional[str]:
        """Get effective email - use packet requester for consistency.

        Within a packet's lifetime, always use the original requester's
        identity for consistency, unless not available.
        """
        return self.packet_requester_email or self.user_email

    @property
    def effective_org_id(self) -> Optional[int]:
        """Get effective org - use packet's org for consistency.

        Within a packet's lifetime, always use the original org
        for consistency, unless not available.
        """
        return self.packet_organization_id or self.organization_id

    def get_previous_result(self, step_name: str) -> Optional[Dict[str, Any]]:
        """Get result from a previous step.

        Args:
            step_name: Name of the previous step

        Returns:
            Result data from that step, or None if not found
        """
        return self.accumulated_results.get(step_name)  # type: ignore[no-any-return]

    def get_input(self, key: str, default: Any = None) -> Any:
        """Get a value from packet inputs or parsed state.

        Looks in the following order:
        1. packet_inputs (original inputs)
        2. packet_state.parsed_inputs (from LLM parsing steps)
        3. accumulated_results from parsing steps

        Args:
            key: Key to look up
            default: Default value if not found

        Returns:
            Value from inputs/state or default
        """
        # First check packet_inputs
        if key in self.packet_inputs:
            return self.packet_inputs[key]

        # Then check parsed_inputs from LLM steps
        parsed_inputs = self.packet_state.get("parsed_inputs", {})
        if key in parsed_inputs:
            return parsed_inputs[key]

        # Check accumulated_results from parsing steps
        for step_name, result in self.accumulated_results.items():
            if step_name.endswith("_data") and isinstance(result, dict):
                if key in result:
                    return result[key]

        return default

    def get_state(self, key: str, default: Any = None) -> Any:
        """Get a value from packet state.

        Args:
            key: Key to look up
            default: Default value if not found

        Returns:
            Value from state or default
        """
        return self.packet_state.get(key, default)

    def get_parameter_value(self, param_name: str, default: Any = None) -> Any:
        """Get a parameter value, checking overrides first.

        For use in handlers with parameter schemas. Checks in order:
        1. User overrides from parameter confirmation
        2. Packet inputs
        3. Packet state
        4. Default value

        Args:
            param_name: Name of the parameter
            default: Default value if not found

        Returns:
            Parameter value from best available source
        """
        # Check user overrides first
        overrides = self.packet_state.get("pending_param_overrides", {})
        if overrides and param_name in overrides:
            return overrides[param_name]

        # Check packet inputs
        if param_name in self.packet_inputs:
            return self.packet_inputs[param_name]

        # Check parsed inputs (from LLM parsing)
        parsed = self.packet_state.get("parsed_inputs", {})
        if parsed and param_name in parsed:
            return parsed[param_name]

        # Check packet state
        if param_name in self.packet_state:
            return self.packet_state[param_name]

        return default

    def set_parameter_override(self, param_name: str, value: Any) -> None:
        """Set a user override for a parameter.

        Called by the confirmation flow when user modifies a parameter.

        Args:
            param_name: Name of the parameter
            value: New value from user
        """
        if "pending_param_overrides" not in self.packet_state:
            self.packet_state["pending_param_overrides"] = {}
        self.packet_state["pending_param_overrides"][param_name] = value

    def clear_parameter_overrides(self) -> None:
        """Clear all parameter overrides after step execution."""
        if "pending_param_overrides" in self.packet_state:
            del self.packet_state["pending_param_overrides"]

    async def query_rag(
        self,
        query: str,
        top_k: int = 5,
        filters: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Query RAG for relevant context on-demand.

        This allows step handlers to retrieve specific context during execution,
        for example when analyzing a grid and needing historical documentation.

        Args:
            query: The search query
            top_k: Number of results to return
            filters: Optional filters (e.g., {"source": "documentation"})

        Returns:
            List of relevant text chunks

        Example:
            # In a step handler
            docs = await context.query_rag(
                f"troubleshooting {grid_name} battery issues",
                top_k=3
            )
        """
        if not self.rag_provider:
            return []

        try:
            # Use the RAG provider's search method
            results = await self.rag_provider.search(
                query=query,
                top_k=top_k,
                filters=filters,
                organization_id=self.effective_org_id,
            )
            return results  # type: ignore[no-any-return]
        except Exception:
            # Fail silently - RAG is supplementary
            return []

    def get_rag_context(self) -> str:
        """Get the combined RAG context as a single string.

        Returns pre-fetched RAG context joined with newlines,
        useful for including in LLM prompts.
        """
        if not self.rag_context:
            return ""
        return "\n\n".join(self.rag_context)

    def clone_for_site(
        self,
        site_name: str,
        site_id: Any,
        state_snapshot: Dict[str, Any],
        preserved_results: Dict[str, Any],
    ) -> "StepContext":
        """Create an independent clone for parallel multi-site execution.

        Deep-copies mutable state (packet_state, packet_inputs, accumulated_results)
        so each site can mutate freely without affecting others.
        Shares read-only resources (mcp_executor, user_context, rag_provider).

        Args:
            site_name: Name of the site this clone is for
            site_id: Database ID of the site
            state_snapshot: Clean state snapshot taken before per-site loop
            preserved_results: Accumulated results to preserve (e.g. resolve_sites)

        Returns:
            New StepContext with independent mutable state
        """
        cloned_state = copy.deepcopy(state_snapshot)
        cloned_state["site_name"] = site_name
        cloned_state["site_id"] = site_id

        cloned_inputs = copy.deepcopy(self.packet_inputs)
        cloned_inputs["site_name"] = site_name
        cloned_inputs["grid_name"] = site_name

        return StepContext(
            # Packet info — deep-copied mutables
            packet_id=self.packet_id,
            packet_type=self.packet_type,
            packet_goal=self.packet_goal,
            packet_inputs=cloned_inputs,
            packet_state=cloned_state,
            # Workflow progress — independent per site
            current_step=self.current_step,
            steps_completed=list(self.steps_completed),
            accumulated_results=copy.deepcopy(preserved_results),
            # Session context — shared (immutable strings/ints)
            session_id=self.session_id,
            user_email=self.user_email,
            organization_id=self.organization_id,
            # Auth context — shared (read-only)
            user_context=self.user_context,
            packet_requester_email=self.packet_requester_email,
            packet_organization_id=self.packet_organization_id,
            # Tool access — shared (read-only, thread-safe async client)
            mcp_executor=self.mcp_executor,
            available_tools=self.available_tools,
            # User interaction
            user_input=self.user_input,
            # RAG access — shared (read-only)
            rag_context=self.rag_context,
            context_message=self.context_message,
            rag_provider=self.rag_provider,
        )

    async def send_progress_to_user(
        self,
        message: str,
        reply_markup: dict | None = None,
    ) -> bool:
        """Send immediate progress message to user via Telegram.

        Use this for long-running steps (>10 seconds) to give users
        immediate feedback that the system is working.

        Automatically attaches a "View State" Mini App button so users
        can track workflow progress, unless an explicit reply_markup is provided.
        When a new View State button is sent, removes the button from the
        previous progress message to keep the chat clean.

        Uses handler._send_telegram_message. Fails silently to not block workflow.

        Args:
            message: Progress message to send (e.g., "⏳ Generating design...")
            reply_markup: Optional Telegram InlineKeyboardMarkup dict.
                If None, a View State button is auto-attached when available.

        Returns:
            True if message sent successfully, False otherwise
        """
        if not self.user_context or not self.user_context.chat_id:
            return False

        import os

        from handler import _send_telegram_message

        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not bot_token:
            return False

        has_view_state = False
        # Auto-attach View State button if no explicit markup provided
        if reply_markup is None:
            try:
                from orchestrator.mini_app.schemas import build_view_state_url
                from shared.utils.telegram_buttons import build_webapp_keyboard

                _ctx_chat_id = self.user_context.chat_id if self.user_context else None
                state_url = build_view_state_url(self.packet_id)
                if state_url:
                    reply_markup = build_webapp_keyboard(
                        "View State", state_url, chat_id=_ctx_chat_id
                    )
                    has_view_state = True
            except Exception:
                pass  # Don't block progress on button failure

        try:
            # Remove View State button from previous progress message
            if has_view_state:
                prev_msg_id = self.packet_state.get("_progress_msg_id")
                if prev_msg_id:
                    try:
                        from shared.utils.telegram_buttons import remove_buttons_from_message

                        await remove_buttons_from_message(self.user_context.chat_id, prev_msg_id)
                    except Exception:
                        pass  # Best-effort cleanup

            msg_id = await _send_telegram_message(
                bot_token,
                self.user_context.chat_id,
                message,
                reply_markup=reply_markup,
            )

            # Track message_id so next progress message can remove this button
            if has_view_state and msg_id:
                self.packet_state["_progress_msg_id"] = msg_id

            # Persist progress message to chat_messages DB so it appears in history
            if self.session_id:
                try:
                    from orchestrator.graphs.nodes.save_user_message import get_or_create_session
                    from orchestrator.models.schemas import ConversationMessage
                    from orchestrator.services.supabase_client import get_supabase_client

                    sb = get_supabase_client()
                    session_obj = await get_or_create_session(
                        sb, self.session_id, self.user_context
                    )
                    progress_msg = ConversationMessage(
                        role="model",
                        content=message,
                        telegram_message_id=msg_id,
                    )
                    await sb.save_messages(
                        session_uuid=session_obj.id,
                        messages=[progress_msg],
                        from_chat_id=self.user_context.chat_id,
                    )
                except Exception:
                    pass  # Don't block workflow on DB failure

            return True
        except Exception:
            return False  # Don't block workflow


@dataclass
class StepResult:
    """Result returned from function step handlers.

    Attributes:
        data: Data to add to accumulated_results (keyed by step name)
        state_updates: Updates to merge into packet_state
        progress_message: Optional message to show user during execution
        needs_user_input: If True, pause workflow and wait for user
        user_prompt: Question to ask user if needs_user_input is True
        error: If set, step failed and workflow should stop
        skip_remaining: If True, skip remaining steps and complete packet
    """

    # Results
    data: Dict[str, Any] = field(default_factory=dict)
    state_updates: Dict[str, Any] = field(default_factory=dict)

    # Progress
    progress_message: Optional[str] = None

    # User interaction
    needs_user_input: bool = False
    user_prompt: Optional[str] = None
    inline_options: Optional[List[str]] = None  # Explicit button labels for Telegram
    mini_app_form: Optional[str] = None  # Form type key for Telegram Mini App popup

    # Error handling
    error: Optional[str] = None

    # Flow control
    skip_remaining: bool = False
    redirect_to_main_llm: bool = False  # Input doesn't belong to this step, route to main LLM

    @classmethod
    def success(
        cls,
        data: Optional[Dict[str, Any]] = None,
        message: Optional[str] = None,
        **state_updates: Any,
    ) -> "StepResult":
        """Create a successful result.

        Args:
            data: Data to store in accumulated_results
            message: Progress message
            **state_updates: State updates to apply

        Returns:
            StepResult indicating success
        """
        return cls(
            data=data or {},
            state_updates=state_updates,
            progress_message=message,
        )

    @classmethod
    def failure(cls, error: str) -> "StepResult":
        """Create a failed result.

        Args:
            error: Error message

        Returns:
            StepResult indicating failure
        """
        return cls(error=error)

    @classmethod
    def needs_input(cls, prompt: str) -> "StepResult":
        """Create a result that pauses for user input.

        Args:
            prompt: Question to ask user

        Returns:
            StepResult that pauses workflow
        """
        return cls(
            needs_user_input=True,
            user_prompt=prompt,
        )

    @classmethod
    def not_my_input(cls, reason: Optional[str] = None) -> "StepResult":
        """Create a result indicating input doesn't belong to this step.

        Use when user input appears to be a new request unrelated to
        the pending workflow step. The workflow will be paused and
        the input routed to the main LLM.

        Args:
            reason: Optional explanation for logging

        Returns:
            StepResult that redirects to main LLM
        """
        return cls(
            redirect_to_main_llm=True,
            progress_message=reason or "Input appears to be a new request",
        )

    @property
    def is_success(self) -> bool:
        """Check if result indicates success."""
        return self.error is None


__all__ = ["StepContext", "StepResult"]
