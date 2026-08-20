"""MCP Knowledge Server - Knowledge base, GTR reviews, and web search tools.

All 'find information' tools live here:
- summarize_knowledge: internal RAG knowledge base
- get_grid_review_history: GTR monthly reviews from Google Sheets
- web_search: web search via Tavily (regulations, news, cultural dates)
- web_extract: extract content from a URL
"""

import difflib
import json
import logging
import os
import sys
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities
from shared_code.tool_registry import ToolRegistry

from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.llm.model_tiers import resolve_model
from shared.prompts import PROMPTS

from .tool_schemas import TOOL_SCHEMAS

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("knowledge-server")

print("🚀 Knowledge MCP Server starting...", file=sys.stderr)

# Initialize MCP server
server = Server("knowledge-server")
registry = ToolRegistry("knowledge")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}

# Database configuration
CHAT_DB_URL = os.getenv("CHAT_DB_URL", "")
CHAT_DB_SERVICE_KEY = os.getenv("CHAT_DB_SERVICE_KEY", "")

# The org id staff conversations resolve to -- same env var and default as
# servers/customer_server/client_base.py's STAFF_ORG_ID. Read independently
# rather than imported: MCP servers are separate deployables, each with its
# own dependency set, and there is no existing precedent in this codebase
# for one server importing another's module.
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))

# MCP tool names that are relevant to various topics
TOOL_TOPIC_MAP = {
    "grid": [
        "customer_get_grid_status",
        "customer_list_all_grids_status",
        "equipment_diagnostics_get_current_status",
    ],
    "meter": ["meters_lookup_meter", "meters_meter_status", "meters_list_meters"],
    "ticket": ["jira_search_issues_with_comments", "jira_get_issue"],
    "payment": ["customer_check_payment_status", "payment_processor_check_transaction"],
    "equipment": [
        "equipment_diagnostics_get_current_status",
        "equipment_diagnostics_get_historical_data",
        "equipment_control_turn_on_grid",
    ],
    "solar": ["solar_assess_solar_potential", "solar_get_solar_data"],
    "schedule": ["schedule_list_user_schedules", "schedule_schedule_user_command"],
    "log": ["logs_query_logs", "logs_search_errors"],
    "design": ["grid_design_generate_bom", "grid_design_get_design_status"],
}


async def generate_query_embedding(text: str) -> Optional[List[float]]:
    """Generate embedding for a query using Google's text-embedding-005 via Vertex AI."""
    try:
        from shared.utils.vertex_embeddings import get_embedding

        return await get_embedding(text, task_type="RETRIEVAL_QUERY")

    except Exception as e:
        logger.error(f"Embedding generation failed: {e}")
        return None


async def search_relevant_chunks(
    query: str, limit: int = 10, threshold: float = 0.65
) -> List[Dict[str, Any]]:
    """Search for relevant chunks in the knowledge base."""
    try:
        from supabase import create_client

        if not CHAT_DB_URL or not CHAT_DB_SERVICE_KEY:
            logger.warning("Database not configured")
            return []

        client = create_client(CHAT_DB_URL, CHAT_DB_SERVICE_KEY)

        # Generate query embedding
        embedding = await generate_query_embedding(query)
        if not embedding:
            return []

        # Search using vector similarity (no permission filtering - this is a staff-only tool)
        # Use search_chunks which doesn't require role/org matching
        results = client.rpc(
            "search_chunks",
            {
                "query_embedding": embedding,
                "match_threshold": threshold,
                "match_count": limit,
            },
        ).execute()

        if not results.data:
            return []

        chunks = []
        for row in results.data:
            metadata = row.get("chunk_metadata") or row.get("source_metadata") or {}
            chunks.append(
                {
                    "content": row.get("content", ""),
                    "title": metadata.get("title", row.get("title", "Unknown")),
                    "doc_type": metadata.get("doc_type", "unknown"),
                    "similarity": row.get("similarity", 0.0),
                    "source_type": metadata.get("source_type", row.get("source_type", "unknown")),
                }
            )

        return chunks

    except Exception as e:
        logger.error(f"Chunk search failed: {e}")
        return []


def identify_relevant_tools(topic: str) -> List[str]:
    """Identify MCP tools that could provide more information on a topic."""
    topic_lower = topic.lower()
    relevant_tools = []

    for keyword, tools in TOOL_TOPIC_MAP.items():
        if keyword in topic_lower:
            relevant_tools.extend(tools)

    # Deduplicate while preserving order
    seen = set()
    unique_tools = []
    for tool in relevant_tools:
        if tool not in seen:
            seen.add(tool)
            unique_tools.append(tool)

    return unique_tools


