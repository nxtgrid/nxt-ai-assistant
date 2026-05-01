"""Save history node for LangGraph.

This node persists conversation history to Supabase,
creating sessions as needed. Messages are tagged with metadata
for downstream context management (message_type).
"""

import asyncio
from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from orchestrator.services.supabase_client import get_supabase_client


def _determine_message_type(state: ConversationState) -> str:
    """Determine message type from state signals.

    Returns one of: "scheduled", "scheduled_user", "expert_workflow",
    "command_result", "interactive".
    """
    metadata = state.get("metadata", {})
    if metadata.get("scheduled_execution"):
        return "scheduled"
    if state.get("expert_executed"):
        return "expert_workflow"
    if state.get("parsed_command"):
        return "command_result"
    return "interactive"


def _tag_new_messages(
    messages: list,
    loaded_message_count: int,
    message_type: str,
) -> None:
    """Tag new messages with metadata for context management.

    Mutates message metadata in-place before saving.
    """
    new_messages = messages[loaded_message_count:]
    for msg in new_messages:
        if not hasattr(msg, "metadata") or msg.metadata is None:
            msg.metadata = {}
        msg.metadata["message_type"] = message_type


async def save_history(state: ConversationState) -> Dict[str, Any]:
    """Save conversation history to Supabase.

    This node:
    1. Gets or creates a session
    2. For new sessions, sends debug notification
    3. Tags new messages with message_type, topic, and entities metadata
    4. Determines group_id for group chats
    5. Saves only new messages (after loaded_message_count)

    Args:
        state: Current conversation state

    Returns:
        Empty state updates (side-effect only node)
    """
    # Use singleton client instead of state (avoids checkpointer serialization errors)
    supabase_client = get_supabase_client()
    user_context = state.get("user_context")
    session_id = state.get("session_id")
    # Use history_messages (populated by prepare/call_gemini/respond nodes)
    # NOT messages (which is only set by respond node that runs AFTER this node)
    messages = state.get("history_messages", [])
    loaded_message_count = state.get("loaded_message_count", 0)

    if not session_id:
        LOGGER.warning("No session_id available, skipping history save")
        return {}

    # Tag new messages with context management metadata before saving
    message_type = _determine_message_type(state)
    _tag_new_messages(
        messages=messages,
        loaded_message_count=loaded_message_count,
        message_type=message_type,
    )

    # Propagate thread disentanglement fields onto new messages
    thread_id = state.get("thread_id")
    sender_telegram_id = state.get("sender_telegram_id")
    telegram_message_id = state.get("telegram_message_id")
    reply_to_telegram_message_id = state.get("reply_to_telegram_message_id")
    if thread_id or sender_telegram_id or telegram_message_id:
        new_messages = messages[loaded_message_count:]
        for msg in new_messages:
            # Telegram-specific fields only apply to user messages
            if msg.role == "user":
                if telegram_message_id and not msg.telegram_message_id:
                    msg.telegram_message_id = telegram_message_id
                if reply_to_telegram_message_id and not msg.reply_to_telegram_message_id:
                    msg.reply_to_telegram_message_id = reply_to_telegram_message_id
                if sender_telegram_id and not msg.sender_id:
                    msg.sender_id = sender_telegram_id
            # All new messages get the assigned thread_id
            if thread_id and not msg.thread_id:
                msg.thread_id = thread_id

    try:
        # Get or create session (shared helper with save_user_message)
        from orchestrator.graphs.nodes.save_user_message import get_or_create_session

        session_obj = await get_or_create_session(supabase_client, session_id, user_context)

        # Now save messages using the session's UUID
        # Determine group_id for group chats (topics use negative chat_id with -100 prefix)
        group_id = None
        if user_context and user_context.chat_id and str(user_context.chat_id).startswith("-"):
            # This is a group chat
            group_id = user_context.chat_id

        # Only save new messages (skip already-persisted ones from conversation history)
        new_messages = messages[loaded_message_count:]

        # If user message was already saved early, skip the first user message
        if state.get("user_message_saved") and new_messages and new_messages[0].role == "user":
            new_messages = new_messages[1:]

        # Enrich message metadata with organization info for group messages
        if new_messages and user_context and group_id:
            org_id = user_context.organization_ids[0] if user_context.organization_ids else None
            org_name = user_context.organization_name
            if org_id or org_name:
                for msg in new_messages:
                    if org_id and "organization_id" not in msg.metadata:
                        msg.metadata["organization_id"] = org_id
                    if org_name and "organization_name" not in msg.metadata:
                        msg.metadata["organization_name"] = org_name

        if new_messages:
            await supabase_client.save_messages(
                session_uuid=session_obj.id,
                messages=new_messages,
                from_chat_id=user_context.chat_id if user_context else None,
                group_id=group_id,
            )
            LOGGER.info(
                f"Saved {len(new_messages)} new messages for session {session_id} "
                f"(UUID: {session_obj.id}, type: {message_type})"
            )

        # Trigger progressive summarization if enabled (true fire-and-forget)
        from orchestrator.services.conversation_summarizer import is_summary_enabled

        if is_summary_enabled():

            async def _run_summarization() -> None:
                try:
                    from orchestrator.services.conversation_summarizer import ConversationSummarizer

                    total_count = loaded_message_count + len(new_messages)
                    summarizer = ConversationSummarizer()
                    await summarizer.maybe_summarize(
                        session_uuid=session_obj.id,
                        total_message_count=total_count,
                    )
                    await summarizer.aclose()
                except Exception as e:
                    LOGGER.warning(f"Progressive summarization failed (non-blocking): {e}")

            # Fire-and-forget: don't block the response on summarization
            asyncio.create_task(_run_summarization())

    except Exception as e:
        # Don't fail the request if history saving fails (e.g., Supabase not configured)
        LOGGER.warning(f"Could not save conversation history: {e}")

    return {}
