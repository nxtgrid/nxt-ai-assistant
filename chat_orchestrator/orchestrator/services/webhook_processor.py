"""Webhook -> conversation-graph processing.

Runs one user turn through the full LangGraph conversation graph and returns the
final response text, tool-call results, and any inline-button reply markup.

Extracted from ``handler.py`` so orchestrator modules (e.g. callback handlers)
can invoke it directly instead of importing back into the top-level serverless
entrypoint. The dependency now flows one way: ``handler`` -> this module.
"""

from __future__ import annotations

from typing import Any, Dict, List

from orchestrator.models.schemas import (
    MediaAttachment,
    ToolCallResult,
    UserContext,
)
from shared.utils.error_messages import ErrorCategory, categorize_error, get_user_message
from shared.utils.langfuse_utils import langfuse_observe, update_trace
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@langfuse_observe(name="chat-request")
async def process_webhook_with_graph(
    user_input: str,
    user_context: UserContext,
    entity_context: Dict[str, Any] | None = None,
    media: List[MediaAttachment] | None = None,
    session_id: str | None = None,
    metadata: Dict[str, Any] | None = None,
) -> tuple[str, List[ToolCallResult], Dict[str, Any] | None]:
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
        Tuple of (final response text, list of tool call results, optional reply_markup for inline buttons)
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

        LOGGER.info(
            f"Graph execution complete: response_len={len(final_response)}, "
            f"rounds={final_state.get('current_round', 0)}, "
            f"tool_results={len(tool_results)}, has_reply_markup={reply_markup is not None}"
        )

        return final_response, tool_results, reply_markup

    except Exception as e:
        LOGGER.exception(f"Error in LangGraph full graph processing: {e}")
        # Fall back to error message
        _, error_message = categorize_error(e)
        return error_message, [], None
