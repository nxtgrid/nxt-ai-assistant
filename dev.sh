#!/bin/bash
# =============================================================================
# Local Development Script for Anansi
# Starts all services for local development without Docker
# =============================================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Anansi Local Development${NC}"
echo -e "${GREEN}========================================${NC}"

# Check for .env file
if [ ! -f "$SCRIPT_DIR/.env" ]; then
    echo -e "${YELLOW}Warning: .env file not found at project root${NC}"
    echo "Copy .env.example to .env and fill in your values"
    echo ""
fi

# Function to cleanup background processes on exit
cleanup() {
    echo -e "\n${YELLOW}Shutting down services...${NC}"
    kill $(jobs -p) 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

# Parse command line arguments
SERVICE="${1:-all}"

case "$SERVICE" in
    orchestrator)
        echo -e "${BLUE}Starting Chat Orchestrator...${NC}"
        cd "$SCRIPT_DIR/chat_orchestrator"
        source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null
        export TOOLS_SERVICE_URL=http://localhost:8080
        uvicorn orchestrator.api.app:app --host 0.0.0.0 --port 8000 --reload
        ;;

    tools|bridge|mcp)
        echo -e "${BLUE}Starting Tools Service...${NC}"
        cd "$SCRIPT_DIR/mcp_servers"
        source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null
        uvicorn bridge:app --host 0.0.0.0 --port 8080 --reload
        ;;

    rag)
        echo -e "${RED}RAG batch pipeline has been removed. Use /ingest command instead.${NC}"
        exit 1
        ;;

    all)
        echo -e "${BLUE}Starting all services...${NC}"
        echo ""

        # Start Tools Service first (orchestrator depends on it)
        echo -e "${YELLOW}[1/2] Starting Tools Service on port 8080...${NC}"
        (
            cd "$SCRIPT_DIR/mcp_servers"
            source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null
            uvicorn bridge:app --host 0.0.0.0 --port 8080 --reload
        ) &

        # Wait for service to start
        sleep 3

        # Start Chat Orchestrator
        echo -e "${YELLOW}[2/2] Starting Chat Orchestrator on port 8000...${NC}"
        (
            cd "$SCRIPT_DIR/chat_orchestrator"
            source .venv/bin/activate 2>/dev/null || source venv/bin/activate 2>/dev/null
            export TOOLS_SERVICE_URL=http://localhost:8080
            uvicorn orchestrator.api.app:app --host 0.0.0.0 --port 8000 --reload
        ) &

        echo ""
        echo -e "${GREEN}========================================${NC}"
        echo -e "${GREEN}  Services Started${NC}"
        echo -e "${GREEN}========================================${NC}"
        echo ""
        echo "Chat Orchestrator: http://localhost:8000"
        echo "  - API docs: http://localhost:8000/docs"
        echo ""
        echo "Tools Service: http://localhost:8080"
        echo "  - API docs: http://localhost:8080/docs"
        echo "  - Functions: http://localhost:8080/gemini/functions"
        echo ""
        echo -e "${YELLOW}Press Ctrl+C to stop all services${NC}"
        echo ""

        # Wait for all background processes
        wait
        ;;

    docker)
        echo -e "${BLUE}Starting with Docker Compose...${NC}"
        docker-compose up --build
        ;;

    *)
        echo "Usage: ./dev.sh [service]"
        echo ""
        echo "Services:"
        echo "  all              Start all services (default)"
        echo "  orchestrator     Start chat orchestrator only"
        echo "  tools|bridge|mcp Start tools service only"
        echo "  rag              Run RAG pipeline"
        echo "  docker           Start with Docker Compose"
        echo ""
        echo "Examples:"
        echo "  ./dev.sh              # Start all services"
        echo "  ./dev.sh tools        # Start tools service only"
        echo "  ./dev.sh docker       # Use Docker Compose"
        ;;
esac
