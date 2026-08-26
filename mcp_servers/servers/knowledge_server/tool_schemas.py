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
  'description': '[READ-ONLY] Search the internal knowledge base and return an LLM-generated '
                 'summary of relevant documentation on a technical topic or procedure, with a '
                 'footer noting which live-data tools apply. Takes a few seconds (semantic '
                 'search + summarization). For a specific known module by slug rather than an '
                 'open-ended topic search, get_knowledge_module is more direct and doesn\'t '
                 'lose detail to summarization.',
  'inputSchema': {'type': 'object',
                  'properties': {'topic': {'type': 'string',
                                           'description': 'Topic or procedure to search for and '
                                                          'summarize (e.g. "meter commissioning '
                                                          'steps", "HPS power limit tiers").'},
                                 'max_words': {'type': 'integer',
                                               'description': 'Target length of the generated '
                                                              'summary in words.',
                                               'default': 250}},
                  'required': ['topic']},
  'visible_to_customer': False},
 {'name': 'get_knowledge_module',
  'description': "[READ-ONLY] Fetch the full body of a technical knowledge module by slug. "
                 "Modules attached to your system prompt are already inlined in full under "
                 "'# Technical Knowledge' / '# Live Context', so you rarely need this -- use it "
                 "to read a module by name that is NOT already in your context, for example one "
                 "referenced by slug in a document or by a colleague.",
  'inputSchema': {'type': 'object',
                  'properties': {'slug': {'type': 'string',
                                          'description': 'Exact module slug, e.g. '
                                                         '"azimuth-calculation".'}},
                  'required': ['slug']},
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
                                                            "for DRC). Defaults to 'ng'. Pass an "
                                                            'empty string for no country bias.',
                                             'default': 'ng'},
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
  'description': '[READ-ONLY] Search Google Drive for a document by name fragment or document code (e.g., '
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
  'description': '[READ-ONLY] Read the full content of a Google Doc as markdown. Use this to understand the '
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
  'description': '[READ-ONLY] Scan a Google Doc or Google Sheet for pending @anansibot '
                 'comments. Returns each comment\'s highlighted text (for a Sheet, the '
                 'commented cell\'s content), instruction, and comment ID. Use before '
                 'edit_doc_section to see what edits a file is asking for. A comment on '
                 'an empty cell cannot be located and is returned with empty text.',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc or Sheet file ID '
                                                                '(required — not a name). If '
                                                                'the user gives a name, use '
                                                                'find_document first to '
                                                                'resolve the ID.'}},
                  'required': ['document_id']},
  'visible_to_customer': False},
 {'name': 'edit_doc_section',
  'description': '[ACTION - DESTRUCTIVE WRITE] Edit ONE section of a Google Doc or a cell of a '
                 'Google Sheet, overwriting the existing content. To apply every pending '
                 '@anansi-chatbot comment in a file, use process_doc_comments instead — it '
                 'orders the edits and this tool does not. Before calling: (1) confirm with the '
                 'user which file and section will be edited, (2) never assume a file ID from '
                 'context — require an explicit file ID. If the user provides a name, use '
                 'find_document first. If find_document returns 2+ results, ask the user which '
                 'one. For a Doc, replacement_markdown supports **bold**, *italic*, ## headings, '
                 '- bullets, 1. numbered lists, | tables |, [links](url) — for a Sheet, it is '
                 'written as the cell\'s literal text with no markdown rendering.',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc or Sheet file ID '
                                                                '(required — not a document '
                                                                'name)'},
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
 {'name': 'process_doc_comments',
  'description': '[ACTION - DESTRUCTIVE WRITE] Apply EVERY pending @anansi-chatbot comment in a '
                 'Google Doc in one batch, then resolve each comment. Prefer this over calling '
                 'edit_doc_section once per comment: it reads the whole document, works out '
                 'which comments depend on the finished text (e.g. "summarise the sections '
                 'above") and writes those last, and edits bottom-to-top so one edit never '
                 'moves the next one\'s anchor. Before calling: confirm the file with the user '
                 'first; never assume a file ID from context. Use scan_doc_comments first if '
                 'the user wants to preview what will change. Caps at 10 edits per run.\n'
                 'Slow: takes up to ~2 min for a full batch — tell the user before calling.',
  'inputSchema': {'type': 'object',
                  'properties': {'document_id': {'type': 'string',
                                                 'description': 'Google Doc file ID (required — '
                                                                'not a document name). Use '
                                                                'find_document to resolve a '
                                                                'name first.'}},
                  'required': ['document_id']},
  'visible_to_customer': False},
 {'name': 'list_document_types',
  'description': '[READ-ONLY] List the types of documents available in the knowledge base, '
                 'with a count of each — a quick orientation before a targeted search.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': False},
 {'name': 'get_graph_schema',
  'description': '[READ-ONLY] List the entity types and relationship types in the knowledge '
                 'graph, with counts and example entity names. Call this FIRST when a question '
                 'needs structured facts about equipment, sites or their connections — it tells '
                 'you what kinds of things exist before you search for specific ones. Returns a '
                 'compact ontology, filtered to what you may see. For free-text passages rather '
                 'than entities, use summarize_knowledge instead.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': False},
 {'name': 'search_entities',
  'description': '[READ-ONLY] Find entities in the knowledge graph by name, optionally narrowed '
                 'to one entity type from get_graph_schema. Use to turn a name a user mentioned '
                 'into a real entity id before traversing. Returns matching entities with their '
                 'ids, types and descriptions; suggests near-matches when nothing matches '
                 'exactly. Follow with get_entity_neighbors to explore what an entity connects '
                 'to.',
  'inputSchema': {'type': 'object',
                  'properties': {'query': {'type': 'string',
                                           'description': 'Name or partial name to search for.'},
                                 'entity_type': {'type': 'string',
                                                 'description': 'Optional type filter — use a '
                                                                'value from get_graph_schema.'},
                                 'limit': {'type': 'integer', 'default': 10}},
                  'required': ['query']},
  'visible_to_customer': False},
 {'name': 'get_entity_neighbors',
  'description': '[READ-ONLY] List what one entity connects to in the knowledge graph, '
                 'optionally narrowed to one relationship type. Use after search_entities to '
                 'follow a connection — which meters sit on a DCU, which site a grid belongs '
                 'to. Returns neighbouring entities with the relationship joining them and its '
                 'direction. For the source passages behind a claim, use get_entity_evidence.',
  'inputSchema': {'type': 'object',
                  'properties': {'entity_id': {'type': 'string',
                                               'description': 'Entity id from search_entities.'},
                                 'relationship_type': {'type': 'string',
                                                       'description': 'Optional filter — use a '
                                                                      'value from '
                                                                      'get_graph_schema.'},
                                 'limit': {'type': 'integer', 'default': 25}},
                  'required': ['entity_id']},
  'visible_to_customer': False},
 {'name': 'get_entity_evidence',
  'description': '[READ-ONLY] Retrieve the source document passages an entity was extracted '
                 'from. Use to ground a claim before stating it, or when a neighbour '
                 'relationship looks surprising and you want to check the underlying text. '
                 'Returns excerpts with their document titles. For a broad topic summary rather '
                 'than one entity\'s sources, use summarize_knowledge.',
  'inputSchema': {'type': 'object',
                  'properties': {'entity_id': {'type': 'string',
                                               'description': 'Entity id from search_entities.'},
                                 'limit': {'type': 'integer', 'default': 5}},
                  'required': ['entity_id']},
  'visible_to_customer': False}]
