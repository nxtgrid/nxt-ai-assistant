#!/usr/bin/env python3
"""
Action Flag Configuration for MCP Servers

Manages environment-based action control flags for each MCP server.

Server-level control:
    - {SERVER_NAME}_ENABLED: Enable/disable entire server (default: true)
    - {SERVER_NAME}_ACTIONS_ENABLED: Legacy flag for write operations

Tool-level control:
    - MCP_DISABLED_TOOLS: JSON array of "server:tool" strings to disable specific tools
"""

import json
import logging
import os
from enum import Enum
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# Servers that can be disabled entirely via {SERVER_NAME}_ENABLED env var
# This list should match SERVER_METADATA in server_registry.py
CONFIGURABLE_SERVERS = [
    "equipment_diagnostics",
    "vrm",
    "jira",
    "codebase",
    "logs",
    "meters",
    "equipment_control",
    "payment_processor",
    "customer",
    "grafana",
    "schedule",
    "meta",
    "grid_design",
    "solar",
    "knowledge",
    "messaging",
    "reference",  # Nigerian import regulatory data (staff only)
]


class ServerName(Enum):
    """Standard server names for environment variables"""

    JIRA = "JIRA"


class ActionFlags:
    """Manages action control flags from environment variables"""

    # Cache for disabled tools (parsed once per process)
    _disabled_tools_cache: Optional[Set[str]] = None
    _disabled_tools_cache_key: Optional[str] = None

    @staticmethod
    def get_env_var_name(server_name: str) -> str:
        """
        Get environment variable name for server action flag
        Pattern: {SERVER_NAME}_ACTIONS_ENABLED

        Examples:
        - JIRA_ACTIONS_ENABLED
        - GRAFANA_ACTIONS_ENABLED
        """
        return f"{server_name.upper()}_ACTIONS_ENABLED"

    @staticmethod
    def is_server_enabled(server_name: str) -> bool:
        """
        Check if an MCP server is enabled.

        Uses {SERVER_NAME}_ENABLED env var. Default: true (enabled).

        Args:
            server_name: Server name (e.g., "jira", "grafana")

        Returns:
            bool: True if server is enabled, False if explicitly disabled
        """
        env_var = f"{server_name.upper()}_ENABLED"
        env_value = os.getenv(env_var, "true").lower()
        enabled = env_value in ["true", "1", "yes", "on"]

        if not enabled:
            logger.debug(f"Server {server_name} is disabled via {env_var}")

        return enabled

    @staticmethod
    def _get_disabled_tools() -> Set[str]:
        """Get set of disabled tools from MCP_DISABLED_TOOLS env var.

        Parses JSON array once and caches result. Format: ["server:tool", ...]

        Returns:
            Set of "server:tool" strings that are disabled
        """
        disabled_tools_json = os.getenv("MCP_DISABLED_TOOLS", "[]")

        # Check cache
        if (
            ActionFlags._disabled_tools_cache is not None
            and ActionFlags._disabled_tools_cache_key == disabled_tools_json
        ):
            return ActionFlags._disabled_tools_cache

        # Parse and cache
        try:
            disabled_list = json.loads(disabled_tools_json)
            if not isinstance(disabled_list, list):
                logger.warning("MCP_DISABLED_TOOLS is not a list, ignoring")
                disabled_list = []
            ActionFlags._disabled_tools_cache = set(disabled_list)
            ActionFlags._disabled_tools_cache_key = disabled_tools_json
            if ActionFlags._disabled_tools_cache:
                logger.info(f"Loaded {len(ActionFlags._disabled_tools_cache)} disabled tools")
            return ActionFlags._disabled_tools_cache
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse MCP_DISABLED_TOOLS: {e}")
            ActionFlags._disabled_tools_cache = set()
            ActionFlags._disabled_tools_cache_key = disabled_tools_json
            return ActionFlags._disabled_tools_cache

    @staticmethod
    def is_tool_disabled(server_name: str, tool_name: str) -> bool:
        """
        Check if a specific tool is disabled.

        Uses MCP_DISABLED_TOOLS env var (JSON array of "server:tool" strings).

        Args:
            server_name: Server name (e.g., "jira")
            tool_name: Tool name (e.g., "jira_add_comment")

        Returns:
            bool: True if the specific tool is disabled
        """
        disabled_tools = ActionFlags._get_disabled_tools()
        tool_key = f"{server_name}:{tool_name}"
        is_disabled = tool_key in disabled_tools

        if is_disabled:
            logger.debug(f"Tool {tool_key} is disabled")

        return is_disabled

    @staticmethod
    def get_disabled_tools_list() -> List[str]:
        """Get list of all disabled tools.

        Returns:
            List of "server:tool" strings
        """
        return list(ActionFlags._get_disabled_tools())

    @staticmethod
    def is_actions_enabled(server_name: str) -> bool:
        """
        Check if actions are enabled for a specific server

        Args:
            server_name: Server name (e.g., "jira", "grafana")

        Returns:
            bool: True if actions are enabled, False otherwise
            Default: True (actions enabled) if environment variable not set
        """
        env_var = ActionFlags.get_env_var_name(server_name)
        env_value = os.getenv(env_var, "true").lower()

        # Consider "true", "1", "yes", "on" as enabled
        enabled = env_value in ["true", "1", "yes", "on"]

        logger.debug(f"Action flag for {server_name}: {env_var}={env_value} -> {enabled}")
        return enabled

    @staticmethod
    def get_all_action_flags() -> Dict[str, bool]:
        """Get action flags for all known servers"""
        servers = ["jira", "grafana", "meters"]
        return {server: ActionFlags.is_actions_enabled(server) for server in servers}

    @staticmethod
    def set_actions_enabled(server_name: str, enabled: bool):
        """
        Set action flag for a server (for testing/runtime configuration)

        Args:
            server_name: Server name
            enabled: Whether to enable actions
        """
        env_var = ActionFlags.get_env_var_name(server_name)
        os.environ[env_var] = "true" if enabled else "false"
        logger.info(f"Set {env_var}={enabled}")


