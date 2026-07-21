"""Tool schemas for the Grid Design MCP server.

Extracted verbatim from ``handle_list_tools``, which had grown to 1,016 lines —
almost entirely these literals. Keeping them as data leaves that function as the
list-tools logic it is meant to be: the gating check and the response.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

Every tool here is ``visible_to_customer: False`` — grid design is staff-only.
That field is what ``user_permissions.filter_tools_for_user`` reads to decide
whether a non-staff user may see a tool, so it is load-bearing, not decoration.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'design_and_bom',
  'description': 'Create a grid design and generate Bill of Materials (BOM). This tool: 1) '
                 "Creates a grid if it doesn't exist by name, 2) Creates a new design with "
                 'specified parameters (every parameter the old AppSheet design form offered '
                 'is accepted — technology choices, connection split, Wp/connection override, '
                 'regulation constraint, 3-phase enforcement, SPD type, distances, tariff), 3) '
                 'Runs auto-design sizing and BOM generation, 4) Returns energy specs, BOM '
                 'items and cost summary. Call list_design_options first to see valid '
                 'technology choices. Use this when users ask to create a new solar grid '
                 'design or generate a BOM.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (will be '
                                                              "created if it doesn't exist)"},
                                 'design_name': {'type': 'string',
                                                 'description': 'Name for the new design'},
                                 'max_connections': {'type': 'integer',
                                                     'description': 'Maximum number of '
                                                                    'connections for this grid '
                                                                    'design'},
                                 'community': {'type': 'string',
                                               'description': 'Community name/location for the '
                                                              'grid'},
                                 'technology_family': {'type': 'string',
                                                       'description': 'Power-plant technology '
                                                                      'family/architecture. '
                                                                      "Use 'deye' for Deye "
                                                                      'Hybrid ESS designs and '
                                                                      "'victron' for the "
                                                                      'legacy Victron '
                                                                      'container architecture.',
                                                       'enum': ['victron', 'deye'],
                                                       'default': 'victron'},
                                 'inverter_type': {'type': 'string',
                                                   'description': 'Inverter type (default: '
                                                                  'Quattro 15kVA)',
                                                   'default': 'Quattro 15kVA'},
                                 'battery_type': {'type': 'string',
                                                  'description': 'Battery type (default: '
                                                                 'Pylontech UP5000)',
                                                  'default': 'Pylontech UP5000'},
                                 'mppt_type': {'type': 'string',
                                               'description': 'MPPT type (default: Victron '
                                                              '250/85 MPPT)',
                                               'default': 'Victron 250/85 MPPT'},
                                 'pv_type': {'type': 'string',
                                             'description': 'PV panel type (default: JA455W '
                                                            'Panel)',
                                             'default': 'JA455W Panel'},
                                 'pv_inverter_type': {'type': 'string',
                                                      'description': 'PV Inverter type '
                                                                     '(optional)'},
                                 'initial_residential_connections': {'type': 'integer',
                                                                     'description': 'Initial '
                                                                                    'residential '
                                                                                    'connections '
                                                                                    '(default: '
                                                                                    '90% of '
                                                                                    'max_connections)'},
                                 'initial_business_connections': {'type': 'integer',
                                                                  'description': 'Initial '
                                                                                 'business '
                                                                                 'connections '
                                                                                 '(default: '
                                                                                 '10% of '
                                                                                 'max_connections)'},
                                 'initial_3phase_connections': {'type': 'integer',
                                                                'description': 'Initial '
                                                                               '3-phase '
                                                                               'connections '
                                                                               '(default: 0)',
                                                                'default': 0},
                                 'num_poc_teams': {'type': 'integer',
                                                   'description': 'Number of PoC teams to '
                                                                  'install meters (default: 1)',
                                                   'default': 1},
                                 'anchor_load_kw': {'type': 'number',
                                                    'description': 'Anchor load in kW '
                                                                   '(default: 0)',
                                                    'default': 0},
                                 'force_3phase': {'type': 'boolean',
                                                  'description': 'Force 3-phase design '
                                                                 '(default: false)',
                                                  'default': False},
                                 'target_kwp': {'type': 'number',
                                                'description': 'Target kWp to constrain the '
                                                               'design (optional, AppSheet '
                                                               'calculates freely if not '
                                                               'provided)'},
                                 'target_kwh': {'type': 'number',
                                                'description': 'Target kWh to constrain the '
                                                               'design (optional, AppSheet '
                                                               'calculates freely if not '
                                                               'provided)'},
                                 'avg_service_drop_length_m': {'type': 'number',
                                                               'description': 'Average service '
                                                                              'drop cable '
                                                                              'length per '
                                                                              'connection in '
                                                                              'meters '
                                                                              '(default: 25)',
                                                               'default': 25},
                                 'wp_per_conn_override': {'type': 'number',
                                                          'description': 'Override the '
                                                                         'Wp-per-connection '
                                                                         'sizing constant '
                                                                         '(e.g. 850). If '
                                                                         'omitted, looked up '
                                                                         'from the Wp/conn '
                                                                         'table based on the '
                                                                         'business-connection '
                                                                         'ratio.'},
                                 'regulation_constraint': {'type': 'string',
                                                           'description': 'Constrain the '
                                                                          'design to a known '
                                                                          "regulation's "
                                                                          'minimum sizing '
                                                                          'rules (default: '
                                                                          "'Nigeria - DARES'). "
                                                                          "Use 'None' to size "
                                                                          'purely from '
                                                                          'connections and '
                                                                          'loads.',
                                                           'enum': ['None', 'Nigeria - DARES'],
                                                           'default': 'Nigeria - DARES'},
                                 'pue_hours_per_day': {'type': 'number',
                                                       'description': 'Hours per day the '
                                                                      'anchor/PUE load runs '
                                                                      '(default: 3)',
                                                       'default': 3},
                                 'daily_generation_potential_kwh_kwp': {'type': 'number',
                                                                        'description': 'Daily '
                                                                                       'generation '
                                                                                       'potential '
                                                                                       'in kWh '
                                                                                       'per '
                                                                                       'kWp '
                                                                                       '(optional; '
                                                                                       'defaults '
                                                                                       'to the '
                                                                                       'Design '
                                                                                       'Rules '
                                                                                       'value)'},
                                 'target_tariff_usd': {'type': 'number',
                                                       'description': 'Target tariff in USD '
                                                                      'per kWh (default: 0.45)',
                                                       'default': 0.45},
                                 'max_distance_to_center_of_consumption_m': {'type': 'number',
                                                                             'description': 'Max '
                                                                                            'distance '
                                                                                            'of '
                                                                                            'power '
                                                                                            'plant '
                                                                                            'to '
                                                                                            'center '
                                                                                            'of '
                                                                                            'load '
                                                                                            'to '
                                                                                            'avoid '
                                                                                            'MV '
                                                                                            'lines, '
                                                                                            'in '
                                                                                            'meters '
                                                                                            '(optional)'},
                                 'avg_distance_to_pv_combiner_m': {'type': 'number',
                                                                   'description': 'Average '
                                                                                  'distance to '
                                                                                  'PV combiner '
                                                                                  'in meters '
                                                                                  '(default: '
                                                                                  '40)',
                                                                   'default': 40},
                                 'distance_to_feeder_pillar_m': {'type': 'number',
                                                                 'description': 'Distance to '
                                                                                'feeder pillar '
                                                                                'in meters '
                                                                                '(default: 7)',
                                                                 'default': 7},
                                 'spd_type': {'type': 'string',
                                              'description': 'Surge protection device strategy '
                                                             '(default: keep T1+T2)',
                                              'enum': ['Keep default T1+T2 Type SPD (Any '
                                                       'lightning probability)',
                                                       'Use T2 type as T1+T2 Type due to Low '
                                                       '(<=16 strikes per sq km per yr) '
                                                       'lightning probability'],
                                              'default': 'Keep default T1+T2 Type SPD (Any '
                                                         'lightning probability)'},
                                 'auto_design': {'type': 'boolean',
                                                 'description': 'Run auto-design sizing after '
                                                                'creating the design row '
                                                                '(default: true). Set false to '
                                                                'only record the inputs.',
                                                 'default': True},
                                 'wait_for_completion': {'type': 'boolean',
                                                         'description': 'Whether to wait for '
                                                                        'generation to '
                                                                        'complete (default: '
                                                                        'true)',
                                                         'default': True},
                                 'wait_for_bom': {'type': 'boolean',
                                                  'description': 'Whether to trigger BOM '
                                                                 'generation. Set False to '
                                                                 'skip BOM and return after '
                                                                 'design autopopulate '
                                                                 '(default: true)',
                                                  'default': True}},
                  'required': ['grid_name', 'design_name', 'max_connections']},
  'visible_to_customer': False},
 {'name': 'find_grid',
  'description': 'Find an existing grid by name',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid to find'}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'get_design_bom',
  'description': 'Get the Bill of Materials for an existing design',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'ID of the design to get BOM '
                                                              'for'}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'update_design',
  'description': 'Update parameters on an existing design. Accepts any design parameter (not '
                 'just layout distances) — e.g. wp_per_conn_override (also called Wp/conn, Wp '
                 'per connection), regulation_constraint (also called Nigerian law/DARES; '
                 "allowed values 'None'/'Nigeria - DARES'), max_connections, technology types "
                 '(inverter_type, battery_type, mppt_type, pv_type), and the layout-derived '
                 'distance fields (Avg Distance to PV Combiner (m), Distance to Feeder Pillar '
                 '(m), Average Service Drop Length (m)). Use after site layout to set real '
                 'cable distances, or any time a design parameter needs to change.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to '
                                                              'update'},
                                 'updates': {'type': 'string',
                                             'description': 'JSON object string of '
                                                            'field/column names to new values, '
                                                            'e.g. \'{"wp_per_conn_override": '
                                                            '150, "max_connections": 200}\' or '
                                                            'the legacy distance-column form '
                                                            '\'{"Avg Distance to PV Combiner '
                                                            '(m)": 15.5}\'. Accepts any design '
                                                            'parameter, not a fixed small '
                                                            'whitelist.'},
                                 'rerun_auto_design': {'type': 'boolean',
                                                       'description': 'Re-run sizing after '
                                                                      'applying updates. '
                                                                      'WARNING: replaces ALL '
                                                                      "of the design's "
                                                                      'subassemblies, '
                                                                      'including any manually '
                                                                      'added/removed/resized '
                                                                      'ones, unless `force` is '
                                                                      'also true.',
                                                       'default': False},
                                 'regenerate_bom': {'type': 'boolean',
                                                    'description': 'Regenerate the BOM after '
                                                                   'applying updates.',
                                                    'default': False},
                                 'force': {'type': 'boolean',
                                           'description': 'Required to `rerun_auto_design` '
                                                          'when the design has manually-edited '
                                                          'subassemblies — without it, the '
                                                          'call is refused to protect your '
                                                          'manual edits.',
                                           'default': False}},
                  'required': ['design_id', 'updates']},
  'visible_to_customer': False},
 {'name': 'trigger_bom',
  'description': 'Trigger BOM generation for a design, wait for completion, and return '
                 'results. Use when component costs may have changed — this recomputes costs '
                 'from the gd_purchases ledger and replaces gd_bom_items, stamping '
                 'bom_generated_at.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to '
                                                              'generate BOM for'},
                                 'grid_name': {'type': 'string',
                                               'description': 'Grid name (for output '
                                                              'context)'}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'list_design_options',
  'description': 'List valid design-creation choices: technology types (inverters, batteries, '
                 'MPPTs, PV panels — from the rental catalogue, with assembly classes and '
                 'compatible technology families to tell them apart), technology families, SPD '
                 'type options, regulation constraint options, and the form defaults applied '
                 'when a parameter is omitted. Call this before design_and_bom when the user '
                 'wants to choose equipment or override defaults interactively.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False},
 {'name': 'list_design_technology_families',
  'description': 'List first-class design technology families/architectures such as victron '
                 "and deye. Use this for requests like 'redo this design using Deye' before "
                 'editing individual equipment fields. Each family includes its default design '
                 'parameters, compatible subassemblies from gd_subassemblies.design_types, and '
                 'the matching site-layout type.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False},
 {'name': 'create_design',
  'description': 'Create a new design row for a grid WITHOUT auto-sizing or generating a BOM '
                 "by default — the low-drama 'just record it' tool. Contrast with "
                 'design_and_bom, which defaults to auto-sizing AND generating a BOM in one '
                 "call. Creates the grid if it doesn't exist by name. `params` accepts the "
                 "same fields as design_and_bom's flat schema (technology choices, connection "
                 'split, wp_per_conn_override — also called Wp/conn, Wp per connection — '
                 'regulation_constraint — also called Nigerian law/DARES, allowed values '
                 "'None'/'Nigeria - DARES' — 3-phase enforcement, SPD type, distances, "
                 'tariff), just as a JSON blob instead of individual arguments. Set '
                 'run_auto_design and/or generate_bom to true to also size and/or BOM the '
                 'design in the same call.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Name of the grid (will be '
                                                              "created if it doesn't exist)"},
                                 'design_name': {'type': 'string',
                                                 'description': 'Name for the new design'},
                                 'params': {'type': 'string',
                                            'description': 'JSON object string of additional '
                                                           'design parameters, keyed by the '
                                                           'same field names as '
                                                           'design_and_bom, e.g. '
                                                           '\'{"max_connections": 100, '
                                                           '"wp_per_conn_override": 150, '
                                                           '"regulation_constraint": "Nigeria '
                                                           '- DARES"}\'.',
                                            'default': '{}'},
                                 'run_auto_design': {'type': 'boolean',
                                                     'description': 'Run auto-design sizing '
                                                                    'after creating the design '
                                                                    'row (default: false — '
                                                                    'opposite of '
                                                                    "design_and_bom's default, "
                                                                    'since this tool is '
                                                                    "explicitly the 'just "
                                                                    "record it' path).",
                                                     'default': False},
                                 'generate_bom': {'type': 'boolean',
                                                  'description': 'Generate a BOM after '
                                                                 'creating (and, if requested, '
                                                                 'sizing) the design (default: '
                                                                 'false — opposite of '
                                                                 "design_and_bom's default).",
                                                  'default': False}},
                  'required': ['grid_name', 'design_name']},
  'visible_to_customer': False},
 {'name': 'get_design',
  'description': 'Return current design parameters, energy specs, and design_parameters for an '
                 'existing design — use before proposing a parameter change so you can quote '
                 'current values.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to '
                                                              'fetch'}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'list_design_artifacts',
  'description': 'List artifact types (maps, layouts, QGIS projects, etc.) generated for this '
                 "design, with version counts and the latest version's metadata. Use before "
                 "get_design_artifact to see what's available.",
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design'}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'get_design_artifact',
  'description': 'Fetch one version of a generated design artifact (e.g. a site map image or '
                 "QGIS project file). `version` is a 0-based index into the artifact's version "
                 'history, newest first — 0 is the latest version, 1 is the one before that, '
                 'etc. Stale or otherwise unreachable versions (files removed from Drive) are '
                 'automatically skipped in favor of the next available older version. The '
                 'returned entry includes a `web_view_link` — relay this link to the user '
                 '(Telegram unfurls Drive links).',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design'},
                                 'artifact_type': {'type': 'string',
                                                   'description': 'The artifact type to fetch. '
                                                                  'Artifact types vary by '
                                                                  'workflow and design — call '
                                                                  'list_design_artifacts first '
                                                                  "to see what's actually "
                                                                  'available for this design, '
                                                                  "e.g. 'map_image', "
                                                                  "'distribution_design_draft', "
                                                                  "or 'site_layout_png'."},
                                 'version': {'type': 'integer',
                                             'description': '0-based version index, newest '
                                                            'first. 0 = latest (default). '
                                                            'Out-of-range or '
                                                            'all-stale/unreachable versions '
                                                            'return an error.',
                                             'default': 0}},
                  'required': ['design_id', 'artifact_type']},
  'visible_to_customer': False},
 {'name': 'run_auto_design',
  'description': 'Re-run sizing (inverter/battery/MPPT/PV selection, kWp/kWh/kVA) for an '
                 'existing design, optionally applying parameter overrides first. WARNING: '
                 "re-running auto-design REPLACES ALL of the design's subassemblies. If any "
                 'subassembly on this design has been manually added, removed, or resized, the '
                 'call is blocked unless force=true — which discards those manual edits.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to '
                                                              '(re-)size'},
                                 'param_overrides': {'type': 'string',
                                                     'description': 'JSON object string of '
                                                                    'parameter overrides to '
                                                                    'apply before re-sizing, '
                                                                    'e.g. '
                                                                    '\'{"wp_per_conn_override": '
                                                                    "150}'.",
                                                     'default': '{}'},
                                 'force': {'type': 'boolean',
                                           'description': 'Required when the design has '
                                                          'manually-edited subassemblies — '
                                                          'without it, the call is refused to '
                                                          'protect your manual edits.',
                                           'default': False}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'change_design_technology',
  'description': "Change an existing design to a first-class technology family such as 'deye' "
                 "or 'victron'. Prefer this over manually editing inverter_type/battery_type "
                 "for requests like 'redo this design using Deye'. Applies the "
                 'family-compatible equipment defaults, optionally reruns auto-design and BOM, '
                 'and returns the matching site_layout_type hint for LPP artifact reruns.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to '
                                                              'convert'},
                                 'technology_family': {'type': 'string',
                                                       'description': 'Target technology '
                                                                      'family',
                                                       'enum': ['victron', 'deye']},
                                 'rerun_auto_design': {'type': 'boolean',
                                                       'description': 'Rerun auto-design after '
                                                                      'applying family '
                                                                      'defaults',
                                                       'default': True},
                                 'regenerate_bom': {'type': 'boolean',
                                                    'description': 'Regenerate BOM after '
                                                                   'applying family defaults',
                                                    'default': True},
                                 'force': {'type': 'boolean',
                                           'description': 'Required when manually-edited '
                                                          'design subassemblies should be '
                                                          'replaced by the family auto-design '
                                                          'output.',
                                           'default': False}},
                  'required': ['design_id', 'technology_family']},
  'visible_to_customer': False},
 {'name': 'duplicate_design',
  'description': 'Create a new design cloned from an existing one, with optional parameter '
                 "overrides — e.g. 'new design like X but with Wp/conn 150 instead of 120'. "
                 'Clones onto the same grid as the source design.',
  'inputSchema': {'type': 'object',
                  'properties': {'source_design_id': {'type': 'string',
                                                      'description': 'UNIQUEID of the design '
                                                                     'to clone'},
                                 'new_design_name': {'type': 'string',
                                                     'description': 'Name for the cloned '
                                                                    'design'},
                                 'param_overrides': {'type': 'string',
                                                     'description': 'JSON object string of '
                                                                    'design parameters to '
                                                                    'override on the clone, '
                                                                    'e.g. '
                                                                    '\'{"wp_per_conn_override": '
                                                                    "150}'.",
                                                     'default': '{}'},
                                 'run_auto_design': {'type': 'boolean',
                                                     'description': 'Run auto-design sizing on '
                                                                    'the clone (default: '
                                                                    'true).',
                                                     'default': True},
                                 'generate_bom': {'type': 'boolean',
                                                  'description': 'Generate a BOM for the clone '
                                                                 '(default: true).',
                                                  'default': True}},
                  'required': ['source_design_id', 'new_design_name']},
  'visible_to_customer': False},
 {'name': 'list_design_subassemblies',
  'description': 'List the active subassembly instances on an existing design.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design'}},
                  'required': ['design_id']},
  'visible_to_customer': False},
 {'name': 'add_subassembly',
  'description': 'Add a subassembly instance to an existing design by catalogue name '
                 '(fuzzy-matched — if the name is ambiguous, the closest candidates are '
                 'returned instead of an arbitrary guess). Marks the design as '
                 'manually-edited, which blocks a future run_auto_design/update_design '
                 'rerun_auto_design call unless force=true is passed there.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_id': {'type': 'string',
                                               'description': 'UNIQUEID of the design to add '
                                                              'the subassembly to'},
                                 'subassembly_name': {'type': 'string',
                                                      'description': 'Name of the subassembly '
                                                                     'in the catalogue '
                                                                     '(fuzzy-matched; closest '
                                                                     'candidates are returned '
                                                                     'if ambiguous).'},
                                 'qty': {'type': 'number',
                                         'description': 'Quantity of this subassembly to add'}},
                  'required': ['design_id', 'subassembly_name', 'qty']},
  'visible_to_customer': False},
 {'name': 'remove_subassembly',
  'description': 'Remove (soft-delete) a subassembly instance from a design. Requires the '
                 'design_subassembly_row_id from list_design_subassemblies — NOT the design '
                 'id. Marks the design as manually-edited, which blocks a future '
                 'run_auto_design/update_design rerun_auto_design call unless force=true is '
                 'passed there.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_subassembly_row_id': {'type': 'string',
                                                               'description': 'The row id of '
                                                                              'the design '
                                                                              'subassembly to '
                                                                              'remove, from '
                                                                              'list_design_subassemblies '
                                                                              '(not the design '
                                                                              'id).'}},
                  'required': ['design_subassembly_row_id']},
  'visible_to_customer': False},
 {'name': 'set_subassembly_qty',
  'description': 'Change the quantity of a subassembly instance already on a design '
                 '(kWp/kWh/kVA on the row are scaled proportionally). Requires the '
                 'design_subassembly_row_id from list_design_subassemblies — NOT the design '
                 'id. Marks the design as manually-edited, which blocks a future '
                 'run_auto_design/update_design rerun_auto_design call unless force=true is '
                 'passed there.',
  'inputSchema': {'type': 'object',
                  'properties': {'design_subassembly_row_id': {'type': 'string',
                                                               'description': 'The row id of '
                                                                              'the design '
                                                                              'subassembly to '
                                                                              'update, from '
                                                                              'list_design_subassemblies '
                                                                              '(not the design '
                                                                              'id).'},
                                 'qty': {'type': 'number',
                                         'description': 'New quantity for this subassembly '
                                                        'instance'}},
                  'required': ['design_subassembly_row_id', 'qty']},
  'visible_to_customer': False},
 {'name': 'list_subassembly_components',
  'description': 'Catalogue-level (staff-only): lists what a subassembly TEMPLATE is made of — '
                 'components and/or nested subassemblies — as opposed to '
                 'list_design_subassemblies, which lists subassembly instances on a specific '
                 'design.',
  'inputSchema': {'type': 'object',
                  'properties': {'subassembly_id': {'type': 'string',
                                                    'description': 'ID of the subassembly '
                                                                   'template'}},
                  'required': ['subassembly_id']},
  'visible_to_customer': False},
 {'name': 'add_subassembly_component',
  'description': 'Catalogue-level (staff-only): add a child component or nested subassembly to '
                 'a subassembly TEMPLATE. This is a GLOBAL catalogue edit — it affects EVERY '
                 'design that uses this subassembly template on its next BOM/auto-design '
                 'regen, not just one design. Exactly one of component_name or '
                 'child_subassembly_name must be given. Nesting a subassembly inside something '
                 'it already (directly or indirectly) contains is rejected as a circular '
                 'reference. Consider duplicate_subassembly first if you want a '
                 'design-specific variant instead of changing the shared template.',
  'inputSchema': {'type': 'object',
                  'properties': {'subassembly_id': {'type': 'string',
                                                    'description': 'ID of the subassembly '
                                                                   'template to add a child '
                                                                   'to'},
                                 'component_name': {'type': 'string',
                                                    'description': 'Name of the plain '
                                                                   'component to add '
                                                                   '(fuzzy-matched). Exactly '
                                                                   'one of '
                                                                   'component_name/child_subassembly_name '
                                                                   'must be given.'},
                                 'child_subassembly_name': {'type': 'string',
                                                            'description': 'Name of the '
                                                                           'subassembly to '
                                                                           'nest as a child '
                                                                           '(fuzzy-matched). '
                                                                           'Exactly one of '
                                                                           'component_name/child_subassembly_name '
                                                                           'must be given. '
                                                                           'Rejected if it '
                                                                           'would create a '
                                                                           'circular '
                                                                           'reference.'},
                                 'qty': {'type': 'number',
                                         'description': 'Quantity of this child per parent '
                                                        'unit (default: 1)',
                                         'default': 1},
                                 'unit': {'type': 'string',
                                          'description': 'Unit label for the quantity '
                                                         '(optional)'}},
                  'required': ['subassembly_id']},
  'visible_to_customer': False},
 {'name': 'remove_subassembly_component',
  'description': 'Catalogue-level (staff-only): remove a child component/subassembly from a '
                 'subassembly TEMPLATE. GLOBAL catalogue edit — affects every design using '
                 'this template on its next BOM/auto-design regen.',
  'inputSchema': {'type': 'object',
                  'properties': {'row_id': {'type': 'string',
                                            'description': 'Row id from '
                                                           'list_subassembly_components '
                                                           'identifying the child to remove.'}},
                  'required': ['row_id']},
  'visible_to_customer': False},
 {'name': 'set_subassembly_component_qty',
  'description': 'Catalogue-level (staff-only): change the quantity of a child '
                 'component/subassembly within a subassembly TEMPLATE. GLOBAL catalogue edit — '
                 'affects every design using this template on its next BOM/auto-design regen.',
  'inputSchema': {'type': 'object',
                  'properties': {'row_id': {'type': 'string',
                                            'description': 'Row id from '
                                                           'list_subassembly_components '
                                                           'identifying the child to update.'},
                                 'qty': {'type': 'number',
                                         'description': 'New quantity for this child within '
                                                        'the template'}},
                  'required': ['row_id', 'qty']},
  'visible_to_customer': False},
 {'name': 'duplicate_subassembly',
  'description': 'Catalogue-level (staff-only): clone a subassembly TEMPLATE (all fields plus '
                 'its full component list) under a new description. Use before editing '
                 'composition to create a design-specific variant without affecting the '
                 'original template used elsewhere.',
  'inputSchema': {'type': 'object',
                  'properties': {'source_subassembly_id': {'type': 'string',
                                                           'description': 'ID of the '
                                                                          'subassembly '
                                                                          'template to clone'},
                                 'new_description': {'type': 'string',
                                                     'description': 'Description for the '
                                                                    'cloned subassembly'}},
                  'required': ['source_subassembly_id', 'new_description']},
  'visible_to_customer': False},
 {'name': 'gd_describe_tables',
  'description': 'Returns the full registry of gd_* tables available to '
                 'gd_list_rows/gd_get_row/gd_upsert_row/gd_delete_row, each with its scope, '
                 "writable columns, and a one-line description. Scopes: 'grid' tables are "
                 'anchored to a single grid/site and require a grid filter on every '
                 "read/write; 'catalog' tables are global reference data (shared across every "
                 'grid) and are staff-only, since a write there affects every grid that '
                 "references it; 'denied' tables (identity/permission surfaces) are never "
                 'accessible through generic CRUD. Call this BEFORE attempting to use the '
                 "other four generic tools — it's the schema map that tells you which table to "
                 'use and what columns it accepts.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': False},
 {'name': 'gd_list_rows',
  'description': 'Generic row listing for any gd_* table from gd_describe_tables. For '
                 "'grid'-scoped tables you MUST supply a grid filter — either grid_name or "
                 "filters['grid']/filters['grid_id'] — otherwise the call is rejected; "
                 "'catalog' tables are staff-only and ignore grid_name. Prefer the dedicated "
                 'Phase A tools (get_design, list_design_subassemblies, '
                 'list_subassembly_components, etc.) for common operations — reserve this for '
                 "the long tail of tables that don't have a purpose-built tool.",
  'inputSchema': {'type': 'object',
                  'properties': {'table': {'type': 'string',
                                           'description': 'Bare table name from '
                                                          "gd_describe_tables (no 'gd_' "
                                                          "prefix), e.g. 'designs'"},
                                 'grid_name': {'type': 'string',
                                               'description': 'Required for grid-scoped tables '
                                                              'unless a grid id is already '
                                                              'known via filters; ignored for '
                                                              'catalog tables.'},
                                 'filters': {'type': 'string',
                                             'description': 'JSON object string of additional '
                                                            'exact-match column filters, e.g. '
                                                            '\'{"status": "active"}\'.'},
                                 'limit': {'type': 'number',
                                           'description': 'Max rows to return',
                                           'default': 50},
                                 'include_inactive': {'type': 'boolean',
                                                      'description': 'Include soft-deleted '
                                                                     '(active=false) rows',
                                                      'default': False}},
                  'required': ['table']},
  'visible_to_customer': False},
 {'name': 'gd_get_row',
  'description': 'Fetch a single row by id from any gd_* table from gd_describe_tables, with '
                 'the same scope-based access rules as gd_list_rows (grid-scoped tables check '
                 "the row's own grid; catalog tables are staff-only; denied tables are never "
                 'accessible).',
  'inputSchema': {'type': 'object',
                  'properties': {'table': {'type': 'string',
                                           'description': 'Bare table name from '
                                                          "gd_describe_tables (no 'gd_' "
                                                          'prefix)'},
                                 'row_id': {'type': 'string',
                                            'description': 'Row id to fetch'}},
                  'required': ['table', 'row_id']},
  'visible_to_customer': False},
 {'name': 'gd_upsert_row',
  'description': 'Create or update a row in any gd_* table from gd_describe_tables. Omit '
                 'row_id to create a new row; provide it to update an existing one. Call '
                 "gd_describe_tables first to see this table's writable_columns — unknown "
                 'columns are rejected. created_by/updated_by are stamped automatically from '
                 "the caller and can never be set here. IMPORTANT: writes to a 'catalog'-scope "
                 'table affect EVERY grid that references that row — confirm with the user '
                 'before writing to a catalog table. Soft-deleted rows are NOT resurrected by '
                 "omitting 'active' from values (there is no 'active' column in any "
                 "writable_columns set; it's server-managed — use gd_delete_row for delete, "
                 "not an upsert). Moving a row's grid-anchor column (e.g. a design's 'grid') "
                 'to a different grid re-checks access against the new grid, not just the '
                 "row's current one.",
  'inputSchema': {'type': 'object',
                  'properties': {'table': {'type': 'string',
                                           'description': 'Bare table name from '
                                                          "gd_describe_tables (no 'gd_' "
                                                          'prefix)'},
                                 'row_id': {'type': 'string',
                                            'description': 'Omit to create a new row; provide '
                                                           'to update an existing one'},
                                 'values': {'type': 'string',
                                            'description': 'JSON object string of column: '
                                                           'value pairs to write. Call '
                                                           'gd_describe_tables first to see '
                                                           'writable_columns for this table — '
                                                           'unknown columns are rejected.'}},
                  'required': ['table', 'values']},
  'visible_to_customer': False},
 {'name': 'gd_delete_row',
  'description': 'SOFT delete a row (sets active=false) in any gd_* table from '
                 'gd_describe_tables — never a hard delete. Check with the user before '
                 "deleting rows in a 'catalog'-scope table, since that has global impact "
                 'across every grid that references it. No automatic referential check is '
                 "performed — other rows may still reference this one after it's deactivated.",
  'inputSchema': {'type': 'object',
                  'properties': {'table': {'type': 'string',
                                           'description': 'Bare table name from '
                                                          "gd_describe_tables (no 'gd_' "
                                                          'prefix)'},
                                 'row_id': {'type': 'string',
                                            'description': 'Row id to soft-delete'}},
                  'required': ['table', 'row_id']},
  'visible_to_customer': False}]
