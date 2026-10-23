"""Webhook -> conversation-graph processing.

Runs one user turn through the full LangGraph conversation graph and returns
the final response text, tool-call results, any inline-button reply markup,
and this turn's token usage. See orchestrator.models.envelope for the
transport-neutral view built from this same tuple.

Extracted from ``handler.py`` so orchestrator modules (e.g. callback handlers)
can invoke it directly instead of importing back into the top-level serverless
entrypoint. The dependency now flows one way: ``handler`` -> this module.
"""

from __future__ import annotations

from typing import Any, Dict, List

from langgraph.errors import GraphRecursionError

from orchestrator.graphs.execution_limit_recovery import (
    ExecutionLimitReason,
    format_execution_limit_response,
)
from orchestrator.models.schemas import (
    ConversationMessage,
    MediaAttachment,
    ToolCallResult,
    UserContext,
)
from shared.utils.error_messages import (
    ErrorCategory,
    categorize_error,
    get_user_message,
    is_llm_billing_block,
)
from shared.utils.langfuse_utils import langfuse_observe, update_trace
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


async def _persist_execution_limit_fallback(
    response: str,
    session_id: str | None,
    user_context: UserContext,
    metadata: Dict[str, Any] | None,
) -> None:
    """Persist a final response when graph execution cannot reach save_history."""
    if not session_id:
        return

    try:
        from orchestrator.graphs.nodes.save_user_message import get_or_create_session
        from orchestrator.services.supabase_client import get_supabase_client

        supabase_client = get_supabase_client()
        session = await get_or_create_session(supabase_client, session_id, user_context)
        group_id = getattr(user_context, "chat_id", None)
        if not group_id or not str(group_id).startswith("-"):
            group_id = None
        message_type = "scheduled" if (metadata or {}).get("scheduled_execution") else "interactive"
        response_message = ConversationMessage(
            role="model",
            content=response,
            metadata={
                "message_type": message_type,
                "execution_limit_reason": ExecutionLimitReason.RECURSION_FALLBACK.value,
            },
        )
        await supabase_client.save_messages(
            session_uuid=session.id,
            messages=[response_message],
            from_chat_id=getattr(user_context, "chat_id", None),
            group_id=group_id,
        )
    except Exception as exc:
        LOGGER.warning(f"Could not persist execution-limit fallback: {exc}")


def _organization_id(user_context: UserContext) -> int | None:
    """The user's primary organization id as an int (see ConversationGraphBuilder)."""
    organization_ids = getattr(user_context, "organization_ids", None)
    if not organization_ids:
        return None
    try:
        return int(organization_ids[0])
    except (TypeError, ValueError):
        return None


async def _escalate_llm_outage(
    user_input: str,
    user_context: UserContext,
    session_id: str | None,
    metadata: Dict[str, Any] | None,
) -> str | None:
    """Escalate a customer message the LLM could not answer.

    Returns the customer-facing text only when an escalation actually exists, so
    the caller never promises support that nobody is coming from -- the rule
    ``_escalate_node`` follows. None means "nothing was escalated". Never raises:
    it runs inside an error handler.

    Skips staff (they are support), turns with no session to attach the
    escalation to, and turns that are not a real inbound Telegram message --
    button callbacks and scheduled runs carry no ``telegram_message_id``, and
    their ``user_input`` is not something a customer wrote.
    """
    metadata = metadata or {}
    if (
        getattr(user_context, "is_staff", False)
        or not session_id
        or not metadata.get("telegram_message_id")
    ):
        return None

    try:
        from orchestrator.services.escalation_service import EscalationService

        service = EscalationService()
        if not service.is_enabled():
            return None
        result = await service.escalate_to_support(
            question_summary=(
                "The AI assistant could not answer this message: its language-model "
                f"service was unavailable. Customer wrote: {user_input[:500]}"
            ),
            session_id=session_id,
            organization_id=_organization_id(user_context),
            organization_short_name=getattr(user_context, "organization_name", None),
            customer_chat_id=getattr(user_context, "chat_id", None),
            customer_topic_id=getattr(user_context, "topic_id", None) or metadata.get("topic_id"),
            customer_username=getattr(user_context, "username", None),
            customer_email=getattr(user_context, "user_email", None),
            reason="llm_unavailable",
        )
    except Exception:
        LOGGER.opt(exception=True).warning("Could not escalate a message the LLM could not answer")
        return None

    if not (result or {}).get("success"):
        LOGGER.warning("Escalating a message the LLM could not answer failed: {}", result)
        return None
    return get_user_message(ErrorCategory.ESCALATION, "llm_unavailable")


