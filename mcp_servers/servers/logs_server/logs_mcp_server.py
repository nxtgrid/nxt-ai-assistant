#!/usr/bin/env python3
"""
Logs MCP Server - Intelligent log analysis using Loki and vector search

NOTE: This server provides access to SOFTWARE/APPLICATION logs from the Anansi backend
services (API servers, MCP tools, etc.). It does NOT provide equipment logs, inverter
logs, or grid operational data. For equipment diagnostics, use the equipment_diagnostics
server or VRM tools instead.

This server provides tools to:
1. Fetch logs from Loki for the past 12 hours (or configurable timeframe)
2. Store logs in a vector database with embeddings for semantic search
3. Chunk logs intelligently and provide relevant context to LLMs
4. Search logs semantically (not just keyword matching)
5. Analyze error patterns and anomalies

The server uses Grafana Loki for log retrieval and ChromaDB for vector storage.
"""

import asyncio
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions

from shared.utils.logging import get_logger

# Load environment variables
load_dotenv()

logger = get_logger("logs-server")

# Startup message
print("🚀 Logs MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("logs-server")

# Configuration
LOKI_URL = os.getenv("LOKI_URL", "http://localhost:3100")
LOKI_USERNAME = os.getenv("LOKI_USERNAME", "")
LOKI_PASSWORD = os.getenv("LOKI_PASSWORD", "")
VECTOR_DB_PATH = os.getenv("VECTOR_DB_PATH", "./data/logs_vectordb")
DEFAULT_LOG_HOURS = int(os.getenv("DEFAULT_LOG_HOURS", "12"))


class LogEntry:
    """Represents a single log entry"""

    def __init__(self, timestamp: str, message: str, labels: Dict[str, str], level: str = "info"):
        self.timestamp = timestamp
        self.message = message
        self.labels = labels
        self.level = level
        self.embedding = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "message": self.message,
            "labels": self.labels,
            "level": self.level,
        }

    def get_id(self) -> str:
        """Generate unique ID for this log entry"""
        content = f"{self.timestamp}_{self.message}_{json.dumps(self.labels, sort_keys=True)}"
        return hashlib.md5(content.encode()).hexdigest()


class LokiClient:
    """Client for interacting with Grafana Loki"""

    def __init__(self, url: str, username: str = "", password: str = ""):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password

    async def query_range(
        self, query: str, start_time: datetime, end_time: datetime, limit: int = 5000
    ) -> List[LogEntry]:
        """
        Query Loki for logs in a time range.

        Args:
            query: LogQL query string (e.g., '{job="mcp-servers"}')
            start_time: Start of time range
            end_time: End of time range
            limit: Maximum number of log entries

        Returns:
            List of LogEntry objects
        """
        try:
            import aiohttp

            # Convert times to nanoseconds (Loki format)
            start_ns = int(start_time.timestamp() * 1e9)
            end_ns = int(end_time.timestamp() * 1e9)

            url = f"{self.url}/loki/api/v1/query_range"
            params = {
                "query": query,
                "start": start_ns,
                "end": end_ns,
                "limit": limit,
                "direction": "backward",  # Most recent first
            }

            auth = None
            if self.username and self.password:
                auth = aiohttp.BasicAuth(self.username, self.password)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url,
                    params=params,
                    auth=auth,
                    timeout=aiohttp.ClientTimeout(total=30),  # type: ignore[arg-type]
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        raise Exception(f"Loki query failed: {response.status} - {error_text}")

                    data = await response.json()

            # Parse Loki response
            log_entries = []
            for stream in data.get("data", {}).get("result", []):
                labels = stream.get("stream", {})
                values = stream.get("values", [])

                for value in values:
                    timestamp_ns, message = value
                    timestamp = datetime.fromtimestamp(int(timestamp_ns) / 1e9).isoformat()

                    # Extract log level from message if present
                    level = self._extract_log_level(message)

                    log_entries.append(
                        LogEntry(timestamp=timestamp, message=message, labels=labels, level=level)
                    )

            return log_entries

        except Exception as e:
            logger.error(f"Error querying Loki: {str(e)}")
            raise

    def _extract_log_level(self, message: str) -> str:
        """Extract log level from message"""
        message_lower = message.lower()
        if "error" in message_lower or "err" in message_lower:
            return "error"
        elif "warn" in message_lower:
            return "warning"
        elif "info" in message_lower:
            return "info"
        elif "debug" in message_lower:
            return "debug"
        return "info"

    async def get_label_values(self, label: str) -> List[str]:
        """Get all values for a specific label"""
        try:
            import aiohttp

            url = f"{self.url}/loki/api/v1/label/{label}/values"

            auth = None
            if self.username and self.password:
                auth = aiohttp.BasicAuth(self.username, self.password)

            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, auth=auth, timeout=aiohttp.ClientTimeout(total=10)
                ) as response:
                    if response.status != 200:
                        return []

                    data = await response.json()
                    return list(data.get("data", []))

        except Exception as e:
            logger.error(f"Error getting label values: {str(e)}")
            return []


