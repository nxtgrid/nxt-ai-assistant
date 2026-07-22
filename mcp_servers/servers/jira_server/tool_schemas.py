"""Tool schemas for the Jira MCP server.

Extracted verbatim from ``handle_list_tools``, which had grown to 367 lines of
almost nothing but these literals.

Split in two because the server always exposed them that way:
``READ_ONLY_TOOL_SCHEMAS`` are always listed, ``ACTION_TOOL_SCHEMAS`` only when
``ActionFlags.is_actions_enabled("jira")`` — add_comment, change_status and
add_on_call_override, which write to Jira, plus get_transitions, which is
read-only but only useful alongside change_status. Merging the two lists would
quietly make those writes unconditional.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call, as it always has; sharing model
instances across calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user`` reads
to decide whether a non-staff user may see a tool. Only two schemas below set it
(both ``False``); the other twelve omit it entirely, carried over verbatim from
handle_list_tools. That omission is not harmless: ``server_registry.list_tools``
defaults a missing flag to ``True``, so on the code path these tools would read
as customer-visible. Today they are saved by ``tool_definitions.json``, which
lists jira with explicit ``false`` and is what the orchestrator actually reads.
See ``tests/servers/jira_server/test_tool_schemas.py``, which pins the omission
so it cannot drift unnoticed.
"""

from typing import Any, Dict, List

