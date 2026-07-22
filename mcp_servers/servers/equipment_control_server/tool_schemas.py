"""Tool schemas for the Equipment Control MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry``.

Plain dicts rather than ``types.Tool`` objects: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing model instances across calls
would let one caller's mutation reach the next.

Both tools set ``visible_to_customer: False`` — equipment control is staff-only.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "restart_inverter",
        "description": (
            "[ACTION - RESTARTS PHYSICAL EQUIPMENT] Restart the inverter at a specific site "
            "(requires equipment.control permission). This tool PHYSICALLY RESTARTS inverter "
            "hardware at the site. CRITICAL SAFETY CHECK REQUIRED: Before calling this action, "
            "you MUST verify with the user that there is no cause for repeated shorts at the "
            "site. Restarting inverters without checking for underlying electrical faults could "
            "cause serious equipment damage or create safety hazards. Always confirm the user "
            "has investigated the root cause before proceeding."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "grid": {"type": "string", "description": "Grid name"},
                "user_email": {
                    "type": "string",
                    "description": "User email for permission check (required)",
                },
            },
            "required": ["grid", "user_email"],
        },
        "visible_to_customer": False,
    },
    {
        "name": "restart_comms_chain",
        "description": (
            "[ACTION - RESTARTS PHYSICAL EQUIPMENT] Restart the communications chain at a "
            "specific site (requires equipment.control permission). This tool PHYSICALLY "
            "RESTARTS communication hardware (Cerbo, router, DCU) at the site, causing temporary "
            "downtime. This tool handles multiple reboot-related requests including: 'reboot "
            "comm chain', 'reboot cerbo', 'reboot router', and 'reboot DCU'. IMPORTANT: Before "
            "calling this action, you MUST verify with the user that the communications chain "
            "still has connectivity problems and that a restart is necessary. WARNING: When "
            "rebooting the DCU, only DCUs connected to the power plant will be rebooted - "
            "confirm this is the intended behavior with the user. Note that once restarted, it "
            "can take up to 10 minutes for the site to fully reconnect and resume normal "
            "operations. Always confirm this downtime is acceptable before proceeding."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "grid": {"type": "string", "description": "Grid name"},
                "user_email": {
                    "type": "string",
                    "description": "User email for permission check (required)",
                },
            },
            "required": ["grid", "user_email"],
        },
        "visible_to_customer": False,
    },
]
