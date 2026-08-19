"""Tool schemas for the Equipment Diagnostics MCP server.

Extracted verbatim from ``handle_list_tools``, which had grown to 335 lines of
almost nothing but these literals.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user`` reads
to decide whether a non-staff user may see a tool, so it is load-bearing, not
decoration. All nine tools here set it to ``False`` — equipment diagnostics is
staff-only. (``tool_definitions.json`` disagrees, marking one of its five stale
equipment_diagnostics entries customer-visible; the code has never done so.)
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'get_equipment_status',
  'description': '[READ-ONLY] Get current real-time status of equipment at a grid site. '
                 'Returns inverter power per phase, battery state of charge, grid connection '
                 'status, PV power, and any active alarms. For "what equipment is installed" '
                 'rather than its live readings, use get_equipment_details instead.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site (fuzzy '
                                                              'matching supported)'},
                                 'metrics': {'type': 'array',
                                             'items': {'type': 'string'},
                                             'description': 'Specific metrics to retrieve: '
                                                            "'inverter', 'battery', 'grid', "
                                                            "'pv', 'alarms'. Defaults to all.",
                                             'default': ['inverter',
                                                         'battery',
                                                         'grid',
                                                         'pv',
                                                         'alarms']}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_site_info',
  'description': '[READ-ONLY] Get general site metadata: online/offline status, phase '
                 'configuration, equipment counts, and location. A summary, not live '
                 'readings — use get_equipment_status for current power/battery/alarm values, '
                 'or get_equipment_details for the individual equipment inventory.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_equipment_details',
  'description': '[READ-ONLY] Get the equipment inventory for a site — inverters, batteries, '
                 'and MPPTs with serial numbers and model information. Static inventory, not '
                 'live readings — use get_equipment_status for current power/battery/alarm '
                 'values.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_historical_power_data',
  'description': '[READ-ONLY] Get historical power time-series data, optionally with '
                 'server-side analysis (outage detection, peak-load finding, phase analysis, '
                 'summary stats) so you don\'t have to eyeball raw points. Up to 90 days via '
                 "time_range='last_90d', or a custom start_time/end_time range. For a single "
                 'specific outage already known to have happened, analyze_grid_outage gives a '
                 'more focused answer; for a chart image instead of data, use '
                 'generate_power_chart.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'},
                                 'time_range': {'type': 'string',
                                                'enum': ['last_hour',
                                                         'last_6h',
                                                         'last_24h',
                                                         'last_7d',
                                                         'last_30d',
                                                         'last_90d',
                                                         'custom'],
                                                'description': 'Time range for data retrieval',
                                                'default': 'last_24h'},
                                 'start_time': {'type': 'string',
                                                'description': 'ISO datetime for custom range '
                                                               'start'},
                                 'end_time': {'type': 'string',
                                              'description': 'ISO datetime for custom range '
                                                             'end'},
                                 'metrics': {'type': 'array',
                                             'items': {'type': 'string'},
                                             'description': 'Metrics to include: '
                                                            "'grid_consumption' (o1-o3, total "
                                                            'load-side consumption), '
                                                            "'grid_power', 'battery_soc', "
                                                            "'battery_power', 'pv_power'",
                                             'default': ['grid_power',
                                                         'grid_consumption',
                                                         'battery_soc']},
                                 'analysis': {'type': 'array',
                                              'items': {'type': 'string'},
                                              'description': "Analysis to perform: 'outages', "
                                                             "'peak_load', 'phase_analysis', "
                                                             "'summary_stats'",
                                              'default': []}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_historical_mppt_performance',
  'description': '[READ-ONLY] Get a time-series of estimated vs. actual power generation for '
                 'one or more specific MPPTs (solar chargers) — the tool for diagnosing why '
                 'one charger underperforms relative to its expected output. For overall site '
                 'power/consumption trends rather than a specific MPPT, use '
                 'get_historical_power_data instead.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site where the '
                                                              'MPPTs are located'},
                                 'time_range': {'type': 'string',
                                                'enum': ['last_hour',
                                                         'last_6h',
                                                         'last_24h',
                                                         'last_7d',
                                                         'last_30d',
                                                         'last_90d',
                                                         'custom'],
                                                'description': 'Time range for data retrieval',
                                                'default': 'last_24h'},
                                 'start_time': {'type': 'string',
                                                'description': 'ISO datetime for custom range '
                                                               'start'},
                                 'end_time': {'type': 'string',
                                              'description': 'ISO datetime for custom range '
                                                             'end'},
                                 'mppt_serial_numbers': {'type': 'array',
                                                         'items': {'type': 'string'},
                                                         'description': 'Optional list of MPPT '
                                                                        'serial numbers to '
                                                                        'query. If omitted, '
                                                                        'returns data for all '
                                                                        'MPPTs on the grid.'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'analyze_grid_outage',
  'description': "[READ-ONLY] Analyze one specific grid outage in detail — affected phases, "
                 "load at failure, and recovery pattern. Omit outage_time to analyze the most "
                 "recent outage. For finding outages in the first place or seeing multiple, "
                 "use get_historical_power_data with analysis=['outages'] instead.",
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'},
                                 'outage_time': {'type': 'string',
                                                 'description': 'Approximate time of outage '
                                                                '(ISO datetime). If not '
                                                                'provided, finds the most '
                                                                'recent outage.'},
                                 'search_window_minutes': {'type': 'integer',
                                                           'description': 'Minutes around '
                                                                          'outage_time to '
                                                                          'search',
                                                           'default': 60}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'generate_power_chart',
  'description': '[READ-ONLY] Generate a PNG chart visualizing power data over time — use '
                 'when the user wants a picture, not just numbers (get_historical_power_data '
                 'returns the same underlying data without an image). chart_type is required '
                 'and has no default; pick the one matching the question (e.g. '
                 "'battery_soc' for charge-level questions, 'outage_events' for downtime).",
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'},
                                 'chart_type': {'type': 'string',
                                                'enum': ['power_timeline',
                                                         'battery_soc',
                                                         'grid_vs_inverter',
                                                         'load_distribution',
                                                         'outage_events'],
                                                'description': 'Type of chart to generate'},
                                 'time_range': {'type': 'string',
                                                'enum': ['last_hour',
                                                         'last_6h',
                                                         'last_24h',
                                                         'last_7d',
                                                         'last_30d',
                                                         'last_90d'],
                                                'description': 'Time range for chart data',
                                                'default': 'last_24h'},
                                 'highlight_events': {'type': 'boolean',
                                                      'description': 'Highlight outage events '
                                                                     'on the chart',
                                                      'default': True}},
                  'required': ['grid_name', 'chart_type']},
  'visible_to_customer': False},
 {'name': 'get_batch_downtime_summary',
  'description': '[READ-ONLY] Get a downtime summary (default 24h window, see hours) for '
                 'multiple grids in parallel — the efficient choice for "how are all my grids '
                 'doing" instead of calling analyze_grid_outage per grid. Returns total '
                 'downtime minutes, outage count, and status icon per grid.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_names': {'type': 'array',
                                                'items': {'type': 'string'},
                                                'description': 'List of grid names to check'},
                                 'hours': {'type': 'integer',
                                           'description': 'Number of hours to analyze '
                                                          '(default: 24)',
                                           'default': 24},
                                 'max_concurrent': {'type': 'integer',
                                                    'description': 'Max parallel API calls '
                                                                   '(default: 5)',
                                                    'default': 5}},
                  'required': ['grid_names']},
  'visible_to_customer': False},
 {'name': 'schedule_equipment_check',
  'description': "[READ-ONLY] Build the details of a follow-up equipment check — useful after "
                 "control actions (restart inverter, reboot comms) to verify success. "
                 "IMPORTANT: this does NOT itself create a persisted schedule — it only "
                 "validates the grid and returns the command/timing to schedule (grid_name, "
                 "resolved check_type, computed delay). To actually make the check happen "
                 "later, pass the returned command and timing to schedule_user_command "
                 "(schedule server) in the same turn. If delay_minutes is omitted, defaults to "
                 "12 min for check_type='site_online' (past restart_comms_chain's ~10 min "
                 "reconnect window) and 5 min otherwise.",
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'},
                                 'delay_minutes': {'type': 'integer',
                                                   'description': 'Minutes to wait before '
                                                                  "check. Omit to use the "
                                                                  "check_type-based default "
                                                                  "(see tool description)."},
                                 'check_type': {'type': 'string',
                                                'enum': ['grid_consumption',
                                                         'site_online',
                                                         'battery_status',
                                                         'full_status'],
                                                'description': 'What to check',
                                                'default': 'full_status'},
                                 'expected_condition': {'type': 'string',
                                                        'description': 'Expected condition to '
                                                                       'verify (e.g., '
                                                                       "'grid_consumption > "
                                                                       "1000', 'is_online == "
                                                                       "true')"},
                                 'notify_on_failure': {'type': 'boolean',
                                                       'description': 'Send alert if expected '
                                                                      'condition not met',
                                                       'default': True}},
                  'required': ['grid_name']},
  'visible_to_customer': False}]
