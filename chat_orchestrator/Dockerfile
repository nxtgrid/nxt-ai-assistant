# Stage 1: Build Mini App (Vue 3)
FROM node:20-slim AS mini-app-builder
WORKDIR /build
COPY mini_app/package.json mini_app/package-lock.json ./
RUN npm ci --no-audit
COPY mini_app/ ./
RUN npm run build

# Stage 2: Python application
FROM python:3.11-slim

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    libgdal-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency files first for layer caching.
# Do NOT copy source code here — any code change would bust the dep cache.
COPY chat_orchestrator/pyproject.toml ./
COPY chat_orchestrator/README.md ./
COPY mcp_servers/requirements.txt ./mcp_requirements.txt

# Create minimal package structure so pyproject.toml install succeeds
# without copying actual source code (preserves layer cache on code-only changes)
RUN mkdir -p orchestrator && touch orchestrator/__init__.py

# Install dependencies (cached by Docker when dep files don't change)
RUN uv pip install . && \
    uv pip install -r mcp_requirements.txt

# Copy remaining application code (this layer changes on every code push,
# but the dependency layer above is cached)
COPY chat_orchestrator/ ./
COPY shared/ /app/shared/
COPY rag_pipeline/ /app/rag_pipeline/
COPY mcp_servers/ /app/mcp_servers/

# Copy built Mini App static files
COPY --from=mini-app-builder /build/dist/ /app/mini_app/dist/

# Set Python path (includes mcp_servers for direct tool execution)
ENV PYTHONPATH=/app:/app/mcp_servers

# Expose port
EXPOSE 8000

RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Run the application
CMD ["uvicorn", "orchestrator.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "570"]
