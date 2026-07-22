"""User permissions and MCP launcher integration."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from orchestrator.models.schemas import UserContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Conditional import for direct registry access (when mcp_servers is available)
try:
    from mcp_servers.server_registry import get_available_servers as registry_get_servers
    from mcp_servers.server_registry import list_tools as registry_list_tools
    from mcp_servers.shared_code.config.action_flags import ActionFlags

    DIRECT_REGISTRY_AVAILABLE = True
    ACTION_FLAGS_AVAILABLE = True
    LOGGER.info("Direct MCP registry available - will use direct imports for tool listing")
except ImportError:
    DIRECT_REGISTRY_AVAILABLE = False
    ACTION_FLAGS_AVAILABLE = False
    registry_get_servers = None  # type: ignore[assignment, misc]
    registry_list_tools = None  # type: ignore[assignment, misc]
    ActionFlags = None  # type: ignore[assignment, misc]
    LOGGER.info("Direct MCP registry not available - will use HTTP bridge for tool listing")

# Parameters that are injected by the orchestrator at execution time
# These should be hidden from the LLM's view of the tool schema
ORCHESTRATOR_INJECTED_PARAMS = {
    "user_email",
    "user_name",
    "organization_id",
    "session_id",
    "chat_id",
    "topic_id",
}

# Module-level cache for MCP tools (survives across requests)
# Tools are defined in code, so they only change on deploy (which clears cache)
_MCP_TOOLS_CACHE: Optional[Dict[str, List[Dict[str, Any]]]] = None
_MCP_TOOLS_CACHE_EXPIRES: float = 0
_MCP_TOOLS_CACHE_TTL = 300  # 5 minutes


def _get_cached_tools() -> Optional[Dict[str, List[Dict[str, Any]]]]:
    """Get cached MCP tools if not expired."""
    global _MCP_TOOLS_CACHE, _MCP_TOOLS_CACHE_EXPIRES
    if _MCP_TOOLS_CACHE is not None and time.time() < _MCP_TOOLS_CACHE_EXPIRES:
        LOGGER.debug("MCP tools cache hit")
        return _MCP_TOOLS_CACHE
    return None


def _set_cached_tools(tools_by_server: Dict[str, List[Dict[str, Any]]]) -> None:
    """Cache MCP tools with TTL."""
    global _MCP_TOOLS_CACHE, _MCP_TOOLS_CACHE_EXPIRES
    _MCP_TOOLS_CACHE = tools_by_server
    _MCP_TOOLS_CACHE_EXPIRES = time.time() + _MCP_TOOLS_CACHE_TTL
    total_tools = sum(len(tools) for tools in tools_by_server.values())
    LOGGER.info(
        f"Cached {total_tools} MCP tools from {len(tools_by_server)} servers (TTL: {_MCP_TOOLS_CACHE_TTL}s)"
    )


class UserPermissionsService:
    """Service to query user permissions and available MCP tools."""

    def __init__(
        self,
        bridge_url: Optional[str] = None,
        timeout: float = 10.0,
    ) -> None:
        """
        Initialize the user permissions service.

        Args:
            bridge_url: URL to the MCP bridge service
            timeout: HTTP request timeout in seconds
        """
        self._bridge_url = bridge_url or os.getenv("TOOLS_SERVICE_URL", "")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def get_user_roles(self, user_context: UserContext) -> List[str]:
        """
        Get user roles from Supabase or other auth system.

        Args:
            user_context: User context information

        Returns:
            List of role names the user has
        """
        # Return roles from context or default
        if user_context.roles:
            return list(user_context.roles)

        # Default roles based on source
        if user_context.source == "telegram":
            return ["user", "telegram_user"]
        elif user_context.source == "roam":
            return ["user", "roam_user"]
        else:
            return ["user"]

    async def get_available_tools(self, user_context: UserContext) -> List[Dict[str, Any]]:
        """
        Get list of MCP tools available to this user based on their roles.

        Uses module-level cache for raw tools (5 min TTL), filters per-request
        based on user's is_staff status.

        Args:
            user_context: User context with roles

        Returns:
            List of tool definitions in Gemini format
        """
        if not self._bridge_url and not DIRECT_REGISTRY_AVAILABLE:
            LOGGER.warning(
                "No TOOLS_SERVICE_URL configured and no direct registry available, no MCP tools available"
            )
            return []

        try:
            # Get user roles
            roles = await self.get_user_roles(user_context)
            LOGGER.info(f"User {user_context.user_id} has roles: {roles}")

            # Check module-level cache first
            cached_tools = _get_cached_tools()
            if cached_tools is not None:
                # Filter cached tools based on user's staff status and return
                return self._filter_and_convert_tools(cached_tools, user_context)

            # Cache miss - fetch from bridge
            tools_by_server = await self._fetch_all_tools()
            if tools_by_server:
                _set_cached_tools(tools_by_server)

            # Filter and convert for this user
            return self._filter_and_convert_tools(tools_by_server, user_context)

        except Exception as e:
            LOGGER.exception(f"Error getting available tools: {e}")
            return []

    async def _fetch_all_tools(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch raw tools from all MCP servers (for caching).

        Prefers direct registry import when available for zero-latency tool listing.
        Falls back to HTTP bridge when registry not available.
        """
        # Use direct registry if available and no bridge URL configured
        if DIRECT_REGISTRY_AVAILABLE and not self._bridge_url:
            return await self._fetch_tools_direct()

        # Fall back to HTTP bridge
        return await self._fetch_tools_via_bridge()

    async def _fetch_tools_direct(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch tools directly from registry (no HTTP overhead)."""
        LOGGER.info("Fetching MCP tools via direct registry import")

        servers = registry_get_servers()
        if not servers:
            LOGGER.info("No MCP servers available in registry")
            return {}

        LOGGER.info(f"Found {len(servers)} MCP servers in registry")

        # Fetch tools from all servers in parallel
        async def fetch_server_tools(
            server_name: str,
        ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
            """Fetch tools from a single server."""
            try:
                tools = await registry_list_tools(server_name)
                return (server_name, tools)
            except Exception as e:
                LOGGER.warning(f"Failed to get tools from {server_name}: {e}")
                return (server_name, None)

        # Launch all fetches concurrently
        results = await asyncio.gather(*[fetch_server_tools(name) for name in servers])

        # Build dict of server -> tools
        tools_by_server: Dict[str, List[Dict[str, Any]]] = {}
        for server_name, server_tools in results:
            if server_tools is not None:
                tools_by_server[server_name] = server_tools
                LOGGER.info(f"Loaded {len(server_tools)} tools from {server_name}")

        return tools_by_server

    async def _fetch_tools_via_bridge(self) -> Dict[str, List[Dict[str, Any]]]:
        """Fetch tools via HTTP bridge (legacy path)."""
        client = await self._get_client()

        # Get API key for bridge authentication
        api_key = os.getenv("API_KEY", "")
        headers = {"X-API-Key": api_key} if api_key else {}

        try:
            servers_response = await client.get(
                f"{self._bridge_url}/servers",
                headers=headers,
            )
            servers_response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 405:
                LOGGER.warning("Tools service returned 405 - check TOOLS_SERVICE_URL configuration")
                return {}
            elif e.response.status_code == 401:
                LOGGER.error(
                    "MCP bridge authentication failed - check API_KEY environment variable"
                )
                return {}
            raise

        servers_data = servers_response.json()
        servers = servers_data.get("servers", {})
        if not servers:
            LOGGER.info("No MCP servers configured in bridge")
            return {}

        LOGGER.info(f"Found {len(servers)} MCP servers available")

        # Fetch tools from all servers in parallel
        async def fetch_server_tools(
            server_name: str,
        ) -> Tuple[str, Optional[List[Dict[str, Any]]]]:
            """Fetch tools from a single server."""
            try:
                tools_response = await client.get(
                    f"{self._bridge_url}/servers/{server_name}/tools",
                    headers=headers,
                )
                tools_response.raise_for_status()
                tools_data = tools_response.json()
                return (server_name, tools_data.get("tools", []))
            except Exception as e:
                LOGGER.warning(f"Failed to get tools from {server_name}: {e}")
                return (server_name, None)

        # Launch all fetches concurrently
        results = await asyncio.gather(*[fetch_server_tools(name) for name in servers.keys()])

        # Build dict of server -> tools
        tools_by_server: Dict[str, List[Dict[str, Any]]] = {}
        for server_name, server_tools in results:
            if server_tools is not None:
                tools_by_server[server_name] = server_tools
                LOGGER.info(f"Fetched {len(server_tools)} tools from {server_name}")

        return tools_by_server

    def _filter_and_convert_tools(
        self,
        tools_by_server: Dict[str, List[Dict[str, Any]]],
        user_context: UserContext,
    ) -> List[Dict[str, Any]]:
        """Filter tools based on server/tool enable status and user's staff status.

        Filters applied in order:
        1. Server-level: Skip entirely disabled servers ({SERVER_NAME}_ENABLED=false)
        2. Tool-level: Skip individually disabled tools (MCP_DISABLED_TOOLS)
        3. Persistent-only / internal-only: not advertised to the LLM at all
        4. Customer visibility: Non-staff users only see visible_to_customer tools

        internal_only tools remain callable via server_registry.call_tool -
        this only hides them from the LLM's tool list.

        Returns:
            List of provider-neutral function declarations.
        """
        all_tools: List[Dict[str, Any]] = []

        for server_name, server_tools in tools_by_server.items():
            if not server_tools:
                continue

            # Check if server is enabled (default: true)
            if ACTION_FLAGS_AVAILABLE and not ActionFlags.is_server_enabled(server_name):
                LOGGER.info(f"Skipping disabled server: {server_name}")
                continue

            # Filter out persistent-only tools (only available to persistent agents,
            # not the normal chat flow) and internal-only tools (programmatic
            # callers only, never advertised to the LLM)
            filtered_tools = [
                tool
                for tool in server_tools
                if not tool.get("persistent_only", False) and not tool.get("internal_only", False)
            ]

            # Filter tools based on customer visibility if user is not staff
            if not user_context.is_staff:
                filtered_tools = [
                    tool for tool in filtered_tools if tool.get("visible_to_customer", False)
                ]
                LOGGER.info(
                    f"Filtered to {len(filtered_tools)} customer-visible tools from {server_name}"
                )

            # Convert to Gemini function declaration format, skipping disabled tools
            for tool in filtered_tools:
                tool_name = tool.get("name", "")

                # Check if this specific tool is disabled
                if ACTION_FLAGS_AVAILABLE and ActionFlags.is_tool_disabled(server_name, tool_name):
                    LOGGER.debug(f"Skipping disabled tool: {server_name}:{tool_name}")
                    continue

                gemini_tool = self._convert_to_gemini_format(server_name, tool)
                all_tools.append(gemini_tool)

            LOGGER.info(f"Added {len(filtered_tools)} tools from {server_name}")

        LOGGER.info(f"Total tools available to user: {len(all_tools)}")
        return all_tools

    def _convert_to_gemini_format(
        self, server_name: str, mcp_tool: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Convert MCP tool definition to Gemini function declaration format.

        Args:
            server_name: Name of the MCP server
            mcp_tool: MCP tool definition

        Returns:
            Gemini function declaration
        """
        # Wrap the MCP tool to include server_name in the call
        tool_name = f"{server_name}_{mcp_tool['name']}"

        # Build parameter schema

        parameters: Dict[str, Any] = {
            "type": "OBJECT",
            "properties": {},
            "required": [],
        }

        # Convert input schema, filtering out orchestrator-injected parameters
        input_schema = mcp_tool.get("inputSchema", {})
        if input_schema and isinstance(input_schema, dict):
            properties = input_schema.get("properties", {})
            required = input_schema.get("required", [])

            # Convert property types to Gemini format, excluding injected params
            for prop_name, prop_def in properties.items():
                if prop_name not in ORCHESTRATOR_INJECTED_PARAMS:
                    parameters["properties"][prop_name] = self._convert_property_type(prop_def)  # type: ignore[index]

            # Filter injected params from required list
            parameters["required"] = [r for r in required if r not in ORCHESTRATOR_INJECTED_PARAMS]

        return {
            "name": tool_name,
            "description": f"[{server_name}] {mcp_tool.get('description', '')}",
            "parameters": parameters,
        }

    def _convert_property_type(self, prop_def: Dict[str, Any]) -> Dict[str, Any]:
        """Convert JSON Schema property type to Gemini format.

        Only copies fields that Gemini supports (type, description, enum, items, properties).
        Explicitly does NOT copy: default, examples, format, etc.
        """
        json_type = prop_def.get("type", "string")
        gemini_prop: Dict[str, Any] = {
            "description": prop_def.get("description", ""),
        }

        # Map JSON Schema types to Gemini types
        type_mapping = {
            "string": "STRING",
            "number": "NUMBER",
            "integer": "INTEGER",
            "boolean": "BOOLEAN",
            "array": "ARRAY",
            "object": "OBJECT",
        }

        gemini_prop["type"] = type_mapping.get(json_type.lower(), "STRING")

        # Handle enums (Gemini supports enum for STRING types)
        if "enum" in prop_def:
            gemini_prop["enum"] = prop_def["enum"]

        # Handle arrays
        if json_type == "array" and "items" in prop_def:
            gemini_prop["items"] = self._convert_property_type(prop_def["items"])

        # Handle objects
        if json_type == "object" and "properties" in prop_def:
            gemini_prop["properties"] = {
                k: self._convert_property_type(v) for k, v in prop_def["properties"].items()
            }

        return gemini_prop

    async def close(self) -> None:
        """Close HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None


# Singleton instance for checkpointer-safe access (not stored in LangGraph state)
_permissions_instance: Optional["UserPermissionsService"] = None


def get_permissions_service() -> "UserPermissionsService":
    """Get singleton UserPermissionsService instance.

    This accessor is used instead of storing the service in LangGraph state,
    which would cause serialization errors with the PostgreSQL checkpointer.

    Returns:
        Singleton UserPermissionsService instance
    """
    global _permissions_instance
    if _permissions_instance is None:
        from orchestrator.config.settings import get_settings

        settings = get_settings()
        _permissions_instance = UserPermissionsService(
            bridge_url=settings.bridge_url or os.getenv("TOOLS_SERVICE_URL"),
        )
    return _permissions_instance


__all__ = ["UserPermissionsService", "get_permissions_service"]
