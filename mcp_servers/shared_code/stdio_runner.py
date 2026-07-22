"""Shared stdio entrypoint for MCP servers.

Collapses the boilerplate every server's own ``main()`` hand-rolled: connect
stdio, run the server loop, print start/fatal-error messages to stderr, and
always run cleanup. Each server still owns its own ``main()`` (and the
``if __name__ == "__main__":`` block), so per-server preambles (e.g. an
async DB init before the server can accept connections) stay in place.
"""

import sys
import traceback
from typing import Awaitable, Callable, Optional

import mcp.server.stdio
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities


async def run_stdio_server(
    server: Server,
    name: str,
    version: str = "1.0.0",
    *,
    capabilities: Optional[object] = None,
    label: Optional[str] = None,
    on_startup: Optional[Callable[[], Awaitable[None]]] = None,
    on_cleanup: Optional[Callable[[], Awaitable[None]]] = None,
) -> None:
    label = label or name
    try:
        print(f"✅ {label} server initialized successfully", file=sys.stderr)
        if on_startup is not None:
            await on_startup()
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name=name,
                    server_version=version,
                    capabilities=capabilities or ServerCapabilities(),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in {label} server: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        raise
    finally:
        if on_cleanup is not None:
            await on_cleanup()
