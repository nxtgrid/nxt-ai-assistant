FROM python:3.11-slim

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY rag_pipeline/requirements.txt .
RUN uv pip install -r requirements.txt

# Copy shared code
COPY shared ./shared

# Copy application code
COPY rag_pipeline/ .

# Create non-root user for security
RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

# Default command: run incremental sync with graph rebuild
CMD ["python", "ingestion/batch_ingest.py", "--since-last-run", "--rebuild-graph"]
