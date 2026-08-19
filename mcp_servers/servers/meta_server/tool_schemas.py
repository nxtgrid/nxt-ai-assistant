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
            "[READ-ONLY] Get a comprehensive bot performance report: response-vs-escalation "
            "counts, escalation reasons and action types, avg_time_to_close_minutes for "
            "resolved escalations (null if none resolved), and feedback stats. Use for a "
            "single all-in-one summary; use the *_chart tools instead when the user wants a "
            "specific chart image, or list_escalated_messages/list_negative_feedback for "
            "individual message detail rather than aggregates."
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
            "[READ-ONLY] Generate a pie chart of the top-level split: how many bot messages "
            "were handled automatically vs. escalated to staff. The broadest of the meta "
            "charts — use escalation_types_chart, action_types_chart, or "
            "issue_type_breakdown_chart instead for a breakdown *within* one of those "
            "categories. Returns a PNG image."
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
            "[READ-ONLY] Generate a pie chart of escalations broken down by reason (why the "
            "bot handed off to staff — e.g. user_requested, could_not_answer, "
            "staff_action_required, safety_escalation). All escalations, not just ones "
            "needing a specific staff action — for that narrower slice use "
            "action_types_chart. Returns a PNG image."
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
            "[READ-ONLY] Generate a pie chart of only the staff_action_required escalations, "
            "broken down by which action was needed (e.g. meter_unassignment, wallet_credit, "
            "hps_power_limit, commissioning_retry). For the reason breakdown across ALL "
            "escalations, not just this subset, use escalation_types_chart instead. Returns a "
            "PNG image."
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
            "[READ-ONLY] List individual escalated messages with context — user message "
            "preview, escalation reason, and timestamp. Use for message-level detail; use "
            "escalation_types_chart or get_performance_report instead for aggregate counts. "
            "No result cap — prefer a narrower `days` window during busy periods."
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
            "[READ-ONLY] List bot messages that received negative feedback (thumbs down), "
            "with response preview and timestamp. Distinct from escalations — this is "
            "explicit user dissatisfaction, not a bot handoff. No result cap — prefer a "
            "narrower `days` window during busy periods."
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
            "[READ-ONLY] Generate a pie chart of NEW conversation threads broken down by "
            "issue type (token, hps, meter, transaction, commissioning, other). Different "
            "population from the other meta charts: this counts incoming conversation topics, "
            "not escalations or bot responses. Returns a PNG image."
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
