"""
Authentication and Authorization Service

This service manages user authentication and permission resolution using a
read-only database connection that contains:
- accounts table: Maps telegram_id/email to user records
- organizations table: User's organization membership
- grids table: Grids accessible to users (RLS controlled)
- meters table: Meters accessible to users (RLS controlled)

Uses direct PostgreSQL connection with a dedicated readonly database user.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, List, Optional

from pydantic import BaseModel

from shared.utils.grid_matcher import find_best_grid_match
from shared.utils.logging import get_logger

# Staff organization ID — the org whose members get staff-mode access.
# Override with STAFF_ORG_ID env var (default: 2 for backward compatibility).
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))

# Name of the boolean column on the grids table that indicates whether generation
# at that site is managed by the operator. Set MANAGED_GENERATION_COLUMN to match
# your schema if you rename this column (value is interpolated into SQL, so use
# only valid PostgreSQL identifier characters).
MANAGED_GENERATION_COLUMN: str = os.getenv(
    "MANAGED_GENERATION_COLUMN", "is_generation_managed_by_nxt_grid"
)
if not __import__("re").fullmatch(r"[a-z][a-z0-9_]*", MANAGED_GENERATION_COLUMN):
    raise ValueError(
        f"MANAGED_GENERATION_COLUMN must be a valid SQL identifier, got: {MANAGED_GENERATION_COLUMN!r}"
    )


@dataclass
class GridNotificationTarget:
    """Resolved internal-Telegram destination for a grid notification."""

    grid_name: str
    chat_id: str
    topic_id: Optional[str] = None
    was_fuzzy: bool = False


@dataclass
class GridTelegramSources:
    """Telegram group/topic IDs associated with a grid.

    Consolidates O&M columns and telegram_config JSON into one structure.
    """

    om_chat_id: str = ""
    om_topic_id: str = ""
    logbook_chat_id: str = ""
    logbook_topic_id: str = ""

    def classify_source(self, chat_id: str, topic_id: str) -> tuple[str, str] | None:
        """Return (source_type, label_prefix) for a chat/topic pair, or None."""
        if self.om_chat_id and chat_id == self.om_chat_id and topic_id == self.om_topic_id:
            return ("om_topic", "O&M")
        if (
            self.logbook_chat_id
            and chat_id == self.logbook_chat_id
            and topic_id == self.logbook_topic_id
        ):
            return ("logbook_topic", "Logbook")
        return None

    @property
    def all_chat_ids(self) -> list[str]:
        """All non-empty chat IDs for session filtering."""
        return [cid for cid in [self.om_chat_id, self.logbook_chat_id] if cid]


def parse_telegram_config(raw: Any) -> dict:
    """Parse grids.telegram_config column value to dict.

    asyncpg returns JSONB as dict natively, but this handles edge cases.
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    return {}


LOGGER = get_logger(__name__, project_name="shared")

# Singleton instance for connection pool reuse across requests
_auth_service_instance: Optional["AuthService"] = None


def get_auth_service() -> "AuthService":
    """Get singleton AuthService instance with shared connection pool.

    This ensures the asyncpg connection pool is created once and reused
    across all requests, rather than creating a new pool per request.
    """
    global _auth_service_instance
    if _auth_service_instance is None:
        _auth_service_instance = AuthService()
    return _auth_service_instance


class UserPermissions(BaseModel):
    """User permissions resolved from auth database."""

    user_id: str
    email: Optional[str] = None
    organization_ids: List[str] = []
    organization_short_name: Optional[str] = None  # Short name for hashtags (e.g., "yourorg")
    grid_ids: List[str] = []
    meter_ids: List[str] = []
    roles: List[str] = []
    is_admin: bool = False
    is_staff: bool = False


