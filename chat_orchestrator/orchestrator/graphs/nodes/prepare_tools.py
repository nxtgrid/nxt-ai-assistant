"""Prepare tools node for LangGraph.

This node prepares the available tools based on user permissions
and adds system tools (escalation, training images).
"""

from typing import Any, Dict, List

from loguru import logger as LOGGER

from orchestrator.config.settings import get_settings
from orchestrator.graphs.state import ConversationState
from orchestrator.services.user_permissions import get_permissions_service

# Escalation tool definition - available to all users
ESCALATION_TOOL_DEF = {
    "name": "escalate_to_support",
    "description": (
        "IMMEDIATELY escalate requests to the Support Team. "
        "REQUIRED for:\n"
        "- Meter unassignment requests\n"
        "- Manual wallet credits (ONLY after transaction verified via check_transaction_status)\n"
        "- Transaction verification (when payment status tool unavailable/failed)\n"
        "- HPS power limit increase reviews (after form filled)\n"
        "- Meter replacements\n"
        "- Manual commissioning retries\n"
        "- Any action beyond available read-only tools\n\n"
        "CRITICAL for financial issues: NEVER recommend wallet credits without first "
        "successfully verifying the transaction. If verification fails, escalate for "
        "'transaction_verification' instead of 'wallet_credit'.\n\n"
        "Call this tool FIRST, then confirm escalation to user in past tense "
        "(e.g. 'I've escalated your request...'). "
        "Do NOT announce 'I will escalate' without calling this tool."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question_summary": {
                "type": "string",
                "description": "Brief summary of the customer's question or issue (1-2 sentences)",
            },
            "reason": {
                "type": "string",
                "enum": [
                    "user_requested",
                    "could_not_answer",
                    "out_of_scope",
                    "staff_action_required",
                    "inappropriate_language",
                    "negative_feedback",
                    "other",
                ],
                "description": (
                    "Category of escalation reason:\n"
                    "- user_requested: User explicitly asked for human help\n"
                    "- could_not_answer: Bot couldn't answer the question\n"
                    "- out_of_scope: Request outside bot capabilities (policy, etc.)\n"
                    "- staff_action_required: Needs action bot can't perform "
                    "(meter swap, credits, unassignment, etc.)\n"
                    "- inappropriate_language: Offensive/inappropriate content from user\n"
                    "- negative_feedback: User expressed dissatisfaction with bot\n"
                    "- other: Doesn't fit other categories"
                ),
            },
            "action_type": {
                "type": "string",
                "enum": [
                    "meter_unassignment",
                    "wallet_credit",
                    "hps_power_limit",
                    "meter_replacement",
                    "commissioning_retry",
                    "transaction_verification",
                    "other_action",
                ],
                "description": (
                    "Specific action needed (REQUIRED when reason=staff_action_required):\n"
                    "- meter_unassignment: Customer wants meter removed from account\n"
                    "- wallet_credit: Manual wallet/account credit needed - "
                    "ONLY use after verifying transaction via check_transaction_status tool. "
                    "If verification failed/unavailable, use transaction_verification instead\n"
                    "- transaction_verification: Transaction needs manual verification by staff "
                    "(use when check_transaction_status tool failed or unavailable)\n"
                    "- hps_power_limit: HPS power limit increase review\n"
                    "- meter_replacement: Physical meter swap needed\n"
                    "- commissioning_retry: Manual commissioning retry needed\n"
                    "- other_action: Other staff action needed"
                ),
            },
            "conversation_context": {
                "type": "string",
                "description": (
                    "Key details from the conversation that support staff need to act on this "
                    "escalation. MUST include any: meter numbers, transaction references, "
                    "payment amounts, account IDs, grid names, dates, and error messages "
                    "discussed. Format as a brief bullet list of facts, not a conversation recap."
                ),
            },
        },
        "required": ["question_summary", "reason"],
    },
}

# Training image tool definition
TRAINING_IMAGE_TOOL_DEF = {
    "name": "fetch_training_image",
    "description": (
        "Fetch a training/reference image from a Google Drive URL to help analyze or compare "
        "with user images. Use this when you need to retrieve a reference image mentioned in "
        "your instructions to help answer a question about equipment, error codes, etc."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Google Drive URL of the image to fetch (must be from drive.google.com or docs.google.com)",
            },
        },
        "required": ["url"],
    },
}


# User preference tool definitions — internal tools (not MCP)
STORE_PREFERENCE_TOOL_DEF = {
    "name": "store_user_preference",
    "description": (
        "Call this when the user expresses a preference about HOW you should respond. "
        "Examples: 'make it shorter', 'use bullet points', 'be more formal', "
        "'always include battery SOC'. Do NOT call for questions, data requests, or commands."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "preference_key": {
                "type": "string",
                "enum": [
                    "response_length",
                    "tone",
                    "format",
                    "field_inclusion",
                    "language_complexity",
                    "other",
                ],
                "description": "Category of the preference",
            },
            "preference_value": {
                "type": "string",
                "description": (
                    "The preference as a concise instruction to a future AI assistant. "
                    "Example: 'Keep grid status summaries under 5 bullet points'"
                ),
            },
        },
        "required": ["preference_key", "preference_value"],
    },
}

