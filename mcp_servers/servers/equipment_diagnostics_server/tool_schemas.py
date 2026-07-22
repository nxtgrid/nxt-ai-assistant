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
  'description': 'Get current real-time status of equipment at a grid site. Returns inverter '
                 'power per phase, battery state of charge, grid connection status, PV power, '
                 'and any active alarms.',
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
  'description': 'Get general site information including online/offline status, phase '
                 'configuration, equipment counts, and location.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_equipment_details',
  'description': 'Get detailed equipment inventory including inverters, batteries, and MPPTs '
                 'with serial numbers and model information.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_historical_power_data',
  'description': 'Get historical power data for analysis. Can detect grid outages, find peak '
                 'loads, and analyze power patterns. Supports up to 90 days of historical '
                 'data.',
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
  'description': 'Get historical performance data for one or more specific MPPTs. Provides a '
                 'time-series of estimated vs. actual power generation, which is essential for '
                 'diagnosing underperformance of a specific solar charger.',
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
  'description': 'Analyze a specific grid outage event in detail. Identifies affected phases, '
                 'load at failure, recovery pattern.',
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
  'description': 'Generate a chart visualizing power data over time. Returns PNG image '
                 'suitable for Telegram.',
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
  'description': 'Get 24-hour downtime summary for multiple grids in parallel. Efficient batch '
                 'operation with concurrency control. Returns total downtime minutes, outage '
                 'count, and status icon per grid.',
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
  'description': 'Schedule a follow-up check of equipment status. Useful after control actions '
                 '(restart inverter, reboot comms) to verify success. Default: 5 min for '
                 'inverter, 12 min for comms.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid site'},
                                 'delay_minutes': {'type': 'integer',
                                                   'description': 'Minutes to wait before '
                                                                  'check',
                                                   'default': 5},
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
