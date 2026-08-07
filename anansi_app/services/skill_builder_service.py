"""Backend for the skill builder page (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md).

Reads and writes chat_messages directly against chat_db -- the same
direct-DB pattern SupabaseReader already uses elsewhere in anansi_app (see
e.g. its delete_bot_message), not a round trip through chat_orchestrator's
HTTP API. A dedicated service rather than more methods bolted onto
SupabaseReader (already 2000+ lines), matching how
agent_management_service.py / broadcast_service.py / scheduling_service.py
are already split out by concern.

Sending a message to get an LLM response goes through chat_orchestrator's
POST /chat instead (see nicegui_app/pages/skill_builder.py) -- this service
only covers the parts that are pure chat_db reads/writes: loading a builder
session's transcript and performing the Rewind (archive) action. It never
calls chat_orchestrator's API itself.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from supabase import Client, create_client

from shared.config.db_credentials import chat_db_service_key, chat_db_url

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Kebab-case identifier, matching the convention already used for
    context-module slugs (see
    chat_orchestrator/orchestrator/experts/handlers/context_expert/propose_module.py's
    normalize_slug -- duplicated rather than imported, since anansi_app and
    chat_orchestrator are separately deployed packages with no shared import
    path outside shared/). Falls back to "skill" for a title with no
    surviving characters (e.g. all emoji/punctuation).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "skill"

# id/archived_at included so the builder page can render envelopes and skip
# already-archived rows defensively; function_call/tool_result/metadata carry
# the tool-invocation and token-count detail the plan's Work section asks
# the transcript to show per step.
_MESSAGE_COLUMNS = (
    "id, session_id, role, content, function_call, tool_result, "
    "metadata, created_at, message_index, archived_at"
)


class SkillBuilderService:
    """Direct chat_db access for the skill builder's own transcript.

    Not read-only: archive_from_message_index performs the Rewind button's
    write. See this module's docstring for why it lives here rather than on
    SupabaseReader or behind a new chat_orchestrator endpoint.
    """

    def __init__(self) -> None:
        url = chat_db_url()
        key = chat_db_service_key()
        self.client: Optional[Client] = create_client(url, key) if url and key else None

    def is_configured(self) -> bool:
        return self.client is not None

    def get_session_uuid(self, session_id: str) -> Optional[str]:
        """Resolve chat_sessions' text session_id to its UUID primary key.

        chat_messages.session_id is a foreign key to chat_sessions.id (the
        UUID), not to the hashed text session_id string -- see
        db/schema/chat_db.sql. Mirrors SupabaseReader.get_session_info's
        lookup.
        """
        if not self.client:
            return None
        try:
            response = (
                self.client.table("chat_sessions")
                .select("id")
                .eq("session_id", session_id)
                .limit(1)
                .execute()
            )
            if response.data:
                return response.data[0]["id"]
            return None
        except Exception:
            logger.exception("Error resolving session UUID for %s", session_id)
            return None

    def get_builder_messages(self, session_id: str) -> List[Dict[str, Any]]:
        """Live (non-archived) messages for a builder session, oldest first.

        Filters archived_at IS NULL here too, independently of
        chat_orchestrator's get_messages/get_messages_filtered -- this is a
        separate codebase reading the same table (see
        0012_message_archive.sql), so the same "no caller can see an
        archived row by forgetting to ask" rule applies here.
        """
        if not self.client:
            return []
        session_uuid = self.get_session_uuid(session_id)
        if not session_uuid:
            return []
        try:
            response = (
                self.client.table("chat_messages")
                .select(_MESSAGE_COLUMNS)
                .eq("session_id", session_uuid)
                .is_("archived_at", "null")
                .order("message_index", desc=False)
                .execute()
            )
            return response.data or []
        except Exception:
            logger.exception("Error loading builder messages for session %s", session_id)
            return []

    def archive_from_message_index(self, session_id: str, from_index: int) -> int:
        """Rewind: archive a step's message and everything after it.

        "Everything after" means message_index >= from_index within the
        session -- message_index is the same strictly-increasing per-session
        ordering column chat_orchestrator sorts by when building LLM
        context, so this cuts the transcript at exactly the point a fresh
        turn's history will also stop seeing it.

        No branching, no undo: archiving is permanent, and side effects a
        discarded step already caused (a ticket filed, a message sent) are
        not rolled back -- see the plan's "Decisions already made".

        Returns the number of rows archived (0 if the session doesn't
        resolve; already-archived rows are left alone rather than
        re-touched, both for idempotency and so a second click can't stomp
        an earlier archived_at timestamp).
        """
        if not self.client:
            return 0
        session_uuid = self.get_session_uuid(session_id)
        if not session_uuid:
            return 0
        try:
            response = (
                self.client.table("chat_messages")
                .update({"archived_at": datetime.now(timezone.utc).isoformat()})
                .eq("session_id", session_uuid)
                .gte("message_index", from_index)
                .is_("archived_at", "null")
                .execute()
            )
            archived_count = len(response.data or [])
            logger.info(
                "Archived %d message(s) from index %d in session %s",
                archived_count,
                from_index,
                session_id,
            )
            return archived_count
        except Exception:
            logger.exception(
                "Error archiving messages from index %d in session %s", from_index, session_id
            )
            return 0

    def _unique_slug(self, title: str) -> str:
        """First free ``slug``, ``slug-2``, ``slug-3``... against existing
        skills -- same collision-handling shape as context_expert's
        unique_slug (see _slugify's docstring)."""
        base = _slugify(title)
        existing = self.client.table("skills").select("slug").execute()
        taken = {row["slug"] for row in (existing.data or [])}
        if base not in taken:
            return base
        n = 2
        while f"{base}-{n}" in taken:
            n += 1
        return f"{base}-{n}"

    def save_skill(
        self,
        title: str,
        summary: str,
        steps: List[Dict[str, Any]],
        staff_only: bool,
        created_by: str,
    ) -> Dict[str, Any]:
        """Persist a finished skill (the builder's Save panel action).

        `steps` must already be in the stored skills.steps shape (see
        skill_validation.py's module docstring) -- the caller is expected to
        have called POST /skills/validate and blocked the save on any
        severity="error" finding first; this method does not re-validate.

        Returns {"success": True, "skill": <row>} or
        {"success": False, "error": <message>} -- matches the dict-result
        convention SupabaseReader.delete_bot_message already uses for
        anansi_app's other direct-DB writes, rather than raising.
        """
        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}
        if not title.strip():
            return {"success": False, "error": "Title is required"}
        if not steps:
            return {"success": False, "error": "A skill needs at least one step"}

        try:
            slug = self._unique_slug(title)
            response = (
                self.client.table("skills")
                .insert(
                    {
                        "slug": slug,
                        "title": title.strip(),
                        "summary": summary.strip(),
                        "steps": steps,
                        "inputs": [],
                        "staff_only": staff_only,
                        "status": "active",
                        "created_by": created_by,
                    }
                )
                .execute()
            )
            if not response.data:
                return {"success": False, "error": "Insert returned no row"}
            return {"success": True, "skill": response.data[0]}
        except Exception as e:
            logger.exception("Error saving skill %r", title)
            return {"success": False, "error": str(e)}
