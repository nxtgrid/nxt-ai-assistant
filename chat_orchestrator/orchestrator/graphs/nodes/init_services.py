"""Initialize services node for LangGraph.

This node initializes services and loads conversation history.

NOTE: Services are NOT stored in LangGraph state to avoid serialization errors
with the PostgreSQL checkpointer. Use singleton accessors instead:
- get_supabase_client() from orchestrator.services.supabase_client
- get_auth_service() from shared.auth
- get_settings() from orchestrator.config.settings
- get_permissions_service() from orchestrator.services.user_permissions
"""

from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState


async def init_services(state: ConversationState) -> Dict[str, Any]:
    """Initialize services and load conversation history.

    This node:
    1. Validates services are accessible via singletons (not stored in state)
    2. Handles session lookup (legacy + hashed) — single lookup, reused
    3. Loads conversation history

    NOTE: Services are accessed via singletons (get_supabase_client(), etc.)
    rather than stored in state, to avoid PostgreSQL checkpointer serialization errors.

    Args:
        state: Current conversation state

    Returns:
        State updates with conversation history (no service objects)
    """
    session_id = state.get("session_id")
    user_context = state.get("user_context")

    # Import singleton accessors
    from orchestrator.services.supabase_client import get_supabase_client

    # Get singleton clients (validates they're configured correctly)
    supabase_client = get_supabase_client()

    LOGGER.info(
        f"Services validated: session={session_id}, "
        f"user={user_context.user_email if user_context else 'unknown'}"
    )

    # Handle session lookup (support both legacy and hashed IDs)
    loaded_message_count = 0
    conversation_history = []

    # Check if this is a scheduled execution - skip loading chat history
    # since scheduled messages don't need conversational context
    metadata = state.get("metadata", {})
    is_scheduled_execution = metadata.get("scheduled_execution", False)

    if is_scheduled_execution:
        LOGGER.info("Scheduled execution - skipping chat history load")

    # Look up session ONCE and reuse for all operations below
    session = None
    if session_id and supabase_client and not is_scheduled_execution:
        try:
            session = await supabase_client.get_session(session_id)

            if not session and user_context and user_context.chat_id:
                # Try legacy format lookup
                session = await supabase_client.get_session_by_chat_id(
                    chat_id=user_context.chat_id,
                    topic_id=user_context.topic_id,
                )

            if session:
                # Load conversation history, excluding scheduled messages
                # which pollute interactive conversation context
                messages = await supabase_client.get_messages_filtered(
                    session.id,
                    exclude_types=["scheduled", "scheduled_user"],
                )
                if messages:
                    conversation_history = messages
                    loaded_message_count = len(messages)
                    LOGGER.info(f"Loaded {loaded_message_count} messages from session {session_id}")
        except Exception as e:
            LOGGER.warning(f"Failed to load session history: {e}")

    # Load progressive conversation summary if enabled (reuse session from above)
    conversation_summary = None
    from orchestrator.services.conversation_summarizer import is_summary_enabled

    if is_summary_enabled() and session and not is_scheduled_execution:
        try:
            from orchestrator.services.conversation_summarizer import ConversationSummarizer

            summarizer = ConversationSummarizer()
            conversation_summary = await summarizer.get_cached_summary(session.id)
            if conversation_summary:
                LOGGER.info(f"Loaded conversation summary ({len(conversation_summary)} chars)")
        except Exception as e:
            LOGGER.warning(f"Failed to load conversation summary (continuing without): {e}")

    # Reply-to context jump: if user is replying to an old message,
    # load surrounding messages from that era to restore context (reuse session)
    metadata = state.get("metadata", {}) if not is_scheduled_execution else metadata
    reply_to = metadata.get("reply_to", {})
    reply_date = reply_to.get("date") if isinstance(reply_to, dict) else None

    if reply_date and session and conversation_history:
        try:
            reply_era_messages = await supabase_client.get_messages_around_timestamp(
                session_uuid=session.id,
                target_timestamp=reply_date,
                window_before=5,
                window_after=3,
                exclude_types=["scheduled", "scheduled_user"],
            )
            if reply_era_messages:
                # Deduplicate against already-loaded messages by timestamp
                existing_timestamps = {m.timestamp for m in conversation_history if m.timestamp}
                new_context = [
                    m for m in reply_era_messages if m.timestamp not in existing_timestamps
                ]
                if new_context:
                    # Insert reply-era context at the start with a separator
                    from orchestrator.models.schemas import ConversationMessage

                    separator = ConversationMessage(
                        role="user",
                        content="[Context from the message being replied to:]",
                    )
                    conversation_history = [separator] + new_context + conversation_history
                    LOGGER.info(f"Added {len(new_context)} reply-era messages for context jump")
        except Exception as e:
            LOGGER.warning(f"Reply-to context jump failed (continuing without): {e}")

    # Cross-session context jump: if this is a brand-new session (no history)
    # and the user is replying to a bot message from a different topic/thread,
    # pull context from the most recent prior session for this chat so the LLM
    # doesn't receive the user's closing message completely out of context.
    # (Example: customer replies "Payment received. Thanks" to a support-team
    # response that was sent to a different thread — without this, the LLM has
    # no context and may echo the user's phrase back.)
    if (
        not conversation_history
        and not is_scheduled_execution
        and isinstance(reply_to, dict)
        and reply_to.get("is_bot")
        and reply_to.get("date")
        and user_context
        and user_context.chat_id
    ):
        try:
            prior_session = await supabase_client.get_recent_session_for_chat(
                telegram_chat_id=user_context.chat_id,
                exclude_session_uuid=session.id if session else None,
            )
            if prior_session:
                cross_msgs = await supabase_client.get_messages_around_timestamp(
                    session_uuid=prior_session.id,
                    target_timestamp=reply_to["date"],
                    window_before=5,
                    window_after=3,
                    exclude_types=["scheduled", "scheduled_user"],
                )
                if cross_msgs:
                    from orchestrator.models.schemas import ConversationMessage

                    separator = ConversationMessage(
                        role="user",
                        content="[Previous conversation context from an earlier thread:]",
                    )
                    conversation_history = [separator] + cross_msgs
                    LOGGER.info(
                        f"Cross-session context jump: loaded {len(cross_msgs)} messages "
                        f"from prior session {prior_session.id} "
                        f"(topic {prior_session.telegram_topic_id})"
                    )
        except Exception as e:
            LOGGER.warning(f"Cross-session context jump failed (continuing without): {e}")

    # NOTE: We intentionally do NOT return service objects here.
    # They are accessed via singletons to avoid checkpointer serialization errors.
    result = {
        "conversation_history": conversation_history,
        "loaded_message_count": loaded_message_count,
    }
    if conversation_summary:
        result["conversation_summary"] = conversation_summary
    return result
