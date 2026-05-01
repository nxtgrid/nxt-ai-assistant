# Adding an MCP Server

This guide walks through adding a new MCP tool server from scratch — the files to create, where to register it, and how to define tools. It uses a hypothetical `weather` server as a running example.

## Concepts

- **MCP Server** — a Python module that exposes a set of tools Gemini can call during a conversation. Each server has a `handle_list_tools()` and a `handle_call_tool()` function.
- **Tool Definition** — a JSON entry in `mcp_servers/tool_definitions.json` that gives the tool its name, description, and input schema. This is the source of truth Gemini uses.
- **Server Registry** — `mcp_servers/server_registry.py` maps server names to their Python modules and lazily imports them.

## Step 1 — Create the Server Module

Create `mcp_servers/servers/weather_server/weather_mcp_server.py`:

```python
import logging
from typing import Any, Dict, List

import mcp.types as types

logger = logging.getLogger("weather-mcp-server")


async def handle_list_tools() -> List[types.Tool]:
    """Fallback: only used if this server has no entry in tool_definitions.json."""
    return []


async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    if name == "get_forecast":
        location = arguments.get("location", "")
        # Replace with your actual API call
        return [types.TextContent(type="text", text=f"Forecast for {location}: sunny")]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
```

Also create `mcp_servers/servers/weather_server/__init__.py` (empty file).

## Step 2 — Register the Server

Add an entry to `SERVER_METADATA` in `mcp_servers/server_registry.py`:

```python
"weather": {
    "description": "Weather forecasts and current conditions",
    "module": "servers.weather_server.weather_mcp_server",
},
```

Add it to `CONFIGURABLE_SERVERS` in `mcp_servers/shared_code/config/action_flags.py`:

```python
CONFIGURABLE_SERVERS = [
    ...
    "weather",
]
```

If your server name contains underscores (e.g., `solar_alerts`), also add it to `multi_word_servers` in `chat_orchestrator/orchestrator/services/tool_executor.py` (line ~62):

```python
multi_word_servers = [
    "equipment_diagnostics",
    "equipment_control",
    "payment_processor",
    "grid_design",
    "solar_alerts",  # add here if your name has underscores
]
```

Single-word server names (like `weather`) don't need this.

## Step 3 — Define Tools in JSON

Add a section to `mcp_servers/tool_definitions.json`. Tool names get the server name prepended when exposed to Gemini — name tools like `get_forecast` (not `weather_get_forecast`) to avoid `weather_weather_get_forecast`.

```json
{
  "tools": {
    ...
    "weather": [
      {
        "name": "get_forecast",
        "description": "Get a weather forecast for a location.",
        "inputSchema": {
          "type": "object",
          "properties": {
            "location": {
              "type": "string",
              "description": "City name or coordinates"
            },
            "days": {
              "type": "integer",
              "description": "Number of days to forecast (1–7)"
            }
          },
          "required": ["location"]
        },
        "visible_to_customer": false
      }
    ]
  }
}
```

Validate the file after editing:

```bash
python3 -c "import json; json.load(open('mcp_servers/tool_definitions.json')); print('OK')"
```

### Gemini inputSchema constraints (critical)

- Property types must be `string`, `integer`, `number`, `boolean`, or `array` with `items`.
- **Never use `"type": "object"` without nested `"properties"`** — Gemini rejects it with a 400 error that breaks every staff conversation's tool payload.
- If you need to pass a dict/JSON blob, declare it as `"type": "string"` and parse it in the handler.

## Step 4 — Enable/Disable Controls

**Server-level:** Set `WEATHER_ENABLED=false` in the environment to hide all weather tools.

**Per-tool:** Add `"weather:get_forecast"` to the `MCP_DISABLED_TOOLS` JSON array in env to disable one tool while keeping others enabled.

**Admin UI:** Settings → MCP Servers & Tools provides toggles without an environment variable change.

## Step 5 — Test Without the Full Stack

The `mcp_servers` venv has credentials for remote databases. `PYTHONPATH` must include both `mcp_servers/` and the repo root so `shared/` is importable:

```bash
cd mcp_servers && source .venv/bin/activate

PYTHONPATH=$PWD:.. python -c "
import asyncio, json
from servers.weather_server.weather_mcp_server import handle_call_tool

async def test():
    result = await handle_call_tool('get_forecast', {'location': 'Lagos'})
    for item in result:
        print(item.text)

asyncio.run(test())
"
```

## Injected Arguments (Security Model)

The `tool_executor` injects context fields into every tool call's `arguments` dict before forwarding to the server. These fields are **not visible to Gemini** and cannot be spoofed by the LLM:

| Field | Description |
|-------|-------------|
| `chat_id` | Telegram chat ID of the originating message |
| `topic_id` | Telegram topic/thread ID (groups) |
| `user_email` | Resolved email of the requesting user |
| `organization_id` | Resolved org ID |
| `session_id` | Chat session UUID |

Use these to scope queries to the right user or organization. **Never trust user-provided org or user IDs from LLM arguments for access decisions.**

## Returning Results

Handlers return `List[types.TextContent]`. For most tools, one text item is enough:

```python
return [types.TextContent(type="text", text=json.dumps(result, default=str))]
```

For multi-part responses (e.g., text + image):

```python
return [
    types.TextContent(type="text", text="Here is the chart:"),
    types.ImageContent(type="image", data=base64_png, mimeType="image/png"),
]
```

Return errors as text — never raise exceptions from `handle_call_tool`. The server registry catches exceptions, sanitizes them, and returns a generic error message to prevent internal details from reaching the LLM or users.

## Checklist

- [ ] `mcp_servers/servers/weather_server/weather_mcp_server.py` created with `handle_list_tools` and `handle_call_tool`
- [ ] `mcp_servers/servers/weather_server/__init__.py` created (empty)
- [ ] Entry added to `SERVER_METADATA` in `server_registry.py`
- [ ] Entry added to `CONFIGURABLE_SERVERS` in `action_flags.py`
- [ ] Tool definitions added to `tool_definitions.json` (no object types without properties)
- [ ] JSON validated: `python3 -c "import json; json.load(open('mcp_servers/tool_definitions.json')); print('OK')"`
- [ ] Server name with underscores? Add to `multi_word_servers` in `tool_executor.py`
- [ ] Tested directly via `PYTHONPATH=$PWD:.. python -c "..."`
