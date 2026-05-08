"""Assign thread node for LangGraph.

Classifies the incoming message into a conversation thread and filters
conversation_history to only thread-relevant messages. Stores the filtered
list as thread_filtered_history for downstream nodes (prepare, grid hints).

When the feature flag is off, this node is a pass-through.
"""

from typing import Any, Dict

from orchestrator.graphs.state import ConversationState
from orchestrator.services.thread_assignment import (
    ThreadAssignmentService,
    classify_issue_type,
    filter_history_by_thread,
    is_thread_disentanglement_enabled,
)
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


async def _fetch_active_work_packet(session_id: str) -> dict | None:
    """Fetch the most recent active/awaiting_input packet for this session.

    This allows the thread assigner to route messages to the workflow's
    thread (Path A.3) instead of falling through to single_active/LLM paths.
    """
    if not session_id:
        return None
    try:
        service = WorkPacketService()
        packets = await service.get_active_packets_for_session(session_id)
        if packets:
            pkt = packets[0]
            # Return in the format ThreadAssignmentService expects
            return {"state": pkt.get("packet_state") or {}}
    except Exception as e:
        LOGGER.debug(f"Could not fetch active work packet: {e}")
    return None


async def assign_thread(state: ConversationState) -> Dict[str, Any]:
    """Assign the current message to a conversation thread.

    If thread disentanglement is disabled, returns empty (pass-through).
    On any failure, returns empty (fail-open — full history used).
    """
    if not is_thread_disentanglement_enabled():
        return {}

    user_input = state.get("user_input", "")
    conversation_history = state.get("conversation_history", [])
    reply_to_id = state.get("reply_to_telegram_message_id")
    session_id = state.get("session_id", "")

    if not user_input:
        return {}

    # Fetch active work packet from DB so Path A.3 (active_expert) can fire.
    # The graph initializes active_work_packet=None and expert_router only
    # populates it later, so without this fetch Path A.3 was dead code.
    active_work_packet = await _fetch_active_work_packet(session_id)

    service = ThreadAssignmentService()
    assignment = await service.assign_thread(
        user_input=user_input,
        conversation_history=conversation_history,
        reply_to_telegram_message_id=reply_to_id,
        active_work_packet=active_work_packet,
    )

    if assignment is None:
        LOGGER.warning("Thread assignment returned None (fail-open), using full history")
        return {}

    # Filter history to thread-relevant messages
    filtered = filter_history_by_thread(conversation_history, assignment.thread_id)

    LOGGER.info(
        f"Thread assigned: {assignment.thread_id} "
        f"(method={assignment.method}, new={assignment.is_new}, "
        f"confidence={assignment.confidence:.2f}, "
        f"filtered={len(filtered)}/{len(conversation_history)} messages)"
    )

    # Persist new threads to chat_threads table with issue type classification.
    # Skip LLM classification for explicit-signal threads ("new issue" etc.) — the signal
    # phrase itself doesn't describe the issue; the next message will be more informative.
    if assignment.is_new:
        try:
            if assignment.method == "explicit_signal":
                issue_type = "other"
            else:
                issue_type = await classify_issue_type(user_input)
            LOGGER.info(f"New thread {assignment.thread_id} classified as issue_type={issue_type}")

            user_context = state.get("user_context")
            organization_id = None
            if user_context and user_context.organization_ids:
                organization_id = int(user_context.organization_ids[0])

            from orchestrator.services.supabase_client import get_supabase_client

            supabase = get_supabase_client()
            if supabase:
                await supabase.save_thread(
                    thread_id=assignment.thread_id,
                    session_id=session_id,
                    organization_id=organization_id,
                    issue_type=issue_type,
                )
        except Exception as e:
            LOGGER.warning(f"Failed to persist new thread metadata (non-fatal): {e}")

    return {
        "thread_id": assignment.thread_id,
        "thread_filtered_history": filtered,
    }
