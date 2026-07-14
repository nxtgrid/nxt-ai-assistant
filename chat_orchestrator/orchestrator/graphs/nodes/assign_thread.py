"""Conversation direction planning node for LangGraph.

Plans the current message's thread context and natural-language expert intent.
Thread disentanglement can be disabled while direction planning still runs.
"""

from typing import Any, Dict

from orchestrator.graphs.state import ConversationState
from orchestrator.services.conversation_direction import ConversationDirectionService
from orchestrator.services.thread_assignment import (
    classify_issue_type,
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
    """Plan current message direction and thread context.

    Thread assignment and natural-language expert intent share this node so
    downstream routing sees one coherent pre-Gemini direction plan. On failure,
    returns empty (fail-open — existing downstream behavior applies).
    """
    user_input = state.get("user_input", "")
    conversation_history = state.get("conversation_history", [])
    reply_to_id = state.get("reply_to_telegram_message_id")
    session_id = state.get("session_id", "")

    if not user_input:
        return {}

    thread_enabled = is_thread_disentanglement_enabled()
    active_work_packet = await _fetch_active_work_packet(session_id) if thread_enabled else None

    try:
        direction = await ConversationDirectionService().plan(
            user_input=user_input,
            conversation_history=conversation_history,
            reply_to_telegram_message_id=reply_to_id,
            active_work_packet=active_work_packet,
            thread_disentanglement_enabled=thread_enabled,
        )
    except Exception as e:
        LOGGER.warning(f"Conversation direction planning failed (fail-open): {e}")
        return {}

    updates: Dict[str, Any] = direction.to_state_updates()

    if not thread_enabled or not direction.thread_id:
        LOGGER.info(
            f"Conversation direction planned: direction={direction.direction}, "
            f"context_scope={direction.context_scope}, method={direction.method}"
        )
        return updates

    LOGGER.info(
        f"Conversation direction planned: direction={direction.direction}, "
        f"thread={direction.thread_id} "
        f"(thread_method={direction.thread_method}, new={direction.thread_is_new}, "
        f"confidence={direction.thread_confidence:.2f}, "
        f"filtered={len(direction.thread_filtered_history or [])}/{len(conversation_history)} messages)"
    )

    # Persist new threads to chat_threads table with issue type classification.
    # Skip LLM classification for explicit-signal threads ("new issue" etc.) — the signal
    # phrase itself doesn't describe the issue; the next message will be more informative.
    if direction.thread_is_new:
        try:
            if direction.thread_method == "explicit_signal":
                issue_type = "other"
            elif direction.issue_type != "other":
                issue_type = direction.issue_type
            else:
                issue_type = await classify_issue_type(user_input)
            LOGGER.info(f"New thread {direction.thread_id} classified as issue_type={issue_type}")

            user_context = state.get("user_context")
            organization_id = None
            if user_context and user_context.organization_ids:
                organization_id = int(user_context.organization_ids[0])

            from orchestrator.services.supabase_client import get_supabase_client

            supabase = get_supabase_client()
            if supabase:
                await supabase.save_thread(
                    thread_id=direction.thread_id,
                    session_id=session_id,
                    organization_id=organization_id,
                    issue_type=issue_type,
                )
        except Exception as e:
            LOGGER.warning(f"Failed to persist new thread metadata (non-fatal): {e}")

    return updates
