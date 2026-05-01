"""Resolve authentication and permissions node for LangGraph.

This node handles permission resolution based on the request source:
- Telegram: Chat-based authentication (resolve org from chat_id)
- Scheduled: Use stored context from schedule creation
- Other: User-based authentication via email
"""

from typing import Any, Dict

from loguru import logger as LOGGER

from orchestrator.graphs.state import ConversationState
from shared.auth import get_auth_service
from shared.auth.auth_service import STAFF_ORG_ID, UserPermissions


def _get_preferences_service_safe():
    """Import preferences service safely; returns None on import failure."""
    try:
        from orchestrator.services.user_preferences_service import get_preferences_service

        return get_preferences_service()
    except Exception:
        return None


async def resolve_auth(state: ConversationState) -> Dict[str, Any]:
    """Resolve user permissions from auth database.

    This node:
    1. Determines auth method (chat-based vs user-based)
    2. Resolves permissions from auth database
    3. Handles scheduled execution context
    4. Updates user_context with resolved permissions

    Args:
        state: Current conversation state with initialized services

    Returns:
        State updates with user_permissions and updated user_context

    Raises:
        PermissionError: If organization cannot be resolved for the chat
    """
    # Use singleton auth service (not from state to avoid checkpointer issues)
    auth_service = get_auth_service()
    user_context = state.get("user_context")
    metadata = state.get("metadata", {})

    if not user_context:
        raise ValueError("user_context not provided")

    # Determine auth method
    is_scheduled_execution = metadata.get("scheduled_execution", False)

    if user_context.source == "telegram" and user_context.chat_id and not is_scheduled_execution:
        # Chat-based authentication - resolve org from chat_id
        lookup_chat_id = metadata.get("original_chat_id", user_context.chat_id)

        user_permissions = await auth_service.resolve_permissions_from_chat(
            chat_id=lookup_chat_id,
            topic_id=user_context.topic_id,
            user_id=user_context.user_id,
            telegram_id=user_context.user_id,
        )

        # Check if organization was found
        if not user_permissions.organization_ids:
            LOGGER.error(
                f"No organization found for chat_id={lookup_chat_id}, "
                f"topic_id={user_context.topic_id}"
            )
            error_msg = f"This chat is not authorized. Chat ID: {lookup_chat_id}"
            if user_context.topic_id:
                error_msg += f", Topic ID: {user_context.topic_id}"
            error_msg += "\n\nPlease contact support to register this chat."
            raise PermissionError(error_msg)

    elif metadata.get("staff_group_auth"):
        # Staff group auth bypass — user already verified as staff in handler.py
        staff_org_id = str(metadata.get("staff_group_organization_id", STAFF_ORG_ID))
        user_permissions = UserPermissions(
            user_id=user_context.user_id or "staff_group",
            email=user_context.user_email,
            organization_ids=[staff_org_id],
            is_staff=True,
        )
        LOGGER.info(f"Staff group auth: org={staff_org_id}, user={user_context.user_id}")

    elif is_scheduled_execution:
        # Use stored context from schedule creation
        scheduled_org_id = metadata.get("scheduled_organization_id")
        scheduled_is_staff = metadata.get("scheduled_is_staff", False)

        if scheduled_org_id:
            org_id_str = str(scheduled_org_id)
            user_permissions = UserPermissions(
                user_id=user_context.user_id or "scheduled",
                email=user_context.user_email,
                organization_ids=[org_id_str],
                grid_ids=metadata.get("grid_ids", []),
                meter_ids=metadata.get("meter_ids", []),
                roles=metadata.get("roles", []),
                is_admin=metadata.get("is_admin", False),
                is_staff=scheduled_is_staff,
            )
            LOGGER.info(
                f"Scheduled execution using stored context: org={scheduled_org_id}, "
                f"is_staff={scheduled_is_staff}, email={user_context.user_email}"
            )
        else:
            # Fallback to user-based lookup for legacy schedules
            LOGGER.warning(
                "Scheduled execution without stored org context, falling back to user lookup"
            )
            user_permissions = await auth_service.get_user_permissions(
                email=user_context.user_email, user_id=user_context.user_id
            )
    else:
        # User-based authentication via email
        user_permissions = await auth_service.get_user_permissions(
            email=user_context.user_email, user_id=user_context.user_id
        )

    # Update user_context with resolved permissions
    user_context.roles = user_permissions.roles
    user_context.organization_ids = user_permissions.organization_ids
    user_context.organization_name = user_permissions.organization_short_name
    user_context.grid_ids = user_permissions.grid_ids
    user_context.meter_ids = user_permissions.meter_ids
    user_context.is_admin = user_permissions.is_admin
    user_context.is_staff = user_permissions.is_staff

    # Update email if resolved from telegram_id
    if user_permissions.email:
        user_context.user_email = user_permissions.email

        # Migrate preferences from telegram ID key to email key (idempotent)
        if user_context.user_id:
            try:
                prefs_service = _get_preferences_service_safe()
                if prefs_service:
                    await prefs_service.migrate_telegram_to_email(
                        telegram_id=user_context.user_id, email=user_permissions.email
                    )
            except Exception as e:
                LOGGER.debug(f"Preference migration skipped: {e}")

    LOGGER.info(
        f"User {user_context.user_email} permissions: "
        f"orgs={len(user_context.organization_ids)}, "
        f"grids={len(user_context.grid_ids)}, "
        f"meters={len(user_context.meter_ids)}, "
        f"admin={user_context.is_admin}, "
        f"staff={user_context.is_staff}"
    )

    # Persist resolved organization_id to session (backfills the NULL from creation)
    if user_context.organization_ids and state.get("session_id"):
        try:
            from orchestrator.services.supabase_client import get_supabase_client

            supabase_client = get_supabase_client()
            org_id = int(user_context.organization_ids[0])
            await supabase_client.update_session_organization(
                session_id=state["session_id"],
                organization_id=org_id,
                organization_short_name=user_context.organization_name,
            )
        except Exception as e:
            LOGGER.debug(f"Session org_id update skipped: {e}")

    return {
        "user_permissions": {
            "user_id": user_permissions.user_id,
            "email": user_permissions.email,
            "organization_ids": user_permissions.organization_ids,
            "grid_ids": user_permissions.grid_ids,
            "meter_ids": user_permissions.meter_ids,
            "roles": user_permissions.roles,
            "is_admin": user_permissions.is_admin,
            "is_staff": user_permissions.is_staff,
        },
        "user_context": user_context,
    }