class VectorLogStore:
    """Vector database for storing and searching logs"""

    def __init__(self, persist_directory: str):
        self.persist_directory = persist_directory
        self.collection = None
        self._initialized = False

    async def initialize(self):
        """Initialize ChromaDB"""
        if self._initialized:
            return

        try:
            import chromadb
            from chromadb.config import Settings

            # Create directory if it doesn't exist
            Path(self.persist_directory).mkdir(parents=True, exist_ok=True)

            # Initialize ChromaDB client
            self.client = chromadb.PersistentClient(
                path=self.persist_directory, settings=Settings(anonymized_telemetry=False)
            )

            # Get or create collection
            self.collection = self.client.get_or_create_collection(
                name="logs", metadata={"description": "Application logs with embeddings"}
            )

            self._initialized = True
            logger.info(f"Vector store initialized at {self.persist_directory}")

        except ImportError:
            logger.warning("ChromaDB not available. Vector search features will be limited.")
            self._initialized = False
        except Exception as e:
            logger.error(f"Error initializing vector store: {str(e)}")
            self._initialized = False

    async def add_logs(self, log_entries: List[LogEntry]):
        """Add log entries to vector store"""
        if not self._initialized or not self.collection:
            logger.warning("Vector store not initialized. Skipping log storage.")
            return

        try:
            # Prepare data for ChromaDB
            ids = []
            documents = []
            metadatas = []

            for entry in log_entries:
                log_id = entry.get_id()

                # Create searchable document from log
                doc = f"{entry.timestamp} [{entry.level.upper()}] {entry.message}"

                # Metadata for filtering
                metadata = {
                    "timestamp": entry.timestamp,
                    "level": entry.level,
                    **{f"label_{k}": v for k, v in entry.labels.items()},
                }

                ids.append(log_id)
                documents.append(doc)
                metadatas.append(metadata)

            # Add to ChromaDB (automatically generates embeddings)
            if ids:
                self.collection.add(ids=ids, documents=documents, metadatas=metadatas)

                logger.info(f"Added {len(ids)} log entries to vector store")

        except Exception as e:
            logger.error(f"Error adding logs to vector store: {str(e)}")

    async def search_logs(
        self,
        query: str,
        n_results: int = 20,
        level_filter: Optional[str] = None,
        time_filter: Optional[Dict[str, str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Search logs using semantic similarity.

        Args:
            query: Natural language search query
            n_results: Number of results to return
            level_filter: Filter by log level (error, warning, info, debug)
            time_filter: Filter by time range

        Returns:
            List of matching log entries with similarity scores
        """
        if not self._initialized or not self.collection:
            return []

        try:
            # Build where filter
            where_filter = {}
            if level_filter:
                where_filter["level"] = level_filter

            # Query vector store
            results = self.collection.query(
                query_texts=[query],
                n_results=n_results,
                where=where_filter if where_filter else None,
            )

            # Format results
            formatted_results = []
            if results["ids"] and len(results["ids"]) > 0:
                for i, doc_id in enumerate(results["ids"][0]):
                    formatted_results.append(
                        {
                            "id": doc_id,
                            "message": results["documents"][0][i],
                            "metadata": results["metadatas"][0][i],
                            "similarity_score": (
                                1 - results["distances"][0][i] if "distances" in results else None
                            ),
                        }
                    )

            return formatted_results

        except Exception as e:
            logger.error(f"Error searching logs: {str(e)}")
            return []

    async def get_log_stats(self) -> Dict[str, Any]:
        """Get statistics about stored logs"""
        if not self._initialized or not self.collection:
            return {"error": "Vector store not initialized"}

        try:
            count = self.collection.count()

            # Get sample to analyze
            sample = self.collection.get(limit=100)

            # Count by level
            level_counts = {}
            if sample and "metadatas" in sample:
                for metadata in sample["metadatas"]:
                    level = metadata.get("level", "unknown")
                    level_counts[level] = level_counts.get(level, 0) + 1

            return {
                "total_logs": count,
                "level_distribution": level_counts,
                "collection_name": self.collection.name,
            }

        except Exception as e:
            logger.error(f"Error getting log stats: {str(e)}")
            return {"error": str(e)}


class LogChunker:
    """Intelligently chunks logs for LLM consumption"""

    @staticmethod
    def chunk_logs(log_entries: List[LogEntry], max_chunk_size: int = 10) -> List[List[LogEntry]]:
        """
        Chunk logs intelligently, grouping related logs together.

        Args:
            log_entries: List of log entries
            max_chunk_size: Maximum logs per chunk

        Returns:
            List of log chunks
        """
        chunks = []
        current_chunk = []

        for entry in log_entries:
            current_chunk.append(entry)

            # Create new chunk if max size reached or error encountered
            if len(current_chunk) >= max_chunk_size or entry.level == "error":
                chunks.append(current_chunk)
                current_chunk = []

        # Add remaining logs
        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    @staticmethod
    def format_chunk_for_llm(chunk: List[LogEntry], include_context: bool = True) -> str:
        """
        Format a chunk of logs for LLM consumption.

        Args:
            chunk: List of log entries
            include_context: Include contextual information

        Returns:
            Formatted string
        """
        output = []

        if include_context:
            # Add summary header
            error_count = sum(1 for e in chunk if e.level == "error")
            warning_count = sum(1 for e in chunk if e.level == "warning")

            output.append("=== Log Chunk Summary ===")
            output.append(f"Total logs: {len(chunk)}")
            output.append(f"Errors: {error_count}, Warnings: {warning_count}")
            output.append(f"Time range: {chunk[0].timestamp} to {chunk[-1].timestamp}")
            output.append("")

        # Add logs
        for entry in chunk:
            level_marker = {"error": "❌", "warning": "⚠️", "info": "ℹ️", "debug": "🔍"}.get(
                entry.level, "•"
            )

            output.append(f"{level_marker} [{entry.timestamp}] {entry.message}")

        return "\n".join(output)


# Global instances
loki_client = LokiClient(LOKI_URL, LOKI_USERNAME, LOKI_PASSWORD)
vector_store = VectorLogStore(VECTOR_DB_PATH)
log_chunker = LogChunker()


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available log analysis tools"""

    tools = [
        types.Tool(
            name="fetch_logs_from_loki",
            description="Fetch SOFTWARE/APPLICATION logs (NOT equipment logs) from Grafana Loki for a specified time range. These are backend service logs (API servers, MCP tools, etc.), not inverter or grid operational logs. Default fetches last 12 hours. Use this to debug software issues, not equipment problems.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": 'LogQL query string (e.g., \'{job="mcp-servers"}\', \'{app="api", level="error"}\'). Use Loki label syntax.',
                        "default": '{job=~".+"}',
                    },
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to look back (default: 12)",
                        "default": 12,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of log entries to fetch (default: 5000)",
                        "default": 5000,
                    },
                    "store_in_vector_db": {
                        "type": "boolean",
                        "description": "Whether to store logs in vector database for semantic search (default: true)",
                        "default": True,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="search_logs_semantic",
            description="Search stored logs using natural language/semantic search powered by vector embeddings. This finds logs that are conceptually related to your query, not just keyword matches. Use this to find relevant logs even when you don't know exact error messages or keywords. Example: 'database connection problems' will find logs about DB timeouts, connection refused, etc.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query (e.g., 'payment processing errors', 'authentication failures', 'slow database queries')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 20)",
                        "default": 20,
                    },
                    "level_filter": {
                        "type": "string",
                        "description": "Filter by log level (error, warning, info, debug)",
                        "enum": ["error", "warning", "info", "debug"],
                    },
                    "chunk_for_llm": {
                        "type": "boolean",
                        "description": "Whether to chunk and format results for LLM consumption (default: true)",
                        "default": True,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_error_logs",
            description="Get all error-level logs from the vector database, chunked and formatted for analysis. Use this to quickly identify all errors in the recent logs. Results are intelligently chunked and formatted for LLM analysis with context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of error logs to return (default: 50)",
                        "default": 50,
                    },
                    "chunk_size": {
                        "type": "integer",
                        "description": "Number of logs per chunk (default: 10)",
                        "default": 10,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="analyze_log_patterns",
            description="Analyze logs to identify patterns, frequent errors, and anomalies. Groups similar log messages together and provides statistics. Use this to understand overall system health and identify recurring issues.",
            inputSchema={
                "type": "object",
                "properties": {
                    "hours": {
                        "type": "integer",
                        "description": "Number of hours to analyze (default: 12)",
                        "default": 12,
                    },
                    "min_occurrences": {
                        "type": "integer",
                        "description": "Minimum occurrences to consider a pattern (default: 3)",
                        "default": 3,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_log_statistics",
            description="Get statistics about stored logs including total count, distribution by log level, and time range. Use this to understand the overall state of the log database.",
            inputSchema={"type": "object", "properties": {}, "required": []},
            visible_to_customer=False,
        ),
        types.Tool(
            name="get_logs_by_timeframe",
            description="Get logs for a specific timeframe, chunked and formatted for LLM analysis. Fetches directly from Loki and formats for easy consumption. Use this when you need logs from a specific time period.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_time": {
                        "type": "string",
                        "description": "Start time in ISO format (e.g., '2024-01-15T10:00:00')",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End time in ISO format (e.g., '2024-01-15T12:00:00')",
                    },
                    "query": {
                        "type": "string",
                        "description": "LogQL query (default: all logs)",
                        "default": '{job=~".+"}',
                    },
                    "chunk_size": {
                        "type": "integer",
                        "description": "Logs per chunk (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["start_time", "end_time"],
            },
        ),
        types.Tool(
            name="get_loki_labels",
            description="Get available Loki labels (e.g., job, app, level) and their values. Use this to understand what labels are available for filtering logs.",
            inputSchema={
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Specific label to get values for (e.g., 'job', 'app', 'level')",
                    }
                },
                "required": ["label"],
            },
        ),
    ]

    logger.info(f"Logs server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls"""
    try:
        # Initialize vector store if needed
        await vector_store.initialize()

        if name == "fetch_logs_from_loki":
            result = await fetch_logs_from_loki(
                query=arguments.get("query", '{job=~".+"}'),
                hours=arguments.get("hours", DEFAULT_LOG_HOURS),
                limit=arguments.get("limit", 5000),
                store_in_vector_db=arguments.get("store_in_vector_db", True),
            )

        elif name == "search_logs_semantic":
            result = await search_logs_semantic(
                query=arguments["query"],
                max_results=arguments.get("max_results", 20),
                level_filter=arguments.get("level_filter"),
                chunk_for_llm=arguments.get("chunk_for_llm", True),
            )

        elif name == "get_error_logs":
            result = await get_error_logs(
                max_results=arguments.get("max_results", 50),
                chunk_size=arguments.get("chunk_size", 10),
            )

        elif name == "analyze_log_patterns":
            result = await analyze_log_patterns(
                hours=arguments.get("hours", DEFAULT_LOG_HOURS),
                min_occurrences=arguments.get("min_occurrences", 3),
            )

        elif name == "get_log_statistics":
            result = await vector_store.get_log_stats()

        elif name == "get_logs_by_timeframe":
            result = await get_logs_by_timeframe(
                start_time=arguments["start_time"],
                end_time=arguments["end_time"],
                query=arguments.get("query", '{job=~".+"}'),
                chunk_size=arguments.get("chunk_size", 10),
            )

        elif name == "get_loki_labels":
            values = await loki_client.get_label_values(arguments["label"])
            result = {"label": arguments["label"], "values": values, "total_values": len(values)}

        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.error(f"Error in {name}: {str(e)}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


async def fetch_logs_from_loki(
    query: str, hours: int, limit: int, store_in_vector_db: bool
) -> Dict[str, Any]:
    """Fetch logs from Loki and optionally store in vector DB"""
    try:
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)

        logger.info(f"Fetching logs from Loki: {query} (last {hours} hours)")
        log_entries = await loki_client.query_range(query, start_time, end_time, limit)

        # Store in vector DB if requested
        if store_in_vector_db and log_entries:
            await vector_store.add_logs(log_entries)

        return {
            "status": "success",
            "total_logs_fetched": len(log_entries),
            "time_range": {"start": start_time.isoformat(), "end": end_time.isoformat()},
            "query": query,
            "stored_in_vector_db": store_in_vector_db,
            "sample_logs": [entry.to_dict() for entry in log_entries[:5]],
        }

    except Exception as e:
        return {"error": str(e)}


async def search_logs_semantic(
    query: str, max_results: int, level_filter: Optional[str], chunk_for_llm: bool
) -> Dict[str, Any]:
    """Search logs semantically"""
    try:
        results = await vector_store.search_logs(
            query=query, n_results=max_results, level_filter=level_filter
        )

        if chunk_for_llm and results:
            # Convert to LogEntry objects for chunking
            log_entries = []
            for r in results:
                metadata = r["metadata"]
                log_entries.append(
                    LogEntry(
                        timestamp=metadata.get("timestamp", ""),
                        message=r["message"],
                        labels={
                            k.replace("label_", ""): v
                            for k, v in metadata.items()
                            if k.startswith("label_")
                        },
                        level=metadata.get("level", "info"),
                    )
                )

            # Chunk logs
            chunks = log_chunker.chunk_logs(log_entries, max_chunk_size=10)
            formatted_chunks = [log_chunker.format_chunk_for_llm(chunk) for chunk in chunks]

            return {
                "query": query,
                "total_results": len(results),
                "chunks": formatted_chunks,
                "chunk_count": len(formatted_chunks),
            }
        else:
            return {"query": query, "total_results": len(results), "results": results}

    except Exception as e:
        return {"error": str(e)}


async def get_error_logs(max_results: int, chunk_size: int) -> Dict[str, Any]:
    """Get error logs chunked for LLM"""
    try:
        results = await vector_store.search_logs(
            query="error exception failure", n_results=max_results, level_filter="error"
        )

        if not results:
            return {"status": "no_errors", "message": "No error logs found in the vector database"}

        # Convert to LogEntry objects
        log_entries = []
        for r in results:
            metadata = r["metadata"]
            log_entries.append(
                LogEntry(
                    timestamp=metadata.get("timestamp", ""),
                    message=r["message"],
                    labels={
                        k.replace("label_", ""): v
                        for k, v in metadata.items()
                        if k.startswith("label_")
                    },
                    level=metadata.get("level", "error"),
                )
            )

        # Chunk logs
        chunks = log_chunker.chunk_logs(log_entries, max_chunk_size=chunk_size)
        formatted_chunks = [log_chunker.format_chunk_for_llm(chunk) for chunk in chunks]

        return {
            "total_errors": len(log_entries),
            "chunks": formatted_chunks,
            "chunk_count": len(formatted_chunks),
        }

    except Exception as e:
        return {"error": str(e)}


async def analyze_log_patterns(hours: int, min_occurrences: int) -> Dict[str, Any]:
    """Analyze log patterns"""
    try:
        # Fetch recent logs
        end_time = datetime.now()
        start_time = end_time - timedelta(hours=hours)

        log_entries = await loki_client.query_range('{job=~".+"}', start_time, end_time, 5000)

        if not log_entries:
            return {"status": "no_logs", "message": "No logs found for analysis"}

        # Group by message pattern (simplified)
        patterns = {}
        for entry in log_entries:
            # Extract pattern (remove timestamps, IDs, etc.)
            pattern = re.sub(r"\d{4}-\d{2}-\d{2}", "DATE", entry.message)
            pattern = re.sub(r"\d+", "NUM", pattern)
            pattern = re.sub(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "UUID", pattern
            )

            if pattern not in patterns:
                patterns[pattern] = {"count": 0, "levels": {}, "sample": entry.message}

            patterns[pattern]["count"] = int(patterns[pattern]["count"]) + 1  # type: ignore[operator, call-overload]
            level = entry.level
            level_count = int(patterns[pattern]["levels"].get(level, 0))  # type: ignore[arg-type, attr-defined, call-overload]
            patterns[pattern]["levels"][level] = level_count + 1  # type: ignore[index]

        # Filter by min occurrences and sort
        filtered_patterns = {
            k: v
            for k, v in patterns.items()
            if int(v["count"]) >= min_occurrences  # type: ignore[operator, call-overload]
        }
        sorted_patterns = sorted(
            filtered_patterns.items(),
            key=lambda x: int(x[1]["count"]),  # type: ignore[call-overload]
            reverse=True,
        )

        return {
            "total_logs_analyzed": len(log_entries),
            "unique_patterns": len(patterns),
            "frequent_patterns": len(filtered_patterns),
            "top_patterns": [
                {"pattern": k, "count": v["count"], "levels": v["levels"], "sample": v["sample"]}
                for k, v in sorted_patterns[:10]
            ],
        }

    except Exception as e:
        return {"error": str(e)}


async def get_logs_by_timeframe(
    start_time: str, end_time: str, query: str, chunk_size: int
) -> Dict[str, Any]:
    """Get logs by specific timeframe"""
    try:
        start_dt = datetime.fromisoformat(start_time)
        end_dt = datetime.fromisoformat(end_time)

        log_entries = await loki_client.query_range(query, start_dt, end_dt, 5000)

        if not log_entries:
            return {"status": "no_logs", "message": "No logs found for the specified timeframe"}

        # Chunk logs
        chunks = log_chunker.chunk_logs(log_entries, max_chunk_size=chunk_size)
        formatted_chunks = [log_chunker.format_chunk_for_llm(chunk) for chunk in chunks]

        return {
            "total_logs": len(log_entries),
            "time_range": {"start": start_time, "end": end_time},
            "chunks": formatted_chunks,
            "chunk_count": len(formatted_chunks),
        }

    except Exception as e:
        return {"error": str(e)}


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources"""
    return [
        types.Resource(
            uri="logs://config",
            name="Logs Server Configuration",
            description="Current logs server configuration",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content"""
    if uri == "logs://config":
        config = {
            "loki_url": LOKI_URL,
            "vector_db_path": VECTOR_DB_PATH,
            "default_log_hours": DEFAULT_LOG_HOURS,
            "loki_auth_configured": bool(LOKI_USERNAME and LOKI_PASSWORD),
        }
        return json.dumps(config, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point"""
    try:
        logger.info("Starting Logs MCP Server...")
        print("✅ Logs server initialized successfully", file=sys.stderr)

        # Initialize vector store
        await vector_store.initialize()

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="logs-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Logs server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Logs server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Logs server crashed: {e}", file=sys.stderr)
        sys.exit(1)
