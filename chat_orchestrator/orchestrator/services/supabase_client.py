"""Enhanced Supabase client for comprehensive database operations."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from orchestrator.models.database import (
    ChatMessageModel,
    ChatSessionModel,
    ConversationSummary,
    DocumentChunkModel,
    RAGSearchResult,
    TokenUsageModel,
    ToolCallModel,
    UserModel,
)
from orchestrator.models.schemas import ConversationMessage, FunctionCall, ToolCallResult
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


class EnhancedSupabaseClient:
    """Enhanced Supabase client for comprehensive database operations."""

    def __init__(
        self,
        url: str,
        key: str,
        anon_key: Optional[str] = None,
        jwt_token: Optional[str] = None,
    ) -> None:
        """Initialize Supabase client.

        Args:
            url: Supabase project URL
            key: Supabase API key (service role, legacy)
            anon_key: Anon/public key (new API approach)
            jwt_token: Service JWT token for authentication (new API approach)
        """
        self.url = url
        self.key = key
        self.anon_key = anon_key
        self.jwt_token = jwt_token
        self._client: Optional[Any] = None

    def _get_client(self) -> Any:
        """Lazy initialization of Supabase client with new API key approach or legacy."""
        if self._client is None:
            try:
                import os

                from supabase import create_client  # type: ignore[attr-defined]

                # Priority: 1) passed service key, 2) passed anon key + JWT, 3) env anon key
                if self.key:
                    # Use the service role key passed to constructor
                    self._client = create_client(self.url, self.key)
                    LOGGER.info("Supabase client initialized with service role key")
                elif self.anon_key:
                    # Use anon key with optional JWT authentication
                    self._client = create_client(self.url, self.anon_key)
                    jwt_token = self.jwt_token or os.getenv("SUPABASE_SERVICE_JWT", "")
                    if jwt_token:
                        self._client.postgrest.auth(jwt_token)
                        LOGGER.info("Supabase client initialized with anon key + JWT")
                    else:
                        LOGGER.info("Supabase client initialized with anon key (no JWT)")
                else:
                    # Fallback to environment variables
                    anon_key = os.getenv("SUPABASE_ANON_KEY", "")
                    if anon_key:
                        self._client = create_client(self.url, anon_key)
                        LOGGER.info("Supabase client initialized with env anon key")
                    else:
                        raise ValueError("No Supabase API key provided")

            except ImportError:
                raise ImportError(
                    "supabase package not installed. Install with: pip install supabase"
                )
        return self._client

    # User Management
    async def create_or_get_user(
        self, external_user_id: str, username: Optional[str] = None, email: Optional[str] = None
    ) -> UserModel:
        """Create or retrieve user by external ID."""
        try:
            client = self._get_client()

            # Try to get existing user from accounts table
            response = (
                client.table("accounts")
                .select("*")
                .eq("external_user_id", external_user_id)
                .is_("deleted_at", None)
                .execute()
            )

            if response.data:
                return UserModel(**response.data[0])

            # Create new user in accounts table
            user_data = {"external_user_id": external_user_id, "username": username, "email": email}
            response = client.table("accounts").insert(user_data).execute()
            return UserModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error creating/getting user: {e}")
            raise

    # Session Management
    async def create_session(
        self,
        session_id: str,
        user_id: Optional[UUID] = None,
        title: Optional[str] = None,
        organization_id: Optional[int] = None,
        telegram_chat_id: Optional[str] = None,
        telegram_topic_id: Optional[str] = None,
    ) -> ChatSessionModel:
        """Create a new chat session.

        Args:
            session_id: Unique session identifier (hashed for security)
            user_id: Optional user UUID
            title: Optional session title
            organization_id: Organization ID for multi-tenant isolation
            telegram_chat_id: Original Telegram chat ID for admin UI lookups
            telegram_topic_id: Telegram forum topic ID if applicable
        """
        try:
            client = self._get_client()

            session_data = {
                "session_id": session_id,
                "user_id": str(user_id) if user_id else None,
                "title": title,
                "organization_id": organization_id,
                "telegram_chat_id": telegram_chat_id,
                "telegram_topic_id": telegram_topic_id,
            }
            response = client.table("chat_sessions").insert(session_data).execute()
            return ChatSessionModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error creating session: {e}")
            raise

    async def update_session_organization(
        self,
        session_id: str,
        organization_id: int,
        organization_short_name: Optional[str] = None,
    ) -> None:
        """Persist resolved organization_id to an existing chat session.

        Called after auth resolution to backfill the org context that wasn't
        available at session creation time. Non-fatal — logs warning on failure.
        """
        try:
            client = self._get_client()
            update_data: dict = {"organization_id": organization_id}
            if organization_short_name:
                # Store org name in metadata JSONB (no schema migration needed)
                update_data["metadata"] = {"organization_short_name": organization_short_name}
            client.table("chat_sessions").update(update_data).eq("session_id", session_id).execute()
        except Exception as e:
            LOGGER.warning(f"Failed to update session org_id: {e}")

    async def update_session_title(self, session_id: str, title: str) -> None:
        """Update the title of an existing chat session.

        Called when a Telegram forum topic is renamed so the sidebar
        reflects the current topic name instead of the stale original.
        Non-fatal — logs warning on failure.
        """
        try:
            client = self._get_client()
            client.table("chat_sessions").update({"title": title}).eq(
                "session_id", session_id
            ).execute()
        except Exception as e:
            LOGGER.warning(f"Failed to update session title: {e}")

    async def get_session(self, session_id: str) -> Optional[ChatSessionModel]:
        """Get session by session_id."""
        try:
            client = self._get_client()
            response = (
                client.table("chat_sessions").select("*").eq("session_id", session_id).execute()
            )

            if response.data:
                return ChatSessionModel(**response.data[0])
            return None

        except Exception as e:
            LOGGER.error(f"Error getting session: {e}")
            return None

    async def get_session_by_chat_id(
        self, source: str, chat_id: str, topic_id: str | None = None
    ) -> Optional[ChatSessionModel]:
        """
        Get session by chat_id, handling both hashed and legacy session ID formats.

        This method handles the transition from legacy unhashed session IDs
        (e.g., 'telegram_1234567890') to new hashed session IDs. It tries:
        1. Hashed format with original chat_id (sessions created with -100 prefix)
        2. Hashed format with normalized chat_id (without -100 prefix)
        3. Legacy unhashed format (old sessions)
        4. Lookup by telegram_chat_id column (fallback)

        Args:
            source: Message source (e.g., 'telegram')
            chat_id: The chat ID to look up
            topic_id: Optional topic/thread ID

        Returns:
            ChatSessionModel if found, None otherwise
        """
        from orchestrator.utils.session_id import generate_session_id

        original_chat_id = str(chat_id)

        # Try 1: Hashed session_id with ORIGINAL chat_id (with -100 prefix)
        # This is how sessions are currently created in handler.py
        hashed_session_id = generate_session_id(
            source=source,
            chat_id=original_chat_id,
            topic_id=topic_id,
        )
        session = await self.get_session(hashed_session_id)
        if session:
            LOGGER.debug(f"Found session with original chat_id hash: {hashed_session_id}")
            return session

        # Try 2: Hashed session_id with NORMALIZED chat_id (without -100 prefix)
        # For backward compatibility with sessions created before this fix
        normalized_chat_id = original_chat_id
        if normalized_chat_id.startswith("-100"):
            normalized_chat_id = normalized_chat_id[4:]
            hashed_normalized = generate_session_id(
                source=source,
                chat_id=normalized_chat_id,
                topic_id=topic_id,
            )
            session = await self.get_session(hashed_normalized)
            if session:
                LOGGER.debug(f"Found session with normalized chat_id hash: {hashed_normalized}")
                return session

        # Try 3: Legacy unhashed format (old sessions before hashing was introduced)
        if topic_id:
            legacy_session_id = f"{source}_{normalized_chat_id}_{topic_id}"
        else:
            legacy_session_id = f"{source}_{normalized_chat_id}"
        session = await self.get_session(legacy_session_id)
        if session:
            LOGGER.debug(f"Found session with legacy format: {legacy_session_id}")
            return session

        # Try 4: Lookup by telegram_chat_id column (fallback for any format)
        try:
            client = self._get_client()
            query = (
                client.table("chat_sessions").select("*").eq("telegram_chat_id", original_chat_id)
            )
            if topic_id:
                query = query.eq("telegram_topic_id", str(topic_id))
            response = query.order("created_at", desc=True).limit(1).execute()

            if response.data:
                LOGGER.debug("Found session by telegram_chat_id column lookup")
                return ChatSessionModel(**response.data[0])
        except Exception as e:
            LOGGER.error(f"Error looking up session by telegram_chat_id: {e}")

        return None

    # Message Management
    async def save_messages(
        self,
        session_uuid: UUID,
        messages: List[ConversationMessage],
        from_chat_id: Optional[str] = None,
        group_id: Optional[str] = None,
    ) -> List[ChatMessageModel]:
        """Save conversation messages to database.

        Args:
            session_uuid: Session UUID to save messages for
            messages: List of conversation messages to save
            from_chat_id: Telegram chat ID where message originated
            group_id: Telegram group ID if from group, null for 1-on-1

        Returns:
            List of saved message models

        Note:
            This method appends new messages to the session, continuing from the
            highest existing message_index to avoid duplicate key violations.
        """
        try:
            client = self._get_client()

            if not messages:
                return []

            # Get the current max message_index for this session
            max_index_response = (
                client.table("chat_messages")
                .select("message_index")
                .eq("session_id", session_uuid)  # Fixed: Use UUID directly, not str()
                .order("message_index", desc=True)
                .limit(1)
                .execute()
            )

            # Start indexing from max + 1, or 0 if no messages exist
            start_index = 0
            if max_index_response.data and len(max_index_response.data) > 0:
                start_index = max_index_response.data[0]["message_index"] + 1

            # Build message rows with correct indices
            message_rows = []
            for idx, message in enumerate(messages):
                message_data = {
                    "session_id": str(
                        session_uuid
                    ),  # Convert UUID to string for JSON serialization
                    "role": message.role,
                    "content": message.content,
                    "function_call": (
                        message.function_call.model_dump() if message.function_call else None
                    ),
                    "tool_result": (
                        message.tool_result.model_dump() if message.tool_result else None
                    ),
                    "message_index": start_index + idx,
                }
                # Add metadata if present (contains token counts for model messages)
                if message.metadata:
                    message_data["metadata"] = message.metadata
                # Add chat_id fields if provided
                if from_chat_id:
                    message_data["from_chat_id"] = from_chat_id
                if group_id:
                    message_data["group_id"] = group_id
                # Thread disentanglement fields
                if message.telegram_message_id:
                    message_data["telegram_message_id"] = message.telegram_message_id
                if message.reply_to_telegram_message_id:
                    message_data["reply_to_telegram_message_id"] = (
                        message.reply_to_telegram_message_id
                    )
                if message.sender_id:
                    message_data["sender_telegram_id"] = message.sender_id
                if message.thread_id:
                    message_data["thread_id"] = message.thread_id

                message_rows.append(message_data)

            # Insert all messages
            response = client.table("chat_messages").insert(message_rows).execute()

            LOGGER.info(
                f"Saved {len(message_rows)} messages to session {session_uuid} "
                f"(indices {start_index} to {start_index + len(message_rows) - 1})"
            )

            return [ChatMessageModel(**row) for row in response.data]

        except Exception as e:
            LOGGER.error(f"Error saving messages: {e}")
            raise

    async def get_messages(
        self,
        session_uuid: UUID,
        max_age_hours: int = 12,
        max_messages: int = 50,
    ) -> List[ConversationMessage]:
        """Get messages for a session by UUID.

        This is the simple message loader used by init_services to restore
        conversation history. For more advanced filtering, use get_conversation_history.

        Args:
            session_uuid: Session UUID
            max_age_hours: Only retrieve messages from last N hours (default: 12)
            max_messages: Maximum number of messages to retrieve (default: 50)

        Returns:
            List of conversation messages in chronological order (oldest first)
        """
        try:
            client = self._get_client()

            # Calculate time threshold
            time_threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

            # Get messages ordered by message_index (chronological)
            response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", str(session_uuid))
                .gte("created_at", time_threshold.isoformat())
                .order("message_index", desc=False)
                .limit(max_messages)
                .execute()
            )

            if not response.data:
                LOGGER.debug(f"No messages found for session UUID {session_uuid}")
                return []

            # Convert to ConversationMessage objects
            messages = []
            for row in response.data:
                message = self._row_to_conversation_message(row)
                messages.append(message)

            LOGGER.info(f"Loaded {len(messages)} messages for session UUID {session_uuid}")
            return messages

        except Exception as e:
            LOGGER.error(f"Error getting messages: {e}")
            return []

    @staticmethod
    def _row_to_conversation_message(row: Dict[str, Any]) -> ConversationMessage:
        """Convert a database row to a ConversationMessage with all fields."""
        metadata = row.get("metadata") or {}
        return ConversationMessage(
            role=row["role"],
            content=row["content"],
            function_call=(
                FunctionCall(**row["function_call"]) if row.get("function_call") else None
            ),
            tool_result=(ToolCallResult(**row["tool_result"]) if row.get("tool_result") else None),
            timestamp=row.get("created_at"),
            metadata=metadata,
            sender_id=row.get("sender_telegram_id") or row.get("from_chat_id"),
            telegram_message_id=(
                int(row["telegram_message_id"]) if row.get("telegram_message_id") else None
            ),
            reply_to_telegram_message_id=(
                int(row["reply_to_telegram_message_id"])
                if row.get("reply_to_telegram_message_id")
                else None
            ),
            thread_id=row.get("thread_id"),
        )

    async def get_messages_filtered(
        self,
        session_uuid: UUID,
        max_age_hours: int = 12,
        max_messages: int = 50,
        exclude_types: Optional[List[str]] = None,
    ) -> List[ConversationMessage]:
        """Get messages for a session, filtering by message_type metadata.

        Fetches extra messages to compensate for filtered-out ones, then
        applies the filter and returns up to max_messages results.
        Legacy messages without message_type default to "interactive" (never excluded).

        Args:
            session_uuid: Session UUID
            max_age_hours: Only retrieve messages from last N hours (default: 12)
            max_messages: Maximum number of messages to retrieve (default: 50)
            exclude_types: Message types to exclude (e.g., ["scheduled", "scheduled_user"])

        Returns:
            List of conversation messages in chronological order (oldest first)
        """
        if not exclude_types:
            # No filtering needed, delegate to standard loader
            return await self.get_messages(session_uuid, max_age_hours, max_messages)

        try:
            client = self._get_client()
            time_threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

            # Fetch extra to compensate for messages we'll filter out
            fetch_limit = max_messages * 2

            response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", str(session_uuid))
                .gte("created_at", time_threshold.isoformat())
                .order("message_index", desc=False)
                .limit(fetch_limit)
                .execute()
            )

            if not response.data:
                return []

            exclude_set = set(exclude_types)
            messages = []
            for row in response.data:
                # Get message_type from metadata; default to "interactive" for legacy messages
                metadata = row.get("metadata") or {}
                msg_type = metadata.get("message_type", "interactive")
                if msg_type in exclude_set:
                    continue

                message = self._row_to_conversation_message(row)
                messages.append(message)

                if len(messages) >= max_messages:
                    break

            LOGGER.info(
                f"Loaded {len(messages)} messages for session UUID {session_uuid} "
                f"(excluded types: {exclude_types})"
            )
            return messages

        except Exception as e:
            LOGGER.error(f"Error getting filtered messages: {e}")
            return []

    async def get_messages_around_timestamp(
        self,
        session_uuid: UUID,
        target_timestamp: str,
        window_before: int = 5,
        window_after: int = 3,
        exclude_types: Optional[List[str]] = None,
    ) -> List[ConversationMessage]:
        """Get messages surrounding a target timestamp for reply-to context.

        Args:
            session_uuid: Session UUID
            target_timestamp: ISO timestamp to center the window on
            window_before: Number of messages to fetch before the target
            window_after: Number of messages to fetch after the target
            exclude_types: Message types to exclude

        Returns:
            List of messages in chronological order surrounding the timestamp
        """
        try:
            client = self._get_client()
            exclude_set = set(exclude_types or [])

            # Fetch messages before the target timestamp
            before_response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", str(session_uuid))
                .lte("created_at", target_timestamp)
                .order("message_index", desc=True)
                .limit(window_before + 5)  # Extra buffer for filtering
                .execute()
            )

            # Fetch messages after the target timestamp
            after_response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", str(session_uuid))
                .gt("created_at", target_timestamp)
                .order("message_index", desc=False)
                .limit(window_after + 5)
                .execute()
            )

            def row_to_message(row: Dict) -> Optional[ConversationMessage]:
                metadata = row.get("metadata") or {}
                msg_type = metadata.get("message_type", "interactive")
                if msg_type in exclude_set:
                    return None
                return ConversationMessage(
                    role=row["role"],
                    content=row["content"],
                    function_call=(
                        FunctionCall(**row["function_call"]) if row.get("function_call") else None
                    ),
                    tool_result=(
                        ToolCallResult(**row["tool_result"]) if row.get("tool_result") else None
                    ),
                    timestamp=row.get("created_at"),
                    metadata=metadata,
                )

            # Build before messages (reverse to get chronological order)
            before_msgs = []
            for row in reversed(before_response.data or []):
                msg = row_to_message(row)
                if msg:
                    before_msgs.append(msg)
            before_msgs = before_msgs[-window_before:]  # Keep only the window

            # Build after messages
            after_msgs: list[ConversationMessage] = []
            for row in after_response.data or []:
                msg = row_to_message(row)
                if msg and len(after_msgs) < window_after:
                    after_msgs.append(msg)

            combined = before_msgs + after_msgs
            LOGGER.info(
                f"Loaded {len(combined)} messages around timestamp {target_timestamp} "
                f"({len(before_msgs)} before, {len(after_msgs)} after)"
            )
            return combined

        except Exception as e:
            LOGGER.error(f"Error getting messages around timestamp: {e}")
            return []

    async def get_conversation_history(
        self,
        session_id: str,
        user_id: Optional[str] = None,
        max_age_hours: int = 12,
        max_messages: int = 30,
        max_words: int = 1000,
        user_organization_ids: Optional[List[int]] = None,
    ) -> List[ConversationMessage]:
        """Retrieve conversation history for a session with limits and org validation.

        Args:
            session_id: Session identifier
            user_id: Optional user identifier (for security logging)
            max_age_hours: Only retrieve messages from last N hours (default: 12)
            max_messages: Maximum number of messages to retrieve (default: 30)
            max_words: Maximum total word count across all messages (default: 1000)
            user_organization_ids: User's organization IDs for access validation

        Returns:
            List of conversation messages, newest first, truncated to meet all limits.
            Returns empty list if user doesn't have access to the session's organization.
        """
        try:
            client = self._get_client()

            # Get session with organization_id for validation
            session_response = (
                client.table("chat_sessions")
                .select("id, organization_id")
                .eq("session_id", session_id)
                .execute()
            )

            if not session_response.data:
                return []

            session_uuid = session_response.data[0]["id"]
            session_org_id = session_response.data[0].get("organization_id")

            # Security: Validate organization access
            if session_org_id and user_organization_ids:
                if session_org_id not in user_organization_ids:
                    # Log security event for cross-org access attempt
                    from orchestrator.utils.security_logger import (
                        SecurityEventType,
                        log_security_event,
                    )

                    log_security_event(
                        event_type=SecurityEventType.CROSS_ORG_ACCESS_ATTEMPT,
                        session_id=session_id,
                        user_org_ids=user_organization_ids,
                        target_org_id=session_org_id,
                        user_id=user_id,
                        details={"action": "get_conversation_history", "access_denied": True},
                    )
                    return []  # Deny access

            # Calculate time threshold (12 hours ago)
            time_threshold = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

            # Get messages filtered by time and ordered newest first
            response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", session_uuid)
                .gte("created_at", time_threshold.isoformat())
                .order("message_index", desc=True)  # Newest first
                .execute()
            )

            if not response.data:
                LOGGER.info(f"No recent messages found for session {session_id}")
                return []

            # Convert to ConversationMessage objects (still newest first)
            all_messages = []
            for row in response.data:
                message = ConversationMessage(
                    role=row["role"],
                    content=row["content"],
                    function_call=(
                        FunctionCall(**row["function_call"]) if row["function_call"] else None
                    ),
                    tool_result=(
                        ToolCallResult(**row["tool_result"]) if row["tool_result"] else None
                    ),
                    timestamp=row.get("created_at"),
                )
                all_messages.append(message)

            # Apply message count limit (keep newest N messages)
            if len(all_messages) > max_messages:
                all_messages = all_messages[:max_messages]
                LOGGER.info(f"Truncated to {max_messages} most recent messages")

            # Apply word count limit (keep newest messages until word limit reached)
            total_words = 0
            truncated_messages: list[ConversationMessage] = []

            for message in all_messages:
                # Count words in message content
                if message.content:
                    message_words = len(message.content.split())

                    # Check if adding this message would exceed word limit
                    if total_words + message_words > max_words:
                        # If we haven't added any messages yet, include at least this one (truncated)
                        if not truncated_messages:
                            words = message.content.split()[:max_words]
                            truncated_content = " ".join(words)
                            truncated_message = ConversationMessage(
                                role=message.role,
                                content=truncated_content,
                                function_call=message.function_call,
                                tool_result=message.tool_result,
                            )
                            truncated_messages.append(truncated_message)
                            total_words = len(words)
                            LOGGER.info(
                                f"Truncated message content from {message_words} to {len(words)} words "
                                f"to stay within {max_words} word limit"
                            )
                        break

                    truncated_messages.append(message)
                    total_words += message_words
                else:
                    # Messages without content (e.g., function calls) don't count toward word limit
                    truncated_messages.append(message)

            # Reverse to restore chronological order (oldest first)
            final_messages = list(reversed(truncated_messages))

            LOGGER.info(
                f"Retrieved {len(final_messages)} messages for session {session_id} "
                f"(from last {max_age_hours}h, total_words={total_words}/{max_words})"
            )
            return final_messages

        except Exception as e:
            LOGGER.error(f"Error retrieving conversation history: {e}")
            return []

    # Tool Call Tracking
    async def save_tool_call(self, session_uuid: UUID, tool_call: ToolCallModel) -> ToolCallModel:
        """Save tool call details to database."""
        try:
            client = self._get_client()

            tool_data = {
                "session_id": str(session_uuid),
                "message_id": str(tool_call.message_id) if tool_call.message_id else None,
                "tool_name": tool_call.tool_name,
                "arguments": tool_call.arguments,
                "result": tool_call.result,
                "success": tool_call.success,
                "error_message": tool_call.error_message,
                "execution_time_ms": tool_call.execution_time_ms,
                "status_code": tool_call.status_code,
                "raw_response": tool_call.raw_response,
            }

            response = client.table("tool_calls").insert(tool_data).execute()
            return ToolCallModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error saving tool call: {e}")
            raise

    # Token Usage Tracking
    async def save_token_usage(self, session_uuid: UUID, usage: TokenUsageModel) -> TokenUsageModel:
        """Save token usage metrics."""
        try:
            client = self._get_client()

            usage_data = {
                "session_id": str(session_uuid),
                "message_id": str(usage.message_id) if usage.message_id else None,
                "model_name": usage.model_name,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cost_usd": usage.cost_usd,
            }

            response = client.table("token_usage").insert(usage_data).execute()
            return TokenUsageModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error saving token usage: {e}")
            raise

    # RAG Operations
    async def save_document_chunks(
        self, chunks: List[DocumentChunkModel]
    ) -> List[DocumentChunkModel]:
        """Save document chunks with embeddings."""
        try:
            client = self._get_client()

            chunk_rows = []
            for chunk in chunks:
                chunk_data = {
                    "document_id": chunk.document_id,
                    "chunk_index": chunk.chunk_index,
                    "content": chunk.content,
                    "embedding": chunk.embedding,
                    "metadata": chunk.metadata,
                    "source_url": chunk.source_url,
                    "source_type": chunk.source_type,
                }
                chunk_rows.append(chunk_data)

            if chunk_rows:
                response = client.table("document_chunks").insert(chunk_rows).execute()
                return [DocumentChunkModel(**row) for row in response.data]

            return []

        except Exception as e:
            LOGGER.error(f"Error saving document chunks: {e}")
            raise

    async def similarity_search(
        self, query_embedding: List[float], limit: int = 5, threshold: float = 0.7
    ) -> List[RAGSearchResult]:
        """Perform similarity search using pgvector."""
        try:
            client = self._get_client()

            # Use Supabase's vector similarity search
            response = client.rpc(
                "match_documents",
                {
                    "query_embedding": query_embedding,
                    "match_threshold": threshold,
                    "match_count": limit,
                },
            ).execute()

            results = []
            for row in response.data:
                result = RAGSearchResult(
                    chunk_id=row["id"],
                    document_id=row["document_id"],
                    content=row["content"],
                    similarity_score=row["similarity"],
                    metadata=row["metadata"],
                    source_url=row["source_url"],
                    source_type=row["source_type"],
                )
                results.append(result)

            return results

        except Exception as e:
            LOGGER.error(f"Error performing similarity search: {e}")
            return []

    # Analytics and Reporting
    async def get_conversation_summary(self, session_id: str) -> Optional[ConversationSummary]:
        """Get conversation metrics and summary."""
        try:
            client = self._get_client()

            # Get session UUID
            session_response = (
                client.table("chat_sessions")
                .select("id, created_at")
                .eq("session_id", session_id)
                .execute()
            )

            if not session_response.data:
                return None

            session_uuid = session_response.data[0]["id"]

            # Get metrics using RPC function
            response = client.rpc("get_session_summary", {"session_uuid": session_uuid}).execute()

            if response.data:
                summary_data = response.data[0]
                return ConversationSummary(
                    session_id=session_id,
                    message_count=summary_data["message_count"],
                    tool_calls_count=summary_data["tool_calls_count"],
                    total_tokens=summary_data["total_tokens"],
                    total_cost_usd=summary_data["total_cost_usd"],
                    duration_minutes=summary_data["duration_minutes"],
                    user_feedback_count=summary_data["user_feedback_count"],
                    last_activity=summary_data["last_activity"],
                )

            return None

        except Exception as e:
            LOGGER.error(f"Error getting conversation summary: {e}")
            return None

    # =========================================================================
    # ESCALATION PERSISTENCE METHODS
    # =========================================================================

    async def save_escalation_mapping(
        self,
        escalation_message_id: int,
        customer_chat_id: str,
        session_id: str,
        customer_topic_id: Optional[str] = None,
        org_hashtag: Optional[str] = None,
        customer_email: Optional[str] = None,
        customer_username: Optional[str] = None,
        reason: Optional[str] = None,
        action_type: Optional[str] = None,
        mapping_id: Optional[str] = None,
        organization_id: Optional[int] = None,
        escalation_topic_id: Optional[int] = None,
        question_text: Optional[str] = None,
        thread_id: Optional[str] = None,
        jira_ticket_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Save escalation mapping for routing support replies back to customer.

        Args:
            escalation_message_id: Telegram message ID of escalation in support group
            customer_chat_id: Customer's Telegram chat ID
            session_id: Chat session identifier
            customer_topic_id: Customer's topic/thread ID (optional)
            org_hashtag: Organization hashtag (optional)
            customer_email: Customer email (optional)
            customer_username: Customer username (optional)
            reason: Categorized escalation reason (optional). Valid values:
                - user_requested: User explicitly asked for human help
                - could_not_answer: Bot couldn't answer the question
                - out_of_scope: Request outside bot capabilities
                - staff_action_required: Needs action bot can't perform
                - inappropriate_language: Offensive content from user
                - negative_feedback: User expressed dissatisfaction
                - verification_failed: LLM judge rejected response twice
                - safety_escalation: Bot claimed escalation without tool call (non-blocking)
                - system_error: Unhandled exception in bot processing (non-blocking)
                - other: Doesn't fit other categories
            action_type: Specific action needed when reason=staff_action_required:
                - meter_unassignment: Customer wants meter removed
                - wallet_credit: Manual wallet credit needed
                - hps_power_limit: HPS power limit review
                - meter_replacement: Physical meter swap
                - commissioning_retry: Manual commissioning retry
                - other_action: Other staff action

        Returns:
            Mapping ID (UUID string) if saved successfully, None otherwise.
            Callers that previously checked truthiness (if result:) are unaffected.
        """
        try:
            client = self._get_client()

            # Use provided ID or generate one client-side for callback buttons
            if not mapping_id:
                mapping_id = str(uuid.uuid4())

            # Insert escalation mapping
            mapping_data = {
                "id": mapping_id,
                "escalation_message_id": escalation_message_id,
                "customer_chat_id": customer_chat_id,
                "session_id": session_id,
                "customer_topic_id": customer_topic_id,
                "org_hashtag": org_hashtag,
                "customer_email": customer_email,
                "customer_username": customer_username,
                "reason": reason,
                "action_type": action_type,
                "is_active": True,
                "organization_id": organization_id,
                "escalation_topic_id": escalation_topic_id,
                "question_text": question_text[:2000] if question_text else None,
                "thread_id": thread_id,
                "jira_ticket_key": jira_ticket_key,
            }

            client.table("escalation_mappings").insert(mapping_data).execute()

            # Non-blocking escalations (e.g., safety_escalation) create a mapping
            # for staff visibility but do NOT block the customer's chat session.
            # The bot's original response already went through — blocking would
            # silently swallow subsequent customer messages.
            NON_BLOCKING_REASONS = {"safety_escalation", "system_error"}
            if reason not in NON_BLOCKING_REASONS:
                await self.update_session_escalation_status(
                    session_id=session_id,
                    is_escalated=True,
                    escalation_message_id=escalation_message_id,
                )

            LOGGER.info(
                f"Saved escalation mapping: id={mapping_id}, "
                f"msg_id={escalation_message_id} → "
                f"chat_id={customer_chat_id}, session={session_id}"
            )
            return mapping_id

        except Exception as e:
            LOGGER.error(f"Error saving escalation mapping: {e}")
            return None

    async def get_escalation_mapping_by_jira_key(
        self, jira_ticket_key: str
    ) -> Optional[Dict[str, Any]]:
        """Get the active escalation mapping for a Jira ticket key.

        Returns the most recent active mapping, or None if not found.
        """
        try:
            client = self._get_client()
            response = (
                client.table("escalation_mappings")
                .select("*")
                .eq("jira_ticket_key", jira_ticket_key)
                .eq("is_active", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            return dict(response.data[0]) if response.data else None
        except Exception as e:
            LOGGER.error(f"Error fetching escalation mapping for Jira key {jira_ticket_key}: {e}")
            return None

    async def get_escalation_mapping(self, escalation_message_id: int) -> Optional[Dict[str, Any]]:
        """
        Get escalation mapping by message ID.

        Args:
            escalation_message_id: Telegram message ID of escalation

        Returns:
            Mapping dict or None if not found
        """
        try:
            client = self._get_client()

            response = (
                client.table("escalation_mappings")
                .select("*")
                .eq("escalation_message_id", escalation_message_id)
                .execute()
            )

            if response.data:
                result: Dict[str, Any] = response.data[0]
                return result
            return None

        except Exception as e:
            LOGGER.error(f"Error getting escalation mapping: {e}")
            return None

    async def update_session_escalation_status(
        self,
        session_id: str,
        is_escalated: bool,
        escalation_message_id: Optional[int] = None,
    ) -> bool:
        """
        Update escalation status for a session.

        Args:
            session_id: Chat session identifier
            is_escalated: Whether session is escalated
            escalation_message_id: Telegram message ID of escalation (optional)

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            client = self._get_client()

            update_data: Dict[str, Any] = {"is_escalated": is_escalated}

            if is_escalated:
                update_data["escalated_at"] = datetime.now(timezone.utc).isoformat()
                if escalation_message_id:
                    update_data["escalation_message_id"] = escalation_message_id
            else:
                # Clear escalation fields when closing
                update_data["escalation_message_id"] = None

            client.table("chat_sessions").update(update_data).eq("session_id", session_id).execute()

            LOGGER.info(f"Updated session {session_id} escalation status: {is_escalated}")
            return True

        except Exception as e:
            LOGGER.error(f"Error updating session escalation status: {e}")
            return False

    async def get_session_escalation_info(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get escalation info for a session.

        Args:
            session_id: Chat session identifier

        Returns:
            Dict with escalation info or None
        """
        try:
            client = self._get_client()

            response = (
                client.table("chat_sessions")
                .select("is_escalated, escalation_message_id, escalated_at")
                .eq("session_id", session_id)
                .execute()
            )

            if response.data:
                result: Dict[str, Any] = response.data[0]
                return result
            return None

        except Exception as e:
            LOGGER.error(f"Error getting session escalation info: {e}")
            return None

    async def close_escalation(self, session_id: str) -> bool:
        """
        Close all active escalations for a session.

        Deactivates ALL escalation mappings and clears the session's
        is_escalated flag so the user resumes chatting with the bot.

        Args:
            session_id: Chat session identifier

        Returns:
            True if closed successfully, False otherwise
        """
        try:
            client = self._get_client()

            # Update session
            await self.update_session_escalation_status(session_id=session_id, is_escalated=False)

            # Deactivate ALL mappings for this session
            client.table("escalation_mappings").update(
                {"is_active": False, "resolved_at": datetime.now(timezone.utc).isoformat()}
            ).eq("session_id", session_id).execute()

            LOGGER.info(f"Closed escalation for session {session_id}")
            return True

        except Exception as e:
            LOGGER.error(f"Error closing escalation: {e}")
            return False

    async def count_active_escalations(self, session_id: str) -> int:
        """Count active escalation mappings for a session."""
        try:
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select("id", count="exact")
                .eq("session_id", session_id)
                .eq("is_active", True)
                .execute()
            )
            return result.count if result.count is not None else 0
        except Exception as e:
            LOGGER.error(f"Error counting active escalations: {e}")
            return 0

    async def count_active_blocking_escalations(self, session_id: str) -> int:
        """Count active escalation mappings that block the chat session.

        Non-blocking reasons (e.g., safety_escalation) don't set is_escalated
        on the session, so they shouldn't prevent session release.
        """
        try:
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select("id", count="exact")
                .eq("session_id", session_id)
                .eq("is_active", True)
                .neq("reason", "safety_escalation")
                .execute()
            )
            return result.count if result.count is not None else 0
        except Exception as e:
            LOGGER.error(f"Error counting active blocking escalations: {e}")
            return 0

    async def claim_escalation_for_tracking(self, mapping_id: str) -> Optional[Dict[str, Any]]:
        """Atomically claim an escalation for JIRA ticket tracking.

        Sets is_active=false and returns the row only if it was active.
        Returns None if already claimed/closed (prevents double-click race).
        """
        try:
            client = self._get_client()
            # Atomic claim: update only if still active
            result = (
                client.table("escalation_mappings")
                .update({"is_active": False})
                .eq("id", mapping_id)
                .eq("is_active", True)
                .execute()
            )
            updated: list[Dict[str, Any]] = result.data or []
            if not updated:
                LOGGER.info(f"Escalation {mapping_id} already claimed or closed")
                return None

            # Fetch the full row after claiming
            fetch = client.table("escalation_mappings").select("*").eq("id", mapping_id).execute()
            data = fetch.data or []
            if data:
                LOGGER.info(f"Claimed escalation {mapping_id} for ticket tracking")
                return dict(data[0])
            return None
        except Exception as e:
            LOGGER.exception(f"Error claiming escalation {mapping_id}: {e}")
            return None

    async def reactivate_escalation(self, mapping_id: str) -> None:
        """Re-activate an escalation after failed ticket creation."""
        try:
            client = self._get_client()
            client.table("escalation_mappings").update({"is_active": True}).eq(
                "id", mapping_id
            ).execute()
            LOGGER.info(f"Reactivated escalation {mapping_id}")
        except Exception as e:
            LOGGER.warning(f"Failed to reactivate escalation {mapping_id}: {e}")

    async def reopen_escalation(self, session_id: str, escalation_message_id: int) -> bool:
        """
        Reopen a closed escalation for a session.

        Re-activates the specific escalation mapping and sets the session's
        is_escalated flag so user messages route to the escalation group again.

        Args:
            session_id: Chat session identifier
            escalation_message_id: The escalation message to reactivate

        Returns:
            True if reopened successfully, False otherwise
        """
        try:
            client = self._get_client()

            # Re-activate the specific mapping
            client.table("escalation_mappings").update({"is_active": True, "resolved_at": None}).eq(
                "session_id", session_id
            ).eq("escalation_message_id", escalation_message_id).execute()

            # Set session back to escalated
            await self.update_session_escalation_status(session_id=session_id, is_escalated=True)

            LOGGER.info(
                f"Reopened escalation for session {session_id}, message_id={escalation_message_id}"
            )
            return True

        except Exception as e:
            LOGGER.error(f"Error reopening escalation: {e}")
            return False

    async def get_escalation_by_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        """
        Get active escalation mapping by session ID.

        Args:
            session_id: Chat session identifier

        Returns:
            Escalation mapping dict or None
        """
        try:
            client = self._get_client()

            response = (
                client.table("escalation_mappings")
                .select("*")
                .eq("session_id", session_id)
                .eq("is_active", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            if response.data:
                result: Dict[str, Any] = response.data[0]
                return result
            return None

        except Exception as e:
            LOGGER.error(f"Error getting escalation by session: {e}")
            return None

    # =========================================================================
    # ESCALATION SWEEP METHODS
    # =========================================================================

    async def get_stale_unfiled_escalations(
        self,
        min_age_hours: int = 1,
        max_age_hours: int = 24,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Return active escalations with no Jira ticket, aged between min and max hours.

        Excludes safety_escalation reason (non-blocking; customer unaware).
        Ordered oldest-first (FIFO) so longest-waiting are filed first.
        """
        try:
            now = datetime.now(timezone.utc)
            cutoff_recent = (now - timedelta(hours=min_age_hours)).isoformat()
            cutoff_old = (now - timedelta(hours=max_age_hours)).isoformat()
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select(
                    "id, session_id, org_hashtag, customer_email, customer_username, "
                    "customer_chat_id, customer_topic_id, organization_id, "
                    "escalation_message_id, escalation_topic_id, reason, jira_ticket_key, "
                    "question_text, created_at"
                )
                .eq("is_active", True)
                .is_("jira_ticket_key", "null")
                .neq("reason", "safety_escalation")
                .gt("created_at", cutoff_old)
                .lt("created_at", cutoff_recent)
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            LOGGER.error(f"Error fetching stale unfiled escalations: {e}")
            return []

    async def get_orphaned_claimed_escalations(
        self, max_age_hours: int = 48, limit: int = 50
    ) -> List[Dict[str, Any]]:
        """Return escalations claimed (is_active=False) but never completed.

        These are rows where:
        - is_active=False (claim was set)
        - jira_ticket_key IS NULL (ticket was never stored)
        - resolved_at IS NULL (not intentionally closed by staff)
        - created_at within the last max_age_hours (avoids touching ancient rows)

        Caused by process kill (SIGTERM) between claim and track_as_ticket completion.
        Reactivating them makes the Track button work again and lets the sweep retry.
        """
        try:
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select("id, session_id, created_at")
                .eq("is_active", False)
                .is_("jira_ticket_key", "null")
                .is_("resolved_at", "null")
                .gte("created_at", cutoff)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            LOGGER.error(f"Error fetching orphaned claimed escalations: {e}")
            return []

    async def get_old_unfiled_escalations(
        self, max_age_hours: int = 24, limit: int = 20
    ) -> List[Dict[str, Any]]:
        """Return active escalations with no Jira ticket older than max_age_hours.

        Used by the sweep to alert staff about escalations that aged out of the
        auto-sweep window without being filed.
        """
        effective_limit = min(limit, 20)
        try:
            now = datetime.now(timezone.utc)
            cutoff = (now - timedelta(hours=max_age_hours)).isoformat()
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select(
                    "id, org_hashtag, customer_username, customer_email, "
                    "escalation_message_id, created_at"
                )
                .eq("is_active", True)
                .is_("jira_ticket_key", "null")
                .neq("reason", "safety_escalation")
                .lt("created_at", cutoff)
                .order("created_at", desc=False)
                .limit(effective_limit)
                .execute()
            )
            return result.data or []
        except Exception as e:
            LOGGER.error(f"Error fetching old unfiled escalations: {e}")
            return []

    async def get_active_tracked_escalations(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Return active escalations that have a Jira ticket key (tracked, pending resolution).

        Used by the sweep to reconcile Jira-closed tickets and notify customers of open ones.
        """
        try:
            client = self._get_client()
            result = (
                client.table("escalation_mappings")
                .select(
                    "id, session_id, customer_chat_id, customer_topic_id, "
                    "jira_ticket_key, org_hashtag, customer_username, created_at"
                )
                .eq("is_active", True)
                .filter("jira_ticket_key", "not.is", "null")
                .order("created_at", desc=False)
                .limit(limit)
                .execute()
            )
            rows = result.data or []
            if len(rows) == limit:
                LOGGER.warning(
                    "get_active_tracked_escalations hit cap of %d — some tracked escalations skipped",
                    limit,
                )
            return rows
        except Exception as e:
            LOGGER.error(f"Error fetching active tracked escalations: {e}")
            return []

    # =========================================================================
    # ORG METADATA METHODS
    # =========================================================================

    async def get_org_escalation_topic(self, organization_id: int) -> Optional[int]:
        """Return the Telegram forum topic_id for this org's escalations, or None."""
        try:
            client = self._get_client()
            response = (
                client.table("org_metadata")
                .select("telegram_config")
                .eq("organization_id", organization_id)
                .maybe_single()
                .execute()
            )
            if response.data:
                val = response.data["telegram_config"].get("escalation_group_topic")
                return int(val) if val is not None else None
            return None
        except Exception as e:
            LOGGER.error(f"Error getting org escalation topic for org={organization_id}: {e}")
            return None

    async def save_org_escalation_topic(self, organization_id: int, topic_id: int) -> None:
        """Merge escalation_group_topic into org_metadata.telegram_config.
        Uses set_org_telegram_topic RPC which preserves other telegram_config keys.
        ON CONFLICT DO UPDATE (last writer wins) handles concurrent first-escalation race.
        """
        try:
            client = self._get_client()
            response = client.rpc(
                "set_org_telegram_topic",
                {"p_org_id": organization_id, "p_topic_id": topic_id},
            ).execute()
            if hasattr(response, "error") and response.error:
                LOGGER.error(
                    f"RPC set_org_telegram_topic failed for org={organization_id}: {response.error}"
                )
                raise RuntimeError(f"RPC set_org_telegram_topic failed: {response.error}")
        except RuntimeError:
            raise
        except Exception as e:
            LOGGER.error(f"Error saving org escalation topic for org={organization_id}: {e}")
            raise

    async def clear_org_escalation_topic(self, organization_id: int) -> None:
        """Remove escalation_group_topic from org_metadata.telegram_config.
        Called when a stale topic_id is detected (topic deleted externally in Telegram).
        """
        try:
            client = self._get_client()
            response = client.rpc(
                "clear_org_telegram_topic",
                {"p_org_id": organization_id},
            ).execute()
            if hasattr(response, "error") and response.error:
                LOGGER.error(
                    f"RPC clear_org_telegram_topic failed for org={organization_id}: {response.error}"
                )
        except Exception as e:
            LOGGER.error(f"Error clearing org escalation topic for org={organization_id}: {e}")

    async def save_thread(
        self,
        thread_id: str,
        session_id: str,
        organization_id: Optional[int] = None,
        issue_type: Optional[str] = None,
    ) -> None:
        """Persist a new conversation thread to chat_threads."""
        try:
            client = self._get_client()
            client.table("chat_threads").insert(
                {
                    "thread_id": thread_id,
                    "session_id": session_id,
                    "organization_id": organization_id,
                    "issue_type": issue_type,
                    "status": "open",
                }
            ).execute()
        except Exception as e:
            LOGGER.error(f"Error saving thread {thread_id}: {e}")

    async def get_open_issues_for_org(
        self,
        organization_id: int,
        issue_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Return open escalation_mappings for an org, joined with chat_threads for issue_type.

        Uses PostgREST embedding (FK relationship) to join chat_threads inline.
        """
        try:
            client = self._get_client()
            query = (
                client.table("escalation_mappings")
                .select(
                    "id, question_text, reason, action_type, created_at, thread_id, "
                    "chat_threads(issue_type)"
                )
                .eq("organization_id", organization_id)
                .eq("is_active", True)
                .order("created_at", desc=True)
                .limit(50)
            )
            if issue_type:
                # PostgREST: filter on embedded table column
                query = query.eq("chat_threads.issue_type", issue_type)
            response = query.execute()
            rows = response.data or []

            results = []
            for row in rows:
                thread_data = row.get("chat_threads") or {}
                row_issue_type = (
                    thread_data.get("issue_type") if isinstance(thread_data, dict) else None
                )
                if issue_type and row_issue_type != issue_type:
                    # PostgREST embedded filters don't always exclude rows; guard here.
                    continue
                results.append(
                    {
                        "id": row.get("id"),
                        "thread_id": row.get("thread_id"),
                        "issue_type": row_issue_type or "unknown",
                        "summary": row.get("question_text"),
                        "reason": row.get("reason"),
                        "action_type": row.get("action_type"),
                        "created_at": row.get("created_at"),
                    }
                )
            return results
        except Exception as e:
            LOGGER.warning(
                f"Error fetching open issues for org={organization_id} "
                f"(possible schema/FK issue — run chat_threads migration?): {e}"
            )
            return []


# Backward compatibility alias
SupabaseClient = EnhancedSupabaseClient

# Singleton instance for checkpointer-safe access (not stored in LangGraph state)
_supabase_instance: Optional[EnhancedSupabaseClient] = None


def get_supabase_client() -> EnhancedSupabaseClient:
    """Get singleton Supabase client instance.

    This accessor is used instead of storing the client in LangGraph state,
    which would cause serialization errors with the PostgreSQL checkpointer.

    Returns:
        Singleton EnhancedSupabaseClient instance
    """
    import os

    global _supabase_instance
    if _supabase_instance is None:
        _supabase_instance = EnhancedSupabaseClient(
            url=os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL") or "",
            key=os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY") or "",
        )
    return _supabase_instance


__all__ = ["SupabaseClient", "EnhancedSupabaseClient", "get_supabase_client"]