async def summarize_with_llm(
    topic: str, chunks: List[Dict[str, Any]], relevant_tools: List[str], max_words: int = 250
) -> str:
    """Use Gemini to summarize the retrieved knowledge."""
    try:
        model = resolve_model("fast")
        gateway = get_default_generation_gateway(
            default_model=model,
        )

        # Format chunks for the prompt
        chunks_text = ""
        for i, chunk in enumerate(chunks[:8], 1):  # Limit to 8 chunks for context
            chunks_text += (
                f"\n[{i}] ({chunk['doc_type']}) {chunk['title']}:\n{chunk['content'][:500]}...\n"
            )

        # Format tools
        tools_text = ""
        if relevant_tools:
            tools_text = "\n\nRelevant MCP tools for live data:\n- " + "\n- ".join(
                relevant_tools[:5]
            )

        prompt = PROMPTS.text(
            "knowledge.summarize_topic",
            topic=topic,
            chunks_text=chunks_text,
            tools_text=tools_text,
            max_words=max_words,
        )

        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(model=model),
        )

        return str(response.text).strip()

    except Exception as e:
        logger.error(f"LLM summarization failed: {e}")
        # Fallback to simple summary
        summary = f"## Knowledge Summary: {topic}\n\n"
        if chunks:
            summary += f"Found {len(chunks)} relevant documents:\n\n"
            for chunk in chunks[:5]:
                summary += f"- **{chunk['title']}** ({chunk['doc_type']})\n"
        else:
            summary += "No relevant documents found in the knowledge base.\n"

        if relevant_tools:
            summary += f"\n**For live data, try:** {', '.join(relevant_tools[:3])}"

        return summary


@registry.tool("summarize_knowledge", _SCHEMAS_BY_NAME["summarize_knowledge"])
async def _handle_summarize_knowledge(arguments: dict) -> list[types.TextContent]:
    """Handle summarize_knowledge tool call."""
    topic = arguments.get("topic", "")
    max_words = arguments.get("max_words", 250)

    if not topic:
        return [types.TextContent(type="text", text="Error: topic is required")]

    logger.info(f"Summarizing knowledge for topic: {topic}")

    # Search for relevant chunks
    chunks = await search_relevant_chunks(topic, limit=15, threshold=0.60)

    # Identify relevant tools
    relevant_tools = identify_relevant_tools(topic)

    # Generate summary
    summary = await summarize_with_llm(topic, chunks, relevant_tools, max_words)

    # Add footer with metadata
    footer = f"\n\n---\n*Based on {len(chunks)} documents from the knowledge base.*"
    if relevant_tools:
        footer += f"\n*Live data available via: {', '.join(relevant_tools[:3])}*"

    return [types.TextContent(type="text", text=summary + footer)]


# Sources whose body depends on row-level database permissions this server
# does not carry. They are composed into context automatically instead.
# `gdoc` is deliberately NOT here: it is JIT, but it resolves from a caller's
# Drive access, which this server *can* check.
UNFETCHABLE_SOURCES = ("graph", "directory", "episodic")


async def fetch_knowledge_module(
    slug: str,
    user_email: Optional[str] = None,
    store: Any = None,
    gdoc_provider: Any = None,
) -> str:
    """Return one knowledge module's full body by slug, for this caller.

    Backs the on-demand tier: the model sees only slug and summary in context
    and calls this when it decides a module is relevant.

    A gdoc module is gated on the caller's Drive access -- without that this
    tool is a read primitive for every attached document, since the model
    chooses the slug. A graph/directory/episodic module cannot resolve here:
    its body depends on row-level permissions this server does not carry, so
    say so plainly rather than returning an empty body, which the model would
    read as "this module has no content".
    """
    from shared.prompts.knowledge import KnowledgeStore

    store = store or KnowledgeStore.from_env()
    modules = {m.slug: m for m in store.all_modules()}
    if not modules:
        return "No knowledge modules are configured."
    module = modules.get(slug)
    if not module:
        return f"No knowledge module named '{slug}'. Available: " + ", ".join(sorted(modules))

    if module.source in UNFETCHABLE_SOURCES:
        return (
            f"'{slug}' is live context. It is composed into your context "
            f"automatically when relevant and cannot be fetched on demand here."
        )

    body = module.body
    if module.source == "gdoc":
        from shared.prompts.providers import ResolutionContext
        from shared.prompts.types import RequestScope

        if gdoc_provider is None:
            from shared.prompts.providers_gdoc import GDocProvider

            gdoc_provider = GDocProvider()

        ctx = ResolutionContext(scope=RequestScope(), user_email=user_email)
        if not await gdoc_provider.visible_to(module, ctx):
            return (
                f"You do not have access to the document behind '{slug}'. "
                f"Ask the document owner to share it with you."
            )
        body = await gdoc_provider.resolve(module, ctx)

    if not body:
        return f"Knowledge module '{slug}' could not be loaded from its source."

    return f"# {module.title}\n\n{body}"


