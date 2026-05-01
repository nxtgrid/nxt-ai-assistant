#!/bin/bash
set -e

# Create .streamlit directory if it doesn't exist
mkdir -p ~/.streamlit

# Generate secrets.toml from environment variables
cat > ~/.streamlit/secrets.toml <<EOF
[auth]
redirect_uri = "${AUTH_REDIRECT_URI}"
cookie_secret = "${AUTH_COOKIE_SECRET}"
client_id = "${AUTH_CLIENT_ID}"
client_secret = "${AUTH_CLIENT_SECRET}"
server_metadata_url = "${AUTH_SERVER_METADATA_URL}"
EOF

echo "Generated secrets.toml from environment variables"

# Start broadcast scheduler daemon in background (polls every 60s)
echo "Starting broadcast scheduler daemon..."
python scripts/broadcast_scheduler.py --daemon --interval 60 &
SCHEDULER_PID=$!
echo "Broadcast scheduler started (PID: $SCHEDULER_PID)"

# Start Streamlit (foreground - main process)
exec streamlit run app.py --server.port=8501 --server.address=0.0.0.0 --server.headless=true
