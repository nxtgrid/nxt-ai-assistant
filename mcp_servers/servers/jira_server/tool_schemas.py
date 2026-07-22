"""Tool schemas for the Jira MCP server.

The advertised manifest for this server. Reconciled with
``mcp_servers/tool_definitions.json`` (the manifest the orchestrator actually
serves in production): tool names are unprefixed (``get_issue``, not
``jira_get_issue``) to match the manifest — the orchestrator adds the
``jira_`` server prefix when advertising, and ``handle_call_tool`` normalizes
it back on dispatch, so both naming layers keep working. For tools present in
both places, the JSON descriptions won; tools only defined here (analysis and
categorization helpers) keep their original text and are simply not yet in the
JSON manifest. ``mcp_servers/tests/test_tool_manifest_sync.py`` enforces that
the JSON stays a subset of this file, so regenerating the manifest with
``scripts/export_tools.py`` can only add tools, never drop them.

Split in two because the server always exposed them that way:
``READ_ONLY_TOOL_SCHEMAS`` are always listed, ``ACTION_TOOL_SCHEMAS`` only when
``ActionFlags.is_actions_enabled("jira")`` — add_comment, change_status,
assign_issue and add_on_call_override, which write to Jira, plus
get_transitions, which is read-only but only useful alongside change_status.
Merging the two lists would quietly make those writes unconditional.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user`` reads
to decide whether a non-staff user may see a tool. Every schema below sets it to
``False`` — Jira is staff-only.
"""

from typing import Any, Dict, List

