#!/bin/bash
# Stop script for MCP servers

echo "🛑 Stopping MCP Servers"
echo "=================================="

# Check if Docker is running
if command -v docker &> /dev/null; then
    # Check if docker-compose is running
    if docker ps --format '{{.Names}}' | grep -q 'mcp_servers'; then
        echo "Stopping Docker containers..."
        if command -v docker-compose &> /dev/null; then
            docker-compose down
        else
            docker compose down
        fi
        echo "✅ Docker containers stopped"
    fi
fi

# Stop locally running Python servers
echo "Stopping locally running MCP servers..."

# Find and kill Python server processes
pids=$(ps aux | grep -E 'python.*servers/.*/.*_mcp_server.py' | grep -v grep | awk '{print $2}')
if [ ! -z "$pids" ]; then
    echo "Found running server processes: $pids"
    echo "$pids" | xargs kill -TERM 2>/dev/null
    sleep 2

    # Force kill if still running
    pids_remaining=$(ps aux | grep -E 'python.*servers/.*/.*_mcp_server.py' | grep -v grep | awk '{print $2}')
    if [ ! -z "$pids_remaining" ]; then
        echo "Force killing remaining processes: $pids_remaining"
        echo "$pids_remaining" | xargs kill -9 2>/dev/null
    fi

    echo "✅ Server processes stopped"
else
    echo "ℹ️  No running server processes found"
fi

# Stop mcp_launcher if running
launcher_pids=$(ps aux | grep 'mcp_launcher.py' | grep -v grep | awk '{print $2}')
if [ ! -z "$launcher_pids" ]; then
    echo "Stopping MCP launcher: $launcher_pids"
    echo "$launcher_pids" | xargs kill -TERM 2>/dev/null
    sleep 1
    echo "✅ MCP launcher stopped"
fi

# Stop gemini_bridge if running
bridge_pids=$(ps aux | grep 'gemini_bridge.py' | grep -v grep | awk '{print $2}')
if [ ! -z "$bridge_pids" ]; then
    echo "Stopping Gemini bridge: $bridge_pids"
    echo "$bridge_pids" | xargs kill -TERM 2>/dev/null
    sleep 1
    echo "✅ Gemini bridge stopped"
fi

echo ""
echo "🎉 All MCP servers stopped!"
echo ""
echo "📊 Verify with: ps aux | grep python | grep -E '(servers|mcp_launcher|gemini_bridge)'"
