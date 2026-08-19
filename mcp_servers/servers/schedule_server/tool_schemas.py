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
  'description': "[ACTION - CREATES A SCHEDULE] Schedule a message (slash command or plain text) "
                 "to run once or on a recurring basis (e.g., 'daily at 9am', 'every other monday "
                 "at 9am', 'monthly on the 1st at 9am'). Use when the user wants something to run "
                 "later or repeatedly, rather than right now. Returns a confirmation with the new "
                 "schedule's ID and next run time (in the chat's timezone) — use list_user_schedules "
                 "to see it again later.",
  'inputSchema': {'type': 'object',
                  'properties': {'message': {'type': 'string',
                                             'description': 'The message to schedule. Can be a '
                                                            "slash command like '/tickets' or any "
                                                            "regular text like 'show me the "
                                                            "tickets assigned to anyone'"},
                                 'time_expression': {'type': 'string',
                                                      'description': "When to run it, in natural "
                                                                     "language — a one-time time "
                                                                     "('tomorrow at 3pm') or a "
                                                                     "recurrence ('daily at 9am', "
                                                                     "'every monday at 9am', "
                                                                     "'monthly on the 1st at "
                                                                     "9am')."}},
                  'required': ['message', 'time_expression']},
  'visible_to_customer': True},
 {'name': 'list_user_schedules',
  'description': '[READ-ONLY] List scheduled commands for the current chat. Shows schedule ID, '
                 'command, timing, and next run time. Active schedules only by default — pass '
                 'include_inactive to also see paused and cancelled ones.',
  'inputSchema': {'type': 'object',
                  'properties': {'include_inactive': {'type': 'boolean',
                                                      'description': 'Include paused and cancelled '
                                                                     'schedules alongside active '
                                                                     'ones',
                                                      'default': False}},
                  'required': []},
  'visible_to_customer': True},
 {'name': 'cancel_user_schedule',
  'description': '[ACTION - CANCELS SCHEDULE] Permanently stop a scheduled command. Unlike '
                 'pause_user_schedule, a cancelled schedule cannot be resumed — to run it again, '
                 'create a new one with schedule_user_command. Find the schedule_id with '
                 'list_user_schedules first if the user only described the schedule (e.g. "the '
                 'daily tickets one").',
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to cancel (UUID '
                                                                'format or first 8 characters)'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True},
 {'name': 'pause_user_schedule',
  'description': '[ACTION - PAUSES SCHEDULE] Temporarily stop a recurring schedule without losing '
                 "it. Unlike cancel_user_schedule, this can be undone with resume_user_schedule. "
                 "Has no effect on one-time (non-recurring) schedules that have already fired.",
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to pause (UUID '
                                                                'format or first 8 characters)'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True},
 {'name': 'resume_user_schedule',
  'description': '[ACTION - RESUMES SCHEDULE] Resume a schedule previously paused with '
                 'pause_user_schedule, recalculating its next run time from now. Fails if the '
                 "schedule isn't currently paused (e.g. it was cancelled, or a one-time schedule's "
                 "original time has already passed — create a new schedule instead).",
  'inputSchema': {'type': 'object',
                  'properties': {'schedule_id': {'type': 'string',
                                                 'description': 'The schedule ID to resume (UUID '
                                                                'format or first 8 characters)'}},
                  'required': ['schedule_id']},
  'visible_to_customer': True}]
