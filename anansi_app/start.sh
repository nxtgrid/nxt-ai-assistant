#!/bin/bash
set -e

# NiceGUI admin app entry point (successor to the Streamlit `streamlit run app.py`).
#
# OAuth is now handled in-process by nicegui_app/auth.py (Authlib), so the old
# ~/.streamlit/secrets.toml generation is gone — the same AUTH_* / GOOGLE_* env
# vars are read directly. Session cookies use nicegui's app.storage.user, signed
# with AUTH_COOKIE_SECRET (falls back to a hash of the client id, matching the
# old behaviour).

# Start the broadcast scheduler daemon in the background (polls every 60s).
# It is independent of the web framework and carries over unchanged.
echo "Starting broadcast scheduler daemon..."
python scripts/broadcast_scheduler.py --daemon --interval 60 &
SCHEDULER_PID=$!
echo "Broadcast scheduler started (PID: $SCHEDULER_PID)"

# Start the NiceGUI app (foreground - main process). Binds 0.0.0.0:8501 and
# serves /healthz for the platform health check.
export PORT="${PORT:-8501}"

# nicegui's app.storage.user persists a `.nicegui/` dir; the container runs as a
# non-root user against a root-owned /app, so point storage at a writable path.
# (Sessions live only for the container's lifetime — fine for an admin login.)
export NICEGUI_STORAGE_PATH="${NICEGUI_STORAGE_PATH:-/tmp/nicegui}"
mkdir -p "$NICEGUI_STORAGE_PATH"

exec python -m nicegui_app.main
