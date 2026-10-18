"""Check escalation status node for LangGraph.

This node checks if a session has an active escalation and, if so, forwards the
customer's message into that escalation's support thread. The bot still answers
the message as usual.
"""

import os
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState


async def check_escalation(state: ConversationState) -> Dict[str, Any]:
    """Forward a message from a session with an active escalation to its thread.

    This node:
    1. Checks if the session has an active escalation
    2. If so, forwards the message (including any media) into the escalation's
       support thread, so staff see the follow-up
    3. Lets the turn continue either way -- the bot answers the message too

    Forwarding used to replace the bot's reply ("Your message has been
    forwarded...") until a human closed the escalation, which could take days:
    one escalation stopped the bot answering that chat entirely, simple questions
    included.

    Args:
        state: Current conversation state

    Returns:
        State updates: is_escalated_session, and escalation_followup_forwarded
        (whether this message actually reached the escalation thread)
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
            "escalation_followup_forwarded": False,
        }

    LOGGER.info(f"Session {session_id} has active escalation, forwarding message to its thread")

    # Forward customer message (including any media) to escalation group
    metadata = state.get("metadata", {})
    forward_result = await escalation_service.forward_customer_message(
        session_id=session_id,
        customer_message=user_input,
        customer_username=user_context.username if user_context else None,
        media_metadata=metadata,
    )

    forwarded = bool(forward_result.get("success"))
    if not forwarded:
        LOGGER.error(f"Failed to forward customer message: {forward_result.get('error')}")

    return {
        "is_escalated_session": True,
        "escalation_followup_forwarded": forwarded,
    }
