"""
Server Registry - Central registry for all MCP servers.

This module provides direct access to MCP server handlers without subprocess spawning.
Used by the bridge to call tools directly for better performance.

IMPORTANT: All errors returned to the orchestrator are sanitized to prevent
technical details from leaking to end users or the LLM.

Tool definitions can be externalized to JSON (tool_definitions.json) for easier editing.
The JSON file is preferred when present; code-based definitions are used as fallback.
"""

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from shared.utils.error_sanitizer import sanitize_error_for_tool_result

# Load environment variables before importing servers
load_dotenv()

logger = logging.getLogger("server-registry")

# Cache for tool definitions loaded from JSON
_tool_definitions: Optional[Dict] = None

# Server metadata for descriptions
SERVER_METADATA = {
    "equipment_diagnostics": {
        "description": "Production equipment diagnostics, historical analysis, charts, and monitoring",
        "module": "servers.equipment_diagnostics_server.equipment_diagnostics_mcp_server",
    },
    # Note: vrm server is being replaced by equipment_diagnostics but kept for backwards compatibility
    "vrm": {
        "description": "Victron VRM monitoring and control (legacy - use equipment_diagnostics)",
        "module": "servers.vrm_server.vrm_mcp_server",
    },
    "jira": {
        "description": "Jira analysis and comment processing",
        "module": "servers.jira_server.jira_mcp_server",
    },
    "logs": {
        "description": "Software/application log analysis (NOT equipment logs) - backend service logs from Loki",
        "module": "servers.logs_server.logs_mcp_server",
    },
    "meters": {
        "description": "Meter management and operations",
        "module": "servers.meters_server.meters_mcp_server",
    },
    "equipment_control": {
        "description": "Equipment control operations",
        "module": "servers.equipment_control_server.equipment_control_mcp_server",
    },
    "payment_processor": {
        "description": "Payment processor transaction status checks",
        "module": "servers.payment_processor_server.payment_processor_mcp_server",
    },
    "customer": {
        "description": "Customer-facing tools for payment and commissioning status",
        "module": "servers.customer_server.customer_mcp_server",
    },
    "grafana": {
        "description": "Grafana dashboard panel rendering",
        "module": "servers.grafana_server.grafana_mcp_server",
    },
    "schedule": {
        "description": "User command scheduling for future or recurring execution",
        "module": "servers.schedule_server.schedule_mcp_server",
    },
    "meta": {
        "description": "Bot performance analytics and metacognition tools",
        "module": "servers.meta_server.meta_mcp_server",
    },
    "grid_design": {
        "description": "Grid design and Bill of Materials generation via AppSheet",
        "module": "servers.grid_design_server.grid_design_mcp_server",
    },
    "solar": {
        "description": "Solar potential assessment using Global Solar Atlas API",
        "module": "servers.solar_server.solar_mcp_server",
    },
    "knowledge": {
        "description": "Knowledge base summarization and exploration tools",
        "module": "servers.knowledge_server.knowledge_mcp_server",
    },
    "messaging": {
        "description": "Send messages to validated Telegram groups (persistent agents only)",
        "module": "servers.messaging_server.messaging_mcp_server",
    },
    "reference": {
        "description": "Nigerian import tariff, prohibition list, and standards lookups (staff only)",
        "module": "servers.reference_server.reference_mcp_server",
    },
}

# Cache for loaded server modules
_server_modules: Dict[str, Any] = {}


def _load_tool_definitions() -> Dict:
    """
    Load tool definitions from JSON file.

    Returns:
        Dictionary with tool definitions, or empty dict if file not found.
    """
    global _tool_definitions
    if _tool_definitions is None:
        json_path = Path(__file__).parent / "tool_definitions.json"
        if json_path.exists():
            try:
                with open(json_path) as f:
                    _tool_definitions = json.load(f)
                logger.info(
                    f"Loaded tool definitions from JSON: "
                    f"{sum(len(v) for v in _tool_definitions.get('tools', {}).values())} tools"
                )
            except (json.JSONDecodeError, IOError) as e:
                logger.warning(f"Failed to load tool_definitions.json: {e}")
                _tool_definitions = {}
        else:
            logger.debug("tool_definitions.json not found, using code-based definitions")
            _tool_definitions = {}
    return _tool_definitions


