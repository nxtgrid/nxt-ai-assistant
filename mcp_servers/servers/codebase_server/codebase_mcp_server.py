#!/usr/bin/env python3
"""
Codebase MCP Server - Helps LLMs understand platform behavior through code analysis

This server provides tools to:
1. Search and analyze indexed codebase to understand why the platform behaves a certain way
2. Retrieve recent PRs merged to production to identify recent code changes
3. Query code structure, dependencies, and implementation details

The server uses local code indexing and GitHub API integration for comprehensive code analysis.
"""

import asyncio
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from shared_code.database.connections import get_supabase_client
from shared_code.utils.logger import setup_logger

# Load environment variables
load_dotenv()

logger = setup_logger("codebase-server")

# Startup message
print("🚀 Codebase MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("codebase-server")

# Configuration
CODEBASE_PATH = os.getenv("CODEBASE_PATH", ".")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_REPO = os.getenv("GITHUB_REPO", "")  # Format: "owner/repo"
VECTOR_DB_PATH = os.getenv("CODEBASE_VECTOR_DB_PATH", "./data/codebase_vectordb")

# Auth DB connection config (read-only investigation)
AUTH_DB_HOST = os.getenv("AUTH_DB_HOST", "")
AUTH_DB_PORT = os.getenv("AUTH_DB_PORT", "5432")
AUTH_DB_USER = os.getenv("AUTH_DB_USER", "")
AUTH_DB_PASSWORD = os.getenv("AUTH_DB_PASSWORD", "")
AUTH_DB_NAME = os.getenv("AUTH_DB_NAME", "")

# Tables and columns allowed for investigation queries
INVESTIGATION_TABLE_ALLOWLIST: Dict[str, List[str]] = {
    "meters": [
        "id",
        "meter_number",
        "status",
        "grid_id",
        "organization_id",
        "last_seen_at",
        "created_at",
    ],
    "orders": [
        "id",
        "meter_id",
        "amount",
        "status",
        "created_at",
        "order_type",
        "token_number",
    ],
    "grids": ["id", "name", "status", "organization_id", "location"],
    "organizations": ["id", "name", "status"],
    "accounts": [
        "id",
        "first_name",
        "last_name",
        "organization_id",
        "role",
        "status",
    ],
}


async def _run_investigation_query(sql_query: str) -> Dict[str, Any]:
    """Execute a read-only SQL query against the Auth DB for investigation.

    Security:
    - Only SELECT queries allowed (simple string check + readonly DB user)
    - Table allowlist enforced (only allowed tables can be queried)
    - Column allowlist enforced (only allowed columns returned)
    - Semicolons and dangerous keywords rejected
    - 10-second statement timeout
    - Max 100 rows returned
    """
    import asyncpg

    sql_stripped = sql_query.strip().rstrip(";")
    sql_upper = sql_stripped.upper()

    if not sql_upper.startswith("SELECT"):
        return {"error": "Only SELECT queries are allowed for investigation"}

    # Reject multiple statements and dangerous keywords
    if ";" in sql_stripped:
        return {"error": "Multiple statements are not allowed"}
    dangerous_keywords = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "GRANT"}
    for keyword in dangerous_keywords:
        if re.search(rf"\b{keyword}\b", sql_upper):
            return {"error": f"Keyword '{keyword}' is not allowed in investigation queries"}

    # Check that the query only references allowed tables
    sql_lower = sql_stripped.lower()
    allowed_tables: Set[str] = set(INVESTIGATION_TABLE_ALLOWLIST.keys())
    from_pattern = re.findall(r"\b(?:from|join)\s+(\w+)", sql_lower)
    if not from_pattern:
        return {"error": "Could not identify target table in query"}
    for table in from_pattern:
        if table not in allowed_tables:
            return {
                "error": f"Table '{table}' is not in the investigation allowlist. "
                f"Allowed tables: {', '.join(sorted(allowed_tables))}"
            }

    if not AUTH_DB_HOST or not AUTH_DB_USER:
        return {"error": "Auth database not configured for investigation"}

    try:
        conn = await asyncpg.connect(
            host=AUTH_DB_HOST,
            port=int(AUTH_DB_PORT),
            user=AUTH_DB_USER,
            password=AUTH_DB_PASSWORD,
            database=AUTH_DB_NAME,
            ssl="require",
            statement_cache_size=0,
        )
        try:
            await conn.execute("SET statement_timeout = '10s'")
            rows = await conn.fetch(sql_stripped)

            # Limit to 100 rows
            limited_rows = rows[:100]
            result_rows = [dict(row) for row in limited_rows]

            # Filter to only allowed columns per table
            primary_table = from_pattern[0] if from_pattern else None
            allowed_columns = (
                set(INVESTIGATION_TABLE_ALLOWLIST.get(primary_table, []))
                if primary_table
                else set()
            )
            if allowed_columns:
                result_rows = [
                    {k: v for k, v in row.items() if k in allowed_columns} for row in result_rows
                ]

            # Convert non-serializable types
            for row in result_rows:
                for key, value in row.items():
                    if isinstance(value, datetime):
                        row[key] = value.isoformat()
                    elif not isinstance(value, (str, int, float, bool, type(None))):
                        row[key] = str(value)

            return {
                "query": sql_stripped,
                "row_count": len(result_rows),
                "total_rows": len(rows),
                "truncated": len(rows) > 100,
                "rows": result_rows,
            }
        finally:
            await conn.close()

    except asyncpg.exceptions.QueryCanceledError:
        return {"error": "Query timed out (10s limit). Try a simpler query."}
    except Exception as e:
        logger.error(f"Investigation query failed: {e}")
        return {"error": "Query execution failed. Check query syntax and try again."}


