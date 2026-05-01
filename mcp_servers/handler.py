"""
DigitalOcean Serverless Function Handler for MCP Bridge

This module provides a serverless function entry point for calling MCP servers.
Each invocation starts the requested MCP server, executes the tool, and returns the result.
"""

import asyncio
import logging
import os
import sys
from typing import Any, Dict

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bridge-handler")

# Server configurations
SERVER_CONFIGS = {
    "jira": {
        "module": "servers.jira_server.jira_mcp_server",
        "description": "Jira analysis, comment processing, and JSM Ops on-call schedule management (get on-call info for any date, create on-call overrides)",
    },
    "vrm": {
        "module": "servers.vrm_server.vrm_mcp_server",
        "description": "Victron VRM monitoring and control",
    },
    "meters": {
        "module": "servers.meters_server.meters_mcp_server",
        "description": "Meters data operations",
    },
    "codebase": {
        "module": "servers.codebase_server.codebase_mcp_server",
        "description": "Codebase analysis and PR tracking",
    },
    "logs": {
        "module": "servers.logs_server.logs_mcp_server",
        "description": "Intelligent log analysis with Loki and vector search",
    },
    "grafana": {
        "module": "servers.grafana_server.grafana_mcp_server",
        "description": "Grafana dashboard panel rendering",
    },
    "schedule": {
        "module": "servers.schedule_server.schedule_mcp_server",
        "description": "User command scheduling for future or recurring execution (staff only)",
    },
}


def main(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Main serverless function handler for MCP bridge.

    Args:
        args: Function arguments containing:
            - server_name: Name of the MCP server to call
            - tool_name: Name of the tool to execute
            - arguments: Arguments to pass to the tool (optional)
            - action: Special actions like 'list_tools' or 'list_servers' (optional)

    Returns:
        Response containing:
            - success: Boolean indicating success
            - result: Tool execution result (if successful)
            - error: Error message (if failed)
    """
    try:
        action = args.get("action")

        # Handle list servers action
        if action == "list_servers":
            return {
                "success": True,
                "servers": {
                    name: {"description": config["description"]}
                    for name, config in SERVER_CONFIGS.items()
                },
                "statusCode": 200,
            }

        # Extract parameters for tool execution
        server_name = args.get("server_name")
        tool_name = args.get("tool_name")
        arguments = args.get("arguments", {})

        if not server_name:
            return {
                "success": False,
                "error": "server_name is required",
                "statusCode": 400,
            }

        if server_name not in SERVER_CONFIGS:
            return {
                "success": False,
                "error": f"Unknown server: {server_name}. Available: {list(SERVER_CONFIGS.keys())}",
                "statusCode": 404,
            }

        # Handle list tools action
        if action == "list_tools" or not tool_name:
            tools = asyncio.run(_list_server_tools(server_name))
            return {
                "success": True,
                "server": server_name,
                "tools": tools,
                "statusCode": 200,
            }

        # Execute tool call
        logger.info(f"Calling {server_name}.{tool_name} with args: {arguments}")
        result = asyncio.run(_call_mcp_tool(server_name, tool_name, arguments))

        return {
            "success": True,
            "result": result,
            "server": server_name,
            "tool": tool_name,
            "statusCode": 200,
        }

    except Exception as e:
        logger.exception(f"Error processing request: {e}")
        return {
            "success": False,
            "error": str(e),
            "statusCode": 500,
        }


async def _call_mcp_tool(server_name: str, tool_name: str, arguments: Dict[str, Any]) -> Any:
    """Call a tool on an MCP server."""
    config = SERVER_CONFIGS[server_name]

    # Use python3 to run the module
    python_path = sys.executable
    module_path = config["module"].replace(".", "/") + ".py"

    server_params = StdioServerParameters(
        command=python_path,
        args=[module_path],
        env=os.environ.copy(),
    )

    logger.info(f"Starting MCP server: {server_name} ({module_path})")

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.info(f"MCP session initialized for {server_name}")

                # Call the tool
                result = await session.call_tool(tool_name, arguments)
                logger.info(f"Tool {tool_name} executed successfully")

                # Extract content from result
                if hasattr(result, "content"):
                    return result.content
                return result

    except Exception as e:
        logger.error(f"Error calling {server_name}.{tool_name}: {e}")
        raise


async def _list_server_tools(server_name: str) -> list:
    """List all tools available on a server."""
    config = SERVER_CONFIGS[server_name]

    # Use python3 to run the module
    python_path = sys.executable
    module_path = config["module"].replace(".", "/") + ".py"

    server_params = StdioServerParameters(
        command=python_path,
        args=[module_path],
        env=os.environ.copy(),
    )

    try:
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools_result = await session.list_tools()
                return [
                    {
                        "name": tool.name,
                        "description": tool.description,
                        "inputSchema": tool.inputSchema,
                        # By default, tools are not visible to customers (staff-only)
                        # Individual tools can override this in their annotations
                        "visible_to_customer": getattr(tool, "visible_to_customer", False),
                    }
                    for tool in tools_result.tools
                ]

    except Exception as e:
        logger.error(f"Error listing tools for {server_name}: {e}")
        return []


# For local testing
if __name__ == "__main__":
    # Test list servers
    print("Testing list_servers:")
    result = main({"action": "list_servers"})
    print(result)

    # Test list tools
    print("\nTesting list_tools for jira:")
    result = main({"action": "list_tools", "server_name": "jira"})
    print(result)