def reload_tool_definitions() -> int:
    """
    Reload tool definitions from JSON file.

    Useful for hot-reloading changes during development without restarting the service.

    Returns:
        Number of tools loaded, or 0 if reload failed.
    """
    global _tool_definitions
    _tool_definitions = None  # Clear cache
    defs = _load_tool_definitions()
    return sum(len(v) for v in defs.get("tools", {}).values())


def _load_server(server_name: str) -> Any:
    """
    Lazily load a server module.

    Args:
        server_name: Name of the server to load

    Returns:
        The loaded server module

    Raises:
        ValueError: If server name is unknown
        ImportError: If module cannot be loaded
    """
    if server_name in _server_modules:
        return _server_modules[server_name]

    if server_name not in SERVER_METADATA:
        raise ValueError(f"Unknown server: {server_name}")

    module_path = SERVER_METADATA[server_name]["module"]

    try:
        import importlib

        module = importlib.import_module(module_path)
        _server_modules[server_name] = module
        logger.info(f"Loaded server module: {server_name}")
        return module
    except ImportError as e:
        logger.error(f"Failed to import server {server_name}: {e}")
        raise


async def list_tools(server_name: str) -> List[Dict[str, Any]]:
    """
    List all tools available on a server.

    Prefers JSON definitions from tool_definitions.json when available.
    Falls back to code-based definitions if JSON is missing or incomplete.

    Args:
        server_name: Name of the server

    Returns:
        List of tool definitions with name, description, and inputSchema
    """
    # Try JSON definitions first
    definitions = _load_tool_definitions()
    tools_from_json = definitions.get("tools", {}).get(server_name)

    if tools_from_json is not None:
        logger.debug(f"Using JSON definitions for {server_name}: {len(tools_from_json)} tools")
        return list(tools_from_json)  # Ensure return type matches signature

    # Fall back to code-based definitions
    logger.debug(f"Using code-based definitions for {server_name}")
    module = _load_server(server_name)

    # Call the handle_list_tools function
    tools = await module.handle_list_tools()

    # Convert MCP Tool objects to dictionaries
    return [
        {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
            "visible_to_customer": getattr(tool, "visible_to_customer", True),
        }
        for tool in tools
    ]


async def call_tool(server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Call a tool on an MCP server.

    Args:
        server_name: Name of the server
        tool_name: Name of the tool to call
        arguments: Tool arguments

    Returns:
        Dictionary with success status and result/error
        Note: Errors are sanitized to prevent technical details from leaking to users
    """
    full_tool_name = f"{server_name}_{tool_name}"

    try:
        module = _load_server(server_name)

        # Call the handle_call_tool function
        result = await module.handle_call_tool(tool_name, arguments)

        # Convert result to serializable format
        content = []
        for item in result:
            if hasattr(item, "text"):
                content.append({"type": "text", "text": item.text})
            elif hasattr(item, "data"):
                content.append({"type": item.type, "data": item.data})
            else:
                content.append({"type": "text", "text": str(item)})

        return {
            "success": True,
            "result": content,
            "server": server_name,
            "tool": tool_name,
        }

    except Exception as e:
        # Log the full technical error for debugging
        logger.error(f"Error calling {server_name}.{tool_name}: {e}", exc_info=True)

        # Return a sanitized error message to prevent technical details from reaching users
        sanitized_error = sanitize_error_for_tool_result(str(e), full_tool_name)

        return {
            "success": False,
            "error": sanitized_error,
            "server": server_name,
            "tool": tool_name,
        }


def get_server_configs() -> Dict[str, Dict[str, str]]:
    """
    Get configuration for all available servers.

    Returns:
        Dictionary mapping server names to their metadata
    """
    return {name: {"description": meta["description"]} for name, meta in SERVER_METADATA.items()}


def get_available_servers() -> List[str]:
    """
    Get list of available server names.

    Returns:
        List of server names
    """
    return list(SERVER_METADATA.keys())
