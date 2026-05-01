FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared code
COPY shared_code/ ./shared_code/

# Copy server code
COPY servers/equipment_control_server/ ./servers/equipment_control_server/

# Set Python path
ENV PYTHONPATH=/app

# Run the server
CMD ["python", "servers/equipment_control_server/equipment_control_mcp_server.py"]
