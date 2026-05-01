"""Enhanced Supabase client for comprehensive database operations."""

from __future__ import annotations

from typing import Any, List, Optional
from uuid import UUID

try:
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
except ImportError:
    # These models are orchestrator-specific, not required for all uses
    pass

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__, project_name="shared")


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

                from supabase import create_client

                # Prefer new API approach with anon key + JWT
                anon_key = self.anon_key or os.getenv("SUPABASE_ANON_KEY", "")
                jwt_token = self.jwt_token or os.getenv("SUPABASE_SERVICE_JWT", "")

                if anon_key:
                    self._client = create_client(self.url, anon_key)

                    # If JWT token is provided, use it for authentication
                    if jwt_token:
                        self._client.postgrest.auth(jwt_token)
                        LOGGER.info("Supabase client initialized with JWT authentication")
                    else:
                        LOGGER.info("Supabase client initialized with anon key (no JWT)")

                # Fallback to legacy service role key
                else:
                    self._client = create_client(self.url, self.key)
                    LOGGER.warning("Supabase client initialized with legacy service role key")

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

            # Try to get existing user
            response = (
                client.table("users").select("*").eq("external_user_id", external_user_id).execute()
            )

            if response.data:
                return UserModel(**response.data[0])

            # Create new user
            user_data = {"external_user_id": external_user_id, "username": username, "email": email}
            response = client.table("users").insert(user_data).execute()
            return UserModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error creating/getting user: {e}")
            raise

    # Session Management
    async def create_session(
        self, session_id: str, user_id: Optional[UUID] = None, title: Optional[str] = None
    ) -> ChatSessionModel:
        """Create a new chat session."""
        try:
            client = self._get_client()

            session_data = {
                "session_id": session_id,
                "user_id": str(user_id) if user_id else None,
                "title": title,
            }
            response = client.table("chat_sessions").insert(session_data).execute()
            return ChatSessionModel(**response.data[0])

        except Exception as e:
            LOGGER.error(f"Error creating session: {e}")
            raise

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

    # Message Management
    async def save_messages(
        self, session_uuid: UUID, messages: List[ConversationMessage]
    ) -> List[ChatMessageModel]:
        """Save conversation messages to database."""
        try:
            client = self._get_client()

            message_rows = []
            for idx, message in enumerate(messages):
                message_data = {
                    "session_id": str(session_uuid),
                    "role": message.role,
                    "content": message.content,
                    "function_call": (
                        message.function_call.model_dump() if message.function_call else None
                    ),
                    "tool_result": (
                        message.tool_result.model_dump() if message.tool_result else None
                    ),
                    "message_index": idx,
                }
                message_rows.append(message_data)

            if message_rows:
                response = client.table("chat_messages").insert(message_rows).execute()
                return [ChatMessageModel(**row) for row in response.data]

            return []

        except Exception as e:
            LOGGER.error(f"Error saving messages: {e}")
            raise

    async def get_conversation_history(
        self, session_id: str, user_id: Optional[str] = None
    ) -> List[ConversationMessage]:
        """Retrieve conversation history for a session."""
        try:
            client = self._get_client()

            # Get session first
            session_response = (
                client.table("chat_sessions").select("id").eq("session_id", session_id).execute()
            )

            if not session_response.data:
                return []

            session_uuid = session_response.data[0]["id"]

            # Get messages
            response = (
                client.table("chat_messages")
                .select("*")
                .eq("session_id", session_uuid)
                .order("message_index", desc=False)
                .execute()
            )

            messages = []
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
                )
                messages.append(message)

            LOGGER.info(f"Retrieved {len(messages)} messages for session {session_id}")
            return messages

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


# Backward compatibility alias
SupabaseClient = EnhancedSupabaseClient

__all__ = ["SupabaseClient", "EnhancedSupabaseClient"]
