#!/usr/bin/env python3
"""
Episodic Memory Distiller Scheduler - Background Worker

Runs the episodic distillation batch once a day at EPISODIC_DISTILL_HOUR,
for both anchor types (grid, then organization). It writes
`episodic_distillations`, which the `episodic` context module reads at render
time (shared/prompts/providers_episodic.py).

Nothing had ever run this. scripts/distill_episodic_memory.py's docstring said
"Run nightly" and migration 0019's comment repeated it, but no scheduler
invoked it: start.sh started only the broadcast and Grafana daemons, the
orchestrator's APScheduler registered only weekly_metrics and
escalation_jira_sweep, and .do/app.example.yaml's entire `jobs:` block is
commented out. On top of that, no deployed image even contains repo-root
scripts/, so an invocation would have failed anyway -- the same shape of bug
as the Grafana job described in grafana_scheduler.py's docstring. The table
had therefore been empty since 0019 created it, and the episodic module had
contributed nothing to any prompt, ever.

anansi_app is the right home, matching grafana_scheduler.py's reasoning: it
already runs this deployment's scheduled batch work, and it inherits the
app-level AUTH_DB_*, CHAT_DB_* and MODEL_* env vars this batch needs. Unlike
the Grafana one this calls shared.episodic_memory in-process rather than
shelling out to a script -- there is no proven subprocess path to reuse here,
and the script it would shell out to is not in the image.

Runs as a standalone script, started alongside broadcast_scheduler.py and
grafana_scheduler.py in start.sh.

Usage:
    python episodic_scheduler.py           # Single check-and-run-if-due pass
    python episodic_scheduler.py --daemon  # Continuous run (poll every 60s)
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

ANCHOR_TYPES = ("grid", "organization")


def _episodic_scheduling_enabled() -> bool:
    """Enabled by default; EPISODIC_DISTILL_ENABLED=false turns it off.

    Matches the default-on convention every other group gate here uses (see
    grafana_scheduler.py's _grafana_scheduling_enabled), so a deployment that
    has never heard of this flag still gets the distillation it has been
    silently missing.
    """
    return os.getenv("EPISODIC_DISTILL_ENABLED", "").strip().lower() != "false"


def _distill_hour() -> int:
    """Default 3, an hour after the Grafana indexer's default of 2.

    Both are LLM-heavy nightly batches in the same container; staggering them
    keeps one from starving the other of rate limit.
    """
    try:
        return int(os.getenv("EPISODIC_DISTILL_HOUR", "3"))
    except ValueError:
        return 3


def _schedule_timezone() -> ZoneInfo:
    # Reuses METRICS_TIMEZONE, as grafana_scheduler.py does -- one operational
    # timezone for the whole deployment rather than a per-job flag.
    try:
        return ZoneInfo(os.getenv("METRICS_TIMEZONE", "UTC"))
    except Exception:
        return ZoneInfo("UTC")


def _is_due(now: datetime, distill_hour: int, last_run_date: "date | None") -> bool:
    """True when `now` falls in the scheduled hour and today hasn't already
    triggered a run. Pulled out as a pure function so the decision can be
    tested without mocking time.sleep."""
    return now.hour == distill_hour and now.date() != last_run_date


def run_distillation_once(verbose: bool = True) -> bool:
    """Distil both anchor types. Returns True only if the run is trustworthy.

    False when any anchor type enumerated zero entities. That is ambiguous by
    design -- it means either an empty deployment or an unreachable Auth DB
    (see shared/entity_eligibility.py) -- and the daemon uses the False to
    retry on the next poll rather than marking the day done on what may have
    been a database outage.
    """
    from shared.episodic_memory import distill_anchor_type

    trustworthy = True
    for anchor_type in ANCHOR_TYPES:
        try:
            result = asyncio.run(distill_anchor_type(anchor_type, apply=True))
        except Exception as e:  # noqa: BLE001 -- one anchor type must not sink the other
            print(f"ERROR distilling {anchor_type}: {e}", file=sys.stderr)
            trustworthy = False
            continue

        if result.get("error"):
            print(f"⚠️  Episodic distillation ({anchor_type}): {result['error']}", file=sys.stderr)
            trustworthy = False
            continue

        if not result["enumerated"]:
            print(
                f"⚠️  No eligible {anchor_type}s enumerated -- treating as a possible "
                f"Auth DB outage and retrying rather than recording a completed run.",
                file=sys.stderr,
            )
            trustworthy = False
            continue

        if verbose:
            print(
                f"✓ Episodic distillation ({anchor_type}): "
                f"{result['written']} written, {len(result['skipped'])} skipped, "
                f"of {len(result['targets'])} target(s)"
            )
    return trustworthy


def run_daemon(poll_interval: int = 60, verbose: bool = True) -> None:
    if not _episodic_scheduling_enabled():
        if verbose:
            print("Episodic distillation disabled (EPISODIC_DISTILL_ENABLED=false); exiting.")
        return

    tz = _schedule_timezone()
    distill_hour = _distill_hour()
    last_run_date: "date | None" = None

    if verbose:
        print(f"Starting episodic distiller scheduler (daily at {distill_hour:02d}:00 {tz.key})")
        print(f"Poll interval: {poll_interval}s")
        print("Press Ctrl+C to stop\n")

    try:
        while True:
            now = datetime.now(tz)
            if _is_due(now, distill_hour, last_run_date):
                if verbose:
                    print(f"[{now.isoformat()}] Due for nightly distillation, running now...")
                ok = False
                try:
                    ok = run_distillation_once(verbose=verbose)
                except Exception as e:  # noqa: BLE001
                    print(f"ERROR running scheduled distillation: {e}", file=sys.stderr)
                # Only a trustworthy run consumes the day. An outage leaves
                # last_run_date alone so the next poll inside the same hour
                # tries again, rather than skipping to tomorrow.
                if ok:
                    last_run_date = now.date()
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        if verbose:
            print("\nEpisodic distiller scheduler stopped")


def main():
    parser = argparse.ArgumentParser(description="Episodic memory distiller background worker")
    parser.add_argument("--daemon", action="store_true", help="Run continuously (poll every 60s)")
    parser.add_argument(
        "--interval", type=int, default=60, help="Poll interval in seconds (daemon mode only)"
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the distillation immediately, ignoring the scheduled hour",
    )

    args = parser.parse_args()

    if args.daemon:
        run_daemon(poll_interval=args.interval, verbose=not args.quiet)
        return

    if not _episodic_scheduling_enabled():
        print("Episodic distillation disabled; nothing to do.")
        sys.exit(0)

    now = datetime.now(_schedule_timezone())
    distill_hour = _distill_hour()
    if args.now or _is_due(now, distill_hour, None):
        ok = run_distillation_once(verbose=not args.quiet)
        sys.exit(0 if ok else 1)

    print(f"Not due yet (current hour {now.hour}, scheduled hour {distill_hour:02d}).")
    sys.exit(0)


if __name__ == "__main__":
    main()
