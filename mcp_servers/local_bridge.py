#!/usr/bin/env python3
"""
Local MCP Bridge for Testing

This runs the MCP bridge handler as a local HTTP server
so you can test with Postman before deploying to DigitalOcean.

Usage:
    python local_bridge.py

Then test with Postman:
    POST http://localhost:8001/
    Body: {"server_name": "jira", "tool_name": "search_issues", "arguments": {...}}
"""

import asyncio
import json
import sys
from pathlib import Path

from aiohttp import web

# Add handler to path
sys.path.insert(0, str(Path(__file__).parent))
from handler import main as bridge_handler


async def handle_request(request):
    """Handle HTTP POST request."""
    try:
        # Get JSON body
        body = await request.json()

        # Call bridge handler in a new event loop thread
        # This is needed because handler.py may use asyncio.run() internally
        import concurrent.futures

        loop = asyncio.get_event_loop()

        with concurrent.futures.ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(pool, bridge_handler, body)

        # Return result
        return web.json_response(result, status=result.get("statusCode", 200))

    except json.JSONDecodeError:
        return web.json_response({"success": False, "error": "Invalid JSON"}, status=400)
    except Exception as e:
        return web.json_response({"success": False, "error": str(e)}, status=500)


async def health_check(request):
    """Health check endpoint."""
    from datetime import datetime

    return web.json_response(
        {
            "status": "healthy",
            "service": "tools-service",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
    )


def main():
    """Run local server."""
    app = web.Application()
    app.router.add_post("/", handle_request)
    app.router.add_get("/health", health_check)

    print("🚀 MCP Bridge running at http://localhost:8001")
    print("📝 Test with Postman:")
    print("   POST http://localhost:8001/")
    print('   Body: {"action": "list_servers"}')
    print()
    print("   POST http://localhost:8001/")
    print('   Body: {"server_name": "jira", "action": "list_tools"}')
    print()

    web.run_app(app, host="localhost", port=8001)


if __name__ == "__main__":
    main()
