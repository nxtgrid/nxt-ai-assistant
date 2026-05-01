#!/bin/bash
# Startup script for MCP servers

echo "🚀 Starting MCP Servers Environment"
echo "=================================="

# Check if virtual environment exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Install dependencies if needed
echo "Installing dependencies..."
pip install -r requirements.txt

# Create logs directory
mkdir -p logs

# Check if Docker is available
if command -v docker &> /dev/null; then
    echo "Docker detected. Starting with Docker Compose..."
    if command -v docker-compose &> /dev/null; then
        docker-compose up -d
    else
        docker compose up -d
    fi
    echo "✅ Services started with Docker"
    echo "📊 Check status with: docker-compose ps"
    echo "📋 View logs with: docker-compose logs -f"
else
    echo "Docker not found. Starting servers locally..."
    echo "⚠️  Note: You'll need to set up databases manually for full functionality"

    # Start servers in background
    echo "Starting Jira Server..."
    python servers/jira_server/jira_mcp_server.py &

    echo "Starting Meters Server..."
    python servers/meters_server/meters_mcp_server.py &

    echo "Starting Grafana Server..."
    python servers/grafana_server/grafana_mcp_server.py &

    echo "Starting MCP List Service on port 8000..."
    python mcp_launcher.py --api --port 8000 &

    echo "✅ All servers started locally"
    echo "📊 Check running processes with: ps aux | grep python"
    echo "🛑 Stop servers with: pkill -f 'python servers/' || pkill -f 'mcp_launcher.py'"
fi

echo ""
echo "🎉 MCP Servers are ready!"
echo "📖 See README.md for detailed usage instructions"
echo "🔧 Configure your .env file with API keys and database URLs"