@langfuse_observe(name="chat-request")
async def process_webhook_with_graph(
    user_input: str,
    user_context: UserContext,
    entity_context: Dict[str, Any] | None = None,
    media: List[MediaAttachment] | None = None,
    session_id: str | None = None,
    metadata: Dict[str, Any] | None = None,
) -> tuple[str, List[ToolCallResult], Dict[str, Any] | None, Dict[str, int]]:
    """Process webhook request using the full LangGraph conversation graph.

    This is the Phase 3 implementation that replaces _process_webhook_async
    with a cleaner graph-based flow.

    Args:
        user_input: User's message text
        user_context: User identity and context
        entity_context: Optional entity context dict
        media: Optional media attachments
        session_id: Session identifier
        metadata: Additional metadata

    Returns:
        Tuple of (final response text, list of tool call results, optional
        reply_markup for inline buttons, token usage as {"input_tokens": N,
        "output_tokens": N} -- empty dict when unavailable, e.g. an error
        path where no reliable total was computed. Never guess a total in
        that case; an empty dict is the honest signal, not {"input_tokens": 0,
        ...}, which would misreport a run that may have spent real tokens
        before failing).
    """
    # Set Langfuse trace metadata (skipped for warmup requests)
    user_email = getattr(user_context, "email", "")
    if user_email != "warmup@system":
        update_trace(
            user_id=session_id,
            session_id=session_id,
            metadata={
                "org_id": getattr(user_context, "organization_id", None),
                "mode": getattr(user_context, "mode", None),
            },
            tags=[t for t in [getattr(user_context, "mode", None), "production"] if t],
        )

    from orchestrator.graphs.full_conversation_graph import (
        build_full_conversation_graph,
        invoke_full_graph,
    )

    LOGGER.info(f"Processing webhook with LangGraph full graph (session={session_id})")

    try:
        # Checkpointer removed — single-turn graph doesn't need state persistence.
        # Multi-turn decisions (duplicate detection, resume) use pending_decisions table.
        graph = build_full_conversation_graph()
        final_state = await invoke_full_graph(
            graph=graph,
            user_input=user_input,
            user_context=user_context,
            session_id=session_id or "",
            metadata=metadata,
            entity_context=entity_context,
        )

        # Extract final response
        final_response: str = final_state.get("final_response", "") or ""

        if not final_response:
            LOGGER.warning("Graph returned empty final_response")
            final_response = get_user_message(ErrorCategory.SYSTEM, "empty_response")

        # Extract tool results (may contain images)
        tool_results: List[ToolCallResult] = final_state.get("accumulated_tool_results", [])

        # Extract reply_markup for inline buttons (decision prompts)
        reply_markup: Dict[str, Any] | None = final_state.get("reply_markup")

        # Extract token usage for this turn (Phase 1 addition — see envelope.py).
        # Absent/zero-default via .get() is fine here (unlike the error paths
        # below): the graph completed normally, so 0 is a real observed count,
        # not a guess standing in for "unknown".
        tokens: Dict[str, int] = {
            "input_tokens": final_state.get("total_input_tokens", 0) or 0,
            "output_tokens": final_state.get("total_output_tokens", 0) or 0,
        }

        LOGGER.info(
            f"Graph execution complete: response_len={len(final_response)}, "
            f"rounds={final_state.get('current_round', 0)}, "
            f"tool_results={len(tool_results)}, has_reply_markup={reply_markup is not None}"
        )

        return final_response, tool_results, reply_markup, tokens

    except GraphRecursionError as exc:
        LOGGER.exception(f"Graph recursion guard fired before graceful recovery: {exc}")
        response = format_execution_limit_response(ExecutionLimitReason.RECURSION_FALLBACK, None)
        await _persist_execution_limit_fallback(
            response=response,
            session_id=session_id,
            user_context=user_context,
            metadata=metadata,
        )
        # No final_state reached this exception -- {} signals "unknown", not
        # a claim that zero tokens were spent before the recursion guard fired.
        return response, [], None, {}
    except Exception as e:
        LOGGER.exception(f"Error in LangGraph full graph processing: {e}")
        # Fall back to error message
        _, error_message = categorize_error(e)
        if is_llm_billing_block(e):
            # categorize_error already gave the neutral temporary-issue text; swap in
            # the escalated wording only if an escalation really was created.
            error_message = (
                await _escalate_llm_outage(user_input, user_context, session_id, metadata)
                or error_message
            )
        return error_message, [], None, {}
