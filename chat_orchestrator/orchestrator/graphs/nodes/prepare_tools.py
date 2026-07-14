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

# Expert meta-tools -- read-only introspection into expert workflow recipes and
# packet state (Phase D of the agentic expert workflows effort). Like
# NL_EXPERT_TOOL_DEF above, these are plain-dict virtual tool declarations not
# backed by an mcp_servers server; dispatched in conversation_graph.py's
# _handle_expert_meta_tool_call (see EXPERT_META_TOOL_NAMES below, mirroring
# PREFERENCE_TOOL_NAMES's routing pattern).
EXPERT_LIST_STEPS_TOOL_DEF = {
    "name": "expert_list_steps",
    "description": (
        "Returns an expert workflow's recipe (ordered steps) merged with each "
        "function step's machine-readable data-dependency contract: which "
        "packet_state keys and prior-step results it reads/writes, and which "
        "parameters it accepts (including synonyms/alternate phrasings). Use "
        "this BEFORE trying to re-run or reason about a specific step of an "
        "expert workflow -- e.g. for a request like 'regenerate the map with "
        "different pole spacing', call this first to find which step actually "
        "produces the map and which parameter name controls pole spacing, "
        "instead of guessing at step or parameter names."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "expert_id": {
                "type": "string",
                "description": "Expert identifier that owns this workflow, e.g. 'lpp_expert'.",
            },
            "packet_type": {
                "type": "string",
                "description": (
                    "Packet type whose workflow recipe to inspect, e.g. "
                    "'light_preliminary_package'."
                ),
            },
        },
        "required": ["expert_id", "packet_type"],
    },
}

EXPERT_FIND_PACKET_TOOL_DEF = {
    "name": "expert_find_packet",
    "description": (
        "Finds an existing work packet -- of ANY status: in-progress, "
        "awaiting input, failed, blocked, or completed -- for a given expert "
        "packet type and site/grid/subject name, and summarizes its progress. "
        "Use this to check whether work already exists before starting fresh, "
        "e.g. 'is there already an LPP for Foo?' or 'has the GTR for Bar "
        "finished?'. Returns found=false (not an error) if nothing exists yet."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "packet_type": {
                "type": "string",
                "description": "Packet type to search for, e.g. 'light_preliminary_package'.",
            },
            "key_entity": {
                "type": "string",
                "description": "Site name, grid name, or subject the packet was created for.",
            },
        },
        "required": ["packet_type", "key_entity"],
    },
}

EXPERT_GET_PACKET_STATE_TOOL_DEF = {
    "name": "expert_get_packet_state",
    "description": (
        "Fetches the current state of a specific work packet by its "
        "packet_id -- packet_state for an in-progress/paused packet, with "
        "packet_outputs merged in for a completed one. Optionally filter to "
        "specific keys. Use this after expert_find_packet to inspect exactly "
        "what a packet has produced so far (e.g. a design_id, or a "
        "*_drive_id artifact reference)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "packet_id": {
                "type": "string",
                "description": (
                    "The packet_id returned by expert_find_packet, or referenced "
                    "earlier in the conversation."
                ),
            },
            "keys": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional list of specific state keys to fetch. Omit or leave "
                    "empty to fetch everything."
                ),
            },
        },
        "required": ["packet_id"],
    },
}

EXPERT_RUN_STEPS_TOOL_DEF = {
    "name": "expert_run_steps",
    "description": (
        "Runs one or more named expert workflow steps out of order -- the ONLY "
        "expert meta-tool that actually executes anything (the other three are "
        "read-only lookups). ALWAYS call expert_list_steps first to learn the "
        "real step names, their parameters (including synonyms), and their "
        "side_effects for the expert_id/packet_type in question -- never guess "
        "a step name.\n\n"
        "This tool may need to auto-run OTHER steps first to satisfy missing "
        "prerequisites (e.g. re-running a map step first requires the layout "
        "step that produced its input). It NEVER does this silently:\n"
        "- If the result has needs_confirmation=true, nothing has been run "
        "yet. Relay auto_inserted_steps (name + description + side_effects) "
        "and the message to the user. If they agree, call this tool again "
        "with the exact same steps/param_overrides_json/force PLUS "
        "confirmation_token set to the confirmation_token value from that "
        "response -- never invent, guess, or reuse a confirmation_token from "
        "a different call; it will simply be rejected and a fresh one "
        "returned.\n"
        "- If the result has blocked=true, nothing has been run and nothing "
        "will help -- relay the details (which step needs which missing "
        "item, with no way to produce it automatically) to the user; do NOT "
        "retry with a confirmation_token.\n"
        "- If the result has needs_user_input=true, execution stopped because "
        "a step is waiting on a question -- relay it to the user.\n"
        "- success=true with executed_steps means everything requested ran "
        "(or was already complete).\n\n"
        "Pass packet_id to act on an existing packet (from expert_find_packet), "
        "or omit it and pass expert_id/packet_type/key_entity to start a new one."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Ordered list of step names to run, e.g. "
                    "['generate_distribution_map']. Get real names from expert_list_steps."
                ),
            },
            "packet_id": {
                "type": "string",
                "description": (
                    "Existing packet to act on (from expert_find_packet). Omit to "
                    "create a new packet, in which case expert_id/packet_type/"
                    "key_entity are all required."
                ),
            },
            "expert_id": {
                "type": "string",
                "description": (
                    "Expert owning the workflow, e.g. 'lpp_expert'. Only used "
                    "when packet_id is omitted."
                ),
            },
            "packet_type": {
                "type": "string",
                "description": (
                    "Packet type to create, e.g. 'light_preliminary_package'. Only "
                    "used when packet_id is omitted."
                ),
            },
            "key_entity": {
                "type": "string",
                "description": (
                    "Site/grid/subject name for the new packet. Only used when "
                    "packet_id is omitted."
                ),
            },
            "param_overrides_json": {
                "type": "string",
                "description": (
                    "Optional JSON-encoded object of per-step parameter overrides, "
                    'shaped as \'{"step_name": {"param_name": value}}\'. Must be a '
                    "JSON string, not a raw object."
                ),
            },
            "force": {
                "type": "boolean",
                "description": (
                    "Re-run a step even if already marked completed, clearing its "
                    "guard keys first. Defaults to false."
                ),
            },
            "confirmation_token": {
                "type": "string",
                "description": (
                    "ONLY set this on a follow-up call, to the exact confirmation_token "
                    "value returned by a prior needs_confirmation response for this same "
                    "plan, after the user has explicitly agreed. Do not fabricate or "
                    "guess a value -- an invalid/stale token is simply rejected with a "
                    "fresh needs_confirmation response, never executed."
                ),
            },
        },
        "required": ["steps"],
    },
}

# All expert meta-tool names for reference (mirrors PREFERENCE_TOOL_NAMES)
EXPERT_META_TOOL_NAMES = {
    "expert_list_steps",
    "expert_find_packet",
    "expert_get_packet_state",
    "expert_run_steps",
}


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

        available_tools.append(EXPERT_LIST_STEPS_TOOL_DEF)
        available_tools.append(EXPERT_FIND_PACKET_TOOL_DEF)
        available_tools.append(EXPERT_GET_PACKET_STATE_TOOL_DEF)
        available_tools.append(EXPERT_RUN_STEPS_TOOL_DEF)
        LOGGER.info(
            "Added expert meta-tools (expert_list_steps/expert_find_packet/"
            "expert_get_packet_state/expert_run_steps)"
        )

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