READ_ONLY_TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'jira_search_issues_with_comments',
  'description': 'Search Jira issues in the OPS project. Returns: key, summary, status, '
                 'assignee, reporter, priority, issue_type, grid, organization, created, '
                 'updated, comments. Filter by grid, organization, status, assignee (accepts '
                 "person's name like 'Chovwe' or email), date range, labels, or text search. "
                 "To find someone's tickets, use the assignee parameter with their name. "
                 'Defaults to last 90 days if no dates specified. When presenting results, '
                 "always include the created date in short format (e.g. '15 Mar') alongside "
                 "each ticket. IMPORTANT: for topic or category questions (e.g. 'tickets about "
                 "DCUs being offline', 'grid downtime tickets'), do NOT rely on text_search — "
                 'tickets rarely use the same words as the question. Instead fetch the open '
                 'tickets without text_search (the list is small) and judge from the summaries '
                 'yourself which ones match the topic.',
  'inputSchema': {'type': 'object',
                  'properties': {'text_search': {'type': 'string',
                                                 'description': 'Literal keyword match against '
                                                                'summary/description — only '
                                                                'finds tickets that contain '
                                                                'these exact words. Monitoring '
                                                                'alert tickets use templated '
                                                                'phrasing that often differs '
                                                                'from how a user describes the '
                                                                'issue (e.g. a DCU outage '
                                                                "ticket reads 'DCU 230401080 "
                                                                'in Okpokunou could have a '
                                                                'problem, causing Meter '
                                                                "Issues', not 'DCU offline'). "
                                                                'Use only for distinctive '
                                                                'literal strings like a meter '
                                                                'number, device ID, or grid '
                                                                'name; otherwise omit and '
                                                                'filter the results yourself.'},
                                 'grid': {'type': 'string',
                                          'description': 'Grid name to filter by'},
                                 'organization': {'type': 'string',
                                                  'description': 'Organization name to filter '
                                                                 "by (e.g., 'Calin')"},
                                 'statuses': {'type': 'array',
                                              'items': {'type': 'string'},
                                              'description': 'Issue statuses to filter by '
                                                             '(optional)'},
                                 'assignee': {'type': 'string',
                                              'description': 'Assignee name or email '
                                                             "(optional). Accepts person's "
                                                             "name (e.g., 'Chovwe', 'Vaibhav') "
                                                             'with fuzzy matching, email '
                                                             "address, 'me' for current user, "
                                                             "or 'unassigned' for unassigned "
                                                             'issues.'},
                                 'created_after': {'type': 'string',
                                                   'description': 'Created after date '
                                                                  '(YYYY-MM-DD), defaults to '
                                                                  '90 days ago'},
                                 'created_before': {'type': 'string',
                                                    'description': 'Created before date '
                                                                   '(YYYY-MM-DD), defaults to '
                                                                   'today'},
                                 'updated_after': {'type': 'string',
                                                   'description': 'Updated after date '
                                                                  '(YYYY-MM-DD), defaults to '
                                                                  '90 days ago'},
                                 'updated_before': {'type': 'string',
                                                    'description': 'Updated before date '
                                                                   '(YYYY-MM-DD), defaults to '
                                                                   'today'},
                                 'labels': {'type': 'array',
                                            'items': {'type': 'string'},
                                            'description': 'Labels to filter by (optional)'},
                                 'max_results': {'type': 'integer',
                                                 'default': 50,
                                                 'description': 'Maximum number of results'}},
                  'required': []}},
 {'name': 'jira_get_issue',
  'description': 'Get detailed information about a specific Jira issue. Returns: key, summary, '
                 'description, status, assignee, reporter, priority, issue_type, grid, '
                 'organization, created, updated, labels, components, custom_fields, comments.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., '
                                                              'PROJ-123)'}},
                  'required': ['issue_key']}},
 {'name': 'jira_analyze_comments',
  'description': 'Analyze and summarize comments from Jira issues with date filtering',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_keys': {'type': 'array',
                                                'items': {'type': 'string'},
                                                'description': 'List of issue keys to analyze'},
                                 'comment_start_date': {'type': 'string',
                                                        'description': 'Filter comments after '
                                                                       'this date (ISO '
                                                                       'format)'},
                                 'comment_end_date': {'type': 'string',
                                                      'description': 'Filter comments before '
                                                                     'this date (ISO format)'},
                                 'include_sentiment': {'type': 'boolean',
                                                       'default': True,
                                                       'description': 'Include sentiment '
                                                                      'analysis'},
                                 'include_themes': {'type': 'boolean',
                                                    'default': True,
                                                    'description': 'Include theme extraction'},
                                 'include_action_items': {'type': 'boolean',
                                                          'default': True,
                                                          'description': 'Include action item '
                                                                         'extraction'}},
                  'required': ['issue_keys']}},
 {'name': 'jira_prepare_llm_categorization',
  'description': 'Prepare filtered Jira issues and comment analysis for LLM-based '
                 'categorization',
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
                                                   'description': 'Include issues created '
                                                                  'after this date '
                                                                  '(YYYY-MM-DD)'},
                                 'created_before': {'type': 'string',
                                                    'description': 'Include issues created '
                                                                   'before this date '
                                                                   '(YYYY-MM-DD)'},
                                 'updated_after': {'type': 'string',
                                                   'description': 'Include issues updated '
                                                                  'after this date '
                                                                  '(YYYY-MM-DD)'},
                                 'updated_before': {'type': 'string',
                                                    'description': 'Include issues updated '
                                                                   'before this date '
                                                                   '(YYYY-MM-DD)'},
                                 'custom_field_filters': {'type': 'object',
                                                          'description': 'Custom field filters '
                                                                         '(field_id: value)'},
                                 'comment_start_date': {'type': 'string',
                                                        'description': 'Include comments after '
                                                                       'this date (ISO '
                                                                       'format)'},
                                 'comment_end_date': {'type': 'string',
                                                      'description': 'Include comments before '
                                                                     'this date (ISO format)'},
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
                                                      'description': 'Include comment '
                                                                     'analysis'},
                                 'max_description_length': {'type': 'integer',
                                                            'default': 500,
                                                            'description': 'Maximum '
                                                                           'description '
                                                                           'length'},
                                 'max_comment_length': {'type': 'integer',
                                                        'default': 300,
                                                        'description': 'Maximum comment '
                                                                       'preview length'}},
                  'required': []}},
 {'name': 'jira_get_fields',
  'description': 'Get all available Jira fields including custom fields',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False},
 {'name': 'jira_generate_categorization_prompt',
  'description': 'Generate a structured prompt for LLM categorization based on Jira data',
  'inputSchema': {'type': 'object',
                  'properties': {'categorization_type': {'type': 'string',
                                                         'enum': ['priority',
                                                                  'theme',
                                                                  'sentiment',
                                                                  'workload',
                                                                  'custom'],
                                                         'description': 'Type of '
                                                                        'categorization to '
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
                  'required': ['categorization_type']}},
 {'name': 'jira_check_mentions',
  'description': 'Check if the current user (or specified user) is mentioned in Jira issues or '
                 'comments',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_keys': {'type': 'array',
                                                'items': {'type': 'string'},
                                                'description': 'List of issue keys to check '
                                                               'for mentions'},
                                 'user_email': {'type': 'string',
                                                'description': 'User email to check (optional, '
                                                               'defaults to current '
                                                               'authenticated user)'}},
                  'required': ['issue_keys']}},
 {'name': 'jira_get_on_call',
  'description': 'Get on-call schedule for a date range from Jira Service Management Ops. '
                 'Returns a clean list of on-call periods with user names, email addresses, '
                 'and time ranges. Automatically queries 1 day before start_date and 1 day '
                 'after end_date to ensure complete coverage.',
  'inputSchema': {'type': 'object',
                  'properties': {'start_date': {'type': 'string',
                                                'description': 'ISO 8601 formatted start '
                                                               'datetime (e.g., '
                                                               "'2025-10-24T00:00:00Z'). "
                                                               'Required.'},
                                 'end_date': {'type': 'string',
                                              'description': 'ISO 8601 formatted end datetime. '
                                                             'Optional. If not provided, '
                                                             'defaults to start_date.'}},
                  'required': ['start_date']}},
 {'name': 'jira_get_schedule_participants',
  'description': 'Get all team members from the on-call schedule rotations. Returns all users '
                 'who are part of any rotation in the schedule, not just who is currently '
                 'on-call. Useful for getting the full list of people who can be assigned to '
                 'on-call duties.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []}},
 {'name': 'jira_get_organization_options',
  'description': 'Get all available organization options for JIRA tickets in the OPS project. '
                 'Returns the list of organizations that can be assigned to tickets.',
  'inputSchema': {'type': 'object', 'properties': {}, 'required': []},
  'visible_to_customer': False}]

