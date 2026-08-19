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

    def __init__(self, client: Optional[Client] = None) -> None:
        if client is not None:
            self.client: Optional[Client] = client
        else:
            url = chat_db_url()
            key = chat_db_service_key()
            self.client = create_client(url, key) if url and key else None

    def is_configured(self) -> bool:
        return self.client is not None

    VALID_STATUSES = ("draft", "active", "disabled", "unusable")

    def list_skills(self) -> List[Dict[str, Any]]:
        """Every skill, whatever its status -- this is the admin list.

        Deliberately not SkillCatalogStore.all_skills(), which filters to
        active because it feeds model context. An operator must see drafts
        and disabled skills; a model must not.
        """
        if not self.client:
            return []
        try:
            response = (
                self.client.table("skills")
                .select(
                    "id, slug, title, summary, steps, staff_only, status, "
                    "created_by, created_at, updated_at"
                )
                .order("updated_at", desc=True)
                .execute()
            )
        except Exception as e:
            logger.warning("Skill list fetch failed: %s", e)
            return []

        skills = []
        for row in response.data or []:
            row = dict(row)
            row["step_count"] = len(row.get("steps") or [])
            skills.append(row)
        return skills

    def update_skill_status(self, skill_id: str, status: str, actor: str) -> Dict[str, Any]:
        """Move a skill between draft/active/disabled/unusable.

        Promotion to 'active' is gated on validation by the caller (the
        modal's Save), not here -- this method is also how a scheduled run
        marks a skill 'unusable', which must never be blocked.
        """
        if status not in self.VALID_STATUSES:
            return {
                "success": False,
                "error": f"'{status}' is not a valid status; expected one of "
                         f"{', '.join(self.VALID_STATUSES)}",
            }
        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}
        try:
            response = (
                self.client.table("skills")
                .update({"status": status})
                .eq("id", skill_id)
                .execute()
            )
            if not response.data:
                return {"success": False, "error": "Update returned no row"}
            logger.info("Skill %s status -> %s (by %s)", skill_id, status, actor)
            return {"success": True, "skill": response.data[0]}
        except Exception as e:
            logger.exception("Error updating skill %s status", skill_id)
            return {"success": False, "error": str(e)}

    def update_skill(
        self,
        skill_id: str,
        title: str,
        summary: str,
        staff_only: bool,
        status: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Update an existing skill's identity and status together.

        The editor modal's Identity card always shows title/summary/staff
        and status together (nicegui_app/pages/skills.py's _open_editor),
        so a single write matches what the UI actually offers -- there is
        no separate "just rename it" affordance today. Does not touch
        steps: this modal has no way to re-edit an existing skill's saved
        steps yet (see render_builder's initial_steps docstring), so a
        steps column would have nothing new to write.
        """
        if not title.strip():
            return {"success": False, "error": "Title is required"}
        if status not in self.VALID_STATUSES:
            return {
                "success": False,
                "error": f"'{status}' is not a valid status; expected one of "
                         f"{', '.join(self.VALID_STATUSES)}",
            }
        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}
        try:
            response = (
                self.client.table("skills")
                .update(
                    {
                        "title": title.strip(),
                        "summary": summary.strip(),
                        "staff_only": staff_only,
                        "status": status,
                    }
                )
                .eq("id", skill_id)
                .execute()
            )
            if not response.data:
                return {"success": False, "error": "Update returned no row"}
            logger.info("Skill %s updated (status -> %s) by %s", skill_id, status, actor)
            return {"success": True, "skill": response.data[0]}
        except Exception as e:
            logger.exception("Error updating skill %s", skill_id)
            return {"success": False, "error": str(e)}

    def schedule_summaries(self) -> Dict[str, Dict[str, Any]]:
        """skill_id -> its schedule row, for the list page's Schedule column.

        Reads user_schedules rather than a skills column: 0013 deliberately
        reused the existing scheduler rather than adding a fifth one, so a
        skill's schedule lives there.
        """
        if not self.client:
            return {}
        try:
            response = (
                self.client.table("user_schedules")
                .select(
                    "skill_id, cron_expression, schedule_type, anchor_entity_type, "
                    "timezone, is_active"
                )
                .not_.is_("skill_id", "null")
                .execute()
            )
        except Exception as e:
            logger.warning("Skill schedule fetch failed: %s", e)
            return {}
        return {
            row["skill_id"]: row for row in (response.data or []) if row.get("skill_id")
        }

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

    # Must match anansi_app/nicegui_app/pages/broadcast.py's _build_recurrence,
    # minus "Does not repeat" -- that maps to no recurrence at all (returns
    # None), which this treats as an error rather than a one-time schedule.
    # A one-off skill run doesn't need a persistent cron row; run it from the
    # builder instead.
    SUPPORTED_ANCHORS = ("grid", "organization")

    def set_skill_schedule(
        self,
        skill_id: str,
        anchor_entity_type: str,
        first_run: str,
        frequency: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Schedule a skill to fan out across every eligible entity.

        Reuses broadcast.py's _build_recurrence rather than deriving cron a
        second way -- the two must agree on what "Weekly" means. `frequency`
        must be one of its REPEAT_OPTIONS other than "Does not repeat":
        "Weekly", "Every other week", "Monthly (same date)" or
        "Monthly (same weekday)".

        `command` is explicitly None: user_schedules_command_xor_skill_chk
        requires exactly one of command / skill_id per row. `chat_id` is
        NOT NULL on user_schedules but meaningless for a skill row -- the
        dispatcher (skill_schedule_dispatch.py) fans out by
        anchor_entity_type and never reads it -- so it gets a clearly
        synthetic placeholder rather than a real chat. `created_by_user_id`
        is also NOT NULL with no real id available from an admin-UI actor
        (unlike schedule_mcp_server.py's chat-originated schedules, which
        have a real Telegram user_id); `actor`'s email fills both identity
        columns rather than leaving either fabricated or null.

        Upserts on skill_id (see migration 0026's partial unique index) --
        reopening the modal and changing the schedule replaces the one row
        rather than accumulating duplicates.
        """
        from datetime import datetime, timezone

        if anchor_entity_type not in self.SUPPORTED_ANCHORS:
            return {
                "success": False,
                "error": f"'{anchor_entity_type}' is not a supported anchor; expected "
                         f"{' or '.join(self.SUPPORTED_ANCHORS)}",
            }
        try:
            when = datetime.strptime(first_run.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, AttributeError):
            return {
                "success": False,
                "error": "Could not read the first run time; expected YYYY-MM-DD HH:MM",
            }

        from nicegui_app.pages.broadcast import _build_recurrence

        recurrence = _build_recurrence(when, frequency) or {}
        if not recurrence.get("cron_expression"):
            return {"success": False, "error": f"Could not derive a schedule from '{frequency}'"}

        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}

        payload = {
            "skill_id": skill_id,
            "command": None,
            "chat_id": f"skill:{skill_id}",
            "created_by_user_id": actor,
            "created_by_email": actor,
            "anchor_entity_type": anchor_entity_type,
            "cron_expression": recurrence["cron_expression"],
            "schedule_type": recurrence.get("schedule_type", "recurring"),
            "timezone": recurrence.get("timezone", "UTC"),
            "is_active": True,
            "status": "active",
        }
        try:
            response = (
                self.client.table("user_schedules")
                .upsert(payload, on_conflict="skill_id")
                .execute()
            )
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "schedule": (response.data or [payload])[0]}

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
        status: str = "active",
    ) -> Dict[str, Any]:
        """Persist a finished skill (the builder's Save panel action).

        `steps` must already be in the stored skills.steps shape (see
        skill_validation.py's module docstring) -- the caller is expected to
        have called POST /skills/validate and blocked the save on any
        severity="error" finding first; this method does not re-validate
        steps.

        `status` defaults to "active" to match the standalone builder page's
        one-shot save (nicegui_app/pages/skill_builder.py), which predates
        the draft concept and has no status picker of its own. The skills
        list editor modal (nicegui_app/pages/skills.py) passes its own
        status choice explicitly instead -- this inserts the row at that
        status directly rather than saving active and immediately
        correcting it with a second update_skill_status call, which would
        put a still-unreviewed draft into the model's context for the
        (small but nonzero) window between the two writes.

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
        if status not in self.VALID_STATUSES:
            return {
                "success": False,
                "error": f"'{status}' is not a valid status; expected one of "
                         f"{', '.join(self.VALID_STATUSES)}",
            }

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
                        "status": status,
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
