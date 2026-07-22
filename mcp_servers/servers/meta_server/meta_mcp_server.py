#!/usr/bin/env python3
"""
Meta MCP Server - Bot Performance Analytics

Provides tools for analyzing bot performance, escalation patterns, and user feedback.
Staff-only access via /meta command.

Tools (prefixed with 'meta_' when exposed via bridge):
- get_performance_report: Comprehensive performance data for date range
- response_distribution_chart: Pie chart of bot responses vs escalations
- escalation_types_chart: Pie chart of escalation reasons
- action_types_chart: Pie chart of action types for staff_action_required
- list_escalated_messages: Recent escalated messages with context
- list_negative_feedback: Messages that received negative feedback
"""

import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import mcp.types as types
import vl_convert as vlc
from mcp.server import NotificationOptions, Server
from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry
from supabase import Client, create_client

from shared.charts import apply_theme
from shared.utils.date_utils import parse_iso_with_timezone
from shared.utils.logging import get_logger

from .tool_schemas import TOOL_SCHEMAS

logger = get_logger("meta-server")

# Startup message
print("🚀 Meta MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("meta-server")
registry = ToolRegistry("meta")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}

# Configuration
# Chat database credentials (with legacy fallback)
CHAT_DB_URL = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
CHAT_DB_SERVICE_KEY = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
ESCALATION_CHAT_ID = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
META_ACTIONS_ENABLED = os.getenv("META_ACTIONS_ENABLED", "true").lower() == "true"

# Messages to exclude from bot response counts
EXCLUDED_MESSAGE_PATTERNS = [
    "💬 **Response from Support Team**",  # Staff passthrough
    "Your issue has been escalated",  # Escalation notification
    "Your issue has been resolved",  # Resolution notification
]

# Valid escalation reason values
ESCALATION_REASONS = [
    "user_requested",
    "could_not_answer",
    "out_of_scope",
    "staff_action_required",
    "inappropriate_language",
    "negative_feedback",
    "verification_failed",
    "safety_escalation",
    "other",
]

# Valid action types for staff_action_required
ACTION_TYPES = [
    "meter_unassignment",
    "wallet_credit",
    "hps_power_limit",
    "meter_replacement",
    "commissioning_retry",
    "other_action",
]


def _get_supabase_client() -> Client:
    """Get Supabase client for database queries."""
    if not CHAT_DB_URL or not CHAT_DB_SERVICE_KEY:
        raise ValueError(
            "Chat database credentials not configured. "
            "Set CHAT_DB_URL and CHAT_DB_SERVICE_KEY (or legacy SUPABASE_URL/SUPABASE_KEY)."
        )
    return create_client(CHAT_DB_URL, CHAT_DB_SERVICE_KEY)


def _build_pie_chart(
    data: List[Dict[str, Any]], title: str, width: int = 400, height: int = 300
) -> bytes:
    """
    Generate pie chart PNG from data with count labels on each segment.

    Args:
        data: List of dicts with 'category' and 'count' keys
        title: Chart title
        width: Chart width in pixels
        height: Chart height in pixels

    Returns:
        PNG image bytes
    """
    # Filter out zero counts
    filtered_data = [d for d in data if d.get("count", 0) > 0]

    if not filtered_data:
        filtered_data = [{"category": "No data", "count": 1}]

    # Calculate total for percentage display
    total = sum(d.get("count", 0) for d in filtered_data)

    # Add percentage to data for labeling
    for d in filtered_data:
        count = d.get("count", 0)
        pct = (count / total * 100) if total > 0 else 0
        d["label"] = f"{count} ({pct:.0f}%)"

    vl_spec = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "title": title,
        "width": width,
        "height": height,
        "data": {"values": filtered_data},
        "encoding": {
            "theta": {"field": "count", "type": "quantitative", "stack": True},
            "color": {
                "field": "category",
                "type": "nominal",
                "title": "Category",
                "legend": {"orient": "right"},
            },
        },
        "layer": [
            {
                "mark": {"type": "arc", "tooltip": True, "innerRadius": 50, "outerRadius": 120},
            },
            {
                "mark": {
                    "type": "text",
                    "radius": 90,
                    "fontSize": 12,
                    "fontWeight": "bold",
                },
                "encoding": {
                    "text": {"field": "label", "type": "nominal"},
                },
            },
        ],
    }

    themed_spec = apply_theme(vl_spec)
    png_bytes: bytes = vlc.vegalite_to_png(themed_spec, scale=2)
    return png_bytes