class AuthService:
    """Service for user authentication and permission resolution."""

    def __init__(self) -> None:
        """
        Initialize auth service with direct PostgreSQL connection to readonly database.

        Required environment variables:
            AUTH_DB_HOST: Database host
            AUTH_DB_USER: Database user (should be a readonly role)
            AUTH_DB_PASSWORD: Database password
            AUTH_DB_PORT: Database port (default: 6543 for pooler)
            AUTH_DB_NAME: Database name (default: postgres)
        """
        # Direct PostgreSQL connection (only supported method)
        self._db_pool = None

        # Validate configuration
        required_vars = ["AUTH_DB_HOST", "AUTH_DB_USER", "AUTH_DB_PASSWORD"]
        missing = [v for v in required_vars if not os.getenv(v)]
        if missing:
            LOGGER.error(
                f"Auth service requires direct database connection. Missing env vars: {missing}"
            )
            raise ValueError(f"Missing required auth database configuration: {missing}")

        LOGGER.info("Auth service using direct PostgreSQL connection (readonly user)")

    async def _get_db_pool(self):
        """Get or create direct PostgreSQL connection pool (readonly role)."""
        if self._db_pool is None:
            try:
                import asyncpg

                # ssl="require" encrypts the connection and is compatible with
                # Supabase's connection pooler (which uses a self-signed cert chain
                # not trusted by the system CA store, so verify-full is not usable
                # without bundling Supabase's root CA).
                self._db_pool = await asyncpg.create_pool(
                    host=os.getenv("AUTH_DB_HOST"),
                    port=int(os.getenv("AUTH_DB_PORT", "6543")),
                    database=os.getenv("AUTH_DB_NAME", "postgres"),
                    user=os.getenv("AUTH_DB_USER"),
                    password=os.getenv("AUTH_DB_PASSWORD"),
                    ssl="require",
                    min_size=1,
                    max_size=10,
                    command_timeout=10,
                    statement_cache_size=0,  # Disable prepared statements for PgBouncer
                )
                LOGGER.info("PostgreSQL connection pool created for auth database")
            except ImportError:
                LOGGER.error("asyncpg not installed. Install with: pip install asyncpg")
                raise
            except Exception as e:
                LOGGER.error(f"Failed to create PostgreSQL connection pool: {e}")
                raise
        return self._db_pool

    async def _get_permissions_by_organization_direct(
        self, organization_id: str, user_id: str, telegram_id: Optional[str] = None
    ) -> UserPermissions:
        """
        Get user permissions based on organization ID using direct PostgreSQL connection.

        Args:
            organization_id: Organization ID to filter by
            user_id: User ID for logging/tracking
            telegram_id: Optional telegram ID to look up user email and staff status

        Returns:
            UserPermissions with organization-filtered grids and meters
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                organization_ids = [organization_id]

                # Optional: Look up user by telegram_id if provided
                user_email = None
                if telegram_id:
                    user_row = await conn.fetchrow(
                        """
                        SELECT email, organization_id
                        FROM public.accounts
                        WHERE telegram_id = $1
                        AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        telegram_id,
                    )

                    if user_row:
                        user_email = user_row["email"]
                        LOGGER.info(f"Found user {user_email} for telegram_id {telegram_id}")
                    else:
                        LOGGER.info(f"No user found for telegram_id {telegram_id}")

                # Staff mode is determined by the resolved organization (the group's org
                # in group chats, or the user's own org in 1:1 chats), not the user's
                # personal org. This ensures staff accessing a customer group get
                # customer-level access (tools, instructions, commands).
                is_staff = int(organization_id) == STAFF_ORG_ID

                # Get accessible grids filtered by organization_id (exclude soft-deleted)
                grid_rows = await conn.fetch(
                    "SELECT id FROM grids WHERE organization_id = $1 AND deleted_at IS NULL",
                    int(organization_id),
                )
                grid_ids = [str(row["id"]) for row in grid_rows]
                LOGGER.info(f"Organization {organization_id} has access to {len(grid_ids)} grids")

                # Get organization short name for context
                org_row = await conn.fetchrow(
                    "SELECT name FROM organizations WHERE id = $1", int(organization_id)
                )
                org_short_name = org_row["name"] if org_row else None

                # Meters are filtered at MCP tool execution time using organization_id
                # No need to pre-load all meter IDs here - MCP servers query by org
                meter_ids: List[str] = []

                permissions = UserPermissions(
                    user_id=user_id,
                    email=user_email,
                    organization_ids=organization_ids,
                    organization_short_name=org_short_name,
                    grid_ids=grid_ids,
                    meter_ids=meter_ids,
                    roles=[],
                    is_admin=False,
                    is_staff=is_staff,
                )

                LOGGER.info(
                    f"Resolved permissions for organization {organization_id}: "
                    f"{len(grid_ids)} grids, email={user_email}, is_staff={is_staff}"
                )

                return permissions

        except Exception as e:
            LOGGER.exception(f"Error getting permissions by organization (direct connection): {e}")
            return UserPermissions(user_id=user_id, email=None, organization_ids=[organization_id])

    async def _get_user_permissions_direct(
        self, email: str, user_id: Optional[str] = None
    ) -> UserPermissions:
        """
        Get user permissions using direct PostgreSQL connection.
        This uses the readonly role with minimal SELECT permissions.
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # Get user record
                # Note: roles and is_admin columns may not exist in all database schemas (e.g., snaplet)
                user_row = await conn.fetchrow(
                    """
                    SELECT id, email, organization_id
                    FROM public.accounts
                    WHERE email = $1
                    AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    email,
                )

                if not user_row:
                    LOGGER.warning(f"No user record found for email: {email}")
                    return UserPermissions(user_id=user_id or email, email=email)

                resolved_user_id = str(user_row["id"])
                organization_id = user_row["organization_id"]

                # Default to empty roles and not admin (columns may not exist in schema)
                roles: List[str] = []
                is_admin: bool = False

                organization_ids = [str(organization_id)] if organization_id else []

                # Staff users belong to organization_id = STAFF_ORG_ID (internal org)
                is_staff = organization_id == STAFF_ORG_ID

                # Get accessible grids filtered by organization_id (exclude soft-deleted)
                if organization_id:
                    grid_rows = await conn.fetch(
                        "SELECT id FROM grids WHERE organization_id = $1 AND deleted_at IS NULL",
                        organization_id,
                    )
                else:
                    grid_rows = []
                grid_ids = [str(row["id"]) for row in grid_rows]
                LOGGER.info(f"User {email} has access to {len(grid_ids)} grids")

                # Meters are filtered at MCP tool execution time using organization_id
                # No need to pre-load all meter IDs here - MCP servers query by org
                meter_ids: List[str] = []

                permissions = UserPermissions(
                    user_id=resolved_user_id,
                    email=email,
                    organization_ids=organization_ids,
                    grid_ids=grid_ids,
                    meter_ids=meter_ids,
                    roles=roles,
                    is_admin=is_admin,
                    is_staff=is_staff,
                )

                LOGGER.info(
                    f"Resolved permissions for {email} (direct): "
                    f"{len(organization_ids)} orgs, {len(grid_ids)} grids, "
                    f"roles={roles}, admin={is_admin}, is_staff={is_staff}"
                )

                return permissions

        except Exception as e:
            LOGGER.exception(f"Error getting user permissions (direct connection): {e}")
            return UserPermissions(user_id=user_id or email, email=email)

    async def get_organization_from_chat(
        self, chat_id: str, topic_id: Optional[str] = None
    ) -> Optional[str]:
        """
        Resolve organization ID from Telegram chat_id and optional topic_id.

        Checks in order:
        1. organizations.developer_group_telegram_chat_id = chat_id
        2. grids.internal_telegram_group_chat_id = chat_id AND
           grids.internal_telegram_group_thread_id = topic_id (when topic_id present)
        3. grids.telegram_config JSON logbook groups (when topic_id present)
        4. grids.internal_telegram_group_chat_id = chat_id alone (fallback for
           standalone @mentions that carry no message_thread_id)

        Args:
            chat_id: Telegram chat ID (group or supergroup)
            topic_id: Optional topic/thread ID within the chat

        Returns:
            Organization ID if found, None otherwise
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # First check: organizations table by chat_id
                org_row = await conn.fetchrow(
                    """
                    SELECT id::text
                    FROM public.organizations
                    WHERE developer_group_telegram_chat_id = $1
                    LIMIT 1
                    """,
                    chat_id,
                )

                if org_row:
                    org_id = org_row["id"]
                    LOGGER.info(f"Found organization {org_id} for chat_id {chat_id}")
                    return str(org_id)

                # Second check: grids table by chat_id and topic_id (O&M groups)
                if topic_id:
                    grid_row = await conn.fetchrow(
                        """
                        SELECT organization_id::text
                        FROM public.grids
                        WHERE internal_telegram_group_chat_id = $1
                        AND internal_telegram_group_thread_id = $2
                        LIMIT 1
                        """,
                        chat_id,
                        topic_id,
                    )

                    if grid_row:
                        org_id = grid_row["organization_id"]
                        LOGGER.info(
                            f"Found organization {org_id} for chat_id {chat_id}, "
                            f"topic_id {topic_id} (O&M group)"
                        )
                        return str(org_id)

                # Third check: grids.telegram_config JSON (Logbook groups)
                # NOTE: Logbook group membership is a security boundary — same as
                # O&M groups above. Anyone in the group resolves to the grid's org.
                if topic_id:
                    logbook_row = await conn.fetchrow(
                        """
                        SELECT organization_id::text
                        FROM public.grids
                        WHERE telegram_config->>'internal_logbook_chat_id' = $1
                        AND telegram_config->>'internal_logbook_topic_id' = $2
                        LIMIT 1
                        """,
                        chat_id,
                        topic_id,
                    )

                    if logbook_row:
                        org_id = logbook_row["organization_id"]
                        LOGGER.info(
                            f"Found organization {org_id} for chat_id {chat_id}, "
                            f"topic_id {topic_id} (Logbook group)"
                        )
                        return str(org_id)

                # Fourth check: grids table by chat_id alone (no topic_id).
                # Standalone @mentions in a grid group carry no message_thread_id,
                # so the topic-scoped checks above are skipped. Match the group by
                # chat_id only so those messages are not silently dropped.
                grid_row_no_topic = await conn.fetchrow(
                    """
                    SELECT organization_id::text
                    FROM public.grids
                    WHERE internal_telegram_group_chat_id = $1
                    LIMIT 1
                    """,
                    chat_id,
                )

                if grid_row_no_topic:
                    org_id = grid_row_no_topic["organization_id"]
                    LOGGER.info(
                        f"Found organization {org_id} for chat_id {chat_id} "
                        f"(O&M group, no topic_id)"
                    )
                    return str(org_id)

                LOGGER.warning(f"No organization found for chat_id {chat_id}, topic_id {topic_id}")
                return None

        except Exception as e:
            LOGGER.exception(f"Error resolving organization from chat: {e}")
            return None

    async def get_org_id_for_telegram_user(self, telegram_id: str) -> Optional[str]:
        """Public wrapper: telegram_id → organization_id lookup."""
        return await self._get_organization_from_telegram_id(telegram_id)

    async def _get_organization_from_telegram_id(self, telegram_id: str) -> Optional[str]:
        """
        Resolve organization ID from user's telegram_id.

        This is a fallback method for when chat-based lookup fails (e.g., direct messages).
        Looks up the user in the accounts table and returns their organization_id.

        Args:
            telegram_id: Telegram user ID

        Returns:
            Organization ID if found, None otherwise
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT organization_id::text
                    FROM public.accounts
                    WHERE telegram_id = $1
                    AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    telegram_id,
                )

                if row and row["organization_id"]:
                    org_id = row["organization_id"]
                    LOGGER.info(f"Found organization {org_id} for telegram_id {telegram_id}")
                    return str(org_id)
                else:
                    LOGGER.warning(f"No organization found for telegram_id {telegram_id}")
                    return None

        except Exception as e:
            LOGGER.exception(f"Error resolving organization from telegram_id: {e}")
            return None

    async def get_user_email(self, user_id: str, source: str = "telegram") -> Optional[str]:
        """
        Translate platform-specific user ID to email.

        Args:
            user_id: Platform user ID (telegram ID, etc.)
            source: Platform source (telegram, roam, web, api)

        Returns:
            User email if found, None otherwise
        """
        # Handle direct email sources
        if source in ("email", "api"):
            return user_id

        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                if source == "telegram":
                    query = "SELECT email FROM public.accounts WHERE telegram_id = $1 AND deleted_at IS NULL LIMIT 1"
                else:
                    query = "SELECT email FROM public.accounts WHERE id = $1 AND deleted_at IS NULL LIMIT 1"

                row = await conn.fetchrow(query, user_id)
                if row:
                    email = row["email"]
                    LOGGER.info(f"Resolved {source} user {user_id} to email {email}")
                    return str(email)

                LOGGER.warning(f"No user found for {source} ID: {user_id}")
                return None

        except Exception as e:
            LOGGER.exception(f"Error resolving user email: {e}")
            return None

    async def get_user_permissions(
        self, email: str, user_id: Optional[str] = None
    ) -> UserPermissions:
        """
        Get all permissions for a user based on their email.

        This queries:
        1. User record (organization membership, roles)
        2. Grids accessible via RLS
        3. Meters accessible via RLS

        Args:
            email: User email
            user_id: Optional platform user ID for logging

        Returns:
            UserPermissions object with all resolved permissions
        """
        return await self._get_user_permissions_direct(email, user_id)

    async def resolve_user_permissions(
        self, user_id: str, source: str = "telegram"
    ) -> UserPermissions:
        """
        Convenience method to translate user ID to email and get permissions.

        Args:
            user_id: Platform-specific user ID
            source: Platform source

        Returns:
            UserPermissions object
        """
        # First translate to email
        email = await self.get_user_email(user_id, source)

        if not email:
            LOGGER.warning(
                f"Could not resolve email for {source} user {user_id}, using limited permissions"
            )
            return UserPermissions(user_id=user_id, email=user_id)

        # Get full permissions
        return await self.get_user_permissions(email, user_id)

    async def resolve_permissions_from_chat(
        self, chat_id: str, topic_id: Optional[str], user_id: str, telegram_id: Optional[str] = None
    ) -> UserPermissions:
        """
        Resolve user permissions based on chat_id and optional topic_id.

        This is the new primary method for Telegram auth, replacing user-based lookup.
        Resolves organization from chat context, then loads all grids/meters for that org.
        If chat-based lookup fails, falls back to user telegram_id lookup.
        Optionally enriches with user email/staff status if telegram_id is provided.

        Args:
            chat_id: Telegram chat ID (group or supergroup)
            topic_id: Optional topic/thread ID within the chat
            user_id: User ID for tracking/logging purposes
            telegram_id: Optional telegram user ID to look up email and staff status

        Returns:
            UserPermissions object with organization-filtered permissions
        """
        # STEP 1: Resolve organization from chat context
        organization_id = await self.get_organization_from_chat(chat_id, topic_id)

        # STEP 1.5: Fallback to user telegram_id lookup if chat-based lookup failed
        if not organization_id and telegram_id:
            LOGGER.info(
                f"Chat-based auth failed for chat_id {chat_id}, topic_id {topic_id}. "
                f"Attempting fallback lookup via telegram_id {telegram_id}"
            )
            organization_id = await self._get_organization_from_telegram_id(telegram_id)

            if organization_id:
                LOGGER.info(
                    f"Found organization {organization_id} via telegram_id {telegram_id} fallback"
                )
            else:
                LOGGER.warning(f"Fallback lookup also failed for telegram_id {telegram_id}")

        if not organization_id:
            LOGGER.warning(
                f"Could not resolve organization for chat_id {chat_id}, "
                f"topic_id {topic_id}, telegram_id {telegram_id}. Returning empty permissions."
            )
            return UserPermissions(user_id=user_id, email=None)

        # Get permissions based on organization
        return await self._get_permissions_by_organization_direct(
            organization_id, user_id, telegram_id
        )

    async def get_first_active_org_user_email(self, organization_id: str) -> Optional[str]:
        """
        Get the email of the first active user in an organization.

        This is used for chat-based authentication where no specific user email is available,
        but MCP tools require a user_email for context. We look up the first active user
        in the organization to use their email for MCP tool calls.

        Args:
            organization_id: Organization ID to look up users for

        Returns:
            Email of first active user, or None if no users found
        """
        try:
            pool = await self._get_db_pool()
            if not pool:
                return None

            async with pool.acquire() as conn:
                result = await conn.fetchrow(
                    """
                    SELECT email
                    FROM accounts
                    WHERE organization_id = $1
                      AND deleted_at IS NULL
                      AND email IS NOT NULL
                      AND email != ''
                    ORDER BY created_at ASC
                    LIMIT 1
                    """,
                    int(organization_id),
                )

                if result:
                    email = result["email"]
                    LOGGER.info(f"Found first active org user for org {organization_id}: {email}")
                    return str(email)
                else:
                    LOGGER.warning(f"No active users found for organization {organization_id}")
                    return None

        except Exception as e:
            LOGGER.exception(f"Error getting first active org user for org {organization_id}: {e}")
            return None

    async def get_organization_short_name(self, organization_id: str) -> Optional[str]:
        """
        Get organization name for hashtag usage.

        Uses the 'name' column (short name for hashtags).
        Note: 'formal_name' contains the longer formal name.

        Args:
            organization_id: Organization ID

        Returns:
            Organization name if found, None otherwise
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT name
                    FROM public.organizations
                    WHERE id = $1
                    LIMIT 1
                    """,
                    int(organization_id),
                )

                if row and row["name"]:
                    name = row["name"]
                    LOGGER.info(f"Found name '{name}' for org {organization_id}")
                    return str(name)
                else:
                    LOGGER.warning(f"No name found for organization {organization_id}")
                    return None

        except Exception as e:
            LOGGER.exception(f"Error getting organization name: {e}")
            return None

    async def get_grid_names_for_organization(
        self,
        organization_id: Optional[str] = None,
        include_all: bool = False,
    ) -> List[str]:
        """
        Get grid names accessible to an organization.

        Args:
            organization_id: Filter by org ID (required unless include_all=True)
            include_all: If True, return all visible grids (for staff mode)

        Returns:
            List of grid names, sorted alphabetically
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                if include_all:
                    rows = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        ORDER BY name
                        """
                    )
                else:
                    if not organization_id:
                        LOGGER.warning("get_grid_names_for_organization called without org_id")
                        return []
                    rows = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        AND organization_id = $1
                        ORDER BY name
                        """,
                        int(organization_id),
                    )

                names = [row["name"] for row in rows if row["name"]]
                LOGGER.info(
                    f"Found {len(names)} grid names "
                    f"(include_all={include_all}, org_id={organization_id})"
                )
                return names

        except Exception as e:
            LOGGER.exception(f"Error getting grid names: {e}")
            return []

    async def get_grid_portal_id(self, grid_name: str) -> Optional[str]:
        """
        Get VRM portal ID (generation_external_gateway_id) for a grid by name.

        This is used by equipment control to look up the VRM gateway ID
        needed to send MQTT commands to Victron equipment.

        Supports fuzzy matching: if exact match fails, tries to find the closest
        matching grid name (handles typos/misspellings).

        Args:
            grid_name: Name of the grid (case-insensitive, fuzzy matched)

        Returns:
            Portal ID string if found, None otherwise
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # First try exact case-insensitive match (fast path)
                row = await conn.fetchrow(
                    """
                    SELECT name, generation_external_gateway_id
                    FROM public.grids
                    WHERE LOWER(name) = LOWER($1)
                    LIMIT 1
                    """,
                    grid_name,
                )

                # If no exact match, try fuzzy matching
                if not row:
                    LOGGER.info(f"No exact match for grid '{grid_name}', trying fuzzy match...")

                    # Get all grid names for fuzzy matching
                    all_grids = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        """
                    )
                    valid_names = [r["name"] for r in all_grids if r["name"]]

                    if valid_names:
                        from shared.utils.grid_matcher import find_best_grid_match

                        matched_name, was_fuzzy, score = find_best_grid_match(
                            grid_name, valid_names
                        )

                        if matched_name and was_fuzzy:
                            LOGGER.info(
                                f"Fuzzy matched '{grid_name}' -> '{matched_name}' (score: {score}%)"
                            )
                            # Query with the corrected name
                            row = await conn.fetchrow(
                                """
                                SELECT name, generation_external_gateway_id
                                FROM public.grids
                                WHERE name = $1
                                LIMIT 1
                                """,
                                matched_name,
                            )

                if not row:
                    LOGGER.warning(f"Grid not found: {grid_name}")
                    return None

                portal_id = row["generation_external_gateway_id"]
                actual_name = row["name"]
                if portal_id:
                    LOGGER.info(f"Found portal_id {portal_id} for grid '{actual_name}'")
                    return str(portal_id)
                else:
                    LOGGER.warning(f"Grid '{actual_name}' has no generation_external_gateway_id")
                    return None

        except Exception as e:
            LOGGER.exception(f"Error looking up grid portal_id: {e}")
            return None

    async def get_grid_vrm_ids(
        self, grid_name: str
    ) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
        """
        Get VRM IDs for a grid by name.

        VRM uses two different IDs:
        - generation_external_site_id: For VRM REST API calls (checking if site is online)
        - generation_external_gateway_id: For MQTT commands (actual equipment control)

        Also returns whether the grid's generation is managed by the operator.
        If False, equipment diagnostics and control should not be available.

        Supports fuzzy matching: if exact match fails, tries to find the closest
        matching grid name (handles typos/misspellings).

        Args:
            grid_name: Name of the grid (case-insensitive, fuzzy matched)

        Returns:
            Tuple of (site_id, gateway_id, actual_grid_name, is_generation_managed)
            - site_id, gateway_id, actual_grid_name may be None if not found
            - is_generation_managed is False if grid not found or flag is NULL/False
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # First try exact case-insensitive match (fast path)
                row = await conn.fetchrow(
                    f"""
                    SELECT name, generation_external_site_id, generation_external_gateway_id,
                           {MANAGED_GENERATION_COLUMN}
                    FROM public.grids
                    WHERE LOWER(name) = LOWER($1)
                    LIMIT 1
                    """,
                    grid_name,
                )

                # If no exact match, try fuzzy matching
                if not row:
                    LOGGER.info(f"No exact match for grid '{grid_name}', trying fuzzy match...")

                    # Get all grid names for fuzzy matching
                    all_grids = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        """
                    )
                    valid_names = [r["name"] for r in all_grids if r["name"]]

                    if valid_names:
                        from shared.utils.grid_matcher import find_best_grid_match

                        matched_name, was_fuzzy, score = find_best_grid_match(
                            grid_name, valid_names
                        )

                        if matched_name and was_fuzzy:
                            LOGGER.info(
                                f"Fuzzy matched '{grid_name}' -> '{matched_name}' (score: {score}%)"
                            )
                            # Query with the corrected name
                            row = await conn.fetchrow(
                                f"""
                                SELECT name, generation_external_site_id, generation_external_gateway_id,
                                       {MANAGED_GENERATION_COLUMN}
                                FROM public.grids
                                WHERE name = $1
                                LIMIT 1
                                """,
                                matched_name,
                            )

                if not row:
                    LOGGER.warning(f"Grid not found: {grid_name}")
                    return (None, None, None, False)

                actual_name = row["name"]
                site_id = row["generation_external_site_id"]
                gateway_id = row["generation_external_gateway_id"]
                is_managed = bool(row[MANAGED_GENERATION_COLUMN])

                # Convert to strings if not None
                site_id_str = str(site_id) if site_id else None
                gateway_id_str = str(gateway_id) if gateway_id else None

                LOGGER.info(
                    f"Found VRM IDs for grid '{actual_name}': "
                    f"site_id={site_id_str is not None}, gateway_id={gateway_id_str is not None}, "
                    f"is_generation_managed={is_managed}"
                )
                return (site_id_str, gateway_id_str, actual_name, is_managed)

        except Exception as e:
            LOGGER.exception(f"Error looking up grid VRM IDs: {e}")
            return (None, None, None, False)

    async def is_grid_generation_managed(self, grid_name: str) -> bool:
        """
        Check if a grid's generation is managed by the operator.

        This is a convenience method that extracts just the is_generation_managed flag
        from get_grid_vrm_ids(). Use this when you only need to check the flag
        without needing the VRM IDs.

        Args:
            grid_name: Name of the grid (case-insensitive, fuzzy matched)

        Returns:
            True if the grid's generation is managed by the operator, False otherwise
            (including if the grid is not found)
        """
        _, _, _, is_managed = await self.get_grid_vrm_ids(grid_name)
        return is_managed

    async def get_dcu_status_by_grid_name(self, grid_name: str) -> list[dict]:
        """
        Get DCU online/offline status for a grid by name.

        Resolves grid name (exact + fuzzy match) to grid id, then queries
        the dcus table for external_reference, is_online, and last_online_at.

        Args:
            grid_name: Name of the grid (case-insensitive, fuzzy matched)

        Returns:
            List of dicts with keys: external_reference, is_online, last_online_at.
            Returns empty list on failure or if grid not found.
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # First try exact case-insensitive match
                row = await conn.fetchrow(
                    """
                    SELECT id FROM public.grids
                    WHERE LOWER(name) = LOWER($1)
                    LIMIT 1
                    """,
                    grid_name,
                )

                # If no exact match, try fuzzy matching
                if not row:
                    LOGGER.info(
                        f"No exact match for grid '{grid_name}' (DCU lookup), trying fuzzy match..."
                    )
                    all_grids = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        """
                    )
                    valid_names = [r["name"] for r in all_grids if r["name"]]

                    if valid_names:
                        from shared.utils.grid_matcher import find_best_grid_match

                        matched_name, was_fuzzy, score = find_best_grid_match(
                            grid_name, valid_names
                        )
                        if matched_name:
                            LOGGER.info(
                                f"Fuzzy matched '{grid_name}' -> '{matched_name}' "
                                f"(score: {score}%) for DCU lookup"
                            )
                            row = await conn.fetchrow(
                                "SELECT id FROM public.grids WHERE name = $1 LIMIT 1",
                                matched_name,
                            )

                if not row:
                    LOGGER.warning(f"Grid not found for DCU lookup: {grid_name}")
                    return []

                grid_id = row["id"]

                # Query DCUs for this grid
                dcu_rows = await conn.fetch(
                    """
                    SELECT external_reference, is_online, last_online_at
                    FROM public.dcus
                    WHERE grid_id = $1
                    ORDER BY external_reference
                    """,
                    grid_id,
                )

                return [
                    {
                        "external_reference": r["external_reference"],
                        "is_online": bool(r["is_online"]) if r["is_online"] is not None else False,
                        "last_online_at": (
                            r["last_online_at"].isoformat() if r["last_online_at"] else None
                        ),
                    }
                    for r in dcu_rows
                ]

        except Exception as e:
            LOGGER.exception(f"Error getting DCU status for grid '{grid_name}': {e}")
            return []

    async def get_all_grid_names(self) -> List[str]:
        """
        Get all valid grid names for suggestions/autocomplete.

        Returns:
            List of grid names (excluding hidden grids)
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT name FROM public.grids
                    WHERE is_hidden_from_reporting IS NOT TRUE
                    AND deleted_at IS NULL
                    ORDER BY name
                    """
                )
                return [r["name"] for r in rows if r["name"]]

        except Exception as e:
            LOGGER.exception(f"Error getting all grid names: {e}")
            return []

    async def get_eligible_grids_for_agents(self) -> List[dict]:
        """Get all grids eligible for auto-provisioned agents.

        Eligibility: not deleted, not hidden, has a telegram chat configured.
        Returns grid metadata needed for instance creation.
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT
                        g.id, g.name, g.organization_id,
                        o.name AS organization_name,
                        g.internal_telegram_group_chat_id,
                        g.internal_telegram_group_thread_id,
                        g.generation_external_site_id,
                        g.generation_external_gateway_id
                    FROM grids g
                    LEFT JOIN organizations o ON o.id = g.organization_id
                    WHERE g.deleted_at IS NULL
                      AND g.is_hidden_from_reporting IS NOT TRUE
                      AND g.internal_telegram_group_chat_id IS NOT NULL
                    ORDER BY g.name
                    """
                )
                return [dict(r) for r in rows]
        except Exception as e:
            LOGGER.exception(f"Error getting eligible grids for agents: {e}")
            return []

    async def get_grid_telegram_sources(
        self, grid_name: str, organization_id: int
    ) -> GridTelegramSources:
        """Get all Telegram group/topic IDs associated with a grid.

        Returns a GridTelegramSources with O&M and Logbook chat/topic IDs.
        Falls back to empty sources on any error.
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT internal_telegram_group_chat_id,
                           internal_telegram_group_thread_id,
                           telegram_config
                    FROM grids
                    WHERE name = $1 AND organization_id = $2 AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    grid_name,
                    organization_id,
                )
                if not row:
                    return GridTelegramSources()

                tg_config = parse_telegram_config(row["telegram_config"])
                return GridTelegramSources(
                    om_chat_id=str(row["internal_telegram_group_chat_id"] or ""),
                    om_topic_id=str(row["internal_telegram_group_thread_id"] or ""),
                    logbook_chat_id=str(tg_config.get("internal_logbook_chat_id", "")),
                    logbook_topic_id=str(tg_config.get("internal_logbook_topic_id", "")),
                )
        except Exception as e:
            LOGGER.warning(f"Error getting grid telegram sources for {grid_name}: {e}")
            return GridTelegramSources()

    async def resolve_grid_notification_target(
        self, grid_name: str
    ) -> Optional[GridNotificationTarget]:
        """Resolve a grid name to its internal Telegram group chat + topic.

        Matches ``grid_name`` against all non-deleted grids that have an internal
        Telegram group configured, using the same fuzzy matcher as the rest of the
        app (exact first, then rapidfuzz at the 80% threshold with the ambiguity
        guard). Returns ``None`` when there is no confident match — callers should
        treat that as an undeliverable notification rather than guessing.

        Grid names are effectively unique per deployment; if two share a name, the
        first row wins (both map to distinct groups, so an internal alert still
        lands in a valid one).

        Returns ``None`` only for a genuine no-match. Infrastructure failures
        (DB unavailable, etc.) propagate so callers can distinguish "unknown grid"
        from "try again later".
        """
        if not grid_name or not grid_name.strip():
            return None

        pool = await self._get_db_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT name,
                       internal_telegram_group_chat_id AS chat_id,
                       internal_telegram_group_thread_id AS topic_id
                FROM public.grids
                WHERE deleted_at IS NULL
                  AND internal_telegram_group_chat_id IS NOT NULL
                """
            )

        by_name: dict[str, Any] = {}
        for r in rows:
            if r["name"]:
                by_name.setdefault(r["name"], r)

        matched, was_fuzzy, _score = find_best_grid_match(grid_name, list(by_name.keys()))
        if not matched:
            LOGGER.warning(
                "resolve_grid_notification_target: no confident grid match for %r", grid_name
            )
            return None

        row = by_name[matched]
        topic_id = row["topic_id"]
        return GridNotificationTarget(
            grid_name=matched,
            chat_id=str(row["chat_id"]),
            topic_id=str(topic_id) if topic_id is not None else None,
            was_fuzzy=was_fuzzy,
        )

    async def get_all_logbook_grid_mapping(self) -> dict[tuple[str, str], str]:
        """Get mapping of (logbook_chat_id, logbook_topic_id) → grid_name.

        Used by the admin UI to resolve logbook topics to grid names.
        """
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT name,
                           telegram_config->>'internal_logbook_chat_id' AS logbook_chat_id,
                           telegram_config->>'internal_logbook_topic_id' AS logbook_topic_id
                    FROM grids
                    WHERE telegram_config->>'internal_logbook_chat_id' IS NOT NULL
                    AND deleted_at IS NULL
                    """
                )
                return {
                    (str(r["logbook_chat_id"]), str(r["logbook_topic_id"])): r["name"]
                    for r in rows
                    if r["logbook_chat_id"] and r["logbook_topic_id"]
                }
        except Exception as e:
            LOGGER.warning(f"Error getting logbook grid mapping: {e}")
            return {}

    async def get_grid_timezone(self, grid_name: str) -> str:
        """
        Get timezone for a grid by name.

        Supports fuzzy matching: if exact match fails, tries to find the closest
        matching grid name (handles typos/misspellings).

        Args:
            grid_name: Name of the grid (case-insensitive, fuzzy matched)

        Returns:
            IANA timezone string (e.g., 'Africa/Lagos'). Defaults to DEFAULT_TIMEZONE if not found.
        """
        default_tz = os.getenv("DEFAULT_TIMEZONE", "UTC")
        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                # First try exact case-insensitive match (fast path)
                row = await conn.fetchrow(
                    """
                    SELECT timezone
                    FROM public.grids
                    WHERE LOWER(name) = LOWER($1)
                    LIMIT 1
                    """,
                    grid_name,
                )

                # If no exact match, try fuzzy matching
                if not row:
                    all_grids = await conn.fetch(
                        """
                        SELECT name FROM public.grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                        """
                    )
                    valid_names = [r["name"] for r in all_grids if r["name"]]

                    if valid_names:
                        from shared.utils.grid_matcher import find_best_grid_match

                        matched_name, was_fuzzy, score = find_best_grid_match(
                            grid_name, valid_names
                        )

                        if matched_name:
                            row = await conn.fetchrow(
                                """
                                SELECT timezone
                                FROM public.grids
                                WHERE name = $1
                                LIMIT 1
                                """,
                                matched_name,
                            )

                if row and row["timezone"]:
                    return str(row["timezone"])
                return default_tz

        except Exception as e:
            LOGGER.exception(f"Error looking up grid timezone: {e}")
            return default_tz

    async def batch_lookup_display_names(self, telegram_ids: List[str]) -> dict:
        """
        Batch lookup display names for telegram IDs (users and organizations).

        For user IDs: looks up full_name/email from accounts table
        For group IDs (starting with -100 or 100): looks up org name from organizations table

        Args:
            telegram_ids: List of telegram IDs to look up

        Returns:
            Dict mapping telegram_id -> display_name
        """
        if not telegram_ids:
            return {}

        try:
            pool = await self._get_db_pool()
            async with pool.acquire() as conn:
                name_map: dict = {}

                # Separate user IDs from group IDs (groups start with "-100" or "100")
                user_ids = [
                    tid
                    for tid in telegram_ids
                    if not (tid.startswith("-100") or tid.startswith("100"))
                ]
                group_ids = [
                    tid for tid in telegram_ids if tid.startswith("-100") or tid.startswith("100")
                ]

                LOGGER.debug(f"Batch lookup: {len(user_ids)} users, {len(group_ids)} groups")

                # Lookup users from accounts table
                if user_ids:
                    user_rows = await conn.fetch(
                        """
                        SELECT telegram_id, full_name, email
                        FROM public.accounts
                        WHERE telegram_id = ANY($1::text[])
                        AND deleted_at IS NULL
                        """,
                        user_ids,
                    )

                    for row in user_rows:
                        telegram_id = row["telegram_id"]
                        name = row["full_name"] or (
                            row["email"].split("@")[0] if row["email"] else None
                        )
                        if telegram_id and name:
                            name_map[telegram_id] = name

                    LOGGER.debug(f"Found {len(user_rows)} user names")

                # Lookup organizations from organizations table
                if group_ids:
                    # Strip suffix like _12 from group IDs for lookup
                    # Database stores IDs WITH minus sign (e.g., "-100326488")
                    base_ids = [gid.split("_")[0] for gid in group_ids]
                    base_to_original = {gid.split("_")[0]: gid for gid in group_ids}

                    org_rows = await conn.fetch(
                        """
                        SELECT developer_group_telegram_chat_id, name
                        FROM public.organizations
                        WHERE developer_group_telegram_chat_id = ANY($1::text[])
                        AND deleted_at IS NULL
                        """,
                        base_ids,
                    )

                    for row in org_rows:
                        chat_id = row["developer_group_telegram_chat_id"]
                        org_name = row["name"]
                        if chat_id and org_name:
                            # Map back to original ID with suffix if applicable
                            original_id = base_to_original.get(chat_id, chat_id)
                            name_map[original_id] = org_name

                    LOGGER.debug(f"Found {len(org_rows)} organization names")

                    # Resolve O&M groups from grids table
                    unresolved_ids = [
                        bid for bid in base_ids if base_to_original.get(bid, bid) not in name_map
                    ]
                    if unresolved_ids:
                        grid_rows = await conn.fetch(
                            """
                            SELECT DISTINCT internal_telegram_group_chat_id::text
                            FROM grids
                            WHERE internal_telegram_group_chat_id::text = ANY($1::text[])
                            AND deleted_at IS NULL
                            """,
                            unresolved_ids,
                        )
                        for row in grid_rows:
                            chat_id = str(row["internal_telegram_group_chat_id"])
                            original_id = base_to_original.get(chat_id, chat_id)
                            name_map[original_id] = "O&M Group"

                    # Resolve Logbook groups from grids.telegram_config
                    still_unresolved = [
                        bid for bid in base_ids if base_to_original.get(bid, bid) not in name_map
                    ]
                    if still_unresolved:
                        logbook_rows = await conn.fetch(
                            """
                            SELECT DISTINCT
                                telegram_config->>'internal_logbook_chat_id' AS logbook_chat_id
                            FROM grids
                            WHERE telegram_config->>'internal_logbook_chat_id' = ANY($1::text[])
                            AND deleted_at IS NULL
                            """,
                            still_unresolved,
                        )
                        for row in logbook_rows:
                            chat_id = str(row["logbook_chat_id"])
                            original_id = base_to_original.get(chat_id, chat_id)
                            name_map[original_id] = "Logbook"

                LOGGER.info(
                    f"Batch lookup resolved {len(name_map)} names from {len(telegram_ids)} IDs"
                )
                return name_map

        except Exception as e:
            LOGGER.exception(f"Error batch looking up display names: {e}")
            return {}


__all__ = ["AuthService", "UserPermissions", "get_auth_service"]
