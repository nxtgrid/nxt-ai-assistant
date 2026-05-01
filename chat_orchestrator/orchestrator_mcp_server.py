#!/usr/bin/env python3
"""
Anansi Orchestrator MCP Server for Local Claude Desktop Testing

This MCP server wraps Anansi's RAG and instructions functionality for local testing
with Claude Desktop. In production, these services are called directly by the
Anansi handler. In development, Claude Desktop can call them via MCP.

This server provides:
1. RAG retrieval with permission filtering
2. Instructions retrieval based on user context
3. System prompt composition

Environment Variables Required:
- SUPABASE_URL: Main Supabase instance
- SUPABASE_KEY: Service key for main Supabase
- AUTH_SUPABASE_URL: Auth Supabase instance (read-only)
- AUTH_SUPABASE_KEY: Service key for auth Supabase
- OPENAI_API_KEY: For embeddings generation
"""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions

# Add orchestrator to path
orchestrator_path = Path(__file__).parent
sys.path.insert(0, str(orchestrator_path))

from orchestrator.models.schemas import EntityContext, UserContext
from orchestrator.services.instructions_provider import InstructionsProvider
from orchestrator.services.rag_provider import RAGProvider

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("orchestrator-mcp-server")

logger.info("🚀 Chat Orchestrator MCP Server starting...")
logger.info(f"📍 Python path: {sys.path}")
logger.info(f"📂 Working directory: {os.getcwd()}")

server = Server("chat-orchestrator")


@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """
    List available Anansi orchestrator tools.
    """
    return [
        types.Tool(
            name="retrieve_rag_context",
            description="Retrieve relevant context from RAG database with permission filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query for RAG retrieval",
                    },
                    "user_email": {
                        "type": "string",
                        "description": "User email for permission filtering",
                    },
                    "limit": {
                        "type": "number",
                        "description": "Maximum number of documents to retrieve",
                        "default": 5,
                    },
                    "source_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Filter by source types (codebase, docs, jira, etc.)",
                    },
                },
                "required": ["query", "user_email"],
            },
        ),
        types.Tool(
            name="get_system_instructions",
            description="Get system instructions based on user context and entity",
            inputSchema={
                "type": "object",
                "properties": {
                    "user_email": {
                        "type": "string",
                        "description": "User email",
                    },
                    "user_roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "User roles (admin, developer, support, etc.)",
                    },
                    "entity_context": {
                        "type": "object",
                        "description": "Entity context (customer_id, grid_id, meter_id, etc.)",
                        "properties": {
                            "customer_id": {"type": "string"},
                            "grid_id": {"type": "string"},
                            "meter_id": {"type": "string"},
                            "site_id": {"type": "string"},
                        },
                    },
                    "task_type": {
                        "type": "string",
                        "description": "Task type (analysis, reporting, troubleshooting, etc.)",
                    },
                },
                "required": ["user_email"],
            },
        ),
        types.Tool(
            name="compose_prompt",
            description="Compose complete prompt with instructions and RAG context",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "User query",
                    },
                    "user_email": {
                        "type": "string",
                        "description": "User email",
                    },
                    "user_roles": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "User roles",
                    },
                    "entity_context": {
                        "type": "object",
                        "description": "Entity context",
                    },
                    "include_rag": {
                        "type": "boolean",
                        "description": "Whether to include RAG context",
                        "default": True,
                    },
                },
                "required": ["query", "user_email"],
            },
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    """Handle tool execution."""
    if arguments is None:
        arguments = {}

    if name == "retrieve_rag_context":
        return await _retrieve_rag_context(arguments)
    elif name == "get_system_instructions":
        return await _get_system_instructions(arguments)
    elif name == "compose_prompt":
        return await _compose_prompt(arguments)
    else:
        return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def _retrieve_rag_context(arguments: Dict[str, Any]) -> list[types.TextContent]:
    """Retrieve RAG context with permission filtering."""
    query = arguments.get("query")
    user_email = arguments.get("user_email")
    limit = arguments.get("limit", 5)
    source_types = arguments.get("source_types")

    if not query or not user_email:
        return [types.TextContent(type="text", text="ERROR: query and user_email are required")]

    try:
        rag_provider = RAGProvider()
        documents = await rag_provider.retrieve(
            query=query,
            user_email=user_email,
            limit=limit,
            source_types=source_types,
        )

        if not documents:
            return [
                types.TextContent(
                    type="text",
                    text=f"No RAG documents found for query: {query}\n"
                    f"User {user_email} may not have access to relevant documents.",
                )
            ]

        # Format documents
        formatted = f"Retrieved {len(documents)} documents for: {query}\n\n"
        for i, doc in enumerate(documents, 1):
            formatted += f"--- Document {i} ({doc.source_type}) ---\n"
            formatted += f"Title: {doc.title}\n"
            if doc.url:
                formatted += f"URL: {doc.url}\n"
            formatted += f"Score: {doc.score:.3f}\n\n"
            formatted += f"{doc.content}\n\n"

        return [types.TextContent(type="text", text=formatted)]

    except Exception as e:
        logger.exception(f"Error retrieving RAG context: {e}")
        return [
            types.TextContent(type="text", text=f"ERROR: Failed to retrieve RAG context: {str(e)}")
        ]


