#!/usr/bin/env python3
"""
Export all MCP tool definitions to JSON.

This script runs handle_list_tools() for each server and exports the complete
tool definitions to mcp_servers/tool_definitions.json.

Usage:
    cd mcp_servers && source .venv/bin/activate
    python scripts/export_tools.py
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add parent directory and repo root to path for imports
repo_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(repo_root))
sys.path.insert(0, str(repo_root / "mcp_servers"))

from dotenv import load_dotenv

load_dotenv()

from server_registry import SERVER_METADATA, _load_server

# Servers whose manifest is computed at runtime and must NOT be frozen into
# JSON: server_registry.list_tools prefers the JSON entry when present, so
# exporting a snapshot would permanently override the live list.
#   - grafana builds its tools from dashboard metadata in the DB (hot-reloaded)
DYNAMIC_MANIFEST_SERVERS = {"grafana"}

def _force_full_manifests() -> None:
    """Make flag-gated tool lists export completely.

    handle_list_tools() output can depend on {SERVER}_ACTIONS_ENABLED (e.g.
    jira hides its action tools when disabled). Exporting with a flag off
    would silently drop those tools from the manifest prod serves, so the
    export always runs with actions enabled.
    """
    for server_name in SERVER_METADATA:
        os.environ[f"{server_name.upper()}_ACTIONS_ENABLED"] = "true"


async def export_all_tools() -> dict:
    """Export all tool definitions from all servers."""
    _force_full_manifests()

    tools_by_server: dict[str, list] = {}

    total_tools = 0
    errors = []

    for server_name in SERVER_METADATA.keys():
        if server_name in DYNAMIC_MANIFEST_SERVERS:
            print(f"Skipping {server_name} (runtime-computed manifest)")
            continue
        print(f"Exporting {server_name}...", end=" ")
        try:
            module = _load_server(server_name)
            tools = await module.handle_list_tools()

            tool_list = []
            for tool in tools:
                tool_dict = {
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.inputSchema,
                    "visible_to_customer": getattr(tool, "visible_to_customer", True),
                }
                # Preserve non-standard flags (persistent_only, command_gated, ...)
                # — user_permissions filters on them, so dropping them would
                # silently widen a tool's exposure.
                extras = getattr(tool, "model_extra", None) or {}
                for key, value in extras.items():
                    tool_dict.setdefault(key, value)

                tool_list.append(tool_dict)

            tools_by_server[server_name] = tool_list
            print(f"{len(tool_list)} tools")
            total_tools += len(tool_list)

        except Exception as e:
            print(f"ERROR: {e}")
            errors.append({"server": server_name, "error": str(e)})

    print(f"\nTotal: {total_tools} tools exported from {len(tools_by_server)} servers")
    if errors:
        print(f"Errors: {len(errors)}")
        for err in errors:
            print(f"  - {err['server']}: {err['error']}")

    return {
        "$schema": "tool_definitions_schema.json",
        "version": "1.0.0",
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tools": tools_by_server,
    }


def main():
    output = asyncio.run(export_all_tools())

    output_path = Path(__file__).parent.parent / "tool_definitions.json"
    with open(output_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"\nWritten to: {output_path}")


if __name__ == "__main__":
    main()
