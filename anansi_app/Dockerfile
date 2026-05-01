FROM python:3.11-slim

# Install uv for fast dependency installation
COPY --from=ghcr.io/astral-sh/uv:0.7 /uv /uvx /bin/
ENV UV_SYSTEM_PYTHON=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for dependency caching
COPY anansi_app/requirements.txt ./requirements.txt
RUN uv pip install -r requirements.txt

# Copy application code
COPY anansi_app/ ./
COPY shared/ /app/shared/

# Set Python path so shared/ is importable
ENV PYTHONPATH=/app

# Expose Streamlit port
EXPOSE 8501

RUN adduser --disabled-password --gecos "" appuser
USER appuser

# Run the application
CMD ["bash", "start.sh"]
