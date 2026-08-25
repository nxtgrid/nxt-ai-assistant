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

# Start the Grafana indexer scheduler daemon (nightly sync at GRAFANA_SYNC_HOUR).
# This is the real home for the job that used to be registered in
# chat_orchestrator's APScheduler instance and could never run there --
# see grafana_scheduler.py's module docstring for why. It no-ops on its own
# when GRAFANA_ENABLED/GRAFANA_ACTIONS_ENABLED is false, so it's safe to
# always start.
echo "Starting Grafana indexer scheduler daemon..."
python scripts/grafana_scheduler.py --daemon --interval 60 &
GRAFANA_SCHEDULER_PID=$!
echo "Grafana indexer scheduler started (PID: $GRAFANA_SCHEDULER_PID)"

# Start the episodic memory distiller (nightly at EPISODIC_DISTILL_HOUR,
# default 03:00 -- an hour after the Grafana indexer, so the two LLM-heavy
# batches in this container don't compete for rate limit).
#
# Nothing had ever run this batch: its script said "run nightly" but no
# scheduler anywhere invoked it, and repo-root scripts/ isn't in any deployed
# image either -- so episodic_distillations had been empty since migration
# 0019 created it. See episodic_scheduler.py's docstring for the full history.
# Disable with EPISODIC_DISTILL_ENABLED=false.
echo "Starting episodic memory distiller scheduler daemon..."
python scripts/episodic_scheduler.py --daemon --interval 60 &
EPISODIC_SCHEDULER_PID=$!
echo "Episodic distiller scheduler started (PID: $EPISODIC_SCHEDULER_PID)"

# Start the NiceGUI app (foreground - main process). Binds 0.0.0.0:8501 and
# serves /healthz for the platform health check.
export PORT="${PORT:-8501}"

# nicegui's app.storage.user persists a `.nicegui/` dir; the container runs as a
# non-root user against a root-owned /app, so point storage at a writable path.
# (Sessions live only for the container's lifetime — fine for an admin login.)
export NICEGUI_STORAGE_PATH="${NICEGUI_STORAGE_PATH:-/tmp/nicegui}"
mkdir -p "$NICEGUI_STORAGE_PATH"

exec python -m nicegui_app.main
