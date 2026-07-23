"""Handler for /pending direct command.

Lists active workflows and sends per-packet Telegram messages with
View State buttons. Bypasses the LLM entirely.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from orchestrator.mini_app.schemas import build_view_state_url
from orchestrator.services.work_packet_service import WorkPacketService
from shared.utils.logging import get_logger
from shared.utils.telegram_buttons import build_webapp_keyboard

LOGGER = get_logger(__name__)

STATUS_EMOJI = {
    "awaiting_input": "\u23f3",  # hourglass
    "in_progress": "\U0001f504",  # arrows
    "pending": "\U0001f4cb",  # clipboard
}


def _format_packet_message(packet: Dict[str, Any]) -> str:
    """Format a single packet into a Telegram message string."""
    status = packet.get("packet_status", "unknown")
    emoji = STATUS_EMOJI.get(status, "\u2753")
    packet_type = (packet.get("packet_type") or "workflow").replace("_", " ").title()
    current_step = packet.get("current_step") or "N/A"
    created_at_raw = packet.get("created_at", "")

    created_display = ""
    if created_at_raw:
        try:
            dt = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
            created_display = dt.strftime("%b %d, %H:%M UTC")
        except (ValueError, TypeError):
            created_display = str(created_at_raw)[:16]

    lines = [
        f"{emoji} {packet_type}",
        f"Status: {status.replace('_', ' ').title()}",
        f"Step: {current_step}",
    ]
    if created_display:
        lines.append(f"Created: {created_display}")

    return "\n".join(lines)


async def handle_pending_command(
    session_id: str,
    user_context: Any,
    related_session_ids: List[str],
    base_result: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Handle /pending command: list active workflows and send Telegram messages.

    Queries active work packets for the current session (and optionally for the
    user's email), sends one Telegram message per packet with status details and
    a View State button, then returns a state dict that skips the LLM entirely.

    Args:
        session_id: Current session ID
        user_context: UserContext with chat_id, email, etc.
        related_session_ids: Pre-computed related session IDs
        base_result: Default result dict to extend

    Returns:
        State update dict if handled, None if something went wrong.
    """
    from shared.utils.telegram_send import send_telegram_message

    try:
        packet_service = WorkPacketService()

        # Gather packets from session
        all_packets: Dict[str, Dict] = {}
        for sid in related_session_ids:
            packets = await packet_service.get_active_packets_for_session(
                sid, auto_fail_stale=False
            )
            for p in packets:
                all_packets[p["packet_id"]] = p

        # Also check by email if available
        email = None
        org_id = None
        if user_context:
            email = getattr(user_context, "email", None)
            org_ids = getattr(user_context, "organization_ids", None)
            if org_ids:
                org_id = int(org_ids[0])

        if email:
            user_packets = await packet_service.get_active_packets_for_user(
                email, organization_id=org_id
            )
            for p in user_packets:
                # Filter by org to prevent cross-org leakage
                if org_id and p.get("organization_id") != org_id:
                    continue
                all_packets[p["packet_id"]] = p

        # Deduplicated list, most recently updated first
        packets = sorted(
            all_packets.values(),
            key=lambda p: p.get("updated_at", ""),
            reverse=True,
        )

        if not packets:
            result = dict(base_result)
            result["final_response"] = "No pending workflows found."
            return result

        # Send one Telegram message per packet
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = str(user_context.chat_id) if user_context and user_context.chat_id else ""

        for packet in packets:
            text = _format_packet_message(packet)

            reply_markup = None
            state_url = build_view_state_url(packet["packet_id"])
            if state_url:
                reply_markup = build_webapp_keyboard("View State", state_url, chat_id=chat_id)

            if bot_token and chat_id:
                try:
                    await send_telegram_message(
                        bot_token,
                        chat_id,
                        text,
                        reply_markup=reply_markup,
                    )
                except Exception as e:
                    LOGGER.warning(f"Failed to send /pending message for packet: {e}")

        summary = f"Found {len(packets)} pending workflow(s)."
        result = dict(base_result)
        result["final_response"] = summary
        return result

    except Exception as e:
        LOGGER.error(f"Error handling /pending command: {e}", exc_info=True)
        result = dict(base_result)
        result["final_response"] = "Sorry, could not retrieve pending workflows."
        return result
