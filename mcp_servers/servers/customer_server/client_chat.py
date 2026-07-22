"""Grid chat chronology methods for CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict

from servers.customer_server.client_base import STAFF_ORG_ID, logger
from servers.customer_server.formatting import _find_closest_grid_name

from shared.auth import get_auth_service


class ClientChatMixin:
    async def get_grid_chat_chronology(
        self,
        grid_name: str,
        organization_id: int,
        days_back: int = 7,
    ) -> Dict[str, Any]:
        """
        Get a chronological timeline of all chat messages related to a specific grid.

        Collects messages from:
        - The grid's O&M group topic (internal_telegram_group_chat_id + thread_id)
        - Individual org user DMs (chat_sessions with matching organization_id)
        - Developer group (organization's developer_group_telegram_chat_id)

        Args:
            grid_name: Grid name (supports fuzzy matching)
            organization_id: Organization ID (injected by orchestrator)
            days_back: Number of days to look back (default 7, max 90)

        Returns:
            Dict with grid info, sources, and chronological timeline of messages
        """
        days_back = min(max(days_back, 1), 90)
        escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")

        try:
            # --- Step 1: Resolve grid via Auth DB ---
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Resolve grid name with fuzzy matching (org-scoped for non-staff)
                if organization_id == STAFF_ORG_ID:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                available_names = [row["name"] for row in available_rows]
                matched_name = _find_closest_grid_name(grid_name, available_names)

                # If no grid match, try matching against organization names
                # (e.g., "AcmeCorp" → org "Acme Corp" → grid "ExampleGrid")
                org_match_grids = []
                if not matched_name:
                    org_rows = await conn.fetch(
                        """
                        SELECT o.id, o.name, g.name as grid_name
                        FROM organizations o
                        JOIN grids g ON g.organization_id = o.id
                        WHERE g.deleted_at IS NULL
                          AND g.is_hidden_from_reporting IS NOT TRUE
                        ORDER BY o.name
                        """
                    )
                    org_names = list({r["name"] for r in org_rows})
                    matched_org = _find_closest_grid_name(grid_name, org_names)
                    if matched_org:
                        org_match_grids = [r for r in org_rows if r["name"] == matched_org]

                if not matched_name and not org_match_grids:
                    grid_list = ", ".join(available_names[:10])
                    suffix = (
                        f" (and {len(available_names) - 10} more)"
                        if len(available_names) > 10
                        else ""
                    )
                    return {
                        "error": f"Grid or organization '{grid_name}' not found. "
                        f"Available grids: {grid_list}{suffix}"
                    }

                # If matched by org name, use the first grid (or aggregate all)
                if not matched_name and org_match_grids:
                    matched_name = org_match_grids[0]["grid_name"]
                    logger.info(
                        f"Resolved org '{grid_name}' → grid '{matched_name}' "
                        f"(org: {org_match_grids[0]['name']})"
                    )

                # Get grid details
                grid_row = await conn.fetchrow(
                    """
                    SELECT id, name, organization_id,
                           internal_telegram_group_chat_id,
                           internal_telegram_group_thread_id,
                           telegram_config
                    FROM grids
                    WHERE name = $1 AND deleted_at IS NULL
                    LIMIT 1
                    """,
                    matched_name,
                )
                if not grid_row:
                    return {"error": f"Grid '{matched_name}' not found"}

                grid_org_id = grid_row["organization_id"]
                group_chat_id = grid_row["internal_telegram_group_chat_id"]
                group_thread_id = grid_row["internal_telegram_group_thread_id"]

                # Extract logbook chat/topic IDs from telegram_config JSON
                from shared.auth import GridTelegramSources, parse_telegram_config

                tg_config = parse_telegram_config(grid_row["telegram_config"])
                logbook_chat_id = tg_config.get("internal_logbook_chat_id")
                logbook_topic_id = tg_config.get("internal_logbook_topic_id")

                # Build sources for classify_source() calls later
                grid_sources = GridTelegramSources(
                    om_chat_id=str(group_chat_id or ""),
                    om_topic_id=str(group_thread_id or ""),
                    logbook_chat_id=str(logbook_chat_id or ""),
                    logbook_topic_id=str(logbook_topic_id or ""),
                )

                # Get organization details
                org_row = await conn.fetchrow(
                    """
                    SELECT name, formal_name, developer_group_telegram_chat_id
                    FROM organizations
                    WHERE id = $1
                    """,
                    grid_org_id,
                )
                org_name = (org_row["formal_name"] or org_row["name"]) if org_row else "Unknown"
                dev_group_chat_id = org_row["developer_group_telegram_chat_id"] if org_row else None

            # --- Step 2: Query Chat DB (Supabase PostgREST) ---
            chat_db_url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
            chat_db_key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
            if not chat_db_url or not chat_db_key:
                return {"error": "Chat database not configured"}

            from supabase import create_client  # type: ignore[attr-defined]

            chat_client = create_client(chat_db_url, chat_db_key)

            cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).isoformat()

            # Build list of telegram_chat_ids to query for sessions
            # 1. Group chat (O&M topic)
            # 2. Developer group
            # 3. Individual org user sessions (by organization_id)

            # Find sessions matching the grid's group chat or developer group
            target_chat_ids = []
            if group_chat_id:
                target_chat_ids.append(str(group_chat_id))
            if dev_group_chat_id:
                target_chat_ids.append(str(dev_group_chat_id))
            if logbook_chat_id:
                target_chat_ids.append(str(logbook_chat_id))

            all_sessions: list = []
            seen_session_ids: set = set()

            def _add_sessions(sessions: list) -> None:
                for s in sessions:
                    if s["id"] not in seen_session_ids:
                        seen_session_ids.add(s["id"])
                        all_sessions.append(s)

            # Fetch sessions by telegram_chat_id (group + developer group)
            # For the O&M group, filter to the specific topic thread for this grid
            # to avoid pulling messages from other grid topics in the same group.
            if group_chat_id and group_thread_id:
                grid_topic_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(group_chat_id))
                    .eq("telegram_topic_id", str(group_thread_id))
                    .execute()
                )
                _add_sessions(grid_topic_resp.data or [])
            elif group_chat_id:
                # No thread ID — non-forum group, fetch all sessions for the chat
                grid_topic_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(group_chat_id))
                    .execute()
                )
                _add_sessions(grid_topic_resp.data or [])

            if dev_group_chat_id:
                dev_sessions_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(dev_group_chat_id))
                    .execute()
                )
                _add_sessions(dev_sessions_resp.data or [])

            # Fetch sessions for the Logbook group topic (from telegram_config)
            if logbook_chat_id and logbook_topic_id:
                logbook_sessions_resp = (
                    chat_client.table("chat_sessions")
                    .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                    .eq("telegram_chat_id", str(logbook_chat_id))
                    .eq("telegram_topic_id", str(logbook_topic_id))
                    .execute()
                )
                _add_sessions(logbook_sessions_resp.data or [])

            # Fetch sessions by organization_id (individual DMs only).
            # Exclude sessions from O&M/Logbook groups — already topic-filtered above.
            org_sessions_resp = (
                chat_client.table("chat_sessions")
                .select("id, telegram_chat_id, telegram_topic_id, organization_id, title")
                .eq("organization_id", grid_org_id)
                .execute()
            )
            # Chat IDs to skip (already fetched with topic filtering)
            skip_chat_ids = {str(cid) for cid in [group_chat_id, logbook_chat_id] if cid}
            for s in org_sessions_resp.data or []:
                if str(s.get("telegram_chat_id", "")) in skip_chat_ids:
                    continue
                _add_sessions([s])

            # Filter out staff org (2) and escalation group sessions
            filtered_sessions = []
            for s in all_sessions:
                chat_id_str = str(s.get("telegram_chat_id", ""))
                sess_org = s.get("organization_id")
                # Skip staff org sessions (unless it's the target group/dev group)
                if sess_org == STAFF_ORG_ID and chat_id_str not in target_chat_ids:
                    continue
                # Skip escalation group
                if escalation_chat_id and chat_id_str == str(escalation_chat_id):
                    continue
                filtered_sessions.append(s)

            if not filtered_sessions:
                return {
                    "grid_name": matched_name,
                    "organization": org_name,
                    "days_back": days_back,
                    "message_count": 0,
                    "sources": [],
                    "timeline": [],
                }

            # --- Step 3: Fetch messages for each session ---
            timeline = []
            source_counts: Dict[str, Dict[str, Any]] = {}
            batch_size = 50

            for i in range(0, len(filtered_sessions), batch_size):
                batch = filtered_sessions[i : i + batch_size]
                session_ids = [s["id"] for s in batch]
                session_map = {s["id"]: s for s in batch}

                messages_resp = (
                    chat_client.table("chat_messages")
                    .select("session_id, role, content, created_at")
                    .in_("session_id", session_ids)
                    .gte("created_at", cutoff)
                    .in_("role", ["user", "model"])
                    .order("created_at", desc=False)
                    .execute()
                )

                for msg in messages_resp.data or []:
                    content = msg.get("content")
                    if not content or not content.strip():
                        continue

                    session = session_map.get(msg["session_id"], {})
                    chat_id_str = str(session.get("telegram_chat_id", ""))
                    topic_id = session.get("telegram_topic_id")

                    # Determine source type and name
                    classified = grid_sources.classify_source(chat_id_str, str(topic_id or ""))
                    if classified:
                        source_type, label_prefix = classified
                        source_name = f"{label_prefix} {matched_name}"
                    elif group_chat_id and chat_id_str == str(group_chat_id):
                        # O&M group but different topic
                        source_type = "om_other"
                        source_name = "O&M Group (other topic)"
                    elif logbook_chat_id and chat_id_str == str(logbook_chat_id):
                        # Logbook group but different topic
                        source_type = "logbook_other"
                        source_name = "Logbook (other topic)"
                    elif dev_group_chat_id and chat_id_str == str(dev_group_chat_id):
                        source_type = "developer_group"
                        source_name = f"{org_name} Dev Group"
                    else:
                        source_type = "individual"
                        title = session.get("title") or "User"
                        source_name = f"{title} (DM)"

                    # Truncate content to 500 chars
                    truncated = content[:500] + "..." if len(content) > 500 else content

                    timeline.append(
                        {
                            "timestamp": msg.get("created_at", ""),
                            "source": source_name,
                            "source_type": source_type,
                            "role": msg["role"],
                            "content": truncated,
                        }
                    )

                    # Track source counts
                    if source_name not in source_counts:
                        source_counts[source_name] = {
                            "name": source_name,
                            "type": source_type,
                            "message_count": 0,
                        }
                    source_counts[source_name]["message_count"] += 1

            # Sort timeline chronologically
            timeline.sort(key=lambda m: m["timestamp"])

            sources = sorted(source_counts.values(), key=lambda s: s["message_count"], reverse=True)

            result = {
                "grid_name": matched_name,
                "organization": org_name,
                "days_back": days_back,
                "message_count": len(timeline),
                "sources": sources,
                "timeline": timeline,
            }

            # Create a work packet so the mini-app can render the timeline
            try:
                from uuid import uuid4

                packet_id = f"chat_chronology_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
                chat_client.table("agent_work_packets").insert(
                    {
                        "packet_id": packet_id,
                        "packet_type": "chat_chronology",
                        "packet_title": f"Chat Timeline: {matched_name}",
                        "packet_goal": f"Chat chronology for {matched_name} ({org_name})",
                        "assigned_expert": "chat_chronology",
                        "packet_status": "completed",
                        "packet_inputs": {
                            "grid_name": matched_name,
                            "organization": org_name,
                            "days_back": days_back,
                        },
                        "packet_state": {
                            "timeline": timeline,
                            "sources": sources,
                        },
                        "packet_outputs": {},
                        "organization_id": grid_org_id,
                    }
                ).execute()

                # Build mini-app URL (same signing as View State)
                import hashlib
                import hmac

                mini_app_url = os.getenv("MINI_APP_BASE_URL", "").rstrip("/")
                hmac_secret = os.getenv("MINI_APP_HMAC_SECRET", "")
                if mini_app_url and hmac_secret:
                    sig = hmac.new(
                        hmac_secret.encode(), packet_id.encode(), hashlib.sha256
                    ).hexdigest()[:16]
                    result["timeline_url"] = (
                        f"{mini_app_url}/?packet_id={packet_id}&view=timeline&sig={sig}"
                    )
                    logger.info(f"Created chronology packet {packet_id}")
            except Exception as e:
                logger.warning(f"Failed to create chronology packet: {e}")

            return result

        except Exception as e:
            logger.error(f"Error getting grid chat chronology: {e}")
            return {"error": f"Failed to get chat chronology: {str(e)}"}

