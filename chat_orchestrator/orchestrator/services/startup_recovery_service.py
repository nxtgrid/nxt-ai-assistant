"""Startup recovery scan — finds and re-enqueues packets orphaned by deployment crashes.

Called once at application startup (via _run_startup_recovery in app.py) after a brief
delay to let the server fully initialise.

Design constraints
------------------
* instance_count = 1 ONLY. At multiple instances, two new containers could both
  scan and try to claim the same packet. If instance_count is ever increased, add a
  distributed lock (Valkey — see docs/VALKEY_CHECKPOINTING_REFERENCE.md) before
  enabling this scan.

* Uses fail_packet() with auto_resumable=True (not a new 'interrupted' status) so
  packets stay in the existing 'failed' status filter and surface through the normal
  ask_resume_failed expert_router path — which is patched to skip the user prompt for
  auto_resumable packets.

* Uses auto_retry_count in packet_state (not retry_count) so automatic recovery does
  not consume the user's manual retry budget.

Race note
---------
There is a narrow window between the scan completing and the first user message
arriving where get_active_packets_for_session() could observe the same packet. This
is safe: fail_packet uses a non-conditional UPDATE, and the ask_resume_failed routing
reads the final status. At worst, the packet is auto-failed twice with auto_resumable=True
and then immediately retried. No data is lost.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

STALENESS_MINUTES = 5  # Packets not heartbeated in 5+ min at startup are orphaned
MAX_AUTO_RETRIES = 2  # After 2 auto-resumes, let the user-facing failed prompt handle it

# Set STARTUP_RECOVERY_ENABLED=false to disable the recovery scan (required when
# scaling beyond 1 instance — add Valkey distributed locking first).
_RECOVERY_ENABLED = os.getenv("STARTUP_RECOVERY_ENABLED", "true").lower() in ("true", "1", "yes")


async def recover_orphaned_packets() -> int:
    """Run once at startup. Returns count of packets re-enqueued for recovery.

    Finds:
    1. Packets already marked auto_resumable=True in packet_state (clean SIGTERM shutdown)
    2. Stale in_progress packets whose heartbeat is older than STALENESS_MINUTES
       (covers hard kills where the SIGTERM handler didn't run)

    Both are marked failed with auto_resumable=True so the expert_router auto-resumes
    them on the next user message without asking.
    """
    if not _RECOVERY_ENABLED:
        LOGGER.info("Startup recovery disabled (STARTUP_RECOVERY_ENABLED=false)")
        return 0

    LOGGER.info(
        "Startup recovery enabled. IMPORTANT: Only safe at instance_count=1. "
        "Set STARTUP_RECOVERY_ENABLED=false before scaling beyond 1 instance."
    )

    from supabase import create_client  # type: ignore[attr-defined]

    url = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
    key = os.getenv("CHAT_DB_SERVICE_KEY") or os.getenv("SUPABASE_KEY", "")
    supabase = create_client(url, key)
    threshold = (datetime.now(timezone.utc) - timedelta(minutes=STALENESS_MINUTES)).isoformat()

    # 1. Find failed packets with auto_resumable=True (from clean SIGTERM shutdown)
    auto_resumable_result = (
        supabase.table("agent_work_packets")
        .select("*")
        .eq("packet_status", "failed")
        .filter("packet_state->>auto_resumable", "eq", "true")
        .execute()
    )
    auto_resumable = auto_resumable_result.data or []

    # 2. Find stale in_progress packets (heartbeat not updated within threshold)
    stale_result = (
        supabase.table("agent_work_packets")
        .select("*")
        .eq("packet_status", "in_progress")
        .lt("updated_at", threshold)
        .execute()
    )
    stale_in_progress = stale_result.data or []

    # Deduplicate by packet id
    seen_ids: set[str] = set()
    orphaned = []
    for p in auto_resumable + stale_in_progress:
        pid = p.get("id")
        if pid and pid not in seen_ids:
            seen_ids.add(pid)
            orphaned.append(p)

    if not orphaned:
        return 0

    LOGGER.info("Startup recovery: found %d orphaned packet(s)", len(orphaned))
    recovered = 0

    for packet in orphaned:
        try:
            await _recover_one(packet, supabase)
            recovered += 1
        except Exception:
            LOGGER.exception(
                "Startup recovery: failed to process packet %s", packet.get("packet_id")
            )

    LOGGER.info("Startup recovery: re-enqueued %d packet(s)", recovered)
    return recovered


async def _recover_one(packet: dict, supabase: Any) -> None:
    """Mark a single orphaned packet for auto-resume on the next user message."""
    packet_id = packet.get("id")
    packet_short_id = packet.get("packet_id", packet_id)
    state = packet.get("packet_state") or {}

    auto_retry_count = state.get("auto_retry_count", 0)

    if auto_retry_count >= MAX_AUTO_RETRIES:
        # Too many auto-retries — let the user-facing failed prompt handle it.
        # Clear auto_resumable so ask_resume_failed shows the normal "Resume?" dialog.
        LOGGER.warning(
            "Startup recovery: packet %s exceeded auto-retry limit (%d/%d), "
            "falling back to user-facing failed prompt",
            packet_short_id,
            auto_retry_count,
            MAX_AUTO_RETRIES,
        )
        new_state = {**state, "auto_resumable": False, "interrupted_too_many_times": True}
        supabase.table("agent_work_packets").update(
            {
                "packet_state": new_state,
                "packet_status": "failed",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        ).eq("id", packet_id).execute()
        return

    steps_done = packet.get("steps_completed") or []
    site_name = state.get("site_name", "unknown")

    # Use compare-and-swap: only claim this packet if it's still in its current status.
    # Prevents a race where a user message arrived between the scan query and this update.
    new_state = {
        **state,
        "auto_resumable": True,
        "auto_retry_count": auto_retry_count + 1,
        "recovery_pending": True,
        "recovery_at": datetime.now(timezone.utc).isoformat(),
    }
    current_status = packet.get("packet_status", "in_progress")
    result = (
        supabase.table("agent_work_packets")
        .update(
            {
                "packet_status": "failed",
                "packet_state": new_state,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", packet_id)
        .eq("packet_status", current_status)  # CAS guard
        .execute()
    )

    if not result.data:
        # Lost the race — packet was already updated by another path (e.g. first user message).
        # This is safe: the user message path will handle it.
        LOGGER.info(
            "Startup recovery: packet %s already claimed by another path, skipping",
            packet_short_id,
        )
        return

    LOGGER.info(
        "Startup recovery: packet %s marked for auto-resume "
        "(site=%s, steps_done=%d, auto_retry=%d)",
        packet_short_id,
        site_name,
        len(steps_done),
        auto_retry_count + 1,
    )
