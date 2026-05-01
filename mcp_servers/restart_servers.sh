#!/bin/bash
# Restart script for MCP servers

echo "🔄 Restarting MCP Servers"
echo "=================================="

# Stop servers first
echo "Step 1: Stopping existing servers..."
./stop_servers.sh

# Wait a moment for cleanup
echo ""
echo "Waiting for cleanup..."
sleep 2

# Start servers again
echo ""
echo "Step 2: Starting servers..."
./start_servers.sh
