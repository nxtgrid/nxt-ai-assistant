"""Early user message persistence node.

Saves the user's input message to the database immediately after auth
resolution, before any processing begins. This ensures the message is
preserved even if the pipeline crashes or the container restarts mid-request.

The session is created if it doesn't exist yet. The full conversation
history (tool calls, bot responses) is still saved by save_history at
the end of the pipeline.
"""

from typing import Any, Dict, Optional

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from orchestrator.models.schemas import ConversationMessage
from orchestrator.services.supabase_client import get_supabase_client


async def get_or_create_session(
    supabase_client, session_id: str, user_context: Optional[Any] = None
):
    """Get existing session or create a new one. Shared by save_user_message and save_history."""
    session_obj = await supabase_client.get_session(session_id)
    if not session_obj:
        session_title = f"Chat {session_id[:20]}"
        if user_context and user_context.chat_title:
            session_title = user_context.chat_title

        session_obj = await supabase_client.create_session(
            session_id=session_id,
            user_id=None,
            title=session_title,
            organization_id=(
                user_context.organization_ids[0]
                if user_context and user_context.organization_ids
                else None
            ),
            telegram_chat_id=user_context.chat_id if user_context else None,
            telegram_topic_id=user_context.topic_id if user_context else None,
        )
        LOGGER.info(f"Created session {session_id} (UUID: {session_obj.id})")
    return session_obj


async def save_user_message(state: ConversationState) -> Dict[str, Any]:
    """Persist the user's input message early in the pipeline.

    Creates the session if needed, then saves a single user message.
    Sets `user_message_saved=True` in state so save_history can skip
    re-saving it.
    """
    session_id = state.get("session_id")
    user_input = state.get("user_input", "")
    user_context = state.get("user_context")

    if not session_id or not user_input:
        return {}

    try:
        supabase_client = get_supabase_client()
        session_obj = await get_or_create_session(supabase_client, session_id, user_context)

        # Build a minimal user message
        group_id = None
        if user_context and user_context.chat_id and str(user_context.chat_id).startswith("-"):
            group_id = user_context.chat_id

        user_msg = ConversationMessage(role="user", content=user_input)

        # Set sender info for thread disentanglement
        sender_telegram_id = state.get("sender_telegram_id")
        telegram_message_id = state.get("telegram_message_id")
        reply_to_telegram_message_id = state.get("reply_to_telegram_message_id")
        if telegram_message_id:
            user_msg.telegram_message_id = telegram_message_id
        if reply_to_telegram_message_id:
            user_msg.reply_to_telegram_message_id = reply_to_telegram_message_id
        if sender_telegram_id:
            user_msg.sender_id = sender_telegram_id

        # Idempotency: skip if this telegram_message_id was already saved
        # (prevents duplicates on Telegram webhook retries after container restarts)
        if telegram_message_id:
            import asyncio

            existing = await asyncio.to_thread(
                lambda: supabase_client._get_client()
                .table("chat_messages")
                .select("id")
                .eq("telegram_message_id", telegram_message_id)
                .limit(1)
                .execute()
            )
            if existing.data:
                LOGGER.debug(f"Early save: message {telegram_message_id} already exists, skipping")
                return {"user_message_saved": True, "session_uuid": str(session_obj.id)}

        await supabase_client.save_messages(
            session_uuid=session_obj.id,
            messages=[user_msg],
            from_chat_id=user_context.chat_id if user_context else None,
            group_id=group_id,
        )
        LOGGER.debug(f"Early save: persisted user message for session {session_id}")

        return {"user_message_saved": True, "session_uuid": str(session_obj.id)}

    except Exception as e:
        # Non-fatal — save_history will still save everything at the end
        LOGGER.warning(f"Early user message save failed (non-fatal): {e}")
        return {}