@registry.tool("get_knowledge_module", _SCHEMAS_BY_NAME["get_knowledge_module"])
async def _handle_get_knowledge_module(arguments: dict) -> list[types.TextContent]:
    """Handle get_knowledge_module tool call."""
    slug = arguments.get("slug", "")
    if not slug:
        return [types.TextContent(type="text", text="Error: slug is required")]
    # user_email is injected by the orchestrator's tool_executor, not declared
    # in the tool schema and never model-controlled.
    return [
        types.TextContent(
            type="text",
            text=await fetch_knowledge_module(slug, user_email=arguments.get("user_email")),
        )
    ]


@registry.tool("list_document_types", _SCHEMAS_BY_NAME["list_document_types"])
async def _handle_list_document_types(arguments: dict) -> list[types.TextContent]:
    """Handle list_document_types tool call."""
    try:
        from supabase import create_client

        if not CHAT_DB_URL or not CHAT_DB_SERVICE_KEY:
            return [types.TextContent(type="text", text="Database not configured")]

        client = create_client(CHAT_DB_URL, CHAT_DB_SERVICE_KEY)

        # Get all documents with metadata
        response = client.table("documents").select("metadata, source_type").execute()

        if not response.data:
            return [types.TextContent(type="text", text="No documents in knowledge base")]

        # Count by doc_type and source_type
        doc_types: Dict[str, int] = {}
        source_types: Dict[str, int] = {}

        for row in response.data:
            metadata = row.get("metadata", {}) or {}
            doc_type = metadata.get("doc_type", "unknown")
            source_type = row.get("source_type", "unknown")

            doc_types[doc_type] = doc_types.get(doc_type, 0) + 1
            source_types[source_type] = source_types.get(source_type, 0) + 1

        # Format response
        output = "## Knowledge Base Summary\n\n"
        output += f"**Total Documents:** {len(response.data)}\n\n"

        output += "### By Document Type\n"
        for dtype, count in sorted(doc_types.items(), key=lambda x: -x[1]):
            output += f"- {dtype}: {count}\n"

        output += "\n### By Source\n"
        for stype, count in sorted(source_types.items(), key=lambda x: -x[1]):
            output += f"- {stype}: {count}\n"

        return [types.TextContent(type="text", text=output)]

    except Exception as e:
        logger.error(f"Error listing document types: {e}")
        return [
            types.TextContent(type="text", text="An error occurred while listing document types")
        ]


