"""Progressive conversation summarizer.

When a session exceeds a configurable message threshold, the oldest messages
are summarized into a compact text blob and cached in the conversation_summaries
table. Subsequent requests load the summary instead of the raw old messages,
keeping the context window manageable.

Gated behind CONVERSATION_SUMMARY_ENABLED env var (default: false).

Usage:
    summarizer = ConversationSummarizer()

    # Check if summarization is needed and generate if so
    summary = await summarizer.maybe_summarize(
        session_uuid=session.id,
        messages=conversation_history,
    )

    # Load cached summary for a session
    summary = await summarizer.get_cached_summary(session_uuid)
"""

from __future__ import annotations

import json
import os
from typing import List, Optional
from uuid import UUID

from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

SUMMARIZE_PROMPT = """Summarize the following conversation messages into a concise context summary.

Focus on:
1. Key topics discussed (grid names, ticket numbers, meter IDs)
2. Decisions made or questions answered
3. Open questions or pending items
4. Any entities mentioned (grid names, people, organizations)

Format the summary as:
**Topics:** [comma-separated list]
**Key Points:**
- [point 1]
- [point 2]
**Entities:** [comma-separated list of mentioned grids, tickets, people]
**Open Items:** [any unresolved questions or pending actions]

Messages to summarize:
{messages}

Keep the summary under 500 tokens. Be factual and concise."""

# Threshold: summarize when total messages exceed this count
SUMMARY_THRESHOLD = 40
# Number of oldest messages to summarize at a time
SUMMARY_BATCH_SIZE = 20


class ConversationSummarizer:
    """Generates and caches progressive conversation summaries."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        gateway: Optional[GeminiGateway] = None,
    ) -> None:
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
        self._model = model or os.getenv("VERIFICATION_MODEL", "gemini-2.5-flash-lite")
        self._gateway = gateway or GeminiGateway(
            api_key=self._api_key,
            default_model=self._model,
        )

    async def aclose(self) -> None:
        return None

    async def maybe_summarize(
        self,
        session_uuid: UUID,
        total_message_count: int,
    ) -> Optional[str]:
        """Check if summarization is needed and trigger if so.

        This is meant to be called fire-and-forget from save_history.
        It checks if the session has enough messages to warrant summarization
        and if no summary exists for the oldest batch.

        Args:
            session_uuid: Session UUID
            total_message_count: Total messages in the session

        Returns:
            Summary text if generated, None otherwise
        """
        if total_message_count < SUMMARY_THRESHOLD:
            return None

        try:
            from orchestrator.services.supabase_client import get_supabase_client

            supabase = get_supabase_client()
            client = supabase._get_client()

            # Check if we already have a summary covering the oldest messages
            existing = (
                client.table("conversation_summaries")
                .select("id, message_range_end")
                .eq("session_id", str(session_uuid))
                .order("message_range_end", desc=True)
                .limit(1)
                .execute()
            )

            # Determine range to summarize
            start_index = 0
            if existing.data:
                last_end = existing.data[0]["message_range_end"]
                # Only summarize if there are enough new messages beyond the last summary
                if total_message_count - last_end < SUMMARY_BATCH_SIZE:
                    return None
                start_index = last_end

            end_index = start_index + SUMMARY_BATCH_SIZE

            # Fetch the messages to summarize
            messages_response = (
                client.table("chat_messages")
                .select("role, content, metadata, message_index")
                .eq("session_id", str(session_uuid))
                .gte("message_index", start_index)
                .lt("message_index", end_index)
                .order("message_index", desc=False)
                .execute()
            )

            if not messages_response.data:
                return None

            # Format messages for summarization
            formatted_messages = []
            for row in messages_response.data:
                content = row.get("content", "")
                if content:
                    formatted_messages.append(f"{row['role']}: {content[:300]}")

            if not formatted_messages:
                return None

            messages_text = "\n".join(formatted_messages)
            prompt = SUMMARIZE_PROMPT.format(messages=messages_text)

            # Generate summary
            summary_text = await self._call_gemini(prompt)
            if not summary_text:
                return None

            # Extract entities from summary for indexing
            entities = self._extract_entities_from_summary(summary_text)

            # Cache in database
            summary_data = {
                "session_id": str(session_uuid),
                "summary_text": summary_text,
                "message_range_start": start_index,
                "message_range_end": end_index,
                "topic_entities": json.dumps(entities),
                "token_count": len(summary_text.split()),  # Approximate
            }

            client.table("conversation_summaries").insert(summary_data).execute()

            LOGGER.info(
                f"Generated conversation summary for session {session_uuid} "
                f"(messages {start_index}-{end_index}, {len(summary_text)} chars)"
            )
            return summary_text

        except Exception as e:
            LOGGER.warning(f"Conversation summarization failed (non-blocking): {e}")
            return None

    async def get_cached_summary(self, session_uuid: UUID) -> Optional[str]:
        """Load the most recent cached summary for a session.

        Args:
            session_uuid: Session UUID

        Returns:
            Summary text or None if no summary exists
        """
        try:
            from orchestrator.services.supabase_client import get_supabase_client

            supabase = get_supabase_client()
            client = supabase._get_client()

            response = (
                client.table("conversation_summaries")
                .select("summary_text")
                .eq("session_id", str(session_uuid))
                .order("message_range_end", desc=True)
                .limit(1)
                .execute()
            )

            if response.data:
                return str(response.data[0]["summary_text"])
            return None

        except Exception as e:
            LOGGER.warning(f"Failed to load conversation summary: {e}")
            return None

    async def _call_gemini(self, prompt: str) -> str:
        """Make a lightweight Gemini API call for summarization."""
        result = await self._gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=self._model,
                temperature=0.2,
                max_output_tokens=512,
            ),
        )
        return result.text

    def _extract_entities_from_summary(self, summary: str) -> List[str]:
        """Extract entity mentions from the summary text."""
        import re

        entities: List[str] = []
        # Look for the Entities line in the structured summary
        entities_match = re.search(r"\*\*Entities:\*\*\s*(.+)", summary)
        if entities_match:
            entities_text = entities_match.group(1)
            entities = [e.strip() for e in entities_text.split(",") if e.strip()]
        return entities


def is_summary_enabled() -> bool:
    """Check if conversation summarization is enabled."""
    return os.getenv("CONVERSATION_SUMMARY_ENABLED", "false").lower() == "true"


__all__ = ["ConversationSummarizer", "is_summary_enabled"]
