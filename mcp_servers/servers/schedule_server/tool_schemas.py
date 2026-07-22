"""Tool schemas for the Schedule MCP server.

The advertised manifest for this server, reconciled verbatim from
``mcp_servers/tool_definitions.json`` (what the orchestrator serves in
production). The five user-schedule tools were previously defined inline in
``handle_list_tools``; the five agent/workflow tools (create_user_agent,
list_user_agents, cancel_user_agent, start_expert_workflow,
check_workflow_result) were implemented and advertised only through the JSON
manifest — running ``scripts/export_tools.py`` would have deleted them.
``mcp_servers/tests/test_tool_manifest_sync.py`` keeps the JSON a subset of
this file.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call; sharing model instances across
calls would let one caller's mutation reach the next.

``visible_to_customer`` mirrors the manifest: user-schedule tools are visible,
agent/workflow tools are staff-only. Chat/topic/user identity is injected by
the tool_executor, never LLM-controlled (see server module docstring).
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
  'visible_to_customer': True},
 {'name': 'create_user_agent',
  'description': '[ACTION] Create a persistent monitoring agent that checks a condition on a '
                 "schedule and notifies the user when it's met. Use when the user says things like "
                 "'alert me when X', 'let me know if Y', 'watch Z for me', 'track when X happens'. "
                 'Do NOT use for simple recurring commands (use schedule_schedule_user_command '
                 'instead). BEFORE calling this tool, verify FEASIBILITY: (1) Can the check_prompt '
                 'be answered using the tools currently available to you? If not, tell the user '
                 "what's missing instead of creating an agent. (2) Does the entity exist? (e.g., "
                 "if they mention a grid name, verify it's a real grid). (3) Is the data source "
                 'accessible? (e.g., battery SOC requires VRM tools). If any check fails, explain '
                 "why the agent can't be created and suggest alternatives. You MUST provide TWO "
                 "prompts: (1) check_prompt — a yes/no gate question (e.g., 'Have any customers in "
                 "ExampleGrid crossed 200W?'), and (2) response_prompt — the full detail query to "
                 "run ONLY when the check triggers (e.g., 'List all customers in ExampleGrid above "
                 "200W with their meter ID and current power.'). Strip 'alert me', 'let me know', "
                 "'watch for' etc. from BOTH prompts. NEVER include 'create', 'agent', 'monitor' "
                 'in either prompt.',
  'inputSchema': {'type': 'object',
                  'properties': {'agent_type': {'type': 'string',
                                                'description': 'Agent type. Use '
                                                               "'condition_monitor' (default) for "
                                                               'threshold alerts and watch-for-X '
                                                               'requests. For other agent types '
                                                               'defined in the expert instructions '
                                                               '(e.g., site_visit_tracker, '
                                                               'project_manager), use the '
                                                               'expert_id from the doc. If unsure, '
                                                               "use 'condition_monitor'.",
                                                'default': 'condition_monitor'},
                                 'instance_name': {'type': 'string',
                                                   'description': 'Short friendly name (e.g., '
                                                                  "'ExampleGrid 200W threshold "
                                                                  "monitor')"},
                                 'check_prompt': {'type': 'string',
                                                  'description': 'For condition_monitor agents: '
                                                                 'YES/NO gate question evaluated '
                                                                 "every wake. Must end in '?'. For "
                                                                 'other agent types: the main task '
                                                                 'description or goal.'},
                                 'response_prompt': {'type': 'string',
                                                     'description': 'For condition_monitor: full '
                                                                    'detail query when check '
                                                                    'triggers. For other agent '
                                                                    'types: additional context or '
                                                                    'desired output format. Can be '
                                                                    'empty for expert-defined '
                                                                    'agents.'},
                                 'anchor_entity': {'type': 'string',
                                                   'description': 'Optional: name of the grid, '
                                                                  'organization, or other entity '
                                                                  'this agent is about. Used to '
                                                                  'enrich agent context with '
                                                                  'relevant chat history, status '
                                                                  'data, etc. Examples: '
                                                                  "'ExampleGrid' (grid), "
                                                                  "'ExampleOrg' (org). Supports "
                                                                  'fuzzy matching. Omit if the '
                                                                  'agent is not entity-specific.'},
                                 'wake_schedule': {'type': 'string',
                                                   'description': 'Cron expression for check '
                                                                  "frequency. Default: '0 8-18 * * "
                                                                  "1-5' (hourly during work "
                                                                  "hours). Use '*/30 8-18 * * 1-5' "
                                                                  "for every 30 min, '0 9,13,17 * "
                                                                  "* 1-5' for 3x daily.",
                                                   'default': '0 8-18 * * 1-5'},
                                 'auto_complete': {'type': 'boolean',
                                                   'description': 'If true, agent auto-terminates '
                                                                  'after condition is met once. '
                                                                  'Default true for one-time '
                                                                  'alerts, false for ongoing '
                                                                  'tracking.',
                                                   'default': True},
                                 'model_tier': {'type': 'string',
                                                'description': "Model tier: 'standard' for simple "
                                                               'threshold checks, status '
                                                               'monitoring, or factual lookups '
                                                               "(e.g., 'alert me when SOC drops "
                                                               "below 60%'). 'pro' for complex "
                                                               'analysis, multi-step reasoning, '
                                                               'regulatory interpretation, trend '
                                                               'analysis, or tasks requiring deep '
                                                               "domain understanding (e.g., 'track "
                                                               'project development and coordinate '
                                                               "site visits'). Default: "
                                                               "'standard'.",
                                                'default': 'standard'}},
                  'required': ['instance_name', 'check_prompt', 'response_prompt']},
  'visible_to_customer': False},
 {'name': 'list_user_agents',
  'description': '[READ-ONLY] List all persistent monitoring agents created by the current user. '
                 "Shows agent name, status, what it's checking, wake count, and last wake time.",
  'inputSchema': {'type': 'object',
                  'properties': {'include_terminated': {'type': 'boolean',
                                                        'description': 'Include cancelled agents '
                                                                       'in the list',
                                                        'default': False}}},
  'visible_to_customer': False},
 {'name': 'cancel_user_agent',
  'description': '[ACTION] Cancel (terminate) a user-created monitoring agent. The agent stops '
                 "checking and is marked as terminated. Use when user says 'cancel agent', 'stop "
                 "monitoring', 'remove agent', or picks an agent to cancel from the list.",
  'inputSchema': {'type': 'object',
                  'properties': {'instance_id': {'type': 'string',
                                                 'description': 'UUID of the agent instance to '
                                                                'cancel (from list_user_agents)'}},
                  'required': ['instance_id']},
  'visible_to_customer': False},
 {'name': 'start_expert_workflow',
  'description': '[ACTION] Start an expert workflow in the background. Returns immediately with a '
                 'packet_id. The agent will be woken when the workflow completes. Use '
                 'check_workflow_result to read outputs. Available experts: lpp_expert (layout '
                 'generation), gtr_expert (grid technical review). IMPORTANT: Provide ALL required '
                 'inputs upfront — the workflow runs without user interaction.',
  'inputSchema': {'type': 'object',
                  'properties': {'expert_id': {'type': 'string',
                                               'description': "Expert to invoke: 'lpp_expert' or "
                                                              "'gtr_expert'"},
                                 'packet_type': {'type': 'string',
                                                 'description': 'Packet type matching expert '
                                                                'definition (e.g., '
                                                                "'lpp_generation', "
                                                                "'grids_technical_review')"},
                                 'inputs': {'type': 'string',
                                            'description': 'JSON object with all required inputs '
                                                           'for the workflow (e.g., '
                                                           '\'{"site_name": "ExampleGrid", '
                                                           '"editable_total_buildings": 150}\')'},
                                 'prefilled_inputs': {'type': 'string',
                                                      'description': 'Optional: JSON object with '
                                                                     'pre-filled responses for '
                                                                     'interactive steps. Format: '
                                                                     '\'{"step_name": '
                                                                     '"response"}\''}},
                  'required': ['expert_id', 'packet_type', 'inputs']},
  'visible_to_customer': False},
 {'name': 'check_workflow_result',
  'description': '[READ-ONLY] Check the status and result of a previously started expert workflow. '
                 'Returns packet status (pending/in_progress/completed/failed), outputs if '
                 'completed, or error details if failed.',
  'inputSchema': {'type': 'object',
                  'properties': {'packet_id': {'type': 'string',
                                               'description': 'Packet ID returned by '
                                                              'start_expert_workflow'}},
                  'required': ['packet_id']},
  'visible_to_customer': False}]
