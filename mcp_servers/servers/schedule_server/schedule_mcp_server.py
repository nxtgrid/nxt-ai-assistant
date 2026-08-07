#!/usr/bin/env python3
"""
Schedule MCP Server

Provides tools for users to schedule commands (like /tickets, /grid) for future
or recurring execution. Results are posted to the originating chat.

This server is STAFF ONLY - customers do not have access to scheduling.

SECURITY MODEL:
- The chat_id, topic_id, user_email, and organization_id are injected by the
  tool_executor from the webhook request metadata - NOT from LLM-provided arguments
- The LLM can only control the tool schema parameters (command, time_expression, timezone)
- The LLM CANNOT specify which chat to send results to
- Schedules are always created for the chat where the command was issued
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from supabase import Client, create_client  # type: ignore[attr-defined]

# Load environment variables
load_dotenv()

from shared_code.tool_registry import ToolRegistry  # noqa: E402

from shared.config.db_credentials import (  # noqa: E402  (must follow load_dotenv)
    chat_db_service_key,
    chat_db_url,
)
from shared.scheduling.recurrence import (  # noqa: E402  (must follow load_dotenv)
    format_schedule_display,
    parse_time_expression,
)

from .tool_schemas import TOOL_SCHEMAS  # noqa: E402

# Configure logging to stderr
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("schedule-mcp-server")

print("📅 Schedule MCP Server starting...", file=sys.stderr)

server = Server("schedule-server")
registry = ToolRegistry("schedule")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}

# Staff organization ID (controls staff-only schedule features)
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))

# Supabase client
_supabase: Optional[Client] = None


def get_supabase() -> Optional[Client]:
    """Get or create Supabase client."""
    global _supabase
    if _supabase is None:
        url = chat_db_url()
        key = chat_db_service_key()
        if url and key:
            _supabase = create_client(url, key)
    return _supabase


# Default timezone
DEFAULT_TIMEZONE = os.getenv("DEFAULT_TIMEZONE", "UTC")

# Maximum schedules per chat
MAX_SCHEDULES_PER_CHAT = 20


@registry.pre_dispatch
async def _inject_context(name: str, arguments: Dict[str, Any]) -> Optional[List[types.TextContent]]:
    """Build chat_id/topic_id/user_context/supabase from tool_executor-injected
    fields and stash them on `arguments` for handlers to read.

    SECURITY: these fields are injected by the tool_executor from webhook
    request metadata — NOT visible to or controllable by the LLM. Every
    schedule tool requires them, so this ran unconditionally before every
    dispatch even when the migration to ToolRegistry moved the branches
    into separate functions; preserved here verbatim as a pre_dispatch hook.
    """
    chat_id = arguments.get("chat_id", "")  # Injected by tool_executor
    topic_id = arguments.get("topic_id")  # Injected by tool_executor
    user_email = arguments.get("user_email", "")  # Injected by tool_executor
    organization_id = arguments.get("organization_id")  # Injected by tool_executor
    session_id = arguments.get("session_id", "")  # Injected by tool_executor

    # Build user context from injected values (not from LLM-provided data)
    user_context = {
        "user_id": user_email or session_id,  # Use email as user_id, fallback to session
        "user_email": user_email,
        "organization_ids": [str(organization_id)] if organization_id else [],
        "is_staff": True,  # Schedule command is staff-only
        "source": "telegram",
    }

    if not chat_id:
        return [
            types.TextContent(
                type="text",
                text="Error: Could not determine chat ID. This tool must be called from a chat context.",
            )
        ]

    supabase = get_supabase()
    if not supabase:
        return [
            types.TextContent(
                type="text",
                text="Error: Database not configured",
            )
        ]

    arguments["_chat_id"] = chat_id
    arguments["_topic_id"] = topic_id
    arguments["_user_context"] = user_context
    arguments["_supabase"] = supabase
    return None


@registry.tool(
    "schedule_user_command",
    _SCHEMAS_BY_NAME["schedule_user_command"],
    aliases=("user_command",),
)
async def _tool_schedule_user_command(arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_schedule_command(
        arguments["_supabase"],
        arguments,
        arguments["_user_context"],
        arguments["_chat_id"],
        arguments["_topic_id"],
    )


@registry.tool(
    "list_user_schedules",
    _SCHEMAS_BY_NAME["list_user_schedules"],
    aliases=("user_schedules",),
)
async def _tool_list_user_schedules(arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_list_schedules(
        arguments["_supabase"], arguments, arguments["_chat_id"], arguments["_topic_id"]
    )


@registry.tool(
    "cancel_user_schedule",
    _SCHEMAS_BY_NAME["cancel_user_schedule"],
    aliases=("user_schedule",),
)
async def _tool_cancel_user_schedule(arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_cancel_schedule(arguments["_supabase"], arguments, arguments["_chat_id"])


@registry.tool("pause_user_schedule", _SCHEMAS_BY_NAME["pause_user_schedule"])
async def _tool_pause_user_schedule(arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_pause_schedule(arguments["_supabase"], arguments, arguments["_chat_id"])


@registry.tool("resume_user_schedule", _SCHEMAS_BY_NAME["resume_user_schedule"])
async def _tool_resume_user_schedule(arguments: Dict[str, Any]) -> List[types.TextContent]:
    return await handle_resume_schedule(arguments["_supabase"], arguments, arguments["_chat_id"])


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def handle_schedule_command(
    supabase: Client,
    arguments: Dict[str, Any],
    user_context: Dict[str, Any],
    chat_id: str,
    topic_id: Optional[str],
) -> List[types.TextContent]:
    """Handle schedule_user_command tool."""
    from uuid import uuid4

    import pytz  # type: ignore[import-untyped]

    # Accept both "message" (new) and "command" (legacy) field names
    command = arguments.get("message") or arguments.get("command", "")
    time_expression = arguments.get("time_expression", "")
    tz_str = arguments.get("timezone", DEFAULT_TIMEZONE)

    if not command:
        return [types.TextContent(type="text", text="Error: message is required")]

    if not time_expression:
        return [types.TextContent(type="text", text="Error: time_expression is required")]

    # Check rate limit
    try:
        count_result = (
            supabase.table("user_schedules")
            .select("id", count="exact")
            .eq("chat_id", chat_id)
            .eq("is_active", True)
            .execute()
        )
        current_count = count_result.count if count_result.count else 0
        if current_count >= MAX_SCHEDULES_PER_CHAT:
            return [
                types.TextContent(
                    type="text",
                    text=f"Error: Maximum {MAX_SCHEDULES_PER_CHAT} schedules per chat. Please cancel some schedules first.",
                )
            ]
    except Exception as e:
        logger.warning(f"Could not check schedule count: {e}")

    # Parse time expression
    try:
        cron_expression, next_run_at, schedule_type = parse_time_expression(time_expression, tz_str)
    except ValueError as e:
        return [types.TextContent(type="text", text=str(e))]

    # Validate that scheduled time is at least 2 minutes in the future
    now = datetime.now(timezone.utc)
    min_schedule_time = now + timedelta(minutes=2)
    if next_run_at < min_schedule_time:
        tz = pytz.timezone(tz_str)
        local_time = next_run_at.astimezone(tz)
        return [
            types.TextContent(
                type="text",
                text=f"Error: Cannot schedule for {local_time.strftime('%I:%M %p')}. Schedules must be at least 2 minutes in the future.",
            )
        ]

    # Generate friendly name
    msg_short = command[:30] + "..." if len(command) > 30 else command
    # Use quotes for non-command messages to distinguish from slash commands
    msg_display = msg_short if msg_short.startswith("/") else f'"{msg_short}"'
    if schedule_type in ("recurring", "biweekly"):
        friendly_name = f"{time_expression.title()} - {msg_display}"
    else:
        friendly_name = f"Once: {time_expression.title()} - {msg_display}"

    # Serialize user context
    # SECURITY: is_staff is derived from the CHAT's organization (org 2 = staff),
    # not from the user's personal staff status. This ensures a staff user
    # scheduling from a customer group gets customer permissions.
    org_ids = user_context.get("organization_ids", [])
    chat_org_id = org_ids[0] if org_ids else None
    # Staff org is determined by STAFF_ORG_ID env var - use chat's org to determine is_staff
    is_staff_for_schedule = (int(chat_org_id) == STAFF_ORG_ID) if chat_org_id else False

    user_context_json = {
        "user_id": user_context.get("user_id", ""),
        "user_email": user_context.get("user_email", ""),
        "username": user_context.get("username"),
        "source": user_context.get("source", "telegram"),
        "roles": user_context.get("roles", []),
        "organization_ids": org_ids,
        "grid_ids": user_context.get("grid_ids", []),
        "meter_ids": user_context.get("meter_ids", []),
        "is_admin": user_context.get("is_admin", False),
        "is_staff": is_staff_for_schedule,
    }

    # Insert schedule
    schedule_id = str(uuid4())

    schedule_data = {
        "id": schedule_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "created_by_user_id": user_context.get("user_id", ""),
        "created_by_email": user_context.get("user_email", ""),
        "organization_id": int(org_ids[0]) if org_ids else None,
        "command": command,
        "schedule_type": schedule_type,
        "cron_expression": cron_expression,
        "timezone": tz_str,
        "next_run_at": next_run_at.isoformat(),
        "is_active": True,
        "status": "active",
        "friendly_name": friendly_name,
        "user_context": user_context_json,
    }

    result = supabase.table("user_schedules").insert(schedule_data).execute()

    if not result.data:
        return [types.TextContent(type="text", text="Error: Failed to create schedule")]

    # Queue first execution
    payload = {
        "schedule_id": schedule_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "command": command,
        "user_context": user_context_json,
    }

    supabase.table("scheduled_messages").insert(
        {
            "message_type": "user_command",
            "payload": payload,
            "scheduled_for": next_run_at.isoformat(),
            "created_by": user_context.get("user_email", ""),
            "status": "pending",
        }
    ).execute()

    # Format response
    display = format_schedule_display(schedule_type, cron_expression, next_run_at, tz_str)

    tz = pytz.timezone(tz_str)
    local_next = next_run_at.astimezone(tz)

    response = f"""✅ Schedule created!