ACTION_TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'jira_add_comment',
  'description': '[ACTION - MODIFIES JIRA] Add a comment to a Jira issue. This tool creates a '
                 'new comment on the specified issue.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., '
                                                              'PROJ-123)'},
                                 'comment_text': {'type': 'string',
                                                  'description': 'The comment text to add'}},
                  'required': ['issue_key', 'comment_text']}},
 {'name': 'jira_get_transitions',
  'description': '[READ-ONLY] Get available status transitions for a Jira issue. This tool '
                 'only retrieves information, it does not change the issue status.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., '
                                                              'PROJ-123)'}},
                  'required': ['issue_key']}},
 {'name': 'jira_change_status',
  'description': '[ACTION - MODIFIES JIRA] Change the status of a Jira issue by applying a '
                 'transition. This tool modifies the issue workflow state. Only works if the '
                 'issue is assigned to the current user.',
  'inputSchema': {'type': 'object',
                  'properties': {'issue_key': {'type': 'string',
                                               'description': 'Jira issue key (e.g., '
                                                              'PROJ-123)'},
                                 'transition': {'type': 'string',
                                                'description': 'Transition ID or name (e.g., '
                                                               "'31' or 'In Progress')"},
                                 'current_user_email': {'type': 'string',
                                                        'description': 'Email of the user '
                                                                       'making the change '
                                                                       '(optional, defaults to '
                                                                       'authenticated user)'}},
                  'required': ['issue_key', 'transition']}},
 {'name': 'jira_add_on_call_override',
  'description': '[ACTION - MODIFIES JSM SCHEDULE] Add an on-call override for a specific user '
                 'and time period in JSM Ops schedule. This tool creates a new on-call '
                 'assignment in the schedule. Use this to assign someone to be on-call for a '
                 'specific date/time range (e.g., 9am-5pm, 5pm-7pm). User will be looked up by '
                 'name from the system.',
  'inputSchema': {'type': 'object',
                  'properties': {'user_name': {'type': 'string',
                                               'description': 'Name of the user to add as '
                                                              "on-call (e.g., 'Vaibhav', 'John "
                                                              "Doe'). System will look up "
                                                              'their email automatically.'},
                                 'start_time': {'type': 'string',
                                                'description': 'ISO 8601 formatted start '
                                                               'datetime (e.g., '
                                                               "'2025-10-24T09:00:00Z' for 9am "
                                                               'UTC)'},
                                 'end_time': {'type': 'string',
                                              'description': 'ISO 8601 formatted end datetime '
                                                             "(e.g., '2025-10-24T17:00:00Z' "
                                                             'for 5pm UTC)'}},
                  'required': ['user_name', 'start_time', 'end_time']}}]