async def main():
    """Main entry point for the MCP server."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="knowledge-server",
                server_version="1.0.0",
                capabilities=ServerCapabilities(tools={}),
            ),
        )


# ── Web Search (Tavily) ────────────────────────────────────────────────

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

AFRICAN_ENERGY_DOMAINS = [
    "nerc.gov.ng",
    "nigerian-electricity-hub.com",
    "premiumtimesng.com",
    "punchng.com",
    "vanguardngr.com",
    "guardian.ng",
    "businessday.ng",
    "thecable.ng",
    "dailypost.ng",
    "nairametrics.com",
    "are.gouv.cd",
    "snel.cd",
    "acp.cd",
    "africa-energy-portal.org",
    "allafrica.com",
    "esi-africa.com",
    "energyconnects.com",
    "africa-energy.com",
]

MINI_GRID_DOMAINS = [
    "amda.power",
    "minigrids.org",
    "afdb.org",
    "esmap.org",
    "seforall.org",
    "rmi.org",
    "nerc.gov.ng",
    "rea.gov.ng",
    "nigerian-electricity-hub.com",
    "power.gov.ng",
    "are.gouv.cd",
    "snel.cd",
    "anser.cd",
    "ewura.go.tz",
    "rea.go.tz",
    "tanesco.co.tz",
    "era.go.ug",
    "rea.or.ug",
    "umeme.co.ug",
    "energypedia.info",
    "sun-connect-news.org",
    "gogla.org",
    "irena.org",
    "worldbank.org",
    "ifc.org",
    "greenminigrid.afdb.org",
    "pv-magazine.com",
    "esi-africa.com",
    "africa-energy-portal.org",
    "x.com",
    "twitter.com",
    "linkedin.com",
]

DOMAIN_PRESETS = {
    "african_energy": AFRICAN_ENERGY_DOMAINS,
    "mini_grid": MINI_GRID_DOMAINS,
    "all": AFRICAN_ENERGY_DOMAINS
    + [d for d in MINI_GRID_DOMAINS if d not in AFRICAN_ENERGY_DOMAINS],
}

COUNTRY_MAP = {
    "ng": "Nigeria",
    "cd": "Congo - Democratic Republic (Kinshasa)",
    "ke": "Kenya",
    "gh": "Ghana",
    "za": "South Africa",
    "cm": "Cameroon",
    "sn": "Senegal",
    "tz": "Tanzania",
    "ug": "Uganda",
    "rw": "Rwanda",
    "et": "Ethiopia",
}


def _get_tavily_client():
    """Get Tavily client."""
    from tavily import TavilyClient  # type: ignore[import-untyped]

    if not TAVILY_API_KEY:
        raise ValueError("Web search is temporarily unavailable")
    return TavilyClient(api_key=TAVILY_API_KEY)


@registry.tool("web_search", _SCHEMAS_BY_NAME["web_search"])
async def _handle_web_search(arguments: dict) -> list[types.TextContent]:
    """Search the web using Tavily."""
    import asyncio
    import json

    query = arguments.get("query", "")
    if not query:
        return [types.TextContent(type="text", text="Error: query is required")]

    try:
        client = _get_tavily_client()
        topic = arguments.get("topic", "general")
        search_params: Dict[str, Any] = {
            "query": query,
            "max_results": min(arguments.get("num_results", 5), 10),
            "search_depth": "basic",
            "include_answer": True,
        }

        # Default to Nigeria — this deployment is Nigeria-focused (see the
        # reference_server's Nigeria-only tariff/standards tools). Pass an
        # explicit empty string to search with no country bias.
        country = arguments.get("country", "ng")
        if country and topic != "news":
            search_params["country"] = COUNTRY_MAP.get(country.lower(), country)
        elif country:
            country_name = COUNTRY_MAP.get(country.lower(), country)
            search_params["query"] = f"{query} {country_name}"

        if topic in ("general", "news"):
            search_params["topic"] = topic

        days_back = arguments.get("days_back")
        if days_back and isinstance(days_back, int):
            time_map = {1: "day", 7: "week", 30: "month", 365: "year"}
            closest = min(time_map.keys(), key=lambda k: abs(k - days_back))
            search_params["time_range"] = time_map[closest]

        include_domains = arguments.get("include_domains")
        if isinstance(include_domains, str) and include_domains in DOMAIN_PRESETS:
            search_params["include_domains"] = DOMAIN_PRESETS[include_domains]
        elif isinstance(include_domains, list):
            search_params["include_domains"] = include_domains

        result = await asyncio.to_thread(client.search, **search_params)

        output: Dict[str, Any] = {
            "query": query,
            "answer": result.get("answer", ""),
            "results": [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "content": item.get("content", "")[:500],
                    "score": item.get("score", 0),
                }
                for item in result.get("results", [])
            ],
        }
        output["result_count"] = len(output["results"])
        return [types.TextContent(type="text", text=json.dumps(output, indent=2, default=str))]

    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return [types.TextContent(type="text", text="Web search is temporarily unavailable")]


@registry.tool("web_extract", _SCHEMAS_BY_NAME["web_extract"])
async def _handle_web_extract(arguments: dict) -> list[types.TextContent]:
    """Extract clean content from a URL using Tavily."""
    import asyncio
    import json
    from urllib.parse import urlparse

    url = arguments.get("url", "")
    if not url:
        return [types.TextContent(type="text", text="Error: url is required")]

    # Validate URL to prevent SSRF
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return [
                types.TextContent(type="text", text="Error: only http/https URLs are supported")
            ]
        hostname = (parsed.hostname or "").lower()
        blocked = ["localhost", "127.0.0.1", "0.0.0.0", "169.254.169.254", "[::1]"]
        if (
            hostname in blocked
            or hostname.startswith("10.")
            or hostname.startswith("192.168.")
            or hostname.startswith("172.")
        ):
            return [types.TextContent(type="text", text="Error: internal URLs are not allowed")]
    except Exception:
        return [types.TextContent(type="text", text="Error: invalid URL")]

    try:
        client = _get_tavily_client()
        result = await asyncio.to_thread(client.extract, urls=[url])
        extracted = result.get("results", [])
        if not extracted:
            return [types.TextContent(type="text", text=f"No content extracted from {url}")]

        content = extracted[0]
        output = {"url": content.get("url", url), "content": content.get("raw_content", "")[:3000]}
        return [types.TextContent(type="text", text=json.dumps(output, indent=2, default=str))]

    except Exception as e:
        logger.error(f"Web extract failed: {e}")
        return [
            types.TextContent(type="text", text="Content extraction is temporarily unavailable")
        ]


# ── Find Document ─────────────────────────────────────────────────────


@registry.tool("find_document", _SCHEMAS_BY_NAME["find_document"])
async def _handle_find_document(arguments: dict) -> list[types.TextContent]:
    """Search Google Drive for a document by name fragment, code, URL, or ID."""
    import json

    query = (arguments.get("query") or "").strip()
    if not query:
        return [types.TextContent(type="text", text="Error: query is required")]

    user_email = arguments.get("user_email")

    try:
        from shared.utils.drive_resolver import AmbiguousDocumentMatch, resolve_document

        doc = await resolve_document(query, user_email=user_email)

        if not doc:
            return [
                types.TextContent(
                    type="text",
                    text=f"No documents found matching '{query}'. "
                    "Try a different name fragment or provide a direct Google Docs link.",
                )
            ]

        output = {
            "document_id": doc["file_id"],
            "name": doc["name"],
            "url": doc["url"],
        }
        return [types.TextContent(type="text", text=json.dumps(output, indent=2))]

    except AmbiguousDocumentMatch as e:
        match_list = "\n".join(f"- {m['name']} ({m['url']})" for m in e.matches)
        return [
            types.TextContent(
                type="text",
                text=f"Multiple documents match '{query}'. "
                f"Please ask the user which one they mean:\n{match_list}",
            )
        ]

    except Exception as e:
        logger.error(f"Find document failed: {e}")
        return [
            types.TextContent(
                type="text",
                text="Document search is temporarily unavailable. "
                "Please provide a direct Google Docs link instead.",
            )
        ]


@registry.tool("read_document", _SCHEMAS_BY_NAME["read_document"])
async def _handle_read_document(arguments: dict) -> list[types.TextContent]:
    """Fetch a Google Doc's content as markdown."""
    document_id = (arguments.get("document_id") or "").strip()
    if not document_id:
        return [types.TextContent(type="text", text="Error: document_id is required")]

    try:
        from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

        markdown = fetch_google_doc_markdown(document_id)
        if not markdown:
            return [
                types.TextContent(
                    type="text",
                    text="Could not read document. Check the document ID and that the service account has access.",
                )
            ]
        return [types.TextContent(type="text", text=markdown)]

    except Exception as e:
        logger.error(f"Read document failed: {e}")
        return [
            types.TextContent(
                type="text",
                text="Document read is temporarily unavailable. Please try again.",
            )
        ]


