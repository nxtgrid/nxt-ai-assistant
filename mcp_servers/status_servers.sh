#!/bin/bash
# Status check script for MCP servers

echo "📊 MCP Servers Status"
echo "=================================="
echo ""

# Check Docker containers
if command -v docker &> /dev/null; then
    docker_containers=$(docker ps --format '{{.Names}}' | grep 'mcp_servers' 2>/dev/null)
    if [ ! -z "$docker_containers" ]; then
        echo "🐳 Docker Containers:"
        docker ps --filter "name=mcp_servers" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
        echo ""
    fi
fi

# Check Python server processes
echo "🐍 Python Server Processes:"
server_procs=$(ps aux | grep -E 'python.*servers/.*/.*_mcp_server.py' | grep -v grep)
if [ ! -z "$server_procs" ]; then
    echo "$server_procs" | awk '{print "  PID:", $2, "| Server:", $NF}'
    echo ""
    echo "  Total running: $(echo "$server_procs" | wc -l | tr -d ' ') servers"
else
    echo "  ℹ️  No Python servers running"
fi
echo ""

# Check MCP launcher
echo "🚀 MCP Launcher:"
launcher_procs=$(ps aux | grep 'mcp_launcher.py' | grep -v grep)
if [ ! -z "$launcher_procs" ]; then
    echo "$launcher_procs" | awk '{print "  PID:", $2, "| Status: Running"}'
else
    echo "  ℹ️  Not running"
fi
echo ""

# Check Gemini bridge
echo "🌉 Gemini Bridge:"
bridge_procs=$(ps aux | grep 'gemini_bridge.py' | grep -v grep)
if [ ! -z "$bridge_procs" ]; then
    echo "$bridge_procs" | awk '{print "  PID:", $2, "| Status: Running"}'
else
    echo "  ℹ️  Not running"
fi
echo ""

# Check listening ports
echo "🔌 Listening Ports:"
if command -v lsof &> /dev/null; then
    # Check common MCP server ports
    for port in 8000 8001 8002 8003 8080; do
        port_info=$(lsof -i :$port -sTCP:LISTEN 2>/dev/null)
        if [ ! -z "$port_info" ]; then
            echo "  Port $port: $(echo "$port_info" | tail -1 | awk '{print $1, "(PID:", $2")"}')"
        fi
    done

    # If no ports found
    if ! lsof -i :8000-8003,8080 -sTCP:LISTEN &>/dev/null; then
        echo "  ℹ️  No services listening on standard ports"
    fi
else
    echo "  ⚠️  lsof not available, cannot check ports"
fi
echo ""

# Summary
echo "=================================="
total_processes=0
if [ ! -z "$server_procs" ]; then
    total_processes=$((total_processes + $(echo "$server_procs" | wc -l | tr -d ' ')))
fi
if [ ! -z "$launcher_procs" ]; then
    total_processes=$((total_processes + 1))
fi
if [ ! -z "$bridge_procs" ]; then
    total_processes=$((total_processes + 1))
fi

if [ $total_processes -gt 0 ]; then
    echo "✅ $total_processes MCP services running"
else
    echo "❌ No MCP services running"
    echo ""
    echo "💡 Start servers with: ./start_servers.sh"
fi