**{friendly_name}**

• Type: {schedule_type.title()}
• Message: `{command}`
• {display}
• Next run: {local_next.strftime("%b %d, %Y at %I:%M %p")} {tz.zone}
• ID: `{schedule_id[:8]}`

To cancel: "cancel schedule {schedule_id[:8]}"
To list all: /schedule"""

    logger.info(f"Created schedule {schedule_id}: {command} ({schedule_type})")

    return [types.TextContent(type="text", text=response)]


async def handle_list_schedules(
    supabase: Client,
    arguments: Dict[str, Any],
    chat_id: str,
    topic_id: Optional[str],
) -> List[types.TextContent]:
    """Handle list_user_schedules tool."""
    import pytz  # type: ignore[import-untyped]

    include_inactive = arguments.get("include_inactive", False)

    query = supabase.table("user_schedules").select("*").eq("chat_id", chat_id)

    if not include_inactive:
        query = query.eq("is_active", True).eq("status", "active")

    result = query.order("created_at", desc=True).execute()
    schedules = list(result.data) if result.data else []

    if not schedules:
        return [
            types.TextContent(
                type="text",
                text='📅 No scheduled messages for this chat.\n\nTo create one: "/schedule daily at 9am /tickets" or "/schedule daily at 9am show me the grid status"',
            )
        ]

    lines = ["📅 **Scheduled Commands**\n"]

    for i, schedule in enumerate(schedules, 1):
        next_run = schedule.get("next_run_at")
        if next_run:
            next_run_dt = datetime.fromisoformat(next_run.replace("Z", "+00:00"))
            tz = pytz.timezone(schedule.get("timezone", DEFAULT_TIMEZONE))
            local_next = next_run_dt.astimezone(tz)
            next_str = local_next.strftime("%b %d at %I:%M %p")
        else:
            next_str = "N/A"

        status = schedule.get("status", "active")
        status_icon = "✅" if status == "active" else "⏸️" if status == "paused" else "✓"

        lines.append(f"{i}. **{schedule.get('friendly_name', 'Unnamed')}**")
        lines.append(f"   Message: `{schedule.get('command', '')}`")
        lines.append(f"   Next: {next_str}")
        lines.append(f"   Status: {status_icon} {status.title()}")
        lines.append(f"   ID: `{schedule.get('id', '')[:8]}`")
        lines.append("")

    lines.append("---")
    lines.append('To cancel: "cancel schedule <id>"')
    lines.append('To pause: "pause schedule <id>"')

    return [types.TextContent(type="text", text="\n".join(lines))]


async def handle_cancel_schedule(
    supabase: Client,
    arguments: Dict[str, Any],
    chat_id: str,
) -> List[types.TextContent]:
    """Handle cancel_user_schedule tool."""
    schedule_id = arguments.get("schedule_id", "")

    if not schedule_id:
        return [types.TextContent(type="text", text="Error: schedule_id is required")]

    # Support partial ID matching (UUID columns don't support ilike, so filter in Python)
    if len(schedule_id) < 36:
        # Fetch all schedules for this chat and filter by partial ID
        result = (
            supabase.table("user_schedules")
            .select("id, chat_id, status, friendly_name")
            .eq("chat_id", chat_id)
            .execute()
        )
        # Filter for IDs starting with the partial ID (case-insensitive)
        partial_lower = schedule_id.lower()
        matches = [s for s in (result.data or []) if s["id"].lower().startswith(partial_lower)]

        if len(matches) == 1:
            schedule_id = matches[0]["id"]
        elif len(matches) > 1:
            return [
                types.TextContent(
                    type="text",
                    text=f"Multiple schedules match '{schedule_id}'. Please use more characters.",
                )
            ]
        else:
            return [types.TextContent(type="text", text="Schedule not found")]

    # Verify ownership
    existing = (
        supabase.table("user_schedules")
        .select("id, chat_id, status, friendly_name")
        .eq("id", schedule_id)
        .single()
        .execute()
    )

    if not existing.data:
        return [types.TextContent(type="text", text="Schedule not found")]

    if existing.data.get("chat_id") != chat_id:
        return [types.TextContent(type="text", text="Schedule not found in this chat")]

    if existing.data.get("status") == "cancelled":
        return [types.TextContent(type="text", text="Schedule already cancelled")]

    # Cancel
    supabase.table("user_schedules").update(
        {
            "status": "cancelled",
            "is_active": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", schedule_id).execute()

    # Cancel any pending scheduled_messages for this schedule
    try:
        supabase.table("scheduled_messages").update(
            {
                "status": "cancelled",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("status", "pending").eq("payload->>schedule_id", schedule_id).execute()
    except Exception as e:
        logger.warning(f"Failed to cancel pending messages for schedule {schedule_id}: {e}")

    name = existing.data.get("friendly_name", schedule_id[:8])
    logger.info(f"Cancelled schedule {schedule_id}")

    return [types.TextContent(type="text", text=f"✅ Cancelled: {name}")]


async def handle_pause_schedule(
    supabase: Client,
    arguments: Dict[str, Any],
    chat_id: str,
) -> List[types.TextContent]:
    """Handle pause_user_schedule tool."""
    schedule_id = arguments.get("schedule_id", "")

    if not schedule_id:
        return [types.TextContent(type="text", text="Error: schedule_id is required")]

    # Support partial ID (UUID columns don't support ilike, so filter in Python)
    if len(schedule_id) < 36:
        result = supabase.table("user_schedules").select("id").eq("chat_id", chat_id).execute()
        partial_lower = schedule_id.lower()
        matches = [s for s in (result.data or []) if s["id"].lower().startswith(partial_lower)]
        if len(matches) == 1:
            schedule_id = matches[0]["id"]
        else:
            return [types.TextContent(type="text", text="Schedule not found")]

    existing = (
        supabase.table("user_schedules")
        .select("id, chat_id, status, friendly_name")
        .eq("id", schedule_id)
        .single()
        .execute()
    )

    if not existing.data:
        return [types.TextContent(type="text", text="Schedule not found")]

    if existing.data.get("chat_id") != chat_id:
        return [types.TextContent(type="text", text="Schedule not found in this chat")]

    if existing.data.get("status") != "active":
        return [types.TextContent(type="text", text="Schedule is not active")]

    supabase.table("user_schedules").update(
        {
            "status": "paused",
            "is_active": False,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", schedule_id).execute()

    # Cancel any pending scheduled_messages for this schedule
    try:
        supabase.table("scheduled_messages").update(
            {
                "status": "cancelled",
                "processed_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("status", "pending").eq("payload->>schedule_id", schedule_id).execute()
    except Exception as e:
        logger.warning(f"Failed to cancel pending messages for schedule {schedule_id}: {e}")

    name = existing.data.get("friendly_name", schedule_id[:8])
    return [types.TextContent(type="text", text=f"⏸️ Paused: {name}")]


async def handle_resume_schedule(
    supabase: Client,
    arguments: Dict[str, Any],
    chat_id: str,
) -> List[types.TextContent]:
    """Handle resume_user_schedule tool."""
    import pytz  # type: ignore[import-untyped]
    from croniter import croniter  # type: ignore[import-untyped]

    schedule_id = arguments.get("schedule_id", "")

    if not schedule_id:
        return [types.TextContent(type="text", text="Error: schedule_id is required")]

    # Support partial ID (UUID columns don't support ilike, so filter in Python)
    if len(schedule_id) < 36:
        result = supabase.table("user_schedules").select("id").eq("chat_id", chat_id).execute()
        partial_lower = schedule_id.lower()
        matches = [s for s in (result.data or []) if s["id"].lower().startswith(partial_lower)]
        if len(matches) == 1:
            schedule_id = matches[0]["id"]
        else:
            return [types.TextContent(type="text", text="Schedule not found")]

    existing = supabase.table("user_schedules").select("*").eq("id", schedule_id).single().execute()

    if not existing.data:
        return [types.TextContent(type="text", text="Schedule not found")]

    if existing.data.get("chat_id") != chat_id:
        return [types.TextContent(type="text", text="Schedule not found in this chat")]

    if existing.data.get("status") != "paused":
        return [types.TextContent(type="text", text="Schedule is not paused")]

    # Calculate new next_run_at
    cron_expr = existing.data.get("cron_expression")
    schedule_type = existing.data.get("schedule_type", "recurring")
    if cron_expr:
        now = datetime.now(pytz.UTC)
        cron = croniter(cron_expr, now)
        next_run_at = cron.get_next(datetime)
        if next_run_at.tzinfo is None:
            next_run_at = next_run_at.replace(tzinfo=pytz.UTC)
        # Biweekly: skip one occurrence so next run is 2 weeks out
        if schedule_type == "biweekly":
            cron = croniter(cron_expr, next_run_at)
            next_run_at = cron.get_next(datetime)
            if next_run_at.tzinfo is None:
                next_run_at = next_run_at.replace(tzinfo=pytz.UTC)
    else:
        original = existing.data.get("next_run_at")
        if original:
            next_run_at = datetime.fromisoformat(original.replace("Z", "+00:00"))
            if next_run_at <= datetime.now(pytz.UTC):
                return [
                    types.TextContent(
                        type="text",
                        text="One-time schedule has already passed. Create a new schedule instead.",
                    )
                ]
        else:
            return [types.TextContent(type="text", text="Cannot resume schedule")]

    # Resume
    supabase.table("user_schedules").update(
        {
            "status": "active",
            "is_active": True,
            "next_run_at": next_run_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).eq("id", schedule_id).execute()

    # Queue execution
    payload = {
        "schedule_id": schedule_id,
        "chat_id": existing.data["chat_id"],
        "topic_id": existing.data.get("topic_id"),
        "command": existing.data["command"],
        "user_context": existing.data.get("user_context", {}),
    }

    supabase.table("scheduled_messages").insert(
        {
            "message_type": "user_command",
            "payload": payload,
            "scheduled_for": next_run_at.isoformat(),
            "created_by": existing.data.get("created_by_email", ""),
            "status": "pending",
        }
    ).execute()

    name = existing.data.get("friendly_name", schedule_id[:8])
    return [types.TextContent(type="text", text=f"▶️ Resumed: {name}")]


async def main():
    """Run the MCP server."""
    logger.info("Starting Schedule MCP Server")

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="schedule-server",
                server_version="1.0.0",
                capabilities=ServerCapabilities(
                    tools=types.ToolsCapability(listChanged=True),
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
