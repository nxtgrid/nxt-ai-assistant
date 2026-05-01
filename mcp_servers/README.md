# MCP Servers - Unified Python Environment

A comprehensive collection of Model Context Protocol (MCP) servers built with Python, featuring role-aware access control, environment-based action flags, and automatic server discovery.

## Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Server Management](#server-management)
- [Development Guide](#development-guide)
- [Claude Desktop Integration](#claude-desktop-integration)
- [Role-Based Access Control](#role-based-access-control)
- [Environment Configuration](#environment-configuration)

---

## Overview

### Available Servers

1. **🎯 JIRA Server** - JIRA API integration for issue management
2. **📝 Logs Server** - System log access and analysis
3. **📊 Codebase Server** - Code search and repository analysis
4. **⚡ VRM Server** - Victron Energy device monitoring
5. **🔌 Equipment Control Server** - IoT device control
6. **📈 Meters Server** - Smart meter data access
7. **📉 Grafana Server** - Dashboard panel rendering
8. **☀️ Solar Server** - Solar potential assessment using Global Solar Atlas API

> **Note on domain-specific servers:** `customer_server`, `vrm_server`, `meters_server`, and `jira_server` contain business logic specific to grid energy deployments. They are included as reference implementations showing how to build MCP servers against real infrastructure. You will likely want to replace them with servers for your own data sources. The `customer_server` meter write tools additionally require the Tiamat API (see main README for details).

### Key Features

✅ **Automatic Server Discovery** - Pattern-based detection, no manual registration required  
✅ **Role-Based Access Control** - Admin, Manager, Analyst, Viewer, Guest roles  
✅ **Environment-Based Action Flags** - Control write operations per server  
✅ **User-Scoped Operations** - Automatic data filtering by user context  
✅ **OAuth Token Caching** - Transparent authentication handling  
✅ **Claude Desktop Integration** - Direct integration with Claude Desktop app

---

## Architecture

### Directory Structure

```
mcp_servers/
├── servers/              # Individual MCP server implementations
│   ├── jira_server/
│   ├── meters_server/
│   ├── grafana_server/
│   └── ...
├── shared_code/          # Shared utilities and configurations
│   ├── auth/            # User context and authentication
│   ├── config/          # Action flags and settings
│   └── database/        # Database connection helpers
├── handler.py           # Serverless/HTTP entry point
├── local_bridge.py      # Local development bridge
├── mcp_launcher.py      # Server discovery and launcher
└── requirements.txt     # Python dependencies
```

### Server Discovery

Servers are automatically discovered using pattern-based detection:

- **Location**: `servers/{server_name}/`
- **Main File**: `{server_name}_mcp_server.py` (or any `*mcp_server.py`)
- **Example**: `servers/analytics_server/analytics_mcp_server.py`

No manual registration required - just follow the naming convention!

---

## Quick Start

### Prerequisites

- Python 3.11+
- Virtual environment (recommended)
- Git

### Installation

1. **Navigate to project:**
   ```bash
   cd mcp_servers
   ```

2. **Set up Python environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   ```

3. **Configure environment:**
   ```bash
   cp .env.example .env
   # Edit .env with your credentials
   ```

4. **Test server discovery:**
   ```bash
   python3 mcp_launcher.py --list
   ```

### Running Servers

**Option 1: Start All Servers**
```bash
./start_servers.sh
```

**Option 2: Start Individual Server**
```bash
PYTHONPATH=$PWD python3 servers/jira_server/jira_mcp_server.py
```

**Option 3: Use MCP Launcher**
```bash
python3 mcp_launcher.py
```

---

## Server Management

### Start/Stop Commands

| Action | Command | Description |
|--------|---------|-------------|
| **Start** | `./start_servers.sh` | Start all MCP servers |
| **Stop** | `./stop_servers.sh` | Stop all servers |
| **Restart** | `./restart_servers.sh` | Restart all servers |
| **Status** | `./status_servers.sh` | Check server status |

### Starting Servers

```bash
./start_servers.sh
```

The script will:
1. Create/activate virtual environment
2. Install dependencies
3. Start all discovered servers as background processes

### Stopping Servers

```bash
./stop_servers.sh
```

Gracefully stops all servers:
- Terminates all Python server processes
- Uses SIGTERM first, then SIGKILL if needed
- Cleans up background processes

### Monitoring Servers

```bash
# Check which servers are running
./status_servers.sh

# View logs
tail -f logs/servers.log
```

---

## Development Guide

### Creating a New MCP Server

#### 1. Create Server Directory

```bash
mkdir -p servers/my_server
cd servers/my_server
```

#### 2. Create Main Server File

File: `my_server_mcp_server.py`

```python
from mcp.server import Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types
import logging
import sys

# Configure logging (stderr for Claude Desktop visibility)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    handlers=[logging.StreamHandler(sys.stderr)]
)
logger = logging.getLogger("my-server-mcp")

# Create server instance
server = Server("my-server")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    """List available tools"""
    return [
        types.Tool(
            name="my_tool",
            description="Description of my tool",
            inputSchema={
                "type": "object",
                "properties": {
                    "param": {"type": "string", "description": "Parameter description"}
                },
                "required": ["param"]
            }
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    """Handle tool execution"""
    if name == "my_tool":
        result = f"Executed with param: {arguments.get('param')}"
        return [types.TextContent(type="text", text=result)]
    else:
        raise ValueError(f"Unknown tool: {name}")

async def main():
    """Run server using stdin/stdout streams"""
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="my-server",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
```

#### 3. Required Patterns

✅ **File Naming**: `{server_name}_mcp_server.py`  
✅ **Server Instance**: `server = Server("your-server-name")`  
✅ **Logging to stderr**: For Claude Desktop debugging  
✅ **Environment vars**: Use shared action flags if needed

#### 4. Add Action Control (Optional)

```python
from shared_code.config.action_flags import ActionFlags

# In your list_tools handler
actions_enabled = ActionFlags.is_actions_enabled("my_server")

if not actions_enabled:
    # Return read-only tools
    return read_only_tools
else:
    # Return all tools including write operations
    return all_tools
```

#### 5. Test Your Server

```bash
# Set PYTHONPATH for shared modules
PYTHONPATH=$PWD python3 servers/my_server/my_server_mcp_server.py

# Or use the launcher
python3 mcp_launcher.py --list
```

---

## Claude Desktop Integration

### Setup

1. **Locate Claude Desktop config:**

   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - Linux: `~/.config/Claude/claude_desktop_config.json`

2. **Update configuration:**

```json
{
  "mcpServers": {
    "jira-server": {
      "command": "/full/path/to/mcp_servers/.venv/bin/python3",
      "args": ["/full/path/to/mcp_servers/servers/jira_server/jira_mcp_server.py"],
      "env": {
        "JIRA_DOMAIN": "your-domain.atlassian.net",
        "JIRA_EMAIL": "your-email@example.com",
        "JIRA_API_TOKEN": "your-api-token",
        "JIRA_ACTIONS_ENABLED": "true"
      }
    },
    "grafana-server": {
      "command": "/full/path/to/mcp_servers/.venv/bin/python3",
      "args": ["/full/path/to/mcp_servers/servers/grafana_server/grafana_mcp_server.py"],
      "env": {
        "GRAFANA_URL": "https://your-grafana-instance",
        "GRAFANA_USERNAME": "your-username",
        "GRAFANA_PASSWORD": "your-password",
        "GRAFANA_ACTIONS_ENABLED": "true"
      }
    }
  }
}
```

3. **Restart Claude Desktop**

### Debugging

Check Claude Desktop logs:
- macOS: `~/Library/Logs/Claude/`
- Windows: `%APPDATA%\Claude\logs\`

Servers log to stderr, which is captured by Claude Desktop.

---

## Role-Based Access Control

### Role Hierarchy

```
Admin    → Full access to everything
Manager  → Read/write access, some admin functions
Analyst  → Read access + limited write access
Viewer   → Read-only access
Guest    → Minimal read access
```

### User Context

User context is passed via environment variables:

```bash
export USER_EMAIL="user@example.com"
export USER_ROLE="analyst"
export USER_ID="user-123"
export USER_GRIDS='["grid1", "grid2"]'  # For grid-based access
```

### Server-Specific Access

#### JIRA Server
- **Admin/Manager**: All tools including `jira_configure`
- **Analyst**: Read + limited analysis (no configure)
- **Viewer**: Read-only (`jira_search_issues`, `jira_get_issue`)

#### Meters Server
- **Grid-based access**: Users see only data from their assigned grids
- **Admin**: Access to all grids (`["*"]`)
- **Others**: Specific grid list

#### Grafana Server
- All roles can render panels
- Panel access controlled via `GRAFANA_ENABLED_PANELS` configuration

### Action Control

Control write operations via environment variables:

```bash
# Global control
export ACTIONS_ENABLED=false  # All servers read-only

# Per-server control
export JIRA_ACTIONS_ENABLED=true
export METERS_ACTIONS_ENABLED=true
export GRAFANA_ACTIONS_ENABLED=true
```

When `ACTIONS_ENABLED=false`:
- Write/delete tools are hidden from tool list
- Server returns read-only operations only
- Prevents accidental data modification

---

## Environment Configuration

### Required Variables

```bash
# Global Settings
ACTIONS_ENABLED=true           # Enable write operations globally
PYTHONPATH=/path/to/mcp_servers  # For shared module imports

# User Context (for role-aware operations)
USER_EMAIL=user@example.com
USER_ROLE=analyst
USER_ID=user-123
USER_GRIDS='["grid1", "grid2"]'
```

### Server-Specific Variables

#### JIRA Server
```bash
JIRA_DOMAIN=your-domain.atlassian.net
JIRA_EMAIL=your-email@example.com
JIRA_API_TOKEN=your-api-token
JIRA_ACTIONS_ENABLED=true
```

#### Grafana Server
```bash
GRAFANA_URL=https://your-grafana-instance
GRAFANA_USERNAME=your-username
GRAFANA_PASSWORD=your-password
GRAFANA_FOLDER_NAME=your-folder-name
GRAFANA_ACTIONS_ENABLED=true
```

#### Meters Server
```bash
METERS_API_URL=https://your-meters-api
METERS_ACTIONS_ENABLED=true
```

### .env File

Create `.env` file from example:

```bash
cp .env.example .env
# Edit .env with your values
```

The `.env` file is automatically loaded by the launcher and individual servers.

---

## Troubleshooting

### Server Won't Start

**Issue**: `ModuleNotFoundError: No module named 'shared_code'`

**Solution**: Set PYTHONPATH
```bash
export PYTHONPATH=$PWD
python3 servers/your_server/your_server_mcp_server.py
```

### Claude Desktop Can't Find Server

**Issue**: Server not showing in Claude Desktop

**Solutions**:
1. Check absolute paths in `claude_desktop_config.json`
2. Verify virtual environment Python path
3. Check Claude Desktop logs for errors
4. Restart Claude Desktop after config changes

### Permission Denied

**Issue**: User can't access certain tools

**Solutions**:
1. Check `USER_ROLE` environment variable
2. Verify role has permission for requested action
3. Check `ACTIONS_ENABLED` flag if trying write operations
4. Review server-specific role restrictions

### Connection Errors

**Issue**: Can't connect to database/API

**Solutions**:
1. Verify credentials in `.env`
2. Check network connectivity
3. Confirm service URLs are correct
4. Test credentials with direct API call

---

## Additional Resources

- **Shared Code**: See `shared_code/` directory for reusable utilities
- **Server Examples**: Check `servers/` for working implementations
- **Logs**: `logs/` directory contains server execution logs

For questions or issues, check the individual server README files in `servers/{server_name}/`.