class WriteOperationChecker:
    """Helper to identify write operations for each server type"""

    # Define write operations by server
    WRITE_OPERATIONS = {
        "jira": {
            "tools": [
                "jira_configure",
                "jira_create_issue",
                "jira_update_issue",
                "jira_add_comment",
                "jira_change_status",
            ],
            "keywords": [
                "configure",
                "create",
                "update",
                "assign",
                "transition",
                "add_comment",
                "change_status",
            ],
        },
        "meters": {
            "tools": [],
            "keywords": ["send", "write", "control"],
        },
        "grafana": {
            "tools": [],
            "keywords": ["render"],
        },
    }

    @staticmethod
    def is_write_operation(server_name: str, tool_name: str) -> bool:
        """
        Check if a tool performs write operations

        Args:
            server_name: Server name (e.g., "supabase")
            tool_name: Tool name (e.g., "insert_data")

        Returns:
            bool: True if it's a write operation
        """
        server_name = server_name.replace("_server", "").lower()

        if server_name not in WriteOperationChecker.WRITE_OPERATIONS:
            # Fallback to keyword checking
            tool_lower = tool_name.lower()
            write_keywords = [
                "insert",
                "update",
                "delete",
                "create",
                "send",
                "generate",
                "configure",
                "set",
            ]
            return any(keyword in tool_lower for keyword in write_keywords)

        server_config = WriteOperationChecker.WRITE_OPERATIONS[server_name]

        # Check explicit tool names first
        if tool_name in server_config["tools"]:
            return True

        # Check keywords
        tool_lower = tool_name.lower()
        return any(keyword in tool_lower for keyword in server_config["keywords"])


# Global action flags instance
action_flags = ActionFlags()
