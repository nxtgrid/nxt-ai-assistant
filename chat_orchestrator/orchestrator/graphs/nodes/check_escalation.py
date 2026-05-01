"""Check escalation status node for LangGraph.

This node checks if a session has an active escalation and auto-forwards
customer messages to the support group instead of processing via LLM.
"""

import os
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState


async def check_escalation(state: ConversationState) -> Dict[str, Any]:
    """Check if session has active escalation and handle auto-forward.

    This node:
    1. Checks if the user is a customer (not staff)
    2. Checks if the session has an active escalation
    3. If escalated, forwards the customer message to support group
    4. Returns early with confirmation message

    The graph should route to END if is_escalated_session is True
    and escalation_forward_result is set.

    Args:
        state: Current conversation state

    Returns:
        State updates with escalation status and forward result
    """
    user_context = state.get("user_context")
    session_id = state.get("session_id")
    user_input = state.get("user_input", "")

    # Note: we do NOT skip staff users here. Messages from the escalation
    # support group are already intercepted upstream in handler.py (before
    # the graph runs). Any message that reaches this node is from the user's
    # own chat, so staff with open escalations should also get forwarded.

    # Import here to avoid circular imports
    from orchestrator.services.escalation_service import EscalationService

    escalation_service = EscalationService(
        supabase_url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL"),
        supabase_key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY"),
    )

    # Check if session has active escalation
    is_escalated = await escalation_service.is_session_escalated(session_id)

    if not is_escalated:
        LOGGER.debug(f"Session {session_id} has no active escalation")
        return {
            "is_escalated_session": False,
            "escalation_forward_result": None,
        }

    LOGGER.info(f"Session {session_id} has active escalation, auto-forwarding message")

    # Forward customer message (including any media) to escalation group
    metadata = state.get("metadata", {})
    forward_result = await escalation_service.forward_customer_message(
        session_id=session_id,
        customer_message=user_input,
        customer_username=user_context.username if user_context else None,
        media_metadata=metadata,
    )

    if forward_result.get("success"):
        confirmation_message = forward_result.get(
            "message", "Your message has been forwarded to the support team."
        )
        return {
            "is_escalated_session": True,
            "escalation_forward_result": confirmation_message,
            "final_response": confirmation_message,  # Set final response for early exit
            "should_continue": False,  # Signal to stop processing
        }
    else:
        # Forwarding failed - continue with normal LLM processing
        LOGGER.error(f"Failed to forward customer message: {forward_result.get('error')}")
        return {
            "is_escalated_session": True,
            "escalation_forward_result": None,
        }