# ── GTR Review History ─────────────────────────────────────────────────


@registry.tool("get_grid_review_history", _SCHEMAS_BY_NAME["get_grid_review_history"])
async def _handle_get_grid_review_history(arguments: dict) -> list[types.TextContent]:
    """Handle get_grid_review_history tool call."""
    import json

    grid_name = arguments.get("grid_name", "")
    months_back = min(int(arguments.get("months_back", 6)), 24)

    if not grid_name:
        return [types.TextContent(type="text", text="Error: grid_name is required")]

    try:
        # Resolve grid name to spreadsheet ID via expert config
        from orchestrator.services.expert_instructions_provider import ExpertInstructionsProvider

        provider = ExpertInstructionsProvider()
        gtr_config = await provider.get_expert_config("grids_technical_reviewer")
        if not gtr_config:
            return [types.TextContent(type="text", text="GTR expert config not found")]

        from orchestrator.experts.handlers.grids_technical_reviewer.resolve_grid_sheets import (
            extract_grid_sheet_mappings,
        )

        raw_sections = gtr_config.raw_sections or {}
        sheets_text = raw_sections.get("grid_sheets", "") or raw_sections.get("grid sheets", "")
        if not sheets_text:
            sheets_text = gtr_config.system_instructions or ""

        sheet_mappings = extract_grid_sheet_mappings(sheets_text)

        # Fuzzy match grid name
        matched_key = grid_name.lower()
        if matched_key not in sheet_mappings:
            from shared.utils.grid_matcher import find_best_grid_match

            available = [v["name"] for v in sheet_mappings.values()]
            matched, _, _ = find_best_grid_match(grid_name, available, threshold=80)
            if matched:
                matched_key = matched.lower()

        if matched_key not in sheet_mappings:
            available = [v["name"] for v in sheet_mappings.values()]
            return [
                types.TextContent(
                    type="text",
                    text=f"Grid '{grid_name}' not found in GTR sheets. "
                    f"Available: {', '.join(available[:10])}",
                )
            ]

        grid_info = sheet_mappings[matched_key]

        # Load review history using shared reader
        from shared.utils.gtr_sheet_reader import load_grid_review_history

        history_md = await load_grid_review_history(
            grids=[{"name": grid_info["name"], "spreadsheet_id": grid_info["spreadsheet_id"]}],
            months_back=months_back,
        )

        result = json.dumps(
            {"grid_name": grid_info["name"], "months_back": months_back, "reviews": history_md},
            default=str,
        )
        return [types.TextContent(type="text", text=result)]

    except Exception as e:
        logger.error(f"GTR review history failed: {e}")
        return [types.TextContent(type="text", text="Review history is temporarily unavailable")]


