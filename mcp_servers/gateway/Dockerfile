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
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for caching
COPY mcp_servers/requirements.txt ./requirements.txt
RUN uv pip install -r requirements.txt

# Copy application code. This is the gateway ASGI app (gateway/app.py), not
# bridge.py (mcp_servers/Dockerfile's own CMD) - the two live in the same
# image family but serve different entrypoints, so this Dockerfile exists
# separately rather than reusing mcp_servers/Dockerfile's CMD.
COPY mcp_servers/ ./
COPY shared/ /app/shared/

# grid_app.lib.perms is the real is_authorized whitelist (see
# gateway/app.py's run_gateway()) - only grid_app/ itself, not all of
# anansi_app/, to avoid pulling NiceGUI/Streamlit/other anansi_app-only
# dependencies into this image. See this repo's own
# image-copy-sets-cause-silent-import-failures lesson: verify this import
# still resolves (`python -c "from grid_app.lib import perms"`) after any
# future change to what's copied here or to perms.py's own imports.
COPY anansi_app/grid_app/ /app/grid_app/

# Set Python path
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8080

RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Run the gateway's own ASGI app (gateway/app.py:run_gateway), not bridge.py.
CMD ["python", "-m", "gateway.app"]
