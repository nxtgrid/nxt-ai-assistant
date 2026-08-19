"""Tool schemas for the Meters MCP server.

Extracted verbatim from ``handle_list_tools`` as part of migrating the server
onto ``shared_code.tool_registry.ToolRegistry``.

Plain dicts rather than ``types.Tool`` objects: ``ToolRegistry.handle_list_tools``
constructs a fresh ``Tool`` per call, so sharing model instances across calls
would let one caller's mutation reach the next.

All five tools set ``visible_to_customer: False`` — meters is staff-only.
``ActionFlags.is_actions_enabled("meters")`` was computed in the old
``handle_list_tools`` but only logged, never used to filter tools — none of
these are gated.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "get_meter_dcu_status",
        "description": (
            "[READ-ONLY] Get DCU/concentrator/gateway online status for any meter type. "
            "Automatically routes to the correct API based on meter type from Supabase: "
            "(1) Calin V1 meters - queries DCU online status via V1 API, (2) Calin V2 meters - "
            "queries RF concentrator online status via V2 API, (3) LoRaWAN meters - queries "
            "Chirpstack gateway status. Meter type, DCU ID, and gateway ID are automatically "
            "retrieved from Supabase 'meters' table based on meter_no. Returns online/offline "
            "status and last communication timestamp. This tool ONLY retrieves status "
            "information - it does NOT initiate communication with the physical meter or take "
            "any actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meter_no": {
                    "type": "string",
                    "description": "Meter number (all other info auto-retrieved from Supabase)",
                },
                "dcu_id": {
                    "type": "string",
                    "description": "DCU/concentrator ID (optional override - auto-retrieved from Supabase if not provided)",
                },
                "gateway_id": {
                    "type": "string",
                    "description": "LoRaWAN gateway ID (optional override - auto-retrieved from Supabase if not provided)",
                },
                "user_email": {
                    "type": "string",
                    "description": "(Injected by orchestrator) User email for RLS authentication",
                },
            },
            "required": ["meter_no"],
        },
        "visible_to_customer": False,
    },
    {
        "name": "get_dcu_status_by_id",
        "description": (
            "[READ-ONLY] Check whether a DCU/concentrator or LoRaWAN base station is currently "
            "online, using the device ID itself - no meter number needed. Use this when a ticket "
            "or alert references the device directly (e.g. 'DCU 230401080' or 'Base Station "
            "a84041ffff29d4da'). Device type is auto-detected from the meters table or the ID "
            "format (base station/gateway IDs are 16-char hex, DCU IDs are numeric). Returns "
            "online/offline status plus the meter numbers served by the device. This tool ONLY "
            "retrieves status information - it does NOT communicate with meters or take any "
            "actions."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "device_id": {
                    "type": "string",
                    "description": "DCU/concentrator ID (e.g. '230401080') or LoRaWAN base station/gateway ID (e.g. 'a84041ffff29d4da'), as referenced in the ticket or alert",
                },
                "device_type": {
                    "type": "string",
                    "description": "Optional: 'dcu' or 'base_station' to skip auto-detection",
                    "enum": ["dcu", "base_station"],
                },
                "user_email": {
                    "type": "string",
                    "description": "(Injected by orchestrator) User email for RLS authentication",
                },
            },
            "required": ["device_id"],
        },
        "visible_to_customer": False,
    },
    {
        "name": "create_meter_reading_task",
        "description": (
            "[ACTION - SENDS COMMAND TO METER] Create remote reading task for any meter type. "
            "This tool ACTIVELY COMMUNICATES with the physical meter by sending a command. "
            "Automatically routes to the correct API based on meter type from Supabase: "
            "(1) Calin V1 meters - uses PLC-based remote reading via V1 API, (2) Calin V2 meters "
            "- uses RF-based remote reading via V2 API with protocol IDs, (3) LoRaWAN meters - "
            "sends Chirpstack downlink with Calin protocol encoding. This is a TWO-STEP "
            "operation: (1) sends downlink command to meter, (2) waits 15 seconds, (3) checks "
            "uplink response from meter. Total time: approximately 15-20 seconds. IMPORTANT: "
            "Only call this tool ONCE per conversation response - do NOT batch multiple meter "
            "readings in a single response. "
            "Reading-type support DIFFERS by meter type (auto-detected from Supabase, not "
            "knowable in advance from this tool) — an unsupported combination fails immediately "
            "with a generic error, not a helpful one, so picking a supported type up front "
            "matters. Calin V2 supports: 'voltage', 'power', 'current_credit', 'relay_status', "
            "'power_down_count', 'maximum_power_threshold' (the meter's configured max-power "
            "cutoff — NOT the same as 'power_limit', which V2 does not support), "
            "'special_status', 'meter_version' — or a numeric protocol ID (e.g. 5 for voltage, "
            "39 for current_credit). LoRaWAN supports: 'voltage', 'current', 'power', 'energy', "
            "'current_credit', 'relay_status'. 'voltage', 'power', 'current_credit', and "
            "'relay_status' are the only four types that work on both. 'power_limit' is not "
            "supported on EITHER V2 or LoRaWAN — do not use it unless the meter is confirmed "
            "Calin V1. Calin V1 accepts any reading_type string as-is; validity is enforced by "
            "the V1 API itself, not checked here. If the meter's type is unknown, prefer one of "
            "the four cross-supported types, or check with the user first. Returns complete "
            "reading result with meter data."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meter_no": {
                    "type": "string",
                    "description": "Meter number (all other info auto-retrieved from Supabase)",
                },
                "reading_type": {
                    "type": "string",
                    "description": "Type of reading to request — support differs by meter type, see the tool description. 'voltage', 'power', 'current_credit', 'relay_status' work everywhere except unconfirmed V1 meters. For Calin V2, can also use a numeric protocol ID (e.g., 5, 11, 39, 46).",
                    "enum": [
                        "voltage",
                        "current",
                        "power",
                        "energy",
                        "current_credit",
                        "power_limit",
                        "relay_status",
                        "power_down_count",
                        "maximum_power_threshold",
                        "special_status",
                        "meter_version",
                    ],
                },
                "customer_id": {
                    "type": "string",
                    "description": "Customer ID (optional override - auto-retrieved from Supabase for V2 meters)",
                },
                "dev_eui": {
                    "type": "string",
                    "description": "Device EUI (optional override - auto-retrieved from Supabase for LoRaWAN meters)",
                },
                "user_email": {
                    "type": "string",
                    "description": "(Injected by orchestrator) User email for RLS authentication",
                },
            },
            "required": ["meter_no", "reading_type"],
        },
        "visible_to_customer": False,
    },
    {
        "name": "get_meter_reading_task_status",
        "description": (
            "[READ-ONLY] Check the status and retrieve results of a previously created reading "
            "task. Automatically routes to the correct API based on meter type from Supabase: "
            "(1) Calin V1 meters - queries task status via V1 API, (2) Calin V2 meters - queries "
            "task status via V2 API, (3) LoRaWAN meters - checks Chirpstack uplink messages. Use "
            "the task ID returned by create_meter_reading_task. Returns task status "
            "(pending/complete/failed) and meter reading data if complete. This tool ONLY "
            "retrieves status information - it does NOT send commands or take actions on the "
            "meter."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "meter_no": {
                    "type": "string",
                    "description": "Meter number (used to auto-detect meter type from Supabase)",
                },
                "task_id": {
                    "type": "string",
                    "description": "Task ID returned from create_meter_reading_task",
                },
                "user_email": {
                    "type": "string",
                    "description": "(Injected by orchestrator) User email for RLS authentication",
                },
            },
            "required": ["meter_no", "task_id"],
        },
        "visible_to_customer": False,
    },
]