@registry.tool("scan_doc_comments", _SCHEMAS_BY_NAME["scan_doc_comments"])
async def _handle_scan_doc_comments(arguments: dict) -> list[types.TextContent]:
    """Scan a Google Doc for pending @anansibot comments."""
    doc_id = arguments.get("document_id", "").strip()
    if not doc_id:
        return [types.TextContent(type="text", text="Error: document_id is required")]

    user_email = arguments.get("user_email")

    try:
        from shared.utils.drive_permissions import user_can_access

        if not await user_can_access(doc_id, user_email, need_write=False):
            return [
                types.TextContent(
                    type="text",
                    text="You don't have permission to access this file. "
                    "Please ask the file owner to share it with you.",
                )
            ]
    except Exception as e:
        logger.error(f"Permission check failed for scan_doc_comments: {e}")
        return [types.TextContent(type="text", text="Permission check failed. Please try again.")]

    try:
        from shared.utils.doc_editing import scan_comments

        comments = await scan_comments(doc_id)

        if not comments:
            return [types.TextContent(type="text", text="No pending @anansibot comments found.")]

        return [types.TextContent(type="text", text=json.dumps(comments, default=str))]

    except Exception as e:
        logger.error(f"scan_doc_comments failed: {e}")
        return [types.TextContent(type="text", text="Could not scan comments. Please try again.")]


@registry.tool("edit_doc_section", _SCHEMAS_BY_NAME["edit_doc_section"])
async def _handle_edit_doc_section(arguments: dict) -> list[types.TextContent]:
    """Edit a section of a Google Doc with formatted markdown."""
    doc_id = arguments.get("document_id", "").strip()
    if not doc_id:
        return [types.TextContent(type="text", text="Error: document_id is required")]

    user_email = arguments.get("user_email")
    comment_id = arguments.get("comment_id")
    instruction = arguments.get("instruction", "")
    section_text = arguments.get("section_text", "")
    replacement_markdown = arguments.get("replacement_markdown", "")

    # Permission check — require write access
    try:
        from shared.utils.drive_permissions import user_can_access

        if not await user_can_access(doc_id, user_email, need_write=True):
            return [
                types.TextContent(
                    type="text",
                    text="You don't have permission to edit this file. "
                    "Please ask the file owner to share it with you.",
                )
            ]
    except Exception as e:
        logger.error(f"Permission check failed for edit_doc_section: {e}")
        return [types.TextContent(type="text", text="Permission check failed. Please try again.")]

    logger.info(
        f"edit_doc_section called: doc_id={doc_id}, comment_id={comment_id}, "
        f"instruction={instruction[:60]!r}, section_text={section_text[:60]!r}, "
        f"has_replacement={'yes' if replacement_markdown else 'no'}"
    )

    # Resolve target text from comment_id or section_text
    target_text = section_text
    if comment_id and not target_text:
        try:
            from shared.utils.doc_editing import get_comment_by_id

            comment_info = await get_comment_by_id(doc_id, comment_id)
            if not comment_info:
                return [
                    types.TextContent(
                        type="text", text=f"Comment {comment_id} not found in document."
                    )
                ]
            target_text = comment_info.get("highlighted_text", "")
            if not instruction:
                instruction = comment_info.get("instruction", "")
        except Exception as e:
            logger.error(f"Failed to fetch comment {comment_id}: {e}")
            return [types.TextContent(type="text", text="Could not fetch comment details.")]

    if not target_text:
        logger.warning(
            f"edit_doc_section: no target_text resolved. comment_id={comment_id}, "
            f"section_text={section_text[:60]!r}"
        )
        return [
            types.TextContent(
                type="text",
                text="Error: either comment_id or section_text is required to identify the section to edit.",
            )
        ]

    logger.info(f"edit_doc_section: resolved target_text={target_text[:80]!r}")

    # Generate replacement markdown if not provided
    if not replacement_markdown:
        if not instruction:
            return [
                types.TextContent(
                    type="text",
                    text="Error: either replacement_markdown or instruction is required.",
                )
            ]
        try:
            from shared.utils.doc_editing import generate_replacement_markdown

            replacement_markdown = await generate_replacement_markdown(
                instruction, target_text, user_email=user_email
            )
        except Exception as e:
            logger.error(f"Failed to generate replacement: {e}")
            return [types.TextContent(type="text", text="Could not generate replacement content.")]

    # Note: verification is bypassed for document edits. The user explicitly
    # requested the edit (via comment or instruction) and confirmed the target
    # document + section. Verification was blocking legitimate edits (e.g.,
    # inserting operational data) because the criteria doc is tuned for
    # outgoing chat messages, not doc edits. See CLAUDE.md for context.

    # Pin revision once before editing, then execute via Apps Script
    try:
        from shared.utils.doc_editing import edit_section, pin_revision

        await pin_revision(doc_id)

        result = await edit_section(
            doc_id=doc_id,
            target_text=target_text,
            replacement_markdown=replacement_markdown,
            comment_id=comment_id,
        )

        if result.get("success"):
            return [
                types.TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "success": True,
                            "elements_written": result.get("elements_written", 0),
                            "message": "Section edited successfully with formatted content.",
                        }
                    ),
                )
            ]
        else:
            return [types.TextContent(type="text", text=result.get("error", "Edit failed."))]

    except Exception as e:
        logger.error(f"edit_doc_section failed: {e}")
        return [
            types.TextContent(
                type="text",
                text="Could not write formatted content to the document. "
                "Please try again or edit the document manually.",
            )
        ]


