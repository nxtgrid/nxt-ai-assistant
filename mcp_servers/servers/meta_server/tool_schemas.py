"""Tool schemas for the Meta MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry``.

Plain dicts rather than ``types.Tool`` objects: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing model instances across calls
would let one caller's mutation reach the next.

All seven tools are ``gated=True`` in the server module — hidden from
``handle_list_tools`` and refused by ``handle_call_tool`` whenever
``META_ACTIONS_ENABLED`` is false (``ActionFlags.get_env_var_name("meta")``
resolves to exactly that name, so the registry's gating reads the same flag
the server always has). All are ``visible_to_customer: False`` — meta
analytics is staff-only.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_performance_report",
        "description": (
            "Get comprehensive bot performance report. "
            "Returns response distribution, escalation breakdown "
            "(including avg_time_to_close_minutes for resolved escalations, null if none), "
            "and feedback stats. "
            "Default: past 7 days, all organizations."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name (short or formal name)",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "response_distribution_chart",
        "description": (
            "Generate pie chart showing bot responses vs escalations. Returns PNG image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "escalation_types_chart",
        "description": (
            "Generate pie chart showing escalation reasons breakdown. Returns PNG image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "action_types_chart",
        "description": (
            "Generate pie chart showing action types for staff_action_required escalations. "
            "Returns PNG image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "list_escalated_messages",
        "description": (
            "Get list of recently escalated messages with context. "
            "Includes user message preview, reason, and timestamp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "list_negative_feedback",
        "description": (
            "Get list of bot messages that received negative feedback (thumbs down). "
            "Includes response preview and timestamp."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
    {
        "name": "issue_type_breakdown_chart",
        "description": (
            "Generate pie chart showing new conversation threads broken down by issue type "
            "(token, hps, meter, transaction, commissioning, other). Returns PNG image."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days to include (default: 7)",
                    "default": 7,
                },
                "organization": {
                    "type": "string",
                    "description": "Filter by organization name",
                },
            },
            "required": [],
        },
        "visible_to_customer": False,
    },
]