READ_ONLY_TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'search_issues_with_comments',
  'description': "List and filter multiple Jira issues i.e. tickets. Use for 'my tickets', 'open "
                 "tickets', or searching by grid/org. Does NOT provide details for a single "
                 'ticket. By default excludes Done tickets unless statuses are explicitly provided '
                 'or exclude_done is set to false. When presenting results, always include the '
                 "created date in short format (e.g. '15 Mar') alongside each ticket. IMPORTANT: "
                 "for topic or category questions (e.g. 'tickets about DCUs being offline', 'grid "
                 "downtime tickets'), do NOT rely on text_search — tickets rarely use the same "
                 'words as the question. Instead fetch the open tickets without text_search (the '
                 'list is small) and judge from the summaries yourself which ones match the topic.',
  'inputSchema': {'type': 'object',
                  'properties': {'text_search': {'type': 'string',
                                                 'description': 'Literal keyword match against '
                                                                'summary/description — only finds '
                                                                'tickets that contain these exact '
                                                                'words. Monitoring alert tickets '
                                                                'use templated phrasing that often '
                                                                'differs from how a user describes '
                                                                'the issue (e.g. a DCU outage '
                                                                "ticket reads 'DCU 230401080 in "
                                                                'Okpokunou could have a problem, '
                                                                "causing Meter Issues', not 'DCU "
                                                                "offline'; an inverter problem "
                                                                "reads 'RESTART FAILED - Inverter "
                                                                "Off' or 'Grid outage', not "
                                                                "'downtime'). Use only for "
                                                                'distinctive literal strings like '
                                                                'a meter number, device ID, or '
                                                                'grid name; otherwise omit and '
                                                                'filter the results yourself.'},
                                 'grid': {'type': 'string'},
                                 'organization': {'type': 'string'},
                                 'statuses': {'type': 'array',
                                              'items': {'type': 'string'},
                                              'description': 'Filter by Jira statuses. Valid '
                                                             "values: 'To Do', 'In Progress', "
                                                             "'Done'. Note: there is no 'Open' "
                                                             'status — omit statuses to get all '
                                                             'non-done tickets.'},
                                 'assignee': {'type': 'string',
                                              'description': "Assignee name, email, 'me', or "
                                                             "'unassigned'. Omit for any "
                                                             'assignee.'},
                                 'exclude_done': {'type': 'boolean',
                                                  'description': 'Exclude Done tickets. Defaults '
                                                                 'to true when no statuses '
                                                                 'provided, false when statuses '
                                                                 'are explicitly set.'}}},
  'visible_to_customer': False},
 {'name': 'get_issue',
  'description': 'Retrieve the full details, history, and comments of one specific Jira issue i.e. '
                 'ticket. Use ONLY when a Ticket Key (e.g., OPS-123) is known.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string', 'description': 'Jira issue key'}},
                  'required': ['issue_key']},
  'visible_to_customer': False},
 {'name': 'get_ticket_statistics',
  'description': 'Get aggregated ticket statistics for the last N days (open + closed). Returns '
                 'tickets per grid, top ticket types, and grids with above-average ticket counts. '
                 'Use for executive summaries.',
  'inputSchema': {'type': 'object',
                  'properties': {'days': {'type': 'integer',
                                          'description': 'Number of days to look back. Default: '
                                                         '30'}}},
  'visible_to_customer': False},
 {'name': 'analyze_comments',
  'description': 'Analyze and summarize comments from Jira issues with date filtering',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_keys': {'type': 'array',
                                                'items': {'type': 'string'},
                                                'description': 'List of issue keys to analyze'},
                                 'comment_start_date': {'type': 'string',
                                                        'description': 'Filter comments after this '
                                                                       'date (ISO format)'},
                                 'comment_end_date': {'type': 'string',
                                                      'description': 'Filter comments before this '
                                                                     'date (ISO format)'},
                                 'include_sentiment': {'type': 'boolean',
                                                       'default': True,
                                                       'description': 'Include sentiment analysis'},
                                 'include_themes': {'type': 'boolean',
                                                    'default': True,
                                                    'description': 'Include theme extraction'},
                                 'include_action_items': {'type': 'boolean',
                                                          'default': True,
                                                          'description': 'Include action item '
                                                                         'extraction'}},
                  'required': ['issue_keys']},
  'visible_to_customer': False},
 {'name': 'prepare_llm_categorization',
  'description': 'Prepare filtered Jira issues and comment analysis for LLM-based categorization',
  'inputSchema': {'type': 'object',
                  'properties': {'project': {'type': 'string',
                                             'description': 'Project key or name'},
                                 'issue_types': {'type': 'array',
                                                 'items': {'type': 'string'},
                                                 'description': 'Issue types to include'},
                                 'statuses': {'type': 'array',
                                              'items': {'type': 'string'},
                                              'description': 'Issue statuses to include'},
                                 'created_after': {'type': 'string',
                                                   'description': 'Include issues created after '
                                                                  'this date (YYYY-MM-DD)'},
                                 'created_before': {'type': 'string',
                                                    'description': 'Include issues created before '
                                                                   'this date (YYYY-MM-DD)'},
                                 'updated_after': {'type': 'string',
                                                   'description': 'Include issues updated after '
                                                                  'this date (YYYY-MM-DD)'},
                                 'updated_before': {'type': 'string',
                                                    'description': 'Include issues updated before '
                                                                   'this date (YYYY-MM-DD)'},
                                 'custom_field_filters': {'type': 'object',
                                                          'description': 'Custom field filters '
                                                                         '(field_id: value)'},
                                 'comment_start_date': {'type': 'string',
                                                        'description': 'Include comments after '
                                                                       'this date (ISO format)'},
                                 'comment_end_date': {'type': 'string',
                                                      'description': 'Include comments before this '
                                                                     'date (ISO format)'},
                                 'max_results': {'type': 'integer',
                                                 'default': 100,
                                                 'description': 'Maximum number of issues to '
                                                                'analyze'},
                                 'include_descriptions': {'type': 'boolean',
                                                          'default': True,
                                                          'description': 'Include issue '
                                                                         'descriptions'},
                                 'include_comments': {'type': 'boolean',
                                                      'default': True,
                                                      'description': 'Include comment analysis'},
                                 'max_description_length': {'type': 'integer',
                                                            'default': 500,
                                                            'description': 'Maximum description '
                                                                           'length'},
                                 'max_comment_length': {'type': 'integer',
                                                        'default': 300,
                                                        'description': 'Maximum comment preview '
                                                                       'length'}},
                  'required': []},
  'visible_to_customer': False},
 {'name': 'get_fields',
  'description': 'Get all available Jira fields including custom fields',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False},
 {'name': 'generate_categorization_prompt',
  'description': 'Generate a structured prompt for LLM categorization based on Jira data',
  'inputSchema': {'type': 'object',
                  'properties': {'categorization_type': {'type': 'string',
                                                         'enum': ['priority',
                                                                  'theme',
                                                                  'sentiment',
                                                                  'workload',
                                                                  'custom'],
                                                         'description': 'Type of categorization to '
                                                                        'perform'},
                                 'custom_categories': {'type': 'array',
                                                       'items': {'type': 'string'},
                                                       'description': 'Custom categories for '
                                                                      'classification'},
                                 'analysis_focus': {'type': 'string',
                                                    'enum': ['issues_only',
                                                             'comments_only',
                                                             'both'],
                                                    'default': 'both',
                                                    'description': 'Focus analysis on issues, '
                                                                   'comments, or both'},
                                 'output_format': {'type': 'string',
                                                   'enum': ['json', 'csv', 'summary'],
                                                   'default': 'json',
                                                   'description': 'Desired output format'}},
                  'required': ['categorization_type']},
  'visible_to_customer': False},
 {'name': 'check_mentions',
  'description': 'Check if the current user (or specified user) is mentioned in Jira issues or '
                 'comments',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_keys': {'type': 'array',
                                                'items': {'type': 'string'},
                                                'description': 'List of issue keys to check for '
                                                               'mentions'},
                                 'user_email': {'type': 'string',
                                                'description': 'User email to check (optional, '
                                                               'defaults to current authenticated '
                                                               'user)'}},
                  'required': ['issue_keys']},
  'visible_to_customer': False},
 {'name': 'get_on_call',
  'description': 'Get the JSM on-call schedule for a date range — use for any question about who '
                 "was, is, or will be on call. Works for past dates (e.g. 'last weekend'), "
                 'present, and future. Pass start_date as YYYY-MM-DD or ISO 8601.',
  'inputSchema': {'type': 'object',
                  'properties': {'start_date': {'type': 'string',
                                                'description': 'Start date in YYYY-MM-DD or ISO '
                                                               "8601 format (e.g. '2026-04-18')"},
                                 'end_date': {'type': 'string',
                                              'description': 'End date (inclusive). Defaults to '
                                                             'start_date if omitted.'}},
                  'required': ['start_date']},
  'visible_to_customer': False},
 {'name': 'get_schedule_participants',
  'description': 'Get all team members from the on-call schedule rotations. Returns all users who '
                 'are part of any rotation in the schedule, not just who is currently on-call. '
                 'Useful for getting the full list of people who can be assigned to on-call '
                 'duties.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False},
 {'name': 'get_organization_options',
  'description': 'Get all available organization options for JIRA tickets in the OPS project. '
                 'Returns the list of organizations that can be assigned to tickets.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False}]

ACTION_TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'add_comment',
  'description': '[ACTION] Add a new comment to a specific Jira ticket.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string'},
                                 'comment_text': {'type': 'string'}},
                  'required': ['issue_key', 'comment_text']},
  'visible_to_customer': False},
 {'name': 'get_transitions',
  'description': '[READ-ONLY] Get available status transitions for a Jira issue. This tool only '
                 'retrieves information, it does not change the issue status.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., PROJ-123)'}},
                  'required': ['issue_key']},
  'visible_to_customer': False},
 {'name': 'change_status',
  'description': '[ACTION] Change the status of a Jira ticket (e.g., close, start progress, '
                 "reopen). Use transition names like 'Done', 'In Progress', 'To Do'. If the ticket "
                 'is unassigned, it will be auto-assigned to the requesting user.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., OPS-2148)'},
                                 'transition': {'type': 'string',
                                                'description': "Target status name (e.g., 'Done', "
                                                               "'In Progress', 'To Do')"}},
                  'required': ['issue_key', 'transition']},
  'visible_to_customer': False},
 {'name': 'assign_issue',
  'description': "[ACTION] Assign or reassign a Jira ticket to a user. Use 'me' to self-assign, "
                 "'unassigned' to remove assignee, or a person's name/email for fuzzy matching. "
                 'Returns an error if the user is not found in Jira.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., OPS-2148)'},
                                 'assignee': {'type': 'string',
                                              'description': "Name, email, 'me' (self-assign), or "
                                                             "'unassigned' (remove assignee)"}},
                  'required': ['issue_key', 'assignee']},
  'visible_to_customer': False},
 {'name': 'add_on_call_override',
  'description': '[ACTION - MODIFIES JSM SCHEDULE] Add an on-call override for a specific user and '
                 'time period in JSM Ops schedule. This tool creates a new on-call assignment in '
                 'the schedule. Use this to assign someone to be on-call for a specific date/time '
                 'range (e.g., 9am-5pm, 5pm-7pm). User will be looked up by name from the system.',
  'inputSchema': {'type': 'object',
                  'properties': {'user_name': {'type': 'string',
                                               'description': 'Name of the user to add as on-call '
                                                              "(e.g., 'Vaibhav', 'John Doe'). "
                                                              'System will look up their email '
                                                              'automatically.'},
                                 'start_time': {'type': 'string',
                                                'description': 'ISO 8601 formatted start datetime '
                                                               "(e.g., '2025-10-24T09:00:00Z' for "
                                                               '9am UTC)'},
                                 'end_time': {'type': 'string',
                                              'description': 'ISO 8601 formatted end datetime '
                                                             "(e.g., '2025-10-24T17:00:00Z' for "
                                                             '5pm UTC)'}},
                  'required': ['user_name', 'start_time', 'end_time']},
  'visible_to_customer': False}]