# ── Agentic graph tools (P4 Phase 3) ────────────────────────────────────────
#
# Agency stays with the main LLM: these are plain read-only query tools, not
# a second orchestration loop. get_graph_schema/search_entities/
# get_entity_neighbors/get_entity_evidence call the permission-filtered RPCs
# from db/migrations/0018 (corrected 0020) and 0023, and every result is
# scoped to the caller's own organization -- see org_ids_for_request below
# for exactly how that caller identity reaches this server at all.


def org_ids_for_request(organization_id: Optional[int]) -> Optional[List[int]]:
    """Org filter for the graph RPCs, from the caller's own organization_id.

    organization_id is not a tool argument the model supplies -- ToolExecutor
    (chat_orchestrator/orchestrator/services/tool_executor.py's
    _execute_direct_tool/_execute_bridge_tool) injects it into every MCP
    call's arguments from the webhook request, never LLM-controllable, the
    same way it already does for customer_server's handlers. It is a single
    int, not a list and not an is_staff flag -- a caller has exactly one
    organization (or is staff, resolved to STAFF_ORG_ID the same way
    servers/customer_server/client_base.py's STAFF_ORG_ID already is).

    None means unrestricted (staff). Raises rather than returning None for
    anything else falsy: every graph RPC reads NULL as "show everything", so
    reaching it by accident (a request with no recognized organization_id at
    all) would hand the whole graph to someone entitled to none of it.
    """
    if organization_id == STAFF_ORG_ID:
        return None
    if organization_id is None:
        raise ValueError(
            "cannot scope a graph query: no organization_id on this request"
        )
    return [organization_id]


def suggest_near_matches(query: str, permitted_names: List[str]) -> str:
    """A 'did you mean' line built ONLY from names the caller may see.

    Suggesting from the unfiltered entity table would leak the existence of
    entities in other organizations -- the subtle version of the permission
    bug these tools exist to avoid.
    """
    close = difflib.get_close_matches(query, permitted_names, n=3, cutoff=0.5)
    if close:
        return f"No entity matching '{query}'. Closest by name: {', '.join(close)}."
    return (
        f"No entity matching '{query}', and no close names either. "
        f"Call get_graph_schema to see which entity types exist."
    )


def format_entity_results(rows: List[Dict[str, Any]], query: str) -> str:
    """Entity search results, or an actionable message. Never a bare empty list."""
    if not rows:
        return suggest_near_matches(query, [])
    lines = [f"Found {len(rows)} entit{'y' if len(rows) == 1 else 'ies'} matching '{query}':", ""]
    for row in rows:
        description = f" — {row['description']}" if row.get("description") else ""
        lines.append(f"- {row['name']} ({row['type']}) [id: {row['id']}]{description}")
    lines.append("")
    lines.append("Use get_entity_neighbors with an id above to see what it connects to.")
    return "\n".join(lines)


def format_neighbors(rows: List[Dict[str, Any]], entity_id: str) -> str:
    """Neighbour results, or an actionable message."""
    if not rows:
        return (
            f"Entity {entity_id} has no visible connections. It may genuinely have "
            f"none, or the entities on the other side may be outside your access. "
            f"Try get_entity_evidence to see the source passages instead."
        )
    lines = [f"{len(rows)} connection(s):", ""]
    for row in rows:
        description = f" — {row['description']}" if row.get("description") else ""
        lines.append(
            f"- [{row['direction']}] {row['relationship_type']} → "
            f"{row['neighbor_name']} ({row['neighbor_type']}) "
            f"[id: {row['neighbor_id']}]{description}"
        )
    return "\n".join(lines)


def _graph_client():
    """The same CHAT_DB_URL/CHAT_DB_SERVICE_KEY client every handler in this
    file already builds -- returns None, never raises, if unconfigured."""
    if not CHAT_DB_URL or not CHAT_DB_SERVICE_KEY:
        return None
    from supabase import create_client

    return create_client(CHAT_DB_URL, CHAT_DB_SERVICE_KEY)


@registry.tool("get_graph_schema", _SCHEMAS_BY_NAME["get_graph_schema"])
async def _handle_get_graph_schema(arguments: dict) -> list[types.TextContent]:
    """Handle get_graph_schema tool call."""
    from shared.graph_primer import render_primer

    try:
        org_ids = org_ids_for_request(arguments.get("organization_id"))
    except ValueError as e:
        return [types.TextContent(type="text", text=str(e))]

    client = _graph_client()
    if client is None:
        return [types.TextContent(type="text", text="Knowledge graph is not configured.")]

    try:
        result = client.rpc(
            "summarize_entity_graph",
            {"p_org_ids": org_ids, "p_max_types": 20, "p_examples": 3},
        ).execute()
    except Exception as e:
        logger.warning(f"get_graph_schema failed: {e}")
        return [types.TextContent(type="text", text=f"Could not load the graph schema: {e}")]

    text = render_primer(result.data or []) or "The knowledge graph has no visible entities."
    return [types.TextContent(type="text", text=text)]


