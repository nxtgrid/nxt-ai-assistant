"""MCP server for sending messages to Telegram groups.

Restricted to staff groups defined in the staff instructions Google Doc.
Validates chat_id against the staff groups registry before sending.

Intended for persistent agents only — not exposed in normal chat flow.

NOTE: This server imports from orchestrator.services.instructions_provider
to access the staff groups registry. This is a known cross-layer dependency
that works because both run in the same Python process. If the services are
ever separated, the allowed chat_ids should be injected as a tool parameter
via ORCHESTRATOR_INJECTED_PARAMS instead.
"""

import json
import logging
import os
from typing import Any, Dict, List

from mcp.types import TextContent
from shared_code.tool_registry import ToolRegistry

from shared.utils.telegram_send import send_telegram_message

from .tool_schemas import TOOL_SCHEMAS

logger = logging.getLogger("messaging-server")

registry = ToolRegistry("messaging")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


@registry.tool("send_to_group", _SCHEMAS_BY_NAME["send_to_group"])
async def _send_to_group(arguments: Dict[str, Any]) -> List[TextContent]:
    """Send a message to a validated Telegram staff group."""
    chat_id = str(arguments.get("chat_id", "")).strip()
    text = arguments.get("text", "").strip()
    topic_id = arguments.get("topic_id")

    if not chat_id or not text:
        return [TextContent(type="text", text="Error: chat_id and text are required")]

    # Validate chat_id against staff groups
    allowed_ids = _get_allowed_chat_ids()
    if not allowed_ids:
        logger.error("Staff groups allowlist is empty — cannot validate any chat_id")
        return [TextContent(type="text", text="Error: staff groups not loaded yet")]

    if chat_id not in allowed_ids:
        logger.warning(f"Blocked send to unauthorized chat_id {chat_id}")
        return [
            TextContent(
                type="text",
                text="Error: chat_id is not a registered staff group.",
            )
        ]

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return [TextContent(type="text", text="Error: TELEGRAM_BOT_TOKEN not configured")]

    msg_id = await send_telegram_message(bot_token, chat_id, text, topic_id=topic_id)
    if msg_id:
        group_name = allowed_ids.get(chat_id, chat_id)
        result: Dict[str, Any] = {"success": True, "message_id": msg_id, "group": group_name}
        if topic_id:
            result["topic_id"] = topic_id
        return [TextContent(type="text", text=json.dumps(result))]
    else:
        return [TextContent(type="text", text="Error: Failed to send message to Telegram")]


def _get_allowed_chat_ids() -> Dict[str, str]:
    """Get chat_ids that this tool is allowed to send to.

    Returns {chat_id: display_name} from the staff groups registry.
    """
    try:
        from orchestrator.services.instructions_provider import get_staff_groups

        groups = get_staff_groups()
        return {cid: info["name"] for cid, info in groups.items()}
    except Exception:
        logger.warning("Could not load staff groups for validation")
        return {}


handle_list_tools = registry.handle_list_tools
handle_call_tool = registry.handle_call_tool
