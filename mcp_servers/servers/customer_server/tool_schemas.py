"""Tool schemas for the Customer MCP server.

Extracted verbatim from ``handle_list_tools``, which had grown to 250 lines of
almost nothing but these literals.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user`` reads
to decide whether a non-staff user may see a tool, so it is load-bearing, not
decoration. Every tool here is customer-visible.

Note that ``handle_call_tool`` implements considerably more tools than are listed
here — ``mcp_servers/tool_definitions.json`` is what the orchestrator actually
reads (``server_registry.list_tools`` prefers it over calling this module), and
that manifest advertises 22 customer tools against the 5 declared below. The
commented-out ``retry_commissioning`` block at the end of this file is one of
them: disabled here, still live in the manifest. Regenerating the manifest with
``scripts/export_tools.py`` would drop every tool not declared here.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'check_payment_completion',
  'description': '[READ-ONLY] Check the completion status of a payment transaction. Provide '
                 'the transaction reference exactly as given by the customer from their '
                 'receipt or records to see if the payment was successful on the payment '
                 'processor, whether the order is marked as completed, and the status of any '
                 'associated directive (meter token). This tool ONLY retrieves information - '
                 'it CANNOT retry payments, resend tokens, or modify orders. Do NOT construct '
                 'or guess transaction references - they must come from the customer.',
  'inputSchema': {'type': 'object',
                  'properties': {'transaction_reference': {'type': 'string',
                                                           'description': 'Transaction '
                                                                          'reference exactly '
                                                                          'as provided by the '
                                                                          'customer from their '
                                                                          'receipt or records. '
                                                                          'Do NOT construct or '
                                                                          'guess this value.'},
                                 'user_email': {'type': 'string',
                                                'description': '(Injected by orchestrator) '
                                                               "User's email for access "
                                                               'verification'},
                                 'organization_id': {'type': 'integer',
                                                     'description': '(Injected by '
                                                                    "orchestrator) User's "
                                                                    'organization ID'}},
                  'required': ['transaction_reference']},
  'visible_to_customer': True},
 {'name': 'find_payment',
  'description': '[READ-ONLY] Search for a payment order from any receipt type (EOS/NXT Pay '
                 'screenshot, FirstBank receipt, OPay receipt, etc.). Use when the exact '
                 'transaction reference is not available. Provide any combination of: '
                 'customer/sender name, amount, date — at least one is required. Searches both '
                 'the transaction reference and the registered customer name, so it works even '
                 'when the bank sender name differs from the EOS customer name. Date-only '
                 'inputs (e.g. 2026-05-29) search the full day; datetime inputs use a ±2h '
                 'window by default. Amount tolerance is ±5%. If exactly one match is found, '
                 'automatically verifies it with the payment processor. If zero or multiple '
                 'matches are found, returns an LLM-readable explanation with suggestions for '
                 'next steps.',
  'inputSchema': {'type': 'object',
                  'properties': {'customer_name': {'type': 'string',
                                                   'description': 'Customer or sender name '
                                                                  'from the receipt. Works for '
                                                                  'EOS customer names, bank '
                                                                  "sender names (e.g. 'ADAMU "
                                                                  "SULEIMAN' from FirstBank), "
                                                                  'or OPay sender names. Each '
                                                                  'word is matched '
                                                                  'independently.'},
                                 'amount': {'type': 'number',
                                            'description': 'Payment amount from the receipt '
                                                           '(±5% tolerance applied)'},
                                 'date': {'type': 'string',
                                          'description': 'Date or datetime from receipt in ISO '
                                                         'format. Date-only (e.g. 2026-05-29) '
                                                         'searches the full calendar day. '
                                                         'Datetime (e.g. 2026-05-29T16:42:43) '
                                                         'uses a ±2h window by default.'},
                                 'organization_name': {'type': 'string',
                                                       'description': 'Organization name '
                                                                      'prefix if visible on '
                                                                      'receipt (optional)'},
                                 'time_window_hours': {'type': 'number',
                                                       'description': 'Hours before/after the '
                                                                      'provided datetime to '
                                                                      'search (default 2.0). '
                                                                      'Only applies to '
                                                                      'datetime inputs, not '
                                                                      'date-only inputs.'},
                                 'user_email': {'type': 'string',
                                                'description': '(Injected by orchestrator) '
                                                               "User's email for access "
                                                               'verification'},
                                 'organization_id': {'type': 'integer',
                                                     'description': '(Injected by '
                                                                    "orchestrator) User's "
                                                                    'organization ID'}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'meter_information',
  'description': '[READ-ONLY] Get comprehensive information about a meter including customer '
                 'details, connection type, grid status, DCU connectivity, meter power state, '
                 'credit balance, power limits (including HPS mode limit), and connection '
                 'quality metrics. Also shows the latest 5 directives sent to the meter '
                 '(commissioning, tokens, configuration changes, etc.) and highlights the most '
                 'recent failed directive if any. This tool ONLY retrieves and displays '
                 'information - it CANNOT retry commissioning, send directives, add credit, '
                 'change settings, or take any actions on the meter. Useful for '
                 'troubleshooting meter issues, checking customer account status, and tracking '
                 'directive delivery.\n'
                 '\n'
                 'Response Fields:\n'
                 '- meter_found: Boolean indicating if meter exists for this organization\n'
                 '- meter_number: The meter number queried\n'
                 '- customer_name: Name of the customer this meter belongs to\n'
                 '- connection_type: Type of connection (Residential, Commercial, etc.)\n'
                 '- grid_name: Name of the grid the meter is connected to\n'
                 "- grid_status: Grid online/offline status ('grid is energized' or 'grid is "
                 "down')\n"
                 "- dcu_status: DCU connectivity ('dcu is online' or 'dcu is offline')\n"
                 '- is_on: Boolean - whether meter is currently powered on\n'
                 '- is_on_updated_at: Timestamp when is_on was last updated. Old timestamps '
                 'indicate lack of successful communication with the meter.\n'
                 '- kwh_credit_available: Available kWh credit balance on the meter\n'
                 '- kwh_credit_available_updated_at: Timestamp when credit balance was last '
                 'updated. Old timestamps indicate lack of successful communication with the '
                 'meter.\n'
                 '- power_limit: Power limit in watts configured for this meter\n'
                 '- power_limit_hps_mode: Power limit in watts when grid is in High Priority '
                 'Service (HPS) mode. This is the reduced power level the meter will be '
                 'limited to when the grid is operating in HPS mode (limited solar/battery '
                 "capacity). Requests to change 'power limit in HPS mode' refer to this "
                 'value.\n'
                 '- connection_metrics: JSON object with connection quality data (signal '
                 'strength, last_seen)\n'
                 '- directives_count: Number of recent directives (max 5)\n'
                 '- directives: Array of most recent directives with id, type, status, '
                 'created_at, updated_at\n'
                 '- last_error_directive: Most recent failed directive with error details '
                 '(null if none)\n'
                 '- last_successful_token: Most recent successful token directive (null if '
                 'none)\n'
                 '  - directive_id (integer): Directive ID\n'
                 '  - token (string): The token value that was successfully delivered\n'
                 "  - token_type (string): Type of token directive (always 'TOKEN')\n"
                 '  - created_at (string): ISO timestamp when created\n'
                 '  - updated_at (string): ISO timestamp when completed\n'
                 '- message: Human-readable summary',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'Meter number to check '
                                                                 'commissioning status for'},
                                 'user_email': {'type': 'string',
                                                'description': '(Injected by orchestrator) '
                                                               "User's email for access "
                                                               'verification'},
                                 'organization_id': {'type': 'integer',
                                                     'description': '(Injected by '
                                                                    "orchestrator) User's "
                                                                    'organization ID'}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'customer_get_grid_status',
  'description': '[READ-ONLY] Get comprehensive status for a grid including power status '
                 '(HPS/FS), capacity (kWp/kWh), DCU connectivity, and current weather. You '
                 'MUST provide the grid_name parameter.\n'
                 '\n'
                 'Response Fields:\n'
                 '- grid_name: Name of the grid\n'
                 '- grid_id: Grid ID\n'
                 '- status.hps_on: Boolean - whether High Power Service is active\n'
                 '- status.hps_updated_at: Timestamp when HPS status was last updated\n'
                 '- status.fs_on: Boolean - whether Full Service is active\n'
                 '- status.fs_updated_at: Timestamp when FS status was last updated\n'
                 '- capacity.kwp: Installed solar capacity in kilowatt-peak\n'
                 '- capacity.kwh: Battery storage capacity in kilowatt-hours\n'
                 '- dcus.all_online: Boolean - whether all DCUs are online\n'
                 '- dcus.offline_dcus: Array of offline DCUs (if any) with name and '
                 'last_online_at\n'
                 '- weather: Current weather at the grid location',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (optional - '
                                                              'defaults to first visible grid '
                                                              'for organization)'}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'customer_get_all_grids_status',
  'description': '[READ-ONLY] Get status of all grids accessible to the user, grouped by '
                 'operational status. Returns grids with icons for status, DCU connectivity, '
                 'weather, and current inverter power:\n'
                 'Status icons: 🟢 FS On | 🟡 HPS On | 🔌 Likely Isolated | 🔴 Off | Ⅹ Unknown\n'
                 'DCU icons: 📶 All online | ⚠️ Some offline\n'
                 'Weather icons: ☀️ ⛅ ☁️ 🌧️ ⛈️ 🌫️ ❄️ 💨\n'
                 '\n'
                 'Each grid includes inverter_power_kw (current total inverter output in kW '
                 "from VRM). 'Likely Isolated' means inverter is ON but power is below HPS "
                 'threshold.\n'
                 '\n'
                 "Staff users see all grids. Other users see only their organization's grids. "
                 'Hidden grids (is_hidden_from_reporting=true) are excluded.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': True}]

# Disabled in code but still advertised by mcp_servers/tool_definitions.json,
# which is what server_registry.list_tools actually returns. Kept verbatim from
# handle_list_tools so the manifest entry has a visible counterpart here.
# types.Tool(
#     name="retry_commissioning",
#     description=(
#         "[ACTION - STARTS METER COMMISSIONING] Retry the commissioning process for a meter. "
#         "This action initiates a new commissioning attempt using the meter's last commissioning ID. "
#         "IMPORTANT: This process takes 2-5 minutes to complete and CANNOT be retried if it fails. "
#         "Only use this tool when a previous commissioning attempt has failed and you need to try again. "
#         "The meter must have a previous commissioning attempt (last_commissioning_id must exist). "
#         "You can use the meter_information tool after 2-5 minutes to check if commissioning succeeded. "
#         "Staff-only tool (not visible to customers)."
#     ),
#     inputSchema={
#         "type": "object",
#         "properties": {
#             "meter_number": {
#                 "type": "string",
#                 "description": "Meter number to retry commissioning for",
#             },
#             "user_email": {
#                 "type": "string",
#                 "description": "Staff email address (required for access verification)",
#             },
#             "organization_id": {
#                 "type": "integer",
#                 "description": "Organization ID (optional, will be looked up if not provided)",
#             },
#         },
#         "required": ["meter_number", "user_email"],
#     },
#     visible_to_customer=False,