class CodebaseAnalyzer:
    """Analyzes codebase structure and content"""

    def __init__(self, codebase_path: str):
        self.codebase_path = Path(codebase_path)
        self.index_cache: Dict[str, Any] = {}

    async def search_code(
        self,
        query: str,
        file_pattern: Optional[str] = None,
        context_lines: int = 3,
        max_results: int = 50,
    ) -> Dict[str, Any]:
        """
        Search for code patterns using grep/ripgrep.

        Args:
            query: Search query (supports regex)
            file_pattern: File pattern to search (e.g., "*.py", "*.ts")
            context_lines: Number of context lines to show
            max_results: Maximum number of results

        Returns:
            Dict with search results including file paths, line numbers, and code snippets
        """
        try:
            cmd = ["grep", "-r", "-n", "-i"]

            # Add context lines
            if context_lines > 0:
                cmd.extend(["-C", str(context_lines)])

            # Add file pattern if specified
            if file_pattern:
                cmd.extend(["--include", file_pattern])

            cmd.extend([query, str(self.codebase_path)])

            # Execute search
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

            # Parse results
            results = []

            for line in result.stdout.split("\n")[:max_results]:
                if not line.strip():
                    continue

                # Parse grep output format: file:line:content
                match = re.match(r"([^:]+):(\d+):(.*)$", line)
                if match:
                    file_path, line_num, content = match.groups()

                    # Relativize path
                    try:
                        rel_path = Path(file_path).relative_to(self.codebase_path)
                    except ValueError:
                        rel_path = Path(file_path)

                    results.append(
                        {
                            "file": str(rel_path),
                            "line": int(line_num),
                            "content": content,
                            "absolute_path": file_path,
                        }
                    )

            return {
                "query": query,
                "total_results": len(results),
                "results": results,
                "truncated": len(result.stdout.split("\n")) > max_results,
            }

        except subprocess.TimeoutExpired:
            return {"error": "Search timed out", "query": query}
        except Exception as e:
            return {"error": str(e), "query": query}

    async def find_definition(
        self, symbol_name: str, symbol_type: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Find definition of a function, class, or variable.

        Args:
            symbol_name: Name of the symbol to find
            symbol_type: Type of symbol (class, function, variable)

        Returns:
            Dict with definition locations and code snippets
        """
        patterns = []

        if symbol_type == "class" or not symbol_type:
            patterns.append(f"class {symbol_name}")
        if symbol_type == "function" or not symbol_type:
            patterns.append(f"def {symbol_name}")
            patterns.append(f"async def {symbol_name}")
            patterns.append(f"function {symbol_name}")
            patterns.append(f"const {symbol_name} =")

        results = []
        for pattern in patterns:
            search_result = await self.search_code(pattern, context_lines=5, max_results=10)
            if search_result.get("results"):
                results.extend(search_result["results"])

        return {
            "symbol": symbol_name,
            "symbol_type": symbol_type or "any",
            "definitions": results,
            "found": len(results) > 0,
        }

    async def find_usage(self, symbol_name: str, max_results: int = 30) -> Dict[str, Any]:
        """
        Find where a symbol is used in the codebase.

        Args:
            symbol_name: Name of the symbol
            max_results: Maximum number of usage examples

        Returns:
            Dict with usage locations
        """
        result = await self.search_code(symbol_name, context_lines=2, max_results=max_results)

        return {
            "symbol": symbol_name,
            "usages": result.get("results", []),
            "total_usages": result.get("total_results", 0),
            "truncated": result.get("truncated", False),
        }

    async def analyze_file(self, file_path: str) -> Dict[str, Any]:
        """
        Analyze a specific file's structure and content.

        Args:
            file_path: Relative path to the file

        Returns:
            Dict with file analysis including imports, classes, functions
        """
        try:
            full_path = self.codebase_path / file_path

            if not full_path.exists():
                return {"error": f"File not found: {file_path}"}

            with open(full_path, "r", encoding="utf-8") as f:
                content = f.read()

            # Extract structure based on file type
            imports_list: List[str] = []
            classes_list: List[str] = []
            functions_list: List[str] = []
            analysis = {
                "file": file_path,
                "lines": len(content.split("\n")),
                "size_bytes": len(content),
                "imports": imports_list,
                "classes": classes_list,
                "functions": functions_list,
                "content_preview": content[:500] if len(content) > 500 else content,
            }

            # Python files
            if file_path.endswith(".py"):
                # Find imports
                for match in re.finditer(r"^(?:from|import)\s+(.+)", content, re.MULTILINE):
                    imports_list.append(match.group(0))

                # Find classes
                for match in re.finditer(r"^class\s+(\w+)", content, re.MULTILINE):
                    classes_list.append(match.group(1))

                # Find functions
                for match in re.finditer(r"^(?:async\s+)?def\s+(\w+)", content, re.MULTILINE):
                    functions_list.append(match.group(1))

            # TypeScript/JavaScript files
            elif file_path.endswith((".ts", ".tsx", ".js", ".jsx")):
                # Find imports
                for match in re.finditer(r"^import\s+.+", content, re.MULTILINE):
                    imports_list.append(match.group(0))

                # Find classes
                for match in re.finditer(r"class\s+(\w+)", content):
                    classes_list.append(match.group(1))

                # Find functions
                for match in re.finditer(r"(?:function|const|let|var)\s+(\w+)\s*[=\(]", content):
                    functions_list.append(match.group(1))

            return analysis

        except Exception as e:
            return {"error": str(e), "file": file_path}

    async def get_file_history(self, file_path: str, max_commits: int = 10) -> Dict[str, Any]:
        """
        Get git history for a specific file.

        Args:
            file_path: Relative path to the file
            max_commits: Maximum number of commits to retrieve

        Returns:
            Dict with commit history
        """
        try:
            cmd = [
                "git",
                "log",
                f"-n{max_commits}",
                "--pretty=format:%H|%an|%ae|%ad|%s",
                "--date=iso",
                "--",
                file_path,
            ]

            result = subprocess.run(
                cmd, capture_output=True, text=True, cwd=self.codebase_path, timeout=10
            )

            commits = []
            for line in result.stdout.split("\n"):
                if not line.strip():
                    continue

                parts = line.split("|")
                if len(parts) >= 5:
                    commits.append(
                        {
                            "hash": parts[0],
                            "author": parts[1],
                            "email": parts[2],
                            "date": parts[3],
                            "message": "|".join(parts[4:]),
                        }
                    )

            return {"file": file_path, "commits": commits, "total_commits": len(commits)}

        except Exception as e:
            return {"error": str(e), "file": file_path}


class GitHubPRAnalyzer:
    """Analyzes GitHub Pull Requests"""

    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    async def get_recent_prs(
        self, branch: str = "main", days: int = 30, max_prs: int = 20
    ) -> Dict[str, Any]:
        """
        Get recent PRs merged to the specified branch.

        Args:
            branch: Target branch (default: main)
            days: Number of days to look back
            max_prs: Maximum number of PRs to retrieve

        Returns:
            Dict with PR information
        """
        if not self.token or not self.repo:
            return {
                "error": "GitHub token or repo not configured. Set GITHUB_TOKEN and GITHUB_REPO environment variables."
            }

        try:
            import aiohttp

            since_date = (datetime.now() - timedelta(days=days)).isoformat()

            url = f"https://api.github.com/repos/{self.repo}/pulls"
            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
            }
            params = {
                "state": "closed",
                "base": branch,
                "sort": "updated",
                "direction": "desc",
                "per_page": max_prs,
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers, params=params) as response:  # type: ignore[arg-type]
                    if response.status != 200:
                        return {"error": f"GitHub API error: {response.status}"}

                    prs_data = await response.json()

            # Filter for merged PRs within the time window
            prs = []
            for pr in prs_data:
                if pr.get("merged_at"):
                    merged_date = datetime.fromisoformat(pr["merged_at"].replace("Z", "+00:00"))
                    since = datetime.fromisoformat(since_date.replace("Z", "+00:00"))

                    if merged_date >= since:
                        prs.append(
                            {
                                "number": pr["number"],
                                "title": pr["title"],
                                "author": pr["user"]["login"],
                                "merged_at": pr["merged_at"],
                                "url": pr["html_url"],
                                "body": pr.get("body", "")[:500],  # First 500 chars
                                "files_changed": pr.get("changed_files", 0),
                                "additions": pr.get("additions", 0),
                                "deletions": pr.get("deletions", 0),
                                "labels": [label["name"] for label in pr.get("labels", [])],
                            }
                        )

            return {"branch": branch, "days": days, "total_prs": len(prs), "prs": prs}

        except Exception as e:
            return {"error": str(e)}

    async def get_pr_files(self, pr_number: int) -> Dict[str, Any]:
        """
        Get files changed in a specific PR.

        Args:
            pr_number: PR number

        Returns:
            Dict with file changes
        """
        if not self.token or not self.repo:
            return {"error": "GitHub token or repo not configured"}

        try:
            import aiohttp

            url = f"https://api.github.com/repos/{self.repo}/pulls/{pr_number}/files"
            headers = {
                "Authorization": f"token {self.token}",
                "Accept": "application/vnd.github.v3+json",
            }

            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=headers) as response:
                    if response.status != 200:
                        return {"error": f"GitHub API error: {response.status}"}

                    files_data = await response.json()

            files = []
            for file_info in files_data:
                files.append(
                    {
                        "filename": file_info["filename"],
                        "status": file_info["status"],
                        "additions": file_info["additions"],
                        "deletions": file_info["deletions"],
                        "changes": file_info["changes"],
                        "patch": file_info.get("patch", "")[:1000],  # First 1000 chars of diff
                    }
                )

            return {"pr_number": pr_number, "total_files": len(files), "files": files}

        except Exception as e:
            return {"error": str(e)}


class VectorCodeSearch:
    """Search indexed code using vector embeddings in Supabase"""

    def __init__(self, vector_db_path: str = None):
        # vector_db_path kept for backwards compatibility but not used
        self._initialized = False

    async def initialize(self):
        """Initialize Supabase connection"""
        if self._initialized:
            return

        try:
            # Initialize Supabase client via db_manager
            if not db_manager.supabase_client:
                await db_manager.initialize_supabase()

            if not db_manager.supabase_client:
                logger.warning("Supabase client not available for vector search")
                self._initialized = False
                return

            self._initialized = True

            # Get count of codebase documents
            result = (
                db_manager.supabase_client.table("rag_documents")
                .select("id", count="exact")
                .eq("source_type", "codebase")
                .execute()
            )
            count = result.count if hasattr(result, "count") else 0
            logger.info(f"Vector code search initialized with {count} codebase chunks")

        except Exception as e:
            logger.error(f"Error initializing vector search: {str(e)}")
            self._initialized = False

    async def search_code_semantic(
        self, query: str, n_results: int = 10, language_filter: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Search code using semantic similarity via Supabase pgvector.

        Args:
            query: Natural language query
            n_results: Number of results
            language_filter: Filter by programming language

        Returns:
            Dict with search results
        """
        if not self._initialized or not db_manager.supabase_client:
            return {
                "error": "Vector search not initialized. Please ensure Supabase is configured and the indexer has run.",
                "indexer_command": "python servers/codebase_server/indexer.py <owner/repo> main",
            }

        try:
            # Generate embedding for the query using OpenAI
            import openai

            openai_client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

            embedding_response = openai_client.embeddings.create(
                model="text-embedding-ada-002", input=query
            )
            query_embedding = embedding_response.data[0].embedding

            # Call Supabase RPC function for vector similarity search
            # This requires the search_codebase function to be created in Supabase
            # See servers/codebase_server/supabase_functions.sql
            try:
                rpc_params = {
                    "query_embedding": query_embedding,
                    "match_count": n_results,
                }

                if language_filter:
                    rpc_params["language_filter"] = language_filter

                result = db_manager.supabase_client.rpc("search_codebase", rpc_params).execute()

                if not result.data:
                    return {
                        "query": query,
                        "total_results": 0,
                        "results": [],
                        "note": "No codebase documents found. Please run the indexer first.",
                    }

                # Format results from RPC function
                formatted_results = []
                for doc in result.data:
                    metadata = doc.get("source_metadata", {})
                    formatted_results.append(
                        {
                            "file": metadata.get("file_path", "unknown"),
                            "language": metadata.get("language", "unknown"),
                            "lines": f"{metadata.get('start_line', '?')}-{metadata.get('end_line', '?')}",
                            "code": doc.get("content", ""),
                            "similarity_score": doc.get("similarity"),
                            "repo": metadata.get("repo", "unknown"),
                            "branch": metadata.get("branch", "main"),
                        }
                    )

                return {
                    "query": query,
                    "total_results": len(formatted_results),
                    "results": formatted_results,
                }

            except Exception as rpc_error:
                # Fallback to basic query if RPC function doesn't exist
                logger.warning(f"RPC search failed, falling back to basic query: {str(rpc_error)}")

                query_builder = (
                    db_manager.supabase_client.table("rag_documents")
                    .select("id, content, source_id, source_metadata, created_at")
                    .eq("source_type", "codebase")
                    .limit(n_results)
                )

                result = query_builder.execute()

                if not result.data:
                    return {
                        "query": query,
                        "total_results": 0,
                        "results": [],
                        "note": "No codebase documents found. Please run the indexer first.",
                    }

                formatted_results = []
                for doc in result.data:
                    metadata = doc.get("source_metadata", {})
                    formatted_results.append(
                        {
                            "file": metadata.get("file_path", "unknown"),
                            "language": metadata.get("language", "unknown"),
                            "lines": f"{metadata.get('start_line', '?')}-{metadata.get('end_line', '?')}",
                            "code": doc.get("content", ""),
                            "similarity_score": None,
                            "repo": metadata.get("repo", "unknown"),
                            "branch": metadata.get("branch", "main"),
                        }
                    )

                return {
                    "query": query,
                    "total_results": len(formatted_results),
                    "results": formatted_results,
                    "note": "Using basic query without semantic search. Run the SQL from servers/codebase_server/supabase_functions.sql to enable vector search.",
                }

        except Exception as e:
            logger.error(f"Error in semantic search: {str(e)}")
            return {"error": str(e)}

    async def get_stats(self) -> Dict[str, Any]:
        """Get vector database statistics from Supabase"""
        if not self._initialized or not db_manager.supabase_client:
            return {"error": "Not initialized"}

        try:
            # Get total count
            count_result = (
                db_manager.supabase_client.table("rag_documents")
                .select("id", count="exact")
                .eq("source_type", "codebase")
                .execute()
            )

            total_count = count_result.count if hasattr(count_result, "count") else 0

            # Get language distribution from metadata
            sample_result = (
                db_manager.supabase_client.table("rag_documents")
                .select("source_metadata")
                .eq("source_type", "codebase")
                .limit(100)
                .execute()
            )

            language_counts: Dict[str, int] = {}
            if sample_result.data:
                for doc in sample_result.data:
                    metadata = doc.get("source_metadata", {})
                    lang = metadata.get("language", "unknown")
                    language_counts[lang] = language_counts.get(lang, 0) + 1

            return {
                "total_chunks": total_count,
                "language_distribution": language_counts,
                "storage": "Supabase rag_documents table",
            }

        except Exception as e:
            logger.error(f"Error getting stats: {str(e)}")
            return {"error": str(e)}


# Global instances
# Note: DatabaseManager has been removed - using direct Supabase client instead
class LegacyDatabaseManager:
    """Temporary shim to avoid import errors until full refactor"""

    def __init__(self):
        self.supabase_client = None

    async def initialize_supabase(self):
        """Initialize Supabase client"""
        try:
            self.supabase_client = get_supabase_client()
        except Exception as e:
            logger.error(f"Failed to initialize Supabase: {e}")


db_manager = LegacyDatabaseManager()
codebase_analyzer = CodebaseAnalyzer(CODEBASE_PATH)
github_pr_analyzer = GitHubPRAnalyzer(GITHUB_TOKEN, GITHUB_REPO)
vector_code_search = VectorCodeSearch(VECTOR_DB_PATH)


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available codebase analysis tools"""

    tools = [
        types.Tool(
            name="search_codebase",
            description="Search the codebase for specific code patterns, functions, classes, or text. Supports regex patterns and file filtering. Use this to understand how specific features are implemented, find where certain logic exists, or trace code flow. Returns file paths, line numbers, and code snippets with context. Essential for understanding why the platform behaves a certain way.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (supports regex). Examples: 'def process_payment', 'class User', 'import stripe'",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "File pattern to limit search (e.g., '*.py' for Python files, '*.ts' for TypeScript)",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Number of context lines to show around matches (default: 3)",
                        "default": 3,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 50)",
                        "default": 50,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="find_symbol_definition",
            description="Find the definition of a function, class, or variable in the codebase. Use this when you need to understand how a specific component is implemented, what parameters it takes, or what it returns. Searches for class definitions, function definitions, and variable assignments across all supported languages (Python, TypeScript, JavaScript).",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": "Name of the symbol to find (e.g., 'UserAuthentication', 'process_payment', 'API_ENDPOINT')",
                    },
                    "symbol_type": {
                        "type": "string",
                        "description": "Type of symbol: 'class', 'function', or 'variable' (optional - searches all if not specified)",
                        "enum": ["class", "function", "variable"],
                    },
                },
                "required": ["symbol_name"],
            },
        ),
        types.Tool(
            name="find_symbol_usage",
            description="Find all places where a function, class, or variable is used in the codebase. Use this to understand the impact of changes, see how a component is being called, or trace data flow through the system. Returns all usage locations with surrounding code context.",
            inputSchema={
                "type": "object",
                "properties": {
                    "symbol_name": {
                        "type": "string",
                        "description": "Name of the symbol to find usages of",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of usage examples to return (default: 30)",
                        "default": 30,
                    },
                },
                "required": ["symbol_name"],
            },
        ),
        types.Tool(
            name="analyze_file",
            description="Analyze a specific file's structure and content. Returns imports, classes, functions, file size, and a content preview. Use this to understand what a specific file does, what it depends on, and what it exports. Supports Python, TypeScript, and JavaScript files.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path to the file from the codebase root (e.g., 'src/utils/payment.py')",
                    }
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="get_file_git_history",
            description="Get the git commit history for a specific file. Use this to understand when a file was last changed, who changed it, and why. Helps identify if recent changes might have caused behavioral changes in the platform.",
            inputSchema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Relative path to the file from the codebase root",
                    },
                    "max_commits": {
                        "type": "integer",
                        "description": "Maximum number of commits to retrieve (default: 10)",
                        "default": 10,
                    },
                },
                "required": ["file_path"],
            },
        ),
        types.Tool(
            name="get_recent_production_prs",
            description="Get recent Pull Requests that were merged to the production main branch. Use this to understand what code changes were recently deployed to production. This is critical for identifying if behavioral changes in the platform are due to recent code updates. Returns PR titles, authors, merge dates, file changes, and labels. Helps answer questions like 'Did something change recently that could cause this behavior?'",
            inputSchema={
                "type": "object",
                "properties": {
                    "branch": {
                        "type": "string",
                        "description": "Target branch to check (default: 'main')",
                        "default": "main",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Number of days to look back (default: 30)",
                        "default": 30,
                    },
                    "max_prs": {
                        "type": "integer",
                        "description": "Maximum number of PRs to retrieve (default: 20)",
                        "default": 20,
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="get_pr_file_changes",
            description="Get detailed file changes for a specific Pull Request. Use this after identifying a relevant PR to see exactly what code was changed. Returns the list of modified files with their diffs/patches. Essential for understanding the exact nature of recent code changes.",
            inputSchema={
                "type": "object",
                "properties": {
                    "pr_number": {"type": "integer", "description": "PR number from GitHub"}
                },
                "required": ["pr_number"],
            },
        ),
        types.Tool(
            name="search_code_semantic",
            description="Search the indexed codebase using natural language/semantic search powered by vector embeddings. This searches the pre-indexed vector database created by running the indexer twice daily. Finds code that is conceptually related to your query, not just keyword matches. Perfect for questions like 'how does authentication work' or 'where is payment processing'. The indexer must be run first to populate the vector database. Returns code chunks with file paths, line numbers, and similarity scores.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural language search query (e.g., 'user authentication logic', 'payment processing', 'database connection')",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of code chunks to return (default: 10)",
                        "default": 10,
                    },
                    "language_filter": {
                        "type": "string",
                        "description": "Filter by programming language (python, javascript, typescript, etc.)",
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="get_vector_db_stats",
            description="Get statistics about the indexed codebase vector database. Shows total code chunks indexed, language distribution, and database path. Use this to verify the indexer has run and to understand what code is available for semantic search.",
            inputSchema={"type": "object", "properties": {}, "required": []},
            visible_to_customer=False,
        ),
        types.Tool(
            name="investigate_database",
            description="Run a read-only SQL SELECT query against the Auth database for investigation. Only allowed tables (meters, orders, grids, organizations, accounts) with allowed columns. Max 100 rows, 10-second timeout.",
            inputSchema={
                "type": "object",
                "properties": {
                    "sql_query": {
                        "type": "string",
                        "description": "SQL SELECT query to execute (e.g., \"SELECT meter_number, status FROM meters WHERE meter_number = '12345'\")",
                    }
                },
                "required": ["sql_query"],
            },
        ),
    ]

    logger.info(f"Codebase server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls"""
    try:
        # Initialize vector search if needed
        await vector_code_search.initialize()

        if name == "search_codebase":
            result = await codebase_analyzer.search_code(
                query=arguments["query"],
                file_pattern=arguments.get("file_pattern"),
                context_lines=arguments.get("context_lines", 3),
                max_results=arguments.get("max_results", 50),
            )
        elif name == "find_symbol_definition":
            result = await codebase_analyzer.find_definition(
                symbol_name=arguments["symbol_name"], symbol_type=arguments.get("symbol_type")
            )
        elif name == "find_symbol_usage":
            result = await codebase_analyzer.find_usage(
                symbol_name=arguments["symbol_name"], max_results=arguments.get("max_results", 30)
            )
        elif name == "analyze_file":
            result = await codebase_analyzer.analyze_file(file_path=arguments["file_path"])
        elif name == "get_file_git_history":
            result = await codebase_analyzer.get_file_history(
                file_path=arguments["file_path"], max_commits=arguments.get("max_commits", 10)
            )
        elif name == "get_recent_production_prs":
            result = await github_pr_analyzer.get_recent_prs(
                branch=arguments.get("branch", "main"),
                days=arguments.get("days", 30),
                max_prs=arguments.get("max_prs", 20),
            )
        elif name == "get_pr_file_changes":
            result = await github_pr_analyzer.get_pr_files(pr_number=arguments["pr_number"])
        elif name == "search_code_semantic":
            result = await vector_code_search.search_code_semantic(
                query=arguments["query"],
                n_results=arguments.get("max_results", 10),
                language_filter=arguments.get("language_filter"),
            )
        elif name == "get_vector_db_stats":
            result = await vector_code_search.get_stats()
        elif name == "investigate_database":
            result = await _run_investigation_query(
                sql_query=arguments["sql_query"],
            )
        else:
            raise ValueError(f"Unknown tool: {name}")

        return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

    except Exception as e:
        logger.error(f"Error in {name}: {str(e)}")
        return [types.TextContent(type="text", text=json.dumps({"error": str(e)}, indent=2))]


@server.list_resources()
async def handle_list_resources() -> List[types.Resource]:
    """List available resources"""
    return [
        types.Resource(
            uri="codebase://config",
            name="Codebase Server Configuration",
            description="Current codebase server configuration",
            mimeType="application/json",
        )
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content"""
    if uri == "codebase://config":
        config = {
            "codebase_path": str(CODEBASE_PATH),
            "github_repo": GITHUB_REPO if GITHUB_REPO else "Not configured",
            "github_token_set": bool(GITHUB_TOKEN),
        }
        return json.dumps(config, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main entry point"""
    try:
        logger.info("Starting Codebase MCP Server...")
        print("✅ Codebase server initialized successfully", file=sys.stderr)

        # Validate configuration
        if not Path(CODEBASE_PATH).exists():
            logger.warning(f"Codebase path does not exist: {CODEBASE_PATH}")

        if not GITHUB_TOKEN or not GITHUB_REPO:
            logger.warning("GitHub integration not configured. PR features will be limited.")

        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="codebase-server",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(), experimental_capabilities={}
                    ),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Codebase server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Codebase server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Codebase server crashed: {e}", file=sys.stderr)
        sys.exit(1)
