"""Ask about duplicate node for LangGraph.

Presents user with option to run new or resume existing work.
This node is invoked when expert_router finds similar completed work.

The decision context is stored in the pending_decisions table (NOT LangGraph
checkpoints) to ensure it persists across HTTP requests. The expert_router
will check this table first on the next turn to handle the user's response.

Options shown depend on whether the expert is "resumable":
- Non-resumable (default): Run new / Cancel
- Resumable: Run new / Resume / Cancel

The document link and summary are always shown in the message itself,
so a separate "View" option is not needed.

Usage in graph:
    builder.add_node("ask_about_duplicate", ask_about_duplicate)
    builder.add_edge("ask_about_duplicate", END)  # Response goes to user
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from orchestrator.graphs.state import ConversationState
from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider
from orchestrator.services.pending_decision_service import (
    DECISION_TYPE_DUPLICATE,
    PendingDecisionService,
)
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import (
    build_decision_keyboard,
    get_options_for_duplicate_decision,
    is_inline_buttons_enabled,
)

LOGGER = get_logger(__name__)


async def _get_expert_resumable(expert_id: Optional[str]) -> bool:
    """Check if an expert is resumable from its config."""
    if not expert_id:
        return False
    try:
        provider = ExpertInstructionsProvider()
        config = await provider.get_expert_config(expert_id)
        if config:
            return bool(config.resumable)
    except Exception as e:
        LOGGER.warning(f"Could not get expert config for {expert_id}: {e}")
    return False


def _extract_summary_from_packet(packet: Dict[str, Any]) -> str:
    """Extract a brief summary from packet outputs for display."""
    outputs = packet.get("packet_outputs") or {}
    state = packet.get("packet_state") or {}

    summary_parts = []

    # Try to get useful stats from outputs or state
    if "statistics" in outputs:
        stats = outputs["statistics"]
        if "served_buildings" in stats:
            summary_parts.append(f"{stats['served_buildings']} served buildings")
        if "total_poles" in stats:
            summary_parts.append(f"{stats['total_poles']} poles")

    # Check state for common fields
    if "served_buildings" in state:
        summary_parts.append(f"{state['served_buildings']} served buildings")
    if "total_poles" in state:
        summary_parts.append(f"{state['total_poles']} poles")
    if "total_kwp" in state:
        summary_parts.append(f"{state['total_kwp']} kWp")

    if summary_parts:
        return "📊 " + ", ".join(summary_parts[:3])  # Limit to 3 items
    return ""


async def ask_about_duplicate(state: ConversationState) -> Dict[str, Any]:
    """Present user with options when similar completed work exists.

    Shows the document link and summary directly, then offers options:
    - Non-resumable: 1. Run new / 2. Cancel
    - Resumable: 1. Run new / 2. Resume / 3. Cancel

    Args:
        state: Current conversation state with similar_work_packet

    Returns:
        State updates including final_response with options
    """
    packet = state.get("similar_work_packet")
    packet_type = state.get("expert_packet_type", "task")
    expert_id = state.get("matched_expert_id")

    if not packet:
        LOGGER.warning("ask_about_duplicate called without similar_work_packet")
        return {
            "final_response": "Let me start that work for you.",
            "expert_routing_decision": "expert",  # Proceed with new packet
        }

    # Check if expert is resumable
    is_resumable = await _get_expert_resumable(expert_id)

    # Extract completion info
    completed_at = packet.get("completed_at", "")
    if completed_at:
        try:
            from datetime import datetime

            dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
            completed_date = dt.strftime("%B %d, %Y at %I:%M %p")
        except Exception:
            completed_date = completed_at[:10]
    else:
        completed_date = "recently"

    packet_goal = packet.get("packet_goal", "")
    external_url = packet.get("external_url")

    # Extract meaningful title from goal (e.g., "/lpp ExampleGrid" -> "ExampleGrid")
    display_title = packet_goal
    if packet_goal.startswith("/"):
        parts = packet_goal.split(maxsplit=1)
        if len(parts) > 1:
            display_title = parts[1]

    # Format packet type for display
    packet_type_display = packet_type.replace("_", " ")

    # Build the message with document info upfront
    message = (
        f"📋 Found existing {packet_type_display} for *{display_title}* from {completed_date}."
    )

    if external_url:
        message += f"\n🔗 {external_url}"

    # Add summary if available
    summary = _extract_summary_from_packet(packet)
    if summary:
        message += f"\n{summary}"

    # Build user mention for group chats (when buttons enabled)
    # Uses Telegram deep link format: [Name](tg://user?id=123)
    user_context = state.get("user_context")
    user_mention = ""
    if is_inline_buttons_enabled() and user_context:
        display_name = user_context.username or "there"
        user_mention = f"[{display_name}](tg://user?id={user_context.user_id}), "

    # Add options based on resumable setting
    if is_resumable:
        message += f"""

{user_mention}What would you like to do?
1. *Run new* - Start fresh
2. *Resume* - Continue existing work
3. *Cancel* - Do something else

Reply with *1*, *2*, or *3*."""
    else:
        message += f"""

{user_mention}What would you like to do?
1. *Run new* - Start a fresh {packet_type_display}
2. *Cancel* - Do something else

Reply with *1* or *2*."""

    LOGGER.info(
        f"Presenting duplicate options for packet {packet['packet_id']} "
        f"(resumable={is_resumable}, completed: {completed_at[:10] if completed_at else 'unknown'})"
    )

    # Store decision context in pending_decisions table
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
                decision_type=DECISION_TYPE_DUPLICATE,
                context={
                    "similar_work_packet": packet,
                    "matched_expert_id": expert_id,
                    "expert_command": state.get("expert_command"),
                    # Raw NL request carries user-supplied parameters (e.g.
                    # "using Deye technology") that the synthetic expert_command
                    # ("lpp <lat>,<lon>") has dropped. Persist it so "Run new"
                    # can re-parse them onto the fresh packet.
                    "expert_raw_request": state.get("expert_raw_request"),
                    "expert_packet_type": packet_type,
                    "expert_key_entity": state.get("expert_key_entity"),
                    "is_resumable": is_resumable,  # Store for response handling
                    # Store user info for button click validation
                    "original_user_id": original_user_id,
                    "original_org_ids": original_org_ids,
                },
                prompt=message,
            )
            decision_id = decision.get("id")
            LOGGER.info(
                f"Created pending decision for session {session_id} "
                f"(type=duplicate, packet={packet['packet_id']}, resumable={is_resumable})"
            )
        except Exception as e:
            LOGGER.error(f"Failed to create pending decision: {e}")
            return {
                "final_response": message,
                "reply_markup": None,
                "awaiting_duplicate_decision": True,
                "similar_work_packet": packet,
                "matched_expert_id": expert_id,
                "expert_command": state.get("expert_command"),
                "expert_raw_request": state.get("expert_raw_request"),
                "expert_packet_type": packet_type,
                "expert_routing_decision": None,
            }

    # Build inline keyboard if feature is enabled and we have a decision_id
    reply_markup = None
    if is_inline_buttons_enabled() and decision_id:
        options = get_options_for_duplicate_decision(is_resumable)
        reply_markup = build_decision_keyboard(decision_id, options)
        LOGGER.info(f"Built inline keyboard for duplicate decision {decision_id}")

    return {
        "final_response": message,
        "reply_markup": reply_markup,
        "awaiting_duplicate_decision": False,
        "similar_work_packet": None,
        "expert_routing_decision": None,
    }


__all__ = ["ask_about_duplicate"]