async def _get_response_distribution(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> Dict[str, int]:
    """
    Get distribution of bot responses vs escalations.

    Counts sessions with user message activity, and separately counts sessions
    with ANY escalation event (including agent-initiated ones that may not have
    user messages in the window). Both sets are unioned before applying the
    org filter and escalation-group exclusion.

    Returns:
        Dict with 'bot_responded' and 'escalated' counts
    """
    # Sessions with user messages in the period
    msg_response = (
        client.table("chat_messages")
        .select("session_id")
        .eq("role", "user")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .execute()
    )
    active_session_ids: set[str] = set(
        row["session_id"] for row in msg_response.data or [] if row.get("session_id")
    )

    # Sessions with ANY escalation event — direct query, not cross-joined against
    # active_session_ids, so agent-initiated escalations are not silently dropped.
    esc_query = (
        client.table("escalation_mappings")
        .select("session_id")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .limit(2000)
    )
    if organization_id:
        esc_query = esc_query.eq("organization_id", organization_id)
    esc_response = esc_query.execute()
    escalated_session_ids: set[str] = set(
        row["session_id"] for row in esc_response.data or [] if row.get("session_id")
    )

    all_relevant_ids = active_session_ids | escalated_session_ids
    if not all_relevant_ids:
        return {"bot_responded": 0, "escalated": 0}

    # Apply org filter and exclude escalation group chats via chat_sessions lookup
    batch_size = 50
    valid_session_ids: set[str] = set()
    all_relevant_list = list(all_relevant_ids)

    for i in range(0, len(all_relevant_list), batch_size):
        batch = all_relevant_list[i : i + batch_size]
        query = client.table("chat_sessions").select("id, telegram_chat_id")
        if organization_id:
            query = query.eq("organization_id", organization_id)
        query = query.in_("id", batch)
        response = query.execute()

        for session in response.data or []:
            chat_id = str(session.get("telegram_chat_id", ""))
            if chat_id == str(ESCALATION_CHAT_ID):
                continue
            valid_session_ids.add(session.get("id"))

    escalated = len(escalated_session_ids & valid_session_ids)
    bot_responded = len((active_session_ids - escalated_session_ids) & valid_session_ids)

    return {"bot_responded": bot_responded, "escalated": escalated}


async def _get_issue_type_breakdown(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> Dict[str, int]:
    """Get thread count breakdown by issue type from chat_threads for new threads in window."""
    _LIMIT = 5000
    query = (
        client.table("chat_threads")
        .select("issue_type")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .limit(_LIMIT)
    )
    if organization_id:
        query = query.eq("organization_id", organization_id)

    try:
        response = query.execute()
    except Exception:
        logger.warning("chat_threads query failed — table may not exist yet", exc_info=True)
        return {}
    rows = response.data or []
    if len(rows) == _LIMIT:
        logger.warning(
            "_get_issue_type_breakdown hit row cap (%d) — counts may be incomplete", _LIMIT
        )
    counts: Dict[str, int] = {}
    for row in rows:
        t = row.get("issue_type") or "other"
        counts[t] = counts.get(t, 0) + 1
    return counts


async def _get_escalation_reasons(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> Dict[str, int]:
    """
    Get escalation breakdown by reason.

    Returns:
        Dict mapping reason to count
    """
    query = (
        client.table("escalation_mappings")
        .select("reason")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .limit(2000)
    )

    if organization_id:
        query = query.eq("organization_id", organization_id)

    response = query.execute()

    reasons: Dict[str, int] = {}
    for row in response.data or []:
        reason = row.get("reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1

    return reasons


async def _get_action_types(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> Dict[str, int]:
    """
    Get action type breakdown for staff_action_required escalations.

    Returns:
        Dict mapping action_type to count
    """
    query = (
        client.table("escalation_mappings")
        .select("action_type")
        .eq("reason", "staff_action_required")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .limit(2000)
    )

    if organization_id:
        query = query.eq("organization_id", organization_id)

    response = query.execute()

    action_types: Dict[str, int] = {}
    for row in response.data or []:
        action_type = row.get("action_type") or "unknown"
        action_types[action_type] = action_types.get(action_type, 0) + 1

    return action_types


async def _get_avg_time_to_close(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> Optional[float]:
    """
    Compute average time to close escalations in minutes.

    Only includes escalations that were resolved (resolved_at is not null)
    and were created within the date range.

    Returns:
        Average minutes to close, or None if no closed escalations exist.
    """
    query = (
        client.table("escalation_mappings")
        .select("created_at, resolved_at")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .not_.is_("resolved_at", "null")
        .limit(2000)
    )
    if organization_id:
        query = query.eq("organization_id", organization_id)
    try:
        response = query.execute()
    except Exception as e:
        logger.error(f"Error fetching avg time to close: {e}")
        return None

    total_minutes = 0.0
    count = 0
    for row in response.data or []:
        created_raw = row.get("created_at")
        resolved_raw = row.get("resolved_at")
        if not created_raw or not resolved_raw:
            continue
        try:
            created_dt = parse_iso_with_timezone(created_raw)
            resolved_dt = parse_iso_with_timezone(resolved_raw)
            delta_minutes = (resolved_dt - created_dt).total_seconds() / 60
            if delta_minutes >= 0:
                total_minutes += delta_minutes
                count += 1
        except (ValueError, AttributeError):
            logger.debug(f"Skipping malformed timestamp row: {row!r}")
            continue

    if count == 0:
        return None

    return round(total_minutes / count, 1)


async def _get_negative_feedback_count(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
) -> int:
    """
    Get count of messages with negative feedback (thumbs_down).

    Returns:
        Count of thumbs_down feedback
    """
    # Query messages with feedback metadata
    query = (
        client.table("chat_messages")
        .select("metadata, session_id")
        .eq("role", "model")
        .not_.is_("metadata->feedback", "null")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
    )

    response = query.execute()

    # If we need to filter by org, we need to check session org_id
    session_org_map: Dict[str, int] = {}
    if organization_id:
        session_ids = list(
            set(row.get("session_id") for row in response.data or [] if row.get("session_id"))
        )
        if session_ids:
            sessions_response = (
                client.table("chat_sessions")
                .select("session_id, organization_id")
                .in_("session_id", session_ids)
                .execute()
            )
            session_org_map = {
                s["session_id"]: s.get("organization_id") for s in sessions_response.data or []
            }

    count = 0
    for row in response.data or []:
        # Filter by org if specified
        if organization_id:
            session_id = row.get("session_id")
            if session_org_map.get(session_id) != organization_id:
                continue

        metadata = row.get("metadata") or {}
        feedback = metadata.get("feedback")
        if not feedback:
            continue

        # Handle both array and single object formats
        feedback_list = feedback if isinstance(feedback, list) else [feedback]

        for fb in feedback_list:
            if isinstance(fb, dict) and fb.get("type") == "thumbs_down":
                count += 1

    return count


async def _get_escalated_messages(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
    char_limit: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Get list of recently escalated messages.

    Returns:
        List of escalation details with message preview
    """
    query = (
        client.table("escalation_mappings")
        .select("session_id, reason, action_type, org_hashtag, created_at, customer_email")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .order("created_at", desc=True)
        .limit(50)  # Fetch more than needed, will filter by char limit
    )

    if organization_id:
        query = query.eq("organization_id", organization_id)

    response = query.execute()

    results = []
    total_chars = 0

    for row in response.data or []:
        # Get the user message that triggered escalation
        session_id = row.get("session_id")
        user_message = ""

        if session_id:
            # Get last user message from session
            msg_response = (
                client.table("chat_messages")
                .select("content")
                .eq("session_id", session_id)
                .eq("role", "user")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

            if msg_response.data:
                user_message = msg_response.data[0].get("content", "")[
                    :200
                ]  # Truncate long messages

        entry = {
            "timestamp": row.get("created_at"),
            "org": row.get("org_hashtag", "").replace("#", ""),
            "reason": row.get("reason", "unknown"),
            "action_type": row.get("action_type"),
            "user_email": row.get("customer_email"),
            "message_preview": user_message,
        }

        entry_str = json.dumps(entry)
        if total_chars + len(entry_str) > char_limit:
            break

        total_chars += len(entry_str)
        results.append(entry)

    return results


async def _get_negative_feedback_messages(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int] = None,
    char_limit: int = 5000,
) -> List[Dict[str, Any]]:
    """
    Get list of messages with negative feedback.

    Returns:
        List of messages that received thumbs_down
    """
    query = (
        client.table("chat_messages")
        .select("content, metadata, session_id, created_at")
        .eq("role", "model")
        .not_.is_("metadata->feedback", "null")
        .gte("created_at", start_date.isoformat())
        .lt("created_at", end_date.isoformat())
        .order("created_at", desc=True)
        .limit(100)
    )

    response = query.execute()

    # Get session org mappings if filtering by org
    session_org_map: Dict[str, int] = {}
    if organization_id:
        session_ids = list(
            set(row.get("session_id") for row in response.data or [] if row.get("session_id"))
        )
        if session_ids:
            sessions_response = (
                client.table("chat_sessions")
                .select("session_id, organization_id")
                .in_("session_id", session_ids)
                .execute()
            )
            session_org_map = {
                s["session_id"]: s.get("organization_id") for s in sessions_response.data or []
            }

    results = []
    total_chars = 0

    for row in response.data or []:
        # Filter by org if specified
        if organization_id:
            session_id = row.get("session_id")
            if session_org_map.get(session_id) != organization_id:
                continue

        metadata = row.get("metadata") or {}
        feedback = metadata.get("feedback")
        if not feedback:
            continue

        # Check for thumbs_down
        feedback_list = feedback if isinstance(feedback, list) else [feedback]
        has_negative = any(
            isinstance(fb, dict) and fb.get("type") == "thumbs_down" for fb in feedback_list
        )

        if not has_negative:
            continue

        entry = {
            "timestamp": row.get("created_at"),
            "response_preview": (row.get("content") or "")[:200],
        }

        entry_str = json.dumps(entry)
        if total_chars + len(entry_str) > char_limit:
            break

        total_chars += len(entry_str)
        results.append(entry)

    return results


async def _lookup_organization(org_name: str) -> Optional[Dict[str, Any]]:
    """
    Look up organization by name (short or formal).

    Returns:
        Dict with org_id, name, and formal_name if found
    """
    try:
        import asyncpg

        # Auth DB connection
        auth_db_host = os.getenv("AUTH_DB_HOST", "")
        auth_db_port = int(os.getenv("AUTH_DB_PORT", "6543"))
        auth_db_name = os.getenv("AUTH_DB_NAME", "postgres")
        auth_db_user = os.getenv("AUTH_DB_USER", "")
        auth_db_password = os.getenv("AUTH_DB_PASSWORD", "")

        if not auth_db_host or not auth_db_user:
            logger.warning("Auth DB credentials not configured")
            return None

        conn = await asyncpg.connect(
            host=auth_db_host,
            port=auth_db_port,
            database=auth_db_name,
            user=auth_db_user,
            password=auth_db_password,
            ssl="require",
            statement_cache_size=0,  # Required for PgBouncer
        )

        try:
            # Search by name or formal_name (case-insensitive)
            row = await conn.fetchrow(
                """
                SELECT id, name, formal_name
                FROM organizations
                WHERE LOWER(name) = LOWER($1) OR LOWER(formal_name) = LOWER($1)
                LIMIT 1
                """,
                org_name,
            )

            if row:
                return {
                    "org_id": row["id"],
                    "name": row["name"],
                    "formal_name": row["formal_name"],
                }

            return None
        finally:
            await conn.close()

    except Exception as e:
        logger.error(f"Error looking up organization: {e}")
        return None


@registry.pre_dispatch
async def _prepare_common_args(
    name: str, arguments: Dict[str, Any]
) -> Optional[List[types.TextContent]]:
    """Shared preamble every meta tool went through before dispatch: the
    disabled-state gate (exact original message, kept here rather than the
    registry's generic gated-refusal text), and parsing/resolving the
    days/organization filter every _handle_* function takes as params.
    Preserved verbatim as a pre_dispatch hook; resolved values are stashed
    on `arguments` for the tool handlers.
    """
    if not META_ACTIONS_ENABLED:
        return [
            types.TextContent(
                type="text",
                text=json.dumps(
                    {
                        "success": False,
                        "error": "Meta analytics actions are disabled.",
                    }
                ),
            )
        ]

    client = _get_supabase_client()

    # Parse common arguments
    days = arguments.get("days", 7)
    org_name = arguments.get("organization")

    # Calculate date range
    end_date = datetime.now(timezone.utc)
    start_date = end_date - timedelta(days=days)

    # Lookup organization if specified
    organization_id: Optional[int] = None
    org_hashtag: Optional[str] = None

    if org_name:
        org_info = await _lookup_organization(org_name)
        if org_info:
            organization_id = org_info["org_id"]
            org_hashtag = f"#{org_info['name'].lower()}"
            logger.info(
                f"Resolved org '{org_name}' to id={organization_id}, hashtag={org_hashtag}"
            )
        else:
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": False,
                            "error": f"Organization '{org_name}' not found",
                        }
                    ),
                )
            ]

    arguments["_client"] = client
    arguments["_start_date"] = start_date
    arguments["_end_date"] = end_date
    arguments["_organization_id"] = organization_id
    arguments["_days"] = days
    arguments["_org_name"] = org_name
    return None


def _common_args(arguments: Dict[str, Any]) -> tuple:
    return (
        arguments["_client"],
        arguments["_start_date"],
        arguments["_end_date"],
        arguments["_organization_id"],
        arguments["_days"],
        arguments["_org_name"],
    )


@registry.tool("get_performance_report", _SCHEMAS_BY_NAME["get_performance_report"], gated=True, refuse_when_disabled=False)
async def _tool_get_performance_report(
    arguments: Dict[str, Any],
) -> List[types.TextContent]:
    return await _handle_performance_report(*_common_args(arguments))


@registry.tool(
    "response_distribution_chart",
    _SCHEMAS_BY_NAME["response_distribution_chart"],
    gated=True, refuse_when_disabled=False,
)
async def _tool_response_distribution_chart(
    arguments: Dict[str, Any],
) -> List[types.TextContent | types.ImageContent]:
    return await _handle_response_distribution_chart(*_common_args(arguments))


@registry.tool(
    "escalation_types_chart", _SCHEMAS_BY_NAME["escalation_types_chart"], gated=True, refuse_when_disabled=False
)
async def _tool_escalation_types_chart(
    arguments: Dict[str, Any],
) -> List[types.TextContent | types.ImageContent]:
    return await _handle_escalation_types_chart(*_common_args(arguments))


@registry.tool("action_types_chart", _SCHEMAS_BY_NAME["action_types_chart"], gated=True, refuse_when_disabled=False)
async def _tool_action_types_chart(
    arguments: Dict[str, Any],
) -> List[types.TextContent | types.ImageContent]:
    return await _handle_action_types_chart(*_common_args(arguments))


@registry.tool(
    "list_escalated_messages", _SCHEMAS_BY_NAME["list_escalated_messages"], gated=True, refuse_when_disabled=False
)
async def _tool_list_escalated_messages(
    arguments: Dict[str, Any],
) -> List[types.TextContent]:
    return await _handle_list_escalated_messages(*_common_args(arguments))


@registry.tool(
    "list_negative_feedback", _SCHEMAS_BY_NAME["list_negative_feedback"], gated=True, refuse_when_disabled=False
)
async def _tool_list_negative_feedback(
    arguments: Dict[str, Any],
) -> List[types.TextContent]:
    return await _handle_list_negative_feedback(*_common_args(arguments))


@registry.tool(
    "issue_type_breakdown_chart",
    _SCHEMAS_BY_NAME["issue_type_breakdown_chart"],
    gated=True, refuse_when_disabled=False,
)
async def _tool_issue_type_breakdown_chart(
    arguments: Dict[str, Any],
) -> List[types.TextContent | types.ImageContent]:
    return await _handle_issue_type_chart(*_common_args(arguments))


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def _handle_performance_report(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent]:
    """Handle meta_get_performance_report tool."""
    # Get all metrics
    distribution = await _get_response_distribution(client, start_date, end_date, organization_id)
    reasons = await _get_escalation_reasons(client, start_date, end_date, organization_id)
    action_types = await _get_action_types(client, start_date, end_date, organization_id)
    issue_type_breakdown = await _get_issue_type_breakdown(
        client, start_date, end_date, organization_id
    )
    negative_feedback = await _get_negative_feedback_count(
        client, start_date, end_date, organization_id
    )
    avg_close_minutes = await _get_avg_time_to_close(client, start_date, end_date, organization_id)

    total_sessions = distribution["bot_responded"] + distribution["escalated"]
    escalation_rate = (
        (distribution["escalated"] / total_sessions * 100) if total_sessions > 0 else 0
    )

    report = {
        "success": True,
        "period": {
            "days": days,
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "filter": {
            "organization": org_name,
        },
        "summary": {
            "total_sessions": total_sessions,
            "bot_responded": distribution["bot_responded"],
            "escalated": distribution["escalated"],
            "escalation_rate_percent": round(escalation_rate, 1),
            "negative_feedback_count": negative_feedback,
        },
        "escalation_breakdown": {
            "by_reason": reasons,
            "by_action_type": action_types,
            "avg_time_to_close_minutes": avg_close_minutes,
        },
        "issue_type_breakdown": issue_type_breakdown,
    }

    return [
        types.TextContent(
            type="text",
            text=json.dumps(report, indent=2),
        )
    ]


async def _handle_response_distribution_chart(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent | types.ImageContent]:
    """Handle meta_response_distribution_chart tool."""
    distribution = await _get_response_distribution(client, start_date, end_date, organization_id)

    data = [
        {"category": "Bot Responded", "count": distribution["bot_responded"]},
        {"category": "Escalated", "count": distribution["escalated"]},
    ]

    title = f"Response Distribution (Last {days} days)"
    if org_name:
        title += f" - {org_name}"

    png_bytes = _build_pie_chart(data, title)
    image_base64 = base64.b64encode(png_bytes).decode("utf-8")

    return [
        types.ImageContent(
            type="image",
            data=image_base64,
            mimeType="image/png",
        ),
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "chart_type": "response_distribution",
                    "data": data,
                }
            ),
        ),
    ]


async def _handle_escalation_types_chart(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent | types.ImageContent]:
    """Handle meta_escalation_types_chart tool."""
    reasons = await _get_escalation_reasons(client, start_date, end_date, organization_id)

    data = [{"category": reason, "count": count} for reason, count in reasons.items()]

    title = f"Escalation Reasons (Last {days} days)"
    if org_name:
        title += f" - {org_name}"

    png_bytes = _build_pie_chart(data, title)
    image_base64 = base64.b64encode(png_bytes).decode("utf-8")

    return [
        types.ImageContent(
            type="image",
            data=image_base64,
            mimeType="image/png",
        ),
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "chart_type": "escalation_types",
                    "data": data,
                }
            ),
        ),
    ]


async def _handle_action_types_chart(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent | types.ImageContent]:
    """Handle meta_action_types_chart tool."""
    action_types = await _get_action_types(client, start_date, end_date, organization_id)

    data = [
        {"category": action_type, "count": count} for action_type, count in action_types.items()
    ]

    title = f"Action Types (Last {days} days)"
    if org_name:
        title += f" - {org_name}"

    png_bytes = _build_pie_chart(data, title)
    image_base64 = base64.b64encode(png_bytes).decode("utf-8")

    return [
        types.ImageContent(
            type="image",
            data=image_base64,
            mimeType="image/png",
        ),
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "chart_type": "action_types",
                    "data": data,
                }
            ),
        ),
    ]


async def _handle_issue_type_chart(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent | types.ImageContent]:
    """Handle issue_type_breakdown_chart tool."""
    breakdown = await _get_issue_type_breakdown(client, start_date, end_date, organization_id)

    data = [{"category": t, "count": c} for t, c in breakdown.items()]
    title = f"Issues by Type (Last {days} days)"
    if org_name:
        title += f" - {org_name}"

    png_bytes = _build_pie_chart(data, title)
    image_base64 = base64.b64encode(png_bytes).decode("utf-8")

    return [
        types.ImageContent(type="image", data=image_base64, mimeType="image/png"),
        types.TextContent(
            type="text",
            text=json.dumps({"success": True, "chart_type": "issue_types", "data": data}),
        ),
    ]


async def _handle_list_escalated_messages(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent]:
    """Handle meta_list_escalated_messages tool."""
    messages = await _get_escalated_messages(client, start_date, end_date, organization_id)

    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "period_days": days,
                    "organization": org_name,
                    "count": len(messages),
                    "messages": messages,
                },
                indent=2,
            ),
        )
    ]


async def _handle_list_negative_feedback(
    client: Client,
    start_date: datetime,
    end_date: datetime,
    organization_id: Optional[int],
    days: int,
    org_name: Optional[str],
) -> List[types.TextContent]:
    """Handle meta_list_negative_feedback tool."""
    messages = await _get_negative_feedback_messages(client, start_date, end_date, organization_id)

    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "success": True,
                    "period_days": days,
                    "organization": org_name,
                    "count": len(messages),
                    "messages": messages,
                },
                indent=2,
            ),
        )
    ]


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources."""
    return [
        types.Resource(
            uri="meta://config",
            name="Meta Server Configuration",
            description="Current meta server configuration",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "meta://config":
        config = {
            "actions_enabled": META_ACTIONS_ENABLED,
            "escalation_chat_id_configured": bool(ESCALATION_CHAT_ID),
            "supabase_configured": bool(CHAT_DB_URL and CHAT_DB_SERVICE_KEY),
            "valid_escalation_reasons": ESCALATION_REASONS,
            "valid_action_types": ACTION_TYPES,
        }
        return json.dumps(config, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point."""
    logger.info("Starting Meta MCP Server...")
    await run_stdio_server(
        server,
        name="meta-server",
        label="Meta",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(),
            experimental_capabilities={},
        ),
    )


if __name__ == "__main__":
    import asyncio

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Meta server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Meta server crashed: {e}", file=sys.stderr)
        sys.exit(1)
