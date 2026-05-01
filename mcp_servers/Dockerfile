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

# Copy application code
COPY mcp_servers/ ./
COPY shared/ /app/shared/

# Set Python path
ENV PYTHONPATH=/app

# Expose port
EXPOSE 8080

RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Run the application
CMD ["uvicorn", "bridge:app", "--host", "0.0.0.0", "--port", "8080"]
