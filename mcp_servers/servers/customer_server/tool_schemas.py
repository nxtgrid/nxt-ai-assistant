"""Tool schemas for the Customer MCP server.

The complete advertised manifest for this server — kept in exact sync with
``mcp_servers/tool_definitions.json`` (enforced by
``mcp_servers/tests/test_tool_manifest_sync.py``). The JSON manifest is what
the orchestrator serves in production (``server_registry.list_tools`` prefers
it), so where the two disagreed historically, the JSON content won: these
entries were reconciled verbatim from the manifest, superseding the older
descriptions that lived in ``handle_list_tools``.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user`` reads
to decide whether a non-staff user may see a tool, so it is load-bearing, not
decoration. Mutating tools (turn_meter_*, resend_*, set_meter_*,
retry_commissioning) rely on it plus server-side org scoping, rate limits, and
the CUSTOMER_METER_ACTIONS_ENABLED gate. ``command_gated`` (turn_meter_on/off)
and any other extra keys ride along into the manifest untouched.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'meter_information',
  'description': '[READ-ONLY] Get customer account details, credit balance, and recent token '
                 'history for a specific meter. Also returns commissioning_date (datetime of most '
                 'recent commissioning attempt) and commissioning_status (e.g. SUCCESSFUL, FAILED, '
                 'PROCESSING) when a commissioning record exists.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string'},
                                 'organization_id': {'type': 'integer'}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'customer_get_meter_consumption',
  'description': '[READ-ONLY] Get daily consumption history for a meter over a time range (default '
                 '30 days). Returns a chart image and daily totals/max values from hourly '
                 "snapshots. Use when the user asks about a meter's usage, consumption pattern, or "
                 'energy history.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'Meter number (external '
                                                                 'reference)'},
                                 'days_back': {'type': 'integer',
                                               'description': 'Number of days to look back '
                                                              '(default 30, max 365)',
                                               'default': 30}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'customer_get_grid_chat_chronology',
  'description': '[READ-ONLY] Get a chronological timeline of all chat messages related to a '
                 'specific grid — from its O&M group topic, individual org users, and developer '
                 'groups. Use when asked about communication history, customer complaints, or '
                 'recent discussions about a grid. Accepts grid name OR organization name (e.g., '
                 "'AcmeCorp' resolves to its grid 'ExampleGrid'). Returns annotated messages with "
                 'source context.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Grid name or organization name '
                                                              '(supports fuzzy matching)'},
                                 'days_back': {'type': 'integer',
                                               'description': 'Number of days to look back '
                                                              '(default 7, max 90)',
                                               'default': 7}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'customer_list_grid_meters',
  'description': '[READ-ONLY] List all meters for a grid with their power limits, HPS mode '
                 'presets, status, credit balance, and phase count. Excludes cabin/AC meters. Use '
                 'this when the user asks about meters across a grid — e.g., which meters have a '
                 'specific power limit, how many meters are on/off, or bulk meter status. Supports '
                 'fuzzy grid name matching.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (required)'}},
                  'required': ['grid_name']},
  'visible_to_customer': True},
 {'name': 'customer_get_meters_on_pole',
  'description': '[READ-ONLY] List all meters connected to a specific pole. Use when the user asks '
                 'which meters are on a pole, or wants to check status of meters at a particular '
                 'pole location. Provide the pole reference number (printed on the pole label).',
  'inputSchema': {'type': 'object',
                  'properties': {'pole_reference': {'type': 'string',
                                                    'description': 'Pole reference number (e.g., '
                                                                   "'701741', 'CVP006')"},
                                 'grid_name': {'type': 'string',
                                               'description': 'Grid name to narrow search '
                                                              '(optional, helps if pole reference '
                                                              'is ambiguous)'}},
                  'required': ['pole_reference']},
  'visible_to_customer': True},
 {'name': 'customer_get_grid_status',
  'description': '[READ-ONLY] Get comprehensive status for a single grid including power status '
                 '(HPS/FS), capacity (kWp/kWh), DCU connectivity, and current weather. You MUST '
                 'provide the grid_name parameter.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (required)'}},
                  'required': ['grid_name']},
  'visible_to_customer': True},
 {'name': 'customer_get_all_grids_status',
  'description': '[READ-ONLY] Status dashboard for all accessible grids, including service status '
                 'icons and weather.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': True},
 {'name': 'customer_get_last_gtr_summary',
  'description': '[READ-ONLY] Get the last Grid Technical Report (GTR) summary for a specific '
                 'grid. Returns KPI values, commentary, and pending issues from the most recent '
                 'monthly review.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Grid name to look up GTR for'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'customer_get_fs_daily_summary',
  'description': '[READ-ONLY] Get daily FS (Full Service) summary for a grid showing scheduled '
                 'command executions vs actual state transitions. Includes daily FS ON hours, '
                 'command delivery rates, and discrepancy detection. Supports arbitrary date '
                 'ranges (max 30 days, defaults to yesterday + today).',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (required)'},
                                 'start_date': {'type': 'string',
                                                'description': 'Start date YYYY-MM-DD (inclusive, '
                                                               'defaults to yesterday)'},
                                 'end_date': {'type': 'string',
                                              'description': 'End date YYYY-MM-DD (inclusive, '
                                                             'defaults to today)'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'check_payment_completion',
  'description': '[READ-ONLY] Check the completion status of a payment transaction. Provide the '
                 'transaction reference exactly as given by the customer from their receipt or '
                 'records to see if the payment was successful on the payment processor, whether '
                 'the order is marked as completed, and the status of any associated directive '
                 '(meter token). Do NOT construct or guess transaction references — they must come '
                 'from the customer.',
  'inputSchema': {'type': 'object',
                  'properties': {'transaction_reference': {'type': 'string',
                                                           'description': 'Transaction reference '
                                                                          'exactly as provided by '
                                                                          'the customer from their '
                                                                          'receipt or records. Do '
                                                                          'NOT construct or guess '
                                                                          'this value.'}},
                  'required': ['transaction_reference']},
  'visible_to_customer': True},
 {'name': 'retry_commissioning',
  'description': '[ACTION - STARTS METER COMMISSIONING] Retry a failed or stuck meter '
                 'commissioning. Looks up the last commissioning attempt and re-triggers it via '
                 'the platform. Only works if the previous attempt was not successful and no retry '
                 'is already in progress. IMPORTANT: Use this tool only if explicitly requested by '
                 'a user or if its use is directly a step in a predefined procedure in your '
                 'instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference) to retry '
                                                                 'commissioning for'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': False},
 {'name': 'unassign_meter',
  'description': '[ACTION - UNASSIGNS METER] Unassign a meter from its current connection. This '
                 'removes the meter from the customer it is assigned to. Typically the first step '
                 'in a reassignment flow — after unassigning, the meter must be manually '
                 'reconnected to a new customer. Use when a meter is assigned to the wrong '
                 'customer or needs to be decommissioned. IMPORTANT: Use this tool only if '
                 'explicitly requested by a user or if its use is directly a step in a predefined '
                 'procedure in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference) to unassign'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'set_meter_power_limit',
  'description': '[ACTION - SETS HPS POWER LIMIT] Set the maximum power limit (HPS threshold) for '
                 'a meter. Sends a SET_POWER_LIMIT interaction to the meter via the platform. Only '
                 'the values listed in the enum are supported: 200W = standard HPS customer, 600W '
                 '= high power tariff upgrade. Change takes effect on the next meter communication '
                 'cycle. IMPORTANT: Use this tool only if explicitly requested by a user or if its '
                 'use is directly a step in a predefined procedure in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'power_limit_watts': {'type': 'string',
                                                       'enum': ['200', '600'],
                                                       'description': 'Power limit in watts. 200 = '
                                                                      'standard HPS, 600 = high '
                                                                      'power. NOTE: if '
                                                                      'CUSTOMER_METER_POWER_LIMIT_OPTIONS '
                                                                      'env var is changed, update '
                                                                      'this enum to match.'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number', 'power_limit_watts']},
  'visible_to_customer': False},
 {'name': 'set_meter_date',
  'description': "[ACTION - SETS METER DATE] Synchronise the date on a meter to today's date "
                 '(Africa/Lagos time). Sends a SET_DATE interaction to the meter via the platform. '
                 'Use when a meter is showing the wrong date, or as part of meter '
                 'diagnostics/commissioning. Change takes effect on the next meter communication '
                 'cycle. IMPORTANT: Use this tool only if explicitly requested by a user or if its '
                 'use is directly a step in a predefined procedure in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'turn_meter_on',
  'command_gated': True,
  'description': '[ACTION - TURNS METER ON] Turn on the relay for a meter, restoring power to the '
                 'customer. Sends a TURN_ON interaction to the meter via the platform. Change '
                 'takes effect on the next meter communication cycle. IMPORTANT: Use this tool '
                 'only if explicitly requested by a user via the /meter_on command.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': False},
 {'name': 'turn_meter_off',
  'command_gated': True,
  'description': '[ACTION - TURNS METER OFF] Turn off the relay for a meter, cutting power to the '
                 'customer. Sends a TURN_OFF interaction to the meter via the platform. Change '
                 'takes effect on the next meter communication cycle. IMPORTANT: Use this tool '
                 'only if explicitly requested by a user via the /meter_off command.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': False},
 {'name': 'resend_meter_token',
  'description': '[ACTION - RESENDS TOP_UP TOKEN] Resend the last TOP_UP prepayment token to a '
                 'meter. Looks up the most recent TOP_UP directive token and delivers it again. '
                 'Only resends top-up (credit) tokens — not power limit or other control tokens. '
                 'Use when a customer did not receive or lost their top-up token. IMPORTANT: Use '
                 'this tool only if explicitly requested by a user or if its use is directly a '
                 'step in a predefined procedure in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': True},
 {'name': 'resend_clear_tamper_token',
  'description': '[ACTION - RESENDS CLEAR_TAMPER TOKEN] Resend the last CLEAR_TAMPER token to a '
                 'meter. Looks up the most recent CLEAR_TAMPER directive token and delivers it '
                 'again. Use when a customer has a tamper flag that needs clearing and did not '
                 'receive or lost the clear-tamper token. IMPORTANT: Use this tool only if '
                 'explicitly requested by a user or if its use is directly a step in a predefined '
                 'procedure in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': False},
 {'name': 'resend_power_limit_token',
  'description': '[ACTION - RESENDS PLS TOKEN] Resend the last PLS (power limit set) token to a '
                 'meter. Looks up the most recent PLS directive token and delivers it again. Use '
                 'when a power limit change was issued but the customer did not receive or the '
                 'meter did not apply the token. IMPORTANT: Use this tool only if explicitly '
                 'requested by a user or if its use is directly a step in a predefined procedure '
                 'in your instructions.',
  'inputSchema': {'type': 'object',
                  'properties': {'meter_number': {'type': 'string',
                                                  'description': 'The meter number (external '
                                                                 'reference)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': ['meter_number']},
  'visible_to_customer': False},
 {'name': 'find_payment',
  'description': '[READ-ONLY] Search for a payment order from any receipt type (EOS/NXT Pay '
                 'screenshot, FirstBank receipt, OPay receipt, etc.). Use when the exact '
                 'transaction reference is not available. Provide any combination of: '
                 'customer/sender name, amount, date — at least one is required. Searches both the '
                 'transaction reference and the registered customer name, so it works even when '
                 'the bank sender name differs from the EOS customer name. Date-only inputs (e.g. '
                 '2026-05-29) search the full day; datetime inputs use a ±2h window by default. '
                 'Amount tolerance is ±5%. If exactly one match is found, automatically verifies '
                 'it with the payment processor. If zero or multiple matches are found, returns an '
                 'LLM-readable explanation with suggestions for next steps.',
  'inputSchema': {'type': 'object',
                  'properties': {'customer_name': {'type': 'string',
                                                   'description': 'Customer or sender name from '
                                                                  'the receipt. Works for EOS '
                                                                  'customer names, bank sender '
                                                                  "names (e.g. 'ADAMU SULEIMAN' "
                                                                  'from FirstBank), or OPay sender '
                                                                  'names. Each word is matched '
                                                                  'independently.'},
                                 'amount': {'type': 'number',
                                            'description': 'Payment amount from the receipt (±5% '
                                                           'tolerance applied)'},
                                 'date': {'type': 'string',
                                          'description': 'Date or datetime from receipt in ISO '
                                                         'format. Date-only (e.g. 2026-05-29) '
                                                         'searches the full calendar day. Datetime '
                                                         '(e.g. 2026-05-29T16:42:43) uses a ±2h '
                                                         'window by default.'},
                                 'organization_name': {'type': 'string',
                                                       'description': 'Organization name prefix if '
                                                                      'visible on receipt '
                                                                      '(optional)'},
                                 'time_window_hours': {'type': 'number',
                                                       'description': 'Hours before/after the '
                                                                      'provided datetime to search '
                                                                      '(default 2.0). Only applies '
                                                                      'to datetime inputs, not '
                                                                      'date-only inputs.'}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'lookup_transactions',
  'description': '[READ-ONLY] List payment transactions for the current organization (or all orgs '
                 'if staff). All filters are optional — omit all to see recent transactions. '
                 'Supports filtering by date range, reference number, amount, and receiver name '
                 '(accepts meter numbers or partial person names). Returns amount, date/time, '
                 'reference number, receiver name, and order status for each match.',
  'inputSchema': {'type': 'object',
                  'properties': {'date_from': {'type': 'string',
                                               'description': 'Start of date range in ISO format, '
                                                              'e.g. 2026-01-01 or '
                                                              '2026-01-01T00:00:00 (optional, '
                                                              'inclusive)'},
                                 'date_to': {'type': 'string',
                                             'description': 'End of date range in ISO format, e.g. '
                                                            '2026-01-31 or 2026-01-31T23:59:59 '
                                                            '(optional, inclusive)'},
                                 'reference_number': {'type': 'string',
                                                      'description': 'Partial or full transaction '
                                                                     'reference number '
                                                                     '(case-insensitive substring '
                                                                     'match)'},
                                 'amount': {'type': 'number',
                                            'description': 'Payment amount to filter by (±5% '
                                                           'tolerance)'},
                                 'receiver_name': {'type': 'string',
                                                   'description': 'Partial receiver name or meter '
                                                                  'number (fuzzy match — each word '
                                                                  'matched independently)'},
                                 'limit': {'type': 'integer',
                                           'description': 'Maximum number of results to return '
                                                          '(default 20, max 50)'},
                                 'user_email': {'type': 'string',
                                                'description': 'Email of the requesting user '
                                                               '(injected by orchestrator)'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'get_my_open_issues',
  'description': "[READ-ONLY] List this organisation's open support escalations, optionally "
                 'filtered by issue type. Returns a summary count by type plus individual issue '
                 'details (summary, reason, when raised). Issue types: token, hps, meter, '
                 'transaction, commissioning, other.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_type': {'type': 'string',
                                                'description': 'Filter by issue type: token, hps, '
                                                               'meter, transaction, commissioning, '
                                                               'or other. Omit to return all open '
                                                               'issues.'},
                                 'organization_id': {'type': 'integer',
                                                     'description': 'Organization ID (injected by '
                                                                    'orchestrator)'}},
                  'required': []},
  'visible_to_customer': True}]
