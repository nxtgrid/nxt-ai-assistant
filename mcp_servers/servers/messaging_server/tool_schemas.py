"""Tool schema for the Messaging MCP server.

Reconciled from ``mcp_servers/tool_definitions.json`` with one fix: the
manifest named the tool ``messaging_send_to_group``, but the orchestrator adds
the ``messaging_`` server prefix when advertising and strips it again on
dispatch — so the manifest name produced the double-prefixed
``messaging_messaging_send_to_group`` for the LLM, which after one strip never
matched the ``send_to_group`` dispatch branch, nor the
``messaging_send_to_group`` entries in persistent_agent_graph's action
allowlists. The name here (and now in the JSON) is the bare ``send_to_group``.

``persistent_only`` is load-bearing: ``user_permissions`` filters this tool
out of the normal chat flow; only persistent agents may see it.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'send_to_group',
  'description': 'Send a message to a registered staff Telegram group. Only groups defined in the '
                 'Staff Groups configuration are allowed. Use this for escalations, cross-grid '
                 'coordination, or posting summaries to specialized groups.',
  'inputSchema': {'type': 'object',
                  'properties': {'chat_id': {'type': 'string',
                                             'description': 'Telegram chat ID of the target group '
                                                            '(must be a registered staff group)'},
                                 'text': {'type': 'string', 'description': 'Message text to send'},
                                 'topic_id': {'type': 'string',
                                              'description': 'Optional topic/thread ID for forum '
                                                             'groups'}},
                  'required': ['chat_id', 'text']},
  'visible_to_customer': False,
  'persistent_only': True}]