async def _get_system_instructions(arguments: Dict[str, Any]) -> list[types.TextContent]:
    """Get system instructions based on context."""
    user_email = arguments.get("user_email")
    user_roles = arguments.get("user_roles", [])
    entity_context_data = arguments.get("entity_context")
    task_type = arguments.get("task_type")

    if not user_email:
        return [types.TextContent(type="text", text="ERROR: user_email is required")]

    try:
        # Build user context
        user_context = UserContext(
            user_id=user_email,
            user_email=user_email,
            source="api",
            roles=user_roles,
        )

        # Build entity context if provided
        entity_context = None
        if entity_context_data:
            entity_context = EntityContext(**entity_context_data)

        # Get instructions
        instructions_provider = InstructionsProvider()
        instructions = await instructions_provider.get_instructions(
            user_context=user_context,
            entity_context=entity_context,
            task_type=task_type,
        )

        return [
            types.TextContent(
                type="text",
                text=f"System Instructions for {user_email}:\n\n{instructions}",
            )
        ]

    except Exception as e:
        logger.exception(f"Error getting instructions: {e}")
        return [types.TextContent(type="text", text=f"ERROR: Failed to get instructions: {str(e)}")]


async def _compose_prompt(arguments: Dict[str, Any]) -> list[types.TextContent]:
    """Compose complete prompt with instructions and RAG."""
    query = arguments.get("query")
    user_email = arguments.get("user_email")
    user_roles = arguments.get("user_roles", [])
    entity_context_data = arguments.get("entity_context")
    include_rag = arguments.get("include_rag", True)

    if not query or not user_email:
        return [types.TextContent(type="text", text="ERROR: query and user_email are required")]

    try:
        # Build contexts
        user_context = UserContext(
            user_id=user_email,
            user_email=user_email,
            source="api",
            roles=user_roles,
        )

        entity_context = None
        if entity_context_data:
            entity_context = EntityContext(**entity_context_data)

        # Get instructions
        instructions_provider = InstructionsProvider()
        instructions = await instructions_provider.get_instructions(
            user_context=user_context, entity_context=entity_context
        )

        # Get RAG context if requested
        rag_context = ""
        if include_rag:
            rag_provider = RAGProvider()
            rag_snippets = await rag_provider.retrieve_as_text(
                query=query, user_email=user_email, limit=3
            )
            if rag_snippets:
                rag_context = "\n\n# Retrieved Context\n\n" + "\n\n".join(rag_snippets)

        # Compose final prompt
        composed = f"{instructions}\n\n{rag_context}\n\n# User Query\n\n{query}"

        return [types.TextContent(type="text", text=composed)]

    except Exception as e:
        logger.exception(f"Error composing prompt: {e}")
        return [types.TextContent(type="text", text=f"ERROR: Failed to compose prompt: {str(e)}")]


async def main():
    """Main entry point for the MCP server."""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="anansi-orchestrator",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=types.NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())
