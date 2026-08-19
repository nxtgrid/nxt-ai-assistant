"""
RAG Provider with Permission Filtering

This service retrieves relevant context from the RAG pipeline's vector database,
filtered by user permissions. It queries the auth Supabase instance to ensure
users only see documents they have access to.

The service can be:
1. Called directly by Anansi orchestrator in production
2. Wrapped as an MCP server for local Claude Desktop testing
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from pydantic import BaseModel

from shared.auth import UserPermissions, get_auth_service
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# The exact parameter names search_chunks_with_permissions declares. Pinned
# and tested (tests/test_rag_provider_rpc_contract.py) because a mismatch
# here does not fail loudly: PostgREST rejects the call, the except swallows
# it, and retrieval silently returns nothing on every request.
#
# HOTFIX 2026-08-19: the first version of this fix (PR #107) got this set
# from db/schema/chat_db.sql:709, which turned out to be stale/aspirational
# -- NOT what production actually runs. Confirmed against the live function
# via `pg_get_function_identity_arguments`:
#
#     search_chunks_with_permissions(query_embedding vector,
#         match_threshold double precision, match_count integer,
#         user_role_ids integer[], user_org_ids integer[])
#
# These are, name-for-name, what the *original* (pre-Phase-0) caller already
# sent. The schema file was wrong, not the caller's argument names -- so
# whatever was actually causing retrieve() to return [] before Phase 0 was
# not a name mismatch. That is still unconfirmed; see the Phase 0 PR/commit
# history rather than assuming this comment is the last word.
SEARCH_RPC_ARGUMENTS = {
    "query_embedding",
    "match_threshold",
    "match_count",
    "user_role_ids",
    "user_org_ids",
}

# Over-fetch so the caller can trim to `limit` after ranking.
OVERFETCH_FACTOR = 2

# The long-standing production value (also what the original caller used).
# Not re-derived here -- the previous "0.3, a low floor" reasoning was based
# on a parameter (`similarity_threshold`) that does not exist on the real
# function, so there is no evidence backing a different number. Revisit with
# Task 4's baseline measurement in hand, not by guessing again.
DEFAULT_MATCH_THRESHOLD = 0.7


def build_search_arguments(
    embedding: List[float],
    organization_ids: List[str],
    limit: int,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    is_staff: bool = False,
) -> Dict[str, Any]:
    """Arguments for search_chunks_with_permissions.

    user_role_ids is always empty: UserPermissions.roles carries role
    *names* (e.g. "ops"), not the numeric role IDs this integer[] parameter
    expects, and there is no client-side mapping from one to the other.
    Role-based filtering inside the function, if any, is not something this
    caller can drive today -- sending [] is honest about that, not a
    regression from before (the pre-Phase-0 code's role extraction only
    matched objects with a numeric `.id` or plain ints; given string role
    names it also always produced []).

    A non-staff caller with no organizations is refused before the RPC is
    even called. This function's own semantics for an empty user_org_ids
    array are not confirmed (unrestricted vs. no-match) -- this refusal is a
    client-side floor, not a claim about what the database does with [].
    Staff with no organizations pass an empty array through unchanged, the
    same as the pre-Phase-0 code did for anyone with no organizations; this
    hotfix does not invent new "unrestricted" behavior it cannot verify.
    """
    if not is_staff and not organization_ids:
        raise ValueError(
            "cannot build a permission-filtered search for a non-staff caller "
            "with no organizations"
        )
    try:
        org_ids = [int(o) for o in organization_ids]
    except (TypeError, ValueError) as e:
        raise ValueError(
            f"organization_ids must be integer-valued for this RPC: {organization_ids!r}"
        ) from e
    return {
        "query_embedding": embedding,
        "match_threshold": threshold,
        "match_count": limit * OVERFETCH_FACTOR,
        "user_role_ids": [],
        "user_org_ids": org_ids,
    }


class RAGDocument(BaseModel):
    """Represents a retrieved RAG document."""

    content: str
    title: str
    source_type: str  # codebase, docs, jira, etc.
    url: Optional[str] = None
    metadata: Dict[str, Any] = {}
    score: float = 0.0


class RAGProvider:
    """
    RAG provider with permission filtering.

    Retrieves relevant documents from Supabase rag_documents table,
    filtered by user's permitted organizations.
    """

    def __init__(
        self,
        rag_supabase_url: Optional[str] = None,
        rag_supabase_key: Optional[str] = None,
        auth_supabase_url: Optional[str] = None,
        auth_supabase_anon_key: Optional[str] = None,
    ):
        """
        Initialize RAG provider.

        Args:
            rag_supabase_url: URL to RAG database (main Supabase)
            rag_supabase_key: Service key for RAG database
            auth_supabase_url: URL to auth database (read-only)
            auth_supabase_anon_key: Anon key for auth database
        """
        self._rag_url = (
            rag_supabase_url or os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
        )
        self._rag_key = (
            rag_supabase_key or os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
        )
        self._rag_client = None

        # Use singleton auth service for permission checking
        self._auth_service = get_auth_service()

        if not self._rag_url or not self._rag_key:
            LOGGER.warning(
                "RAG database not configured. "
                "Set CHAT_DB_URL and CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/SUPABASE_KEY)."
            )

    def _get_rag_client(self):
        """Get or create RAG Supabase client."""
        if self._rag_client is None and self._rag_url and self._rag_key:
            try:
                from supabase import create_client

                self._rag_client = create_client(self._rag_url, self._rag_key)
                LOGGER.info("RAG Supabase client initialized")
            except ImportError:
                LOGGER.error("supabase-py not installed. Install with: pip install supabase")
                raise
        return self._rag_client

    async def retrieve(
        self,
        query: str,
        user_email: str,
        limit: int = 5,
        source_types: Optional[List[str]] = None,
        intent: str = "general",
        user_permissions: Any = None,
    ) -> List[RAGDocument]:
        """
        Retrieve relevant documents filtered by user permissions.

        Uses vector similarity search with permission-aware filtering via
        the search_chunks_with_permissions database function.

        Args:
            query: Search query
            user_email: User email for permission filtering
            limit: Maximum number of documents to return
            source_types: Filter by source types (codebase, docs, jira, etc.)
            intent: Query intent for embedding task type (general, factual, qa, code)
            user_permissions: Pre-resolved permissions (UserContext or UserPermissions)
                to avoid redundant auth DB query. Falls back to DB lookup if not provided.

        Returns:
            List of RAG documents user has access to
        """
        client = self._get_rag_client()
        if not client:
            LOGGER.warning("RAG client not available")
            return []

        try:
            # Step 1: Get user permissions (use pre-resolved if available)
            if user_permissions and hasattr(user_permissions, "organization_ids"):
                permissions = user_permissions
                LOGGER.debug("Using pre-resolved permissions for RAG retrieval")
            else:
                permissions = await self._auth_service.get_user_permissions(user_email)

            LOGGER.info(
                f"RAG retrieval for {user_email}: "
                f"query='{query}', limit={limit}, intent={intent}, "
                f"permissions: {len(permissions.organization_ids)} orgs"
            )

            # Step 2: Generate embedding for query with intent-appropriate task type
            embedding = await self._generate_embedding(query, intent=intent)

            if not embedding:
                LOGGER.warning("Failed to generate embedding, falling back to keyword search")
                return []

            # Step 3: Build permission arrays for the database function.
            # search_chunks_with_permissions filters by organization only --
            # it has no role-based parameter.
            user_org_ids = (
                list(permissions.organization_ids) if permissions.organization_ids else []
            )

            # Step 4: Search with permission-aware vector similarity.
            # Uses the search_chunks_with_permissions database function.
            try:
                arguments = build_search_arguments(
                    embedding=embedding,
                    organization_ids=user_org_ids,
                    limit=limit,
                    is_staff=bool(getattr(permissions, "is_staff", False)),
                )
            except ValueError as e:
                LOGGER.warning(f"RAG retrieval refused for {user_email}: {e}")
                return []

            try:
                results = client.rpc("search_chunks_with_permissions", arguments).execute()
            except Exception as e:
                # Deliberately no fallback. The previous code fell back to
                # match_rag_documents, which applies no permission filter at
                # all -- a bypass waiting to start working the moment
                # somebody defined it. Failing closed is correct here.
                LOGGER.error(
                    f"Permission-filtered RAG search failed for {user_email}; "
                    f"returning no results rather than falling back to an "
                    f"unfiltered search: {e}"
                )
                return []

            if not results.data:
                LOGGER.info("No RAG documents found")
                return []

            # Step 5: Convert to RAGDocument objects
            docs = []
            for row in results.data:
                # Handle both chunk-based and document-based results
                content = row.get("content", "")
                metadata = row.get("chunk_metadata") or row.get("source_metadata") or {}

                # For chunk results, we may need to look up document title
                title = metadata.get("title", row.get("title", ""))
                if not title and row.get("document_id"):
                    title = f"Document {str(row['document_id'])[:8]}"

                docs.append(
                    RAGDocument(
                        content=content,
                        title=title,
                        source_type=metadata.get("source_type", row.get("source_type", "unknown")),
                        url=metadata.get("url"),
                        metadata=metadata,
                        score=row.get("similarity", 0.0),
                    )
                )

                if len(docs) >= limit:
                    break

            LOGGER.info(f"Retrieved {len(docs)} RAG documents (from {len(results.data)} results)")

            return docs

        except Exception as e:
            LOGGER.exception(f"Error retrieving RAG documents: {e}")
            return []

    async def retrieve_as_text(
        self,
        query: str,
        user_email: str,
        limit: int = 5,
        source_types: Optional[List[str]] = None,
        user_permissions: Any = None,
    ) -> List[str]:
        """
        Retrieve relevant documents as text snippets.

        This is the interface used by ConversationOrchestrator.

        Args:
            query: Search query
            user_email: User email for permission filtering
            limit: Maximum number of documents to return
            source_types: Filter by source types
            user_permissions: Pre-resolved permissions to avoid redundant DB query

        Returns:
            List of text snippets
        """
        docs = await self.retrieve(
            query, user_email, limit, source_types, user_permissions=user_permissions
        )
        return [self._format_document(doc) for doc in docs]

    def _has_document_access(self, doc: Dict[str, Any], permissions: UserPermissions) -> bool:
        """
        Check if user has access to a document.

        Args:
            doc: Document data from database
            permissions: User permissions

        Returns:
            True if user has access, False otherwise
        """
        # Public documents are accessible to all
        if doc.get("is_public", False):
            return True

        # Admin has access to everything
        if permissions.is_admin:
            return True

        # Check role-based access
        access_roles = doc.get("access_roles", [])
        if access_roles:
            # User must have at least one of the required roles
            if any(role in permissions.roles for role in access_roles):
                return True

        # Check organization/grid/meter access
        # This requires structured metadata in source_metadata field
        metadata = doc.get("source_metadata", {})

        # Filter by organization_id only (optimization)
        # Note: grid_ids and meter_ids are no longer loaded during auth
        # since MCP tools already filter by organization_id when querying
        org_id = metadata.get("organization_id")
        if org_id and org_id in permissions.organization_ids:
            return True

        # Default: no access
        return False

    def _format_document(self, doc: RAGDocument) -> str:
        """
        Format a document for inclusion in prompt.

        Args:
            doc: RAG document

        Returns:
            Formatted text
        """
        formatted = f"[{doc.source_type.upper()}] {doc.title}\n"
        if doc.url:
            formatted += f"URL: {doc.url}\n"
        formatted += f"\n{doc.content}\n"

        return formatted

    async def _generate_embedding(
        self, text: str, intent: str = "general"
    ) -> Optional[List[float]]:
        """
        Generate embedding for query text.

        Uses Google's text-embedding-005 via Vertex AI with intent-appropriate task types
        for asymmetric retrieval (queries use different task types than documents).

        Args:
            text: Text to embed
            intent: Query intent (general, factual, qa, code)

        Returns:
            768-dimensional embedding vector, or None on failure
        """
        try:
            from shared.utils.vertex_embeddings import get_embedding

            # Map intent to task type
            task_type_map = {
                "general": "RETRIEVAL_QUERY",
                "factual": "FACT_VERIFICATION",
                "qa": "QUESTION_ANSWERING",
                "code": "CODE_RETRIEVAL_QUERY",
            }
            task_type = task_type_map.get(intent, "RETRIEVAL_QUERY")

            return await get_embedding(text, task_type=task_type)

        except ImportError:
            LOGGER.warning("vertex_embeddings not available, embeddings disabled")
            return None
        except Exception as e:
            LOGGER.error(f"Embedding generation failed: {e}")
            return None


__all__ = ["RAGProvider", "RAGDocument"]
