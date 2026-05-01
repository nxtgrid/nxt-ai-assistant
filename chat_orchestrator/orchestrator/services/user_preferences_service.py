"""User preferences service for persistent per-user response customization.

Stores and retrieves user preferences that modify how the bot formats responses.
Preferences are injected into context_message (never systemInstruction) with
structural isolation framing so they cannot override core system instructions.

Key design decisions:
- canonical_user_id: email (preferred) or tg:{telegram_id} (fallback)
- Last-write-wins upsert on (canonical_user_id, preference_key)
- Regex safety blocking + word-count cap for injection defense
- Cap: 10 preferences per user, 200 chars per value, 15 words max
- Rate limit: 5 storage attempts per user per hour (in-memory, resets on deploy)
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any

from shared.utils.logging import get_logger

if TYPE_CHECKING:
    from orchestrator.models.schemas import UserContext

LOGGER = get_logger(__name__)

# Maximum preferences per user
MAX_PREFERENCES_PER_USER = 10

# Maximum length of a preference value
MAX_PREFERENCE_VALUE_LENGTH = 200

# Maximum words in a preference value (injection defense)
MAX_PREFERENCE_WORDS = 15

# Rate limit: max storage attempts per user per hour
MAX_STORES_PER_HOUR = 5

# Maximum length of raw_expression stored for debugging
MAX_RAW_EXPRESSION_LENGTH = 500

# Allowed preference key categories
VALID_PREFERENCE_KEYS = {
    "response_length",
    "tone",
    "format",
    "field_inclusion",
    "language_complexity",
    "other",
}

# Patterns that indicate prompt injection, not legitimate preferences
BLOCKED_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?(previous|prior|above|your)\s+instructions", re.IGNORECASE),
    re.compile(r"(you are|act as|pretend)\s+", re.IGNORECASE),
    re.compile(r"(system prompt|your instructions)\s*(reveal|show|ignore|display)", re.IGNORECASE),
    re.compile(r"override\s+(your|the|all)\s+(rules|instructions)", re.IGNORECASE),
    re.compile(r"forget\s+(everything|all|your)\s+(your\s+)?(rules|instructions)", re.IGNORECASE),
    re.compile(r"jailbreak|DAN|developer mode|unrestricted", re.IGNORECASE),
    re.compile(r"disregard\s+(safety|all|your)", re.IGNORECASE),
    re.compile(r"new\s+instruction", re.IGNORECASE),
    re.compile(r"(append|include|translate)\s+.*(system|prompt|instructions)", re.IGNORECASE),
]

# TTL for preference cache (seconds)
_CACHE_TTL_SECONDS = 60


class UserPreferencesService:
    """Service for managing per-user response preferences."""

    def __init__(self) -> None:
        # Rate limit: {canonical_user_id: [timestamp, ...]}
        self._rate_limit_tracker: dict[str, list[float]] = {}
        # TTL cache: {canonical_user_id: (fetch_time, [prefs])}
        self._prefs_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
        # Migration cache: users already checked this process lifetime
        self._migrated_users: set[str] = set()

    def _get_supabase(self) -> Any:
        """Lazy access to Supabase client."""
        from orchestrator.services.supabase_client import get_supabase_client

        return get_supabase_client()

    @staticmethod
    def resolve_canonical_id(user_email: str | None, telegram_id: str | None) -> str | None:
        """Resolve to a canonical user identifier.

        Email is preferred (stable, unique across sessions).
        Telegram ID is fallback with 'tg:' prefix.
        """
        if user_email:
            return user_email
        if telegram_id:
            return f"tg:{telegram_id}"
        return None

    @staticmethod
    def resolve_canonical_id_from_context(user_context: UserContext | None) -> str | None:
        """Resolve canonical ID from a UserContext object.

        Encapsulates the telegram source check + canonical ID resolution
        to avoid duplicating this logic across graph nodes.
        """
        if not user_context:
            return None
        telegram_id = user_context.user_id if user_context.source == "telegram" else None
        return UserPreferencesService.resolve_canonical_id(user_context.user_email, telegram_id)

    def _contains_injection_pattern(self, text: str) -> bool:
        """Check if text contains prompt injection patterns."""
        return any(pattern.search(text) for pattern in BLOCKED_PATTERNS)

    def _check_rate_limit(self, canonical_user_id: str) -> bool:
        """Check if user has exceeded storage rate limit.

        Note: In-memory only, resets on process restart. Acceptable given
        the 10-preference cap already bounds total writes.

        Returns:
            True if within limit, False if exceeded.
        """
        now = time.time()
        one_hour_ago = now - 3600

        timestamps = self._rate_limit_tracker.get(canonical_user_id)
        if timestamps is None:
            self._rate_limit_tracker[canonical_user_id] = [now]
            return True

        # Clean old entries
        timestamps = [ts for ts in timestamps if ts > one_hour_ago]

        if not timestamps:
            # All entries expired — remove stale key and allow
            del self._rate_limit_tracker[canonical_user_id]
            self._rate_limit_tracker[canonical_user_id] = [now]
            return True

        if len(timestamps) >= MAX_STORES_PER_HOUR:
            self._rate_limit_tracker[canonical_user_id] = timestamps
            return False

        timestamps.append(now)
        self._rate_limit_tracker[canonical_user_id] = timestamps
        return True

    def _invalidate_cache(self, canonical_user_id: str) -> None:
        """Invalidate cached preferences for a user after writes."""
        self._prefs_cache.pop(canonical_user_id, None)

    async def get_all(self, canonical_user_id: str) -> list[dict[str, Any]]:
        """Fetch all preferences for a user.

        Uses a 60-second TTL cache to avoid redundant DB queries during
        rapid-fire conversations. Cache is invalidated on writes.
        """
        # Check TTL cache
        now = time.time()
        cached = self._prefs_cache.get(canonical_user_id)
        if cached and (now - cached[0]) < _CACHE_TTL_SECONDS:
            return cached[1]

        try:
            client = self._get_supabase()
            result = (
                client._get_client()
                .table("user_preferences")
                .select("*")
                .eq("canonical_user_id", canonical_user_id)
                .order("created_at")
                .execute()
            )
            prefs = result.data or []
            self._prefs_cache[canonical_user_id] = (now, prefs)
            return prefs
        except Exception as e:
            LOGGER.warning(f"Failed to fetch preferences for {canonical_user_id}: {e}")
            return []

    async def store_preference(
        self,
        canonical_user_id: str,
        preference_key: str,
        preference_value: str,
        raw_expression: str | None = None,
    ) -> dict[str, Any]:
        """Store or update a user preference.

        Uses last-write-wins upsert on (canonical_user_id, preference_key).
        """
        # 1. Safety check — regex patterns
        if self._contains_injection_pattern(preference_value):
            LOGGER.warning(
                f"Blocked preference injection attempt from {canonical_user_id}: "
                f"{preference_value[:100]}"
            )
            return {
                "status": "rejected",
                "reason": "This doesn't look like a formatting preference.",
            }

        # 2. Safety check — word count cap (defense against free-form instructions)
        if len(preference_value.split()) > MAX_PREFERENCE_WORDS:
            LOGGER.warning(
                f"Preference too verbose from {canonical_user_id}: "
                f"{len(preference_value.split())} words"
            )
            return {
                "status": "rejected",
                "reason": "Preference is too long. Please keep it to a short phrase.",
            }

        # 3. Rate limit check
        if not self._check_rate_limit(canonical_user_id):
            LOGGER.warning(f"Rate limit exceeded for {canonical_user_id}")
            return {
                "status": "rejected",
                "reason": "Too many preference changes. Please try again later.",
            }

        # 4. Validate preference key
        if preference_key not in VALID_PREFERENCE_KEYS:
            preference_key = "other"

        # 5. Truncate preference value
        if len(preference_value) > MAX_PREFERENCE_VALUE_LENGTH:
            preference_value = preference_value[:MAX_PREFERENCE_VALUE_LENGTH]

        # 6. Truncate raw_expression
        if raw_expression and len(raw_expression) > MAX_RAW_EXPRESSION_LENGTH:
            raw_expression = raw_expression[:MAX_RAW_EXPRESSION_LENGTH]

        # 7. Check max preferences cap
        try:
            existing = await self.get_all(canonical_user_id)
            existing_keys = {p["preference_key"] for p in existing}

            if len(existing) >= MAX_PREFERENCES_PER_USER and preference_key not in existing_keys:
                return {
                    "status": "rejected",
                    "reason": (
                        f"You've reached the maximum of {MAX_PREFERENCES_PER_USER} preferences. "
                        "Use /preferences to review and remove some."
                    ),
                }
        except Exception as e:
            LOGGER.warning(f"Failed to check preference count: {e}")
            # Continue — fail open on count check

        # 8. Upsert — last write wins
        try:
            client = self._get_supabase()
            client._get_client().table("user_preferences").upsert(
                {
                    "canonical_user_id": canonical_user_id,
                    "preference_key": preference_key,
                    "preference_value": preference_value,
                    "raw_expression": raw_expression,
                },
                on_conflict="canonical_user_id,preference_key",
            ).execute()

            # Invalidate cache after write
            self._invalidate_cache(canonical_user_id)

            LOGGER.info(
                f"Stored preference for {canonical_user_id}: "
                f"{preference_key}={preference_value[:60]}"
            )
            return {"status": "saved", "message": "Preference stored successfully."}
        except Exception as e:
            LOGGER.error(f"Failed to store preference: {e}")
            return {"status": "error", "reason": "Failed to save preference. Please try again."}

    async def delete_preference(
        self, canonical_user_id: str, preference_key: str
    ) -> dict[str, Any]:
        """Delete a user preference by key."""
        try:
            client = self._get_supabase()
            client._get_client().table("user_preferences").delete().eq(
                "canonical_user_id", canonical_user_id
            ).eq("preference_key", preference_key).execute()

            # Invalidate cache after write
            self._invalidate_cache(canonical_user_id)

            LOGGER.info(f"Deleted preference {preference_key} for {canonical_user_id}")
            return {"status": "deleted", "message": f"Preference '{preference_key}' removed."}
        except Exception as e:
            LOGGER.error(f"Failed to delete preference: {e}")
            return {"status": "error", "reason": "Failed to delete preference."}

    async def delete_all_preferences(self, canonical_user_id: str) -> dict[str, Any]:
        """Delete all preferences for a user."""
        try:
            client = self._get_supabase()
            client._get_client().table("user_preferences").delete().eq(
                "canonical_user_id", canonical_user_id
            ).execute()

            self._invalidate_cache(canonical_user_id)

            LOGGER.info(f"Deleted all preferences for {canonical_user_id}")
            return {"status": "deleted", "message": "All preferences removed."}
        except Exception as e:
            LOGGER.error(f"Failed to delete all preferences: {e}")
            return {"status": "error", "reason": "Failed to delete preferences."}

    async def migrate_telegram_to_email(self, telegram_id: str, email: str) -> None:
        """Migrate preferences from telegram ID key to email key.

        Called when a user's email is first resolved from their telegram_id.
        Idempotent — safe to call multiple times. Uses a process-level cache
        to skip the DB call for users already checked this process lifetime.
        """
        cache_key = f"{telegram_id}:{email}"
        if cache_key in self._migrated_users:
            return

        tg_canonical = f"tg:{telegram_id}"
        try:
            client = self._get_supabase()

            # Check if there are telegram-keyed preferences to migrate
            existing = (
                client._get_client()
                .table("user_preferences")
                .select("id, preference_key")
                .eq("canonical_user_id", tg_canonical)
                .execute()
            )

            if not existing.data:
                self._migrated_users.add(cache_key)
                return

            # Check for conflicts — email may already have some preferences
            email_prefs = (
                client._get_client()
                .table("user_preferences")
                .select("preference_key")
                .eq("canonical_user_id", email)
                .execute()
            )
            email_keys = {p["preference_key"] for p in (email_prefs.data or [])}

            # Migrate non-conflicting preferences
            for pref in existing.data:
                if pref["preference_key"] not in email_keys:
                    client._get_client().table("user_preferences").update(
                        {"canonical_user_id": email}
                    ).eq("id", pref["id"]).execute()
                else:
                    # Conflict — delete the telegram-keyed version (email version wins)
                    client._get_client().table("user_preferences").delete().eq(
                        "id", pref["id"]
                    ).execute()

            # Invalidate caches for both old and new IDs
            self._invalidate_cache(tg_canonical)
            self._invalidate_cache(email)

            LOGGER.info(f"Migrated {len(existing.data)} preferences from {tg_canonical} to {email}")
        except Exception as e:
            LOGGER.warning(f"Preference migration failed (non-critical): {e}")

        # Mark as checked regardless of outcome (avoid retrying on every message)
        self._migrated_users.add(cache_key)


# Singleton
_preferences_instance: UserPreferencesService | None = None


def get_preferences_service() -> UserPreferencesService:
    """Get singleton UserPreferencesService instance."""
    global _preferences_instance
    if _preferences_instance is None:
        _preferences_instance = UserPreferencesService()
    return _preferences_instance
