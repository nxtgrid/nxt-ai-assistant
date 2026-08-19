#!/usr/bin/env python3
"""
Grafana Indexer Scheduler - Background Worker

Runs the Grafana panel indexer once a day at GRAFANA_SYNC_HOUR (the same
flag the NiceGUI settings page's "Nightly Sync Hour" slider writes to --
see shared/config/flag_registry.py and nicegui_app/pages/settings.py).

This replaces a nightly job that used to be registered in chat_orchestrator's
own APScheduler instance (orchestrator/api/app.py's run_grafana_indexer). That
job could never actually succeed: it imported grafana_indexer_incremental
from a `rag_pipeline/ingestion` path that has only ever contained README.md
and __init__.py -- the real module has always lived in anansi_app/scripts/,
which chat_orchestrator's own Dockerfile never copies into that image in the
first place (only chat_orchestrator/, shared/, rag_pipeline/, and
mcp_servers/ are). Every nightly run raised ImportError, was swallowed by the
job's own `except Exception`, and logged a one-line error nobody was
watching for -- so it silently never ran, once per night, indefinitely.

anansi_app is the correct home for this job: it already has the indexer
script, its dependencies, and Supabase write access, because it's the exact
process the "Sync Now" button (nicegui_app/pages/settings.py's _sync_now)
already runs this same script from. This scheduler drives the identical
subprocess invocation on a timer instead of a button click, so there's only
one proven code path for "run the indexer," not two.

Runs as a standalone script, started alongside broadcast_scheduler.py in
start.sh.

Usage:
    python grafana_scheduler.py           # Single check-and-run-if-due pass
    python grafana_scheduler.py --daemon  # Continuous run (poll every 60s)
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

_SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _grafana_scheduling_enabled() -> bool:
    """Mirrors grafana_mcp_server.py's own enable check: GRAFANA_ENABLED
    (admin UI standard) or GRAFANA_ACTIONS_ENABLED (legacy name, still read
    for back-compat). Explicitly "false" on either disables; otherwise
    enabled by default, matching every other MCP-server group gate."""
    server_enabled = os.getenv("GRAFANA_ENABLED", "").strip().lower()
    legacy_enabled = os.getenv("GRAFANA_ACTIONS_ENABLED", "").strip().lower()
    if server_enabled == "false" or legacy_enabled == "false":
        return False
    return True


def _sync_hour() -> int:
    try:
        return int(os.getenv("GRAFANA_SYNC_HOUR", "2"))
    except ValueError:
        return 2


def _schedule_timezone() -> ZoneInfo:
    # Reuses METRICS_TIMEZONE rather than introducing a Grafana-specific
    # flag: this was the same env var chat_orchestrator's old CronTrigger
    # read for the nightly Grafana job (one operational timezone for the
    # whole deployment), and adding a second, always-identical-in-practice
    # flag plus registry entry plus UI field would be pure duplication.
    try:
        return ZoneInfo(os.getenv("METRICS_TIMEZONE", "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def _is_due(now: datetime, sync_hour: int, last_run_date: date | None) -> bool:
    """True when `now` falls in the scheduled hour and today hasn't already
    triggered a run. Pulled out as a pure function so the decision can be
    tested without mocking time.sleep/subprocess."""
    return now.hour == sync_hour and now.date() != last_run_date


def run_indexer_once(verbose: bool = True) -> bool:
    """Run grafana_indexer_incremental.py as a subprocess, exactly as the
    NiceGUI "Sync Now" button does (settings.py's _run_grafana_indexer) --
    process isolation keeps this daemon's own import/env state from leaking
    into the indexer's, and reuses a call path already proven to work inside
    this container. Returns True on a clean (status 0) exit."""
    script_path = os.path.join(_SCRIPTS_DIR, "grafana_indexer_incremental.py")
    result = subprocess.run(
        [sys.executable, script_path],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode == 0:
        if verbose:
            last_line = next(
                (line for line in reversed(result.stdout.strip().splitlines()) if line.strip()),
                "ok",
            )
            print(f"✓ Scheduled Grafana sync completed: {last_line}")
        return True

    # Non-zero covers both a hard failure and "completed_with_generation_
    # failures" (grafana_indexer_incremental.py's __main__) -- either way,
    # surface the real message instead of a bare "it failed." Prefer
    # stderr: the indexer duplicates its authoritative final status line
    # there specifically so callers don't have to guess which stream has
    # the signal (see grafana_indexer_incremental.py's __main__ block).
    tail = (result.stderr or result.stdout or "").strip().splitlines()[-5:]
    print("⚠️  Scheduled Grafana sync did not complete cleanly:", file=sys.stderr)
    for line in tail:
        print(f"    {line}", file=sys.stderr)
    return False


def run_daemon(poll_interval: int = 60, verbose: bool = True) -> None:
    if not _grafana_scheduling_enabled():
        if verbose:
            print(
                "Grafana scheduling disabled (GRAFANA_ENABLED/GRAFANA_ACTIONS_ENABLED=false); "
                "exiting."
            )
        return

    tz = _schedule_timezone()
    sync_hour = _sync_hour()
    last_run_date: date | None = None

    if verbose:
        print(f"Starting Grafana indexer scheduler (daily at {sync_hour:02d}:00 {tz.key})")
        print(f"Poll interval: {poll_interval}s")
        print("Press Ctrl+C to stop\n")

    try:
        while True:
            now = datetime.now(tz)
            if _is_due(now, sync_hour, last_run_date):
                if verbose:
                    print(f"[{now.isoformat()}] Due for nightly Grafana sync, running now...")
                try:
                    run_indexer_once(verbose=verbose)
                except subprocess.TimeoutExpired:
                    print("ERROR: scheduled Grafana sync timed out after 600s", file=sys.stderr)
                except Exception as e:  # noqa: BLE001
                    print(f"ERROR running scheduled Grafana sync: {e}", file=sys.stderr)
                last_run_date = now.date()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        if verbose:
            print("\nGrafana scheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="Grafana indexer scheduler background worker")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run continuously (poll every 60s)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="Poll interval in seconds (daemon mode only)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages",
    )

    args = parser.parse_args()

    if args.daemon:
        run_daemon(poll_interval=args.interval, verbose=not args.quiet)
        return

    if not _grafana_scheduling_enabled():
        print("Grafana scheduling disabled; nothing to do.")
        sys.exit(0)

    now = datetime.now(_schedule_timezone())
    sync_hour = _sync_hour()
    if _is_due(now, sync_hour, None):
        ok = run_indexer_once(verbose=not args.quiet)
        sys.exit(0 if ok else 1)

    print(f"Not due yet (current hour {now.hour}, scheduled hour {sync_hour:02d}).")
    sys.exit(0)


if __name__ == "__main__":
    main()
