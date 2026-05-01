#!/usr/bin/env python3
"""
Gemini Bridge for MCP Servers

Converts MCP servers to REST API endpoints that Gemini can call
via function calling or extensions.

Uses direct imports for better performance (no subprocess spawning).

IMPORTANT: All errors returned to clients are sanitized to prevent
technical details from leaking to end users or the LLM.
"""

import logging
import os
from typing import Any, Dict

import uvicorn
from fastapi import FastAPI, HTTPException, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import APIKeyHeader
from server_registry import call_tool as registry_call_tool
from server_registry import get_available_servers, get_server_configs
from server_registry import list_tools as registry_list_tools

from shared.utils.error_sanitizer import sanitize_error

# API Key security
API_KEY_HEADER = APIKeyHeader(name="X-API-Key", auto_error=False)


def get_api_key():
    """Get API key from environment."""
    return os.getenv("API_KEY", "")


async def verify_api_key(api_key: str = Security(API_KEY_HEADER)):
    """Verify the API key from request header."""
    expected_key = get_api_key()
    if not expected_key:
        raise HTTPException(
            status_code=500,
            detail="API_KEY not configured on server",
        )
    if not api_key or api_key != expected_key:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key",
            headers={"WWW-Authenticate": "API key"},
        )
    return True


# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("gemini-bridge")

app = FastAPI(
    title="Gemini MCP Bridge",
    description="REST API bridge to convert MCP servers for Gemini integration",
    version="2.0.0",
)

# Enable CORS — bridge is called server-to-server (Python imports in production),
# but restrict origins in case it is ever exposed directly.
_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:8501")
_cors_origins = [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["X-API-Key", "Content-Type"],
)


@app.get("/")
async def root():
    """Root endpoint with service information"""
    return {
        "service": "Gemini MCP Bridge",
        "version": "2.0.0",
        "description": "REST API bridge for MCP servers to work with Gemini (direct imports)",
        "available_servers": get_available_servers(),
        "endpoints": {
            "list_servers": "/servers",
            "list_tools": "/servers/{server_name}/tools",
            "call_tool": "/servers/{server_name}/tools/{tool_name}",
        },
    }


@app.get("/servers", dependencies=[Security(verify_api_key)])
async def list_servers():
    """List all available MCP servers. Requires X-API-Key header."""
    configs = get_server_configs()
    return {
        "servers": {
            name: {"description": config["description"]} for name, config in configs.items()
        }
    }


@app.get("/servers/{server_name}/tools", dependencies=[Security(verify_api_key)])
async def list_tools(server_name: str):
    """List all tools for a specific server. Requires X-API-Key header."""
    try:
        tools = await registry_list_tools(server_name)
        return {"server": server_name, "tools": tools}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error(f"Error listing tools for {server_name}: {e}", exc_info=True)
        # Sanitize error before returning to client
        sanitized = sanitize_error(str(e), context=f"list_tools:{server_name}")
        raise HTTPException(status_code=500, detail=sanitized)


@app.post("/servers/{server_name}/tools/{tool_name}", dependencies=[Security(verify_api_key)])
async def call_tool(server_name: str, tool_name: str, arguments: Dict[str, Any] = None):
    """Call a specific tool on a server. Requires X-API-Key header."""
    if arguments is None:
        arguments = {}

    try:
        result = await registry_call_tool(server_name, tool_name, arguments)
        if result["success"]:
            return result
        else:
            # Error from registry is already sanitized
            raise HTTPException(status_code=400, detail=result["error"])
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except HTTPException:
        # Re-raise HTTPExceptions as-is
        raise
    except Exception as e:
        logger.error(f"Error calling {server_name}.{tool_name}: {e}", exc_info=True)
        # Sanitize error before returning to client
        sanitized = sanitize_error(str(e), context=f"{server_name}.{tool_name}")
        raise HTTPException(status_code=500, detail=sanitized)


# Gemini-specific endpoints with simplified schemas
@app.get("/gemini/functions", dependencies=[Security(verify_api_key)])
async def gemini_functions():
    """
    Get all available functions in Gemini function calling format.
    Use this endpoint to register functions with Gemini.
    Requires X-API-Key header.
    """
    functions = []

    for server_name in get_available_servers():
        try:
            tools = await registry_list_tools(server_name)
            for tool in tools:
                # Convert MCP tool schema to Gemini function schema
                function_def = {
                    "name": f"{server_name}_{tool['name']}",
                    "description": f"[{server_name.upper()}] {tool['description']}",
                    "parameters": tool.get("inputSchema", {}),
                    "visible_to_customer": tool.get("visible_to_customer", True),
                }
                functions.append(function_def)
        except Exception as e:
            logger.warning(f"Could not load tools for {server_name}: {e}")

    return {"functions": functions, "total_count": len(functions), "call_endpoint": "/gemini/call"}


@app.post("/gemini/call", dependencies=[Security(verify_api_key)])
async def gemini_call(function_call: Dict[str, Any]):
    """
    Execute function calls from Gemini.
    Expected format: {"name": "server_tool", "arguments": {...}}
    Requires X-API-Key header.
    """
    function_name = function_call.get("name", "")
    arguments = function_call.get("arguments", {})

    # Parse server and tool name by matching against known server names
    # Server names can contain underscores (e.g., "equipment_control")
    # So we find the longest matching server prefix
    if "_" not in function_name:
        raise HTTPException(
            status_code=400, detail="Invalid function name format. Expected: server_toolname"
        )

    available_servers = get_available_servers()
    server_name = None
    tool_name = None

    # Sort by length descending to match longest prefix first
    # e.g., "equipment_control" before "equipment"
    for srv in sorted(available_servers, key=len, reverse=True):
        prefix = f"{srv}_"
        if function_name.startswith(prefix):
            server_name = srv
            tool_name = function_name[len(prefix) :]
            break

    if not server_name or not tool_name:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown server prefix in function name: {function_name}",
        )

    try:
        result = await registry_call_tool(server_name, tool_name, arguments)

        # Format response for Gemini
        if result["success"]:
            return {
                "result": result["result"],
                "function_name": function_name,
                "execution_success": True,
            }
        else:
            # Error from registry is already sanitized
            return {
                "error": result["error"],
                "function_name": function_name,
                "execution_success": False,
            }
    except Exception as e:
        logger.error(f"Error executing {function_name}: {e}", exc_info=True)
        # Sanitize error before returning to client
        sanitized = sanitize_error(str(e), context=function_name)
        raise HTTPException(status_code=500, detail=sanitized)


if __name__ == "__main__":
    print("🌉 Starting Gemini MCP Bridge (Direct Import Mode)")
    print("📊 Available endpoints:")
    print("   - Bridge API: http://localhost:8080")
    print("   - Gemini Functions: http://localhost:8080/gemini/functions")
    print("   - Function Calls: POST http://localhost:8080/gemini/call")
    print(f"🔧 Available servers: {', '.join(get_available_servers())}")

    uvicorn.run(app, host="0.0.0.0", port=8080)
