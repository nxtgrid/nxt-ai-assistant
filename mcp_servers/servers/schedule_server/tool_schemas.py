"""Tool schemas for the Schedule MCP server.

The advertised manifest for this server, reconciled verbatim from
``mcp_servers/tool_definitions.json`` (what the orchestrator serves in
production). The five user-schedule tools were previously defined inline in
``handle_list_tools``. ``mcp_servers/tests/test_tool_manifest_sync.py`` keeps
the JSON a subset of this file.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call; sharing model instances across
calls would let one caller's mutation reach the next.

``visible_to_customer`` mirrors the manifest: all five tools here are visible
to customers. Chat/topic/user identity is injected by the tool_executor,
never LLM-controlled (see server module docstring).
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'schedule_user_command',
  'description': 'Schedule a message (slash command or plain text) to run later or on a recurring '
                 "basis (e.g., 'daily at 9am', 'every other monday at 9am', 'monthly on the 1st at "
                 "9am').",
  'inputSchema': {'type': 'object',
                  'properties': {'message': {'type': 'string',
                                             'description': 'The message to schedule. Can be a '
                                                            "slash command like '/tickets' or any "
                                                            "regular text like 'show me the "
                                                            "tickets assigned to anyone'"},
                                 'time_expression': {'type': 'string'}},
                  'required': ['message', 'time_expression']},
  'visible_to_customer': True},
 {'name': 'list_user_schedules',
  'description': 'List all active scheduled commands for the current chat. Shows schedule ID, '
                 'command, timing, and next run time.',
  'inputSchema': {'type': 'object',
                  'properties': {'include_inactive': {'type': 'boolean',
                                                      'description': 'Include paused and completed '
                                                                     'schedules',
                                                      'default': False}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'cancel_user_schedule',
  'description': 'Cancel a scheduled command by its ID. The ID can be found using '
                 'list_user_schedules.',
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to cancel (UUID '
                                                                'format or first 8 characters)'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True},
 {'name': 'pause_user_schedule',
  'description': 'Pause a recurring schedule. Can be resumed later.',
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to pause'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True},
 {'name': 'resume_user_schedule',
  'description': 'Resume a paused schedule.',
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to resume'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True}]
