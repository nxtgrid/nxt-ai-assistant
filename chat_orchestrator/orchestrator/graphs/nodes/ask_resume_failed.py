"""Ask resume failed node for LangGraph.

Presents user with option to retry a failed/blocked packet or start fresh.
This node is invoked when expert_router finds a resumable packet.

The decision context is stored in the pending_decisions table (NOT LangGraph
checkpoints) to ensure it persists across HTTP requests. The expert_router
will check this table first on the next turn to handle the user's response.

Usage in graph:
    builder.add_node("ask_resume_failed", ask_resume_failed)
    builder.add_edge("ask_resume_failed", END)  # Response goes to user
"""

from __future__ import annotations

from typing import Any, Dict

from orchestrator.graphs.state import ConversationState
from orchestrator.services.pending_decision_service import (
    DECISION_TYPE_RESUME,
    PendingDecisionService,
)
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import (
    build_decision_keyboard,
    get_options_for_resume_decision,
    is_inline_buttons_enabled,
)

LOGGER = get_logger(__name__)


async def ask_resume_failed(state: ConversationState) -> Dict[str, Any]:
    """Present user with options for a failed/blocked packet.

    Generates a message asking if user wants to:
    1. Resume/retry the failed packet
    2. Start fresh with a new packet
    3. Abandon and do something else

    Args:
        state: Current conversation state with resumable_packet

    Returns:
        State updates including final_response with options
    """
    packet = state.get("resumable_packet")

    if not packet:
        LOGGER.warning("ask_resume_failed called without resumable_packet")
        return {
            "final_response": "I couldn't find the previous work. Let me know what you'd like to do.",
        }

    # Extract error info from packet state
    packet_state = packet.get("packet_state") or {}
    error_info = packet_state.get("last_error", "Unknown issue")
    failed_step = packet_state.get("error_step") or packet.get("current_step", "unknown")
    blocked_reason = packet_state.get("blocked_reason")

    # Get progress info
    steps_done = packet.get("steps_completed", [])
    packet_type = packet.get("packet_type", "task").replace("_", " ")
    packet_title = packet.get("packet_title", "Previous work")
    packet_status = packet.get("packet_status", "failed")

    # Format the message based on status
    if packet_status == "blocked":
        status_emoji = "⏸️"
        status_desc = "paused"
        issue_desc = f"Waiting on: {blocked_reason or 'external resolution'}"
    else:
        status_emoji = "⚠️"
        status_desc = "stopped"
        # Truncate error message for readability
        issue_desc = f"Issue: {error_info[:150]}{'...' if len(error_info) > 150 else ''}"

    # Build user mention for group chats (when buttons enabled)
    # Uses Telegram deep link format: [Name](tg://user?id=123)
    user_context = state.get("user_context")
    user_mention = ""
    if is_inline_buttons_enabled() and user_context:
        display_name = user_context.username or "there"
        user_mention = f"[{display_name}](tg://user?id={user_context.user_id}), "

    message = f"""{status_emoji} I found an incomplete {packet_type} that {status_desc} earlier:

**{packet_title}**
📍 Progress: {len(steps_done)} step{"s" if len(steps_done) != 1 else ""} completed, stopped at `{failed_step}`
{issue_desc}

{user_mention}Would you like to:
1. **Resume** - Retry from where it stopped
2. **Start fresh** - Begin a new {packet_type}
3. **Abandon** - Cancel this and do something else

Reply with **1**, **2**, or **3**, or just tell me what you'd like to do."""

    LOGGER.info(
        f"Presenting resume options for packet {packet['packet_id']} "
        f"(status: {packet_status}, steps_done: {len(steps_done)})"
    )

    # Store decision context in pending_decisions table (NOT graph state)
    # This ensures it persists across HTTP requests even if checkpointing fails
    session_id = state.get("session_id")
    decision_id = None

    # Extract user info for button click validation
    user_context = state.get("user_context")
    original_user_id = user_context.user_id if user_context else None
    original_org_ids = user_context.organization_ids if user_context else []

    if session_id:
        try:
            decision_service = PendingDecisionService()
            decision = await decision_service.create_decision(
                session_id=session_id,
                decision_type=DECISION_TYPE_RESUME,
                context={
                    "resumable_packet": packet,
                    "matched_expert_id": state.get("matched_expert_id"),
                    "expert_command": state.get("expert_command"),
                    # Raw NL request carries user-supplied parameters that the
                    # synthetic expert_command has dropped; persist it so
                    # "start fresh" can re-parse them onto the new packet.
                    "expert_raw_request": state.get("expert_raw_request"),
                    "expert_packet_type": state.get("expert_packet_type"),
                    "expert_key_entity": state.get("expert_key_entity"),
                    # Store user info for button click validation
                    "original_user_id": original_user_id,
                    "original_org_ids": original_org_ids,
                },
                prompt=message,
            )
            decision_id = decision.get("id")
            LOGGER.info(
                f"Created pending decision for session {session_id} "
                f"(type=resume, packet={packet['packet_id']})"
            )
        except Exception as e:
            LOGGER.error(f"Failed to create pending decision: {e}")
            # Fall back to graph state if database fails
            return {
                "final_response": message,
                "reply_markup": None,
                "awaiting_resume_decision": True,
                "resumable_packet": packet,
                "expert_routing_decision": None,
            }

    # Build inline keyboard if feature is enabled and we have a decision_id
    reply_markup = None
    if is_inline_buttons_enabled() and decision_id:
        options = get_options_for_resume_decision()
        reply_markup = build_decision_keyboard(decision_id, options)
        LOGGER.info(f"Built inline keyboard for resume decision {decision_id}")

    return {
        "final_response": message,
        "reply_markup": reply_markup,
        # Clear graph state flags - database is now source of truth
        "awaiting_resume_decision": False,
        "resumable_packet": None,
        # Clear any expert routing flags - we're waiting for user input
        "expert_routing_decision": None,
    }


__all__ = ["ask_resume_failed"]