@registry.tool("search_entities", _SCHEMAS_BY_NAME["search_entities"])
async def _handle_search_entities(arguments: dict) -> list[types.TextContent]:
    """Handle search_entities tool call."""
    query = arguments.get("query", "")
    if not query:
        return [types.TextContent(type="text", text="Error: query is required")]
    entity_type = arguments.get("entity_type")
    limit = arguments.get("limit", 10)

    try:
        org_ids = org_ids_for_request(arguments.get("organization_id"))
    except ValueError as e:
        return [types.TextContent(type="text", text=str(e))]

    client = _graph_client()
    if client is None:
        return [types.TextContent(type="text", text="Knowledge graph is not configured.")]

    try:
        result = client.rpc(
            "search_entities_permitted",
            {"p_query": query, "p_org_ids": org_ids, "p_type": entity_type, "p_limit": limit},
        ).execute()
    except Exception as e:
        logger.warning(f"search_entities failed: {e}")
        return [types.TextContent(type="text", text=f"Entity search failed: {e}")]

    rows = result.data or []
    if not rows:
        # Suggestions come from the caller's own permitted set only -- an
        # unfiltered name search would leak entities outside their access.
        try:
            permitted = client.rpc(
                "search_entities_permitted",
                {"p_query": "", "p_org_ids": org_ids, "p_type": None, "p_limit": 50},
            ).execute()
            permitted_names = [r["name"] for r in (permitted.data or [])]
        except Exception:
            permitted_names = []
        return [types.TextContent(type="text", text=suggest_near_matches(query, permitted_names))]

    return [types.TextContent(type="text", text=format_entity_results(rows, query=query))]


@registry.tool("get_entity_neighbors", _SCHEMAS_BY_NAME["get_entity_neighbors"])
async def _handle_get_entity_neighbors(arguments: dict) -> list[types.TextContent]:
    """Handle get_entity_neighbors tool call."""
    entity_id = arguments.get("entity_id", "")
    if not entity_id:
        return [types.TextContent(type="text", text="Error: entity_id is required")]
    relationship_type = arguments.get("relationship_type")
    limit = arguments.get("limit", 25)

    try:
        org_ids = org_ids_for_request(arguments.get("organization_id"))
    except ValueError as e:
        return [types.TextContent(type="text", text=str(e))]

    client = _graph_client()
    if client is None:
        return [types.TextContent(type="text", text="Knowledge graph is not configured.")]

    try:
        result = client.rpc(
            "get_entity_neighbors_permitted",
            {
                "p_entity_id": entity_id,
                "p_org_ids": org_ids,
                "p_rel_type": relationship_type,
                "p_limit": limit,
            },
        ).execute()
    except Exception as e:
        logger.warning(f"get_entity_neighbors failed: {e}")
        return [types.TextContent(type="text", text=f"Could not load neighbors: {e}")]

    return [
        types.TextContent(
            type="text", text=format_neighbors(result.data or [], entity_id=entity_id)
        )
    ]


@registry.tool("get_entity_evidence", _SCHEMAS_BY_NAME["get_entity_evidence"])
async def _handle_get_entity_evidence(arguments: dict) -> list[types.TextContent]:
    """Handle get_entity_evidence tool call."""
    entity_id = arguments.get("entity_id", "")
    if not entity_id:
        return [types.TextContent(type="text", text="Error: entity_id is required")]
    limit = arguments.get("limit", 5)

    try:
        org_ids = org_ids_for_request(arguments.get("organization_id"))
    except ValueError as e:
        return [types.TextContent(type="text", text=str(e))]

    client = _graph_client()
    if client is None:
        return [types.TextContent(type="text", text="Knowledge graph is not configured.")]

    try:
        result = client.rpc(
            "get_entity_evidence_permitted",
            {"p_entity_id": entity_id, "p_org_ids": org_ids, "p_limit": limit},
        ).execute()
    except Exception as e:
        logger.warning(f"get_entity_evidence failed: {e}")
        return [types.TextContent(type="text", text=f"Could not load evidence: {e}")]

    rows = result.data or []
    if not rows:
        return [
            types.TextContent(
                type="text",
                text=f"No visible source passages for entity {entity_id}.",
            )
        ]
    lines = [f"{len(rows)} source passage(s) for entity {entity_id}:", ""]
    for row in rows:
        lines.append(f"- {row.get('document_title', 'Untitled')}: {row.get('excerpt', '')}")
    return [types.TextContent(type="text", text="\n".join(lines))]


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
