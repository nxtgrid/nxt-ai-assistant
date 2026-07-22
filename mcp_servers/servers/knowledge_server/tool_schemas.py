"""Tool schemas for the Knowledge MCP server.

The advertised manifest for this server, reconciled verbatim from
``mcp_servers/tool_definitions.json`` (what the orchestrator serves in
production) plus ``list_document_types``, which is implemented and was listed
by the old inline ``handle_list_tools`` but never made it into the JSON
manifest. ``mcp_servers/tests/test_tool_manifest_sync.py`` keeps the JSON a
subset of this file.

Plain dicts rather than ``types.Tool`` objects on purpose. ``handle_list_tools``
constructs a fresh ``types.Tool`` per call; sharing model instances across
calls would let one caller's mutation reach the next.

``visible_to_customer`` is what ``user_permissions.filter_tools_for_user``
reads to decide whether a non-staff user may see a tool. Only ``web_search``
is customer-visible; the rest are staff-only. The old inline definitions
omitted the flag entirely, which read as customer-visible through
``server_registry``'s fail-open default — declaring it here closes that.
"""

from typing import Any, Dict, List

TOOL_SCHEMAS: List[Dict[str, Any]] = [{'name': 'summarize_knowledge',
  'description': 'Search the internal knowledge base for structured documentation on technical '
                 'procedures.',
  'inputSchema': {'type': 'object',
                  'properties': {'topic': {'type': 'string'}},
                  'required': ['topic']},
  'visible_to_customer': False},
 {'name': 'get_grid_review_history',
  'description': '[READ-ONLY] Get monthly Grid Technical Review (GTR) history for a grid. Returns '
                 'distilled monthly reviews with KPIs (CUF, losses, revenue collection, '
                 'connections), commentary, actions taken, and pending issues. This is the monthly '
                 'technical review summary — NOT live data. For live KPIs use Grafana tools, for '
                 'recent O&M chat history use customer_get_grid_chat_chronology. Supports fuzzy '
                 'grid name matching.',
  'inputSchema': {'type': 'object',
                  'properties': {'grid_name': {'type': 'string',
                                               'description': 'Grid name (supports fuzzy '
                                                              'matching)'},
                                 'months_back': {'type': 'integer',
                                                 'description': 'Number of months to look back '
                                                                '(default 6, max 24)',
                                                 'default': 6}},
                  'required': ['grid_name']},
  'visible_to_customer': False},
 {'name': 'web_search',
  'description': '[READ-ONLY] Search the web for current information not in the knowledge base. '
                 'Use for: recent regulations or policy changes, current news, cultural/religious '
                 'dates (Ramadan, Eid, holidays), market prices, or any question requiring '
                 'up-to-date web information. Supports country targeting (default: Nigeria) and '
                 'African energy domain prioritization.',
  'inputSchema': {'type': 'object',
                  'properties': {'query': {'type': 'string',
                                           'description': 'Search query. Be specific — include '
                                                          'country, year, and topic for best '
                                                          'results.'},
                                 'country': {'type': 'string',
                                             'description': 'Two-letter country code to bias '
                                                            "results (e.g., 'ng' for Nigeria, 'cd' "
                                                            'for DRC). Optional.'},
                                 'topic': {'type': 'string',
                                           'description': "'general' (default) or 'news' for "
                                                          'recent news articles',
                                           'default': 'general'},
                                 'num_results': {'type': 'integer',
                                                 'description': 'Number of results (1-10, default '
                                                                '5)',
                                                 'default': 5},
                                 'days_back': {'type': 'integer',
                                               'description': 'Limit results to the last N days '
                                                              '(e.g., 7 for last week, 30 for last '
                                                              'month). Optional.'},
                                 'include_domains': {'type': 'string',
                                                     'description': 'Domain filter preset: '
                                                                    "'african_energy' "
                                                                    '(Nigerian/DRC news + '
                                                                    "regulatory), 'mini_grid' "
                                                                    '(mini-grid sector sources — '
                                                                    'AMDA, ESMAP, REA, IRENA, '
                                                                    "etc.), 'all' (both combined). "
                                                                    'Omit for general web.'}},
                  'required': ['query']},
  'visible_to_customer': True},
 {'name': 'web_extract',
  'description': '[READ-ONLY] Extract clean text content from a specific URL. Use when you have a '
                 'URL (from search results or user-provided) and need to read the full page '
                 'content. Returns cleaned text, not raw HTML.',
  'inputSchema': {'type': 'object',
                  'properties': {'url': {'type': 'string',
                                         'description': 'Full URL to extract content from'}},
                  'required': ['url']},
  'visible_to_customer': False},
 {'name': 'find_document',
  'description': 'Search Google Drive for a document by name fragment or document code (e.g., '
                 "'DOC-1234', 'ExampleSite Technical Review'). Returns the document name, URL, and "
                 'Google Doc ID if exactly one match is found. If multiple matches are found, '
                 'returns an error listing them so the user can provide a specific link. Use this '
                 'when a user references a document by name or code and you need its ID for '
                 'editing or reading.',
  'inputSchema': {'type': 'object',
                  'properties': {'query': {'type': 'string',
                                           'description': 'Document name fragment or code to '
                                                          "search for (e.g., 'DOC-1234', "
                                                          "'ExampleSite Grids Technical Review')"}},
                  'required': ['query']},
  'visible_to_customer': False},
 {'name': 'read_document',
  'description': 'Read the full content of a Google Doc as markdown. Use this to understand the '
                 "document's structure, style, and existing content before editing. Requires a "
                 'document ID — use find_document first if you only have a name.',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc file ID (from '
                                                                'find_document or a Google Docs '
                                                                'URL)'}},
                  'required': ['document_id']},
  'visible_to_customer': False},
 {'name': 'scan_doc_comments',
  'description': 'Scan a Google Doc for pending @anansibot comments. Returns a list of comments '
                 'with their highlighted text, instruction, and comment ID. Use this before '
                 'edit_doc_section to see what edits are requested in a document.',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc file ID (required — '
                                                                'not a document name). If the user '
                                                                'gives a name, use find_document '
                                                                'first to resolve the ID.'}},
                  'required': ['document_id']},
  'visible_to_customer': False},
 {'name': 'edit_doc_section',
  'description': 'Edit a section of a Google Doc with formatted markdown. SAFETY: This is a '
                 'destructive write operation. Before calling: (1) confirm with the user which '
                 'document and section will be edited, (2) never assume a document ID from context '
                 '— require an explicit file ID. If the user provides a name, use find_document '
                 'first. If find_document returns 2+ results, ask the user which one. Supports: '
                 '**bold**, *italic*, ## headings, - bullets, 1. numbered lists, | tables |, '
                 '[links](url).',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc file ID (required — '
                                                                'not a document name)'},
                                 'comment_id': {'type': 'string',
                                                'description': 'Comment ID from scan_doc_comments '
                                                               '(for comment-driven editing)'},
                                 'instruction': {'type': 'string',
                                                 'description': 'Edit instruction (for '
                                                                'instruction-driven editing, or to '
                                                                'override the comment text)'},
                                 'section_text': {'type': 'string',
                                                  'description': 'Exact text of the section to '
                                                                 'edit (for instruction-driven '
                                                                 'mode without comment_id)'},
                                 'replacement_markdown': {'type': 'string',
                                                          'description': 'Markdown-formatted '
                                                                         'replacement content. If '
                                                                         'not provided, the bot '
                                                                         'will generate it from '
                                                                         'the instruction.'}},
                  'required': ['document_id']},
  'visible_to_customer': False},
 {'name': 'list_document_types',
  'description': 'List the types of documents available in the knowledge base with counts.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': False}]