LIST_PREFERENCES_TOOL_DEF = {
    "name": "list_user_preferences",
    "description": "List all stored preferences for the current user.",
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

DELETE_PREFERENCE_TOOL_DEF = {
    "name": "delete_user_preference",
    "description": "Delete a user preference by its key.",
    "parameters": {
        "type": "object",
        "properties": {
            "preference_key": {
                "type": "string",
                "description": "The preference key to delete",
            },
        },
        "required": ["preference_key"],
    },
}

# All preference tool names for reference
PREFERENCE_TOOL_NAMES = {
    "store_user_preference",
    "list_user_preferences",
    "delete_user_preference",
}


def _build_packet_type_to_command() -> dict:
    """Derive reverse mapping (packet_type → slash command) from COMMAND_REGISTRY.

    Picks the shortest command for each packet_type to avoid aliases like /analyse vs /analyze.
    """
    from orchestrator.services.command_registry import get_expert_command_mapping

    # get_expert_command_mapping() returns {"/lpp": "light_preliminary_package", ...}
    forward = get_expert_command_mapping()
    # Invert: group commands by packet_type, pick shortest
    by_type: dict = {}
    for cmd, ptype in forward.items():
        if ptype not in by_type or len(cmd) < len(by_type[ptype]):
            by_type[ptype] = cmd
    return by_type


# Reverse mapping: packet_type → shortest slash command
# Used by _execute_tools_node to construct synthetic slash commands
# Derived from COMMAND_REGISTRY to stay in sync automatically.
PACKET_TYPE_TO_COMMAND = _build_packet_type_to_command()

# Natural language expert routing — virtual tool definition
NL_EXPERT_TOOL_DEF = {
    "name": "start_expert_workflow",
    "description": (
        "Triggers a multi-step expert workflow to CREATE, GENERATE, or PRODUCE "
        "a deliverable document or report. Use this when the user asks to:\n"
        "- Create an LPP (Light Preliminary Package) for a site/village\n"
        "- Generate a GTR (Grid Technical Review) for grid(s)\n"
        "- Analyze a grid's performance or issues in depth\n"
        "- Generate a KPI or performance report\n"
        "- Ingest or learn a document into the knowledge base\n"
        "- Investigate code issues in Platform or Anansi codebase\n\n"
        "Do NOT use this for:\n"
        "- Simple questions about grid status, battery, power, or weather\n"
        "- Ticket lookups or JIRA searches\n"
        "- General conversation, greetings, or follow-up questions\n"
        "- Requests that already start with a / command"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "packet_type": {
                "type": "string",
                "enum": list(PACKET_TYPE_TO_COMMAND.keys()),
                "description": "The type of expert workflow to run",
            },
            "key_entity": {
                "type": "string",
                "description": "The site name, grid name, or subject for the workflow",
            },
        },
        "required": ["packet_type"],
    },
}

# Name for filtering in tool execution
NL_EXPERT_TOOL_NAME = "start_expert_workflow"


async def prepare_tools(state: ConversationState) -> Dict[str, Any]:
    """Prepare available tools based on user permissions.

    This node:
    1. Gets available tools from permissions service
    2. Adds escalation tool (available to all)
    3. Adds training image tool
    4. Wraps in Gemini format (functionDeclarations)

    Args:
        state: Current conversation state

    Returns:
        State updates with available_tools and tools_payload
    """
    # Use singleton services (not from state to avoid checkpointer serialization errors)
    permissions_service = get_permissions_service()
    user_context = state.get("user_context")

    # Get tools based on user roles
    available_tools: List[Dict[str, Any]] = await permissions_service.get_available_tools(
        user_context
    )
    if available_tools is None:
        available_tools = []
    LOGGER.info(f"User has access to {len(available_tools)} tools")

    # Add escalation tool (available to all users including staff for testing)
    available_tools.append(ESCALATION_TOOL_DEF)
    LOGGER.info(
        f"Added escalation tool (user is_staff={user_context.is_staff if user_context else 'unknown'})"
    )

    # Add training image tool
    available_tools.append(TRAINING_IMAGE_TOOL_DEF)
    LOGGER.info("Added training image tool for Google Drive images")

    # Add user preference tools (available to all users)
    available_tools.append(STORE_PREFERENCE_TOOL_DEF)
    available_tools.append(LIST_PREFERENCES_TOOL_DEF)
    available_tools.append(DELETE_PREFERENCE_TOOL_DEF)
    LOGGER.info("Added user preference tools")

    # Add natural language expert routing tool (staff only)
    # Staff-only gate: all expert workflows are staff_only=True in command_registry,
    # so this tool must only be visible to staff users to prevent bypass.
    is_staff = user_context.is_staff if user_context else False
    if is_staff:
        available_tools.append(NL_EXPERT_TOOL_DEF)
        LOGGER.info("Added NL expert routing tool (start_expert_workflow)")

    # Wrap in Gemini format (functionDeclarations wrapper)
    tools_payload = None
    if available_tools:
        tools_payload = [{"functionDeclarations": available_tools}]

    # Google Search grounding cannot be combined with function calling
    # (Gemini API rejects the combination as of March 2026).
    # Only add grounding when NO function tools are present.
    settings = get_settings()
    if is_staff and settings.gemini.google_search_grounding and not tools_payload:
        tools_payload = [{"google_search": {}}]  # type: ignore[list-item,dict-item]
        LOGGER.info("Added Google Search grounding (staff, no function tools)")

    # NOTE: ToolExecutor is NOT stored in state to avoid checkpointer serialization errors.
    # It's created locally in expert_handler.py when needed.

    return {
        "available_tools": available_tools,
        "tools_payload": tools_payload,
    }
