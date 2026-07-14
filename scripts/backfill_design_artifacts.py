#!/usr/bin/env python3
"""Backfill ``gd_designs.artifacts`` from pre-existing LPP ``agent_work_packets`` rows.

Phase B (Tasks 1-3, already merged) added a live write-path: new
``light_preliminary_package`` (LPP) packets now populate ``gd_designs.artifacts``
automatically as they run, via ``shared.grid_design.artifact_log``. This script
is the one-time backfill for packets that were created *before* that write-path
existed -- their generated Drive artifact IDs (map images, site layout renders,
QGIS projects, ...) are sitting unused in ``agent_work_packets.packet_state``
jsonb and were never attached to the corresponding design's artifact history.

Usage:
    python scripts/backfill_design_artifacts.py [--dry-run] [--limit N]

Requires ``CHAT_DB_URL`` / ``CHAT_DB_SERVICE_KEY`` (see ``shared/grid_design/db.py``
and ``shared/grid_design/settings.py``) -- no other configuration.

Idempotency: ``append_design_artifact`` always unconditionally prepends a new
version entry -- it does not check whether a given ``drive_file_id`` is already
recorded for that design/artifact_type. To make this script safely re-runnable,
the dedup check lives here: for each packet, the design's current
``artifacts[artifact_type]`` list is read *once* (not once per ``*_drive_id``
key) and any key whose ``drive_file_id`` is already present is skipped.
"""

from __future__ import annotations

import argparse
from typing import Any, Iterator

from shared.grid_design.artifact_log import _DRIVE_ID_SUFFIX, append_design_artifact
from shared.grid_design.db import Repository, get_client
from shared.utils.logging import get_logger

logger = get_logger(__name__)

PACKET_TYPE = "light_preliminary_package"
# Mirrors shared/grid_design/db.py Repository._PAGE -- PostgREST caps a single
# response (Supabase default ~1000 rows), so we page through in matching chunks.
PAGE_SIZE = 1000


def iter_lpp_packets(limit: int | None = None) -> Iterator[dict[str, Any]]:
    """Yield every ``agent_work_packets`` row with packet_type == PACKET_TYPE.

    Paginates with offset-based ``.range(start, end)`` calls (PostgREST/Supabase
    caps a single response), ordered by the row's ``id`` for a stable, gap-free
    walk across pages. Stops early once ``limit`` rows have been yielded.

    A non-positive ``limit`` (e.g. ``0``) yields nothing and never touches the
    DB -- callers passing ``--limit 0`` mean "process zero packets", not "one".
    """
    if limit is not None and limit <= 0:
        return
    client = get_client()
    yielded = 0
    start = 0
    while True:
        end = start + PAGE_SIZE - 1
        response = (
            client.table("agent_work_packets")
            .select("id, packet_id, packet_type, packet_state")
            .eq("packet_type", PACKET_TYPE)
            .order("id")
            .range(start, end)
            .execute()
        )
        batch = response.data or []
        if not batch:
            return
        for row in batch:
            yield row
            yielded += 1
            if limit is not None and yielded >= limit:
                return
        if len(batch) < PAGE_SIZE:
            return
        start += PAGE_SIZE


def extract_drive_id_keys(state: dict[str, Any]) -> dict[str, str]:
    """Return {artifact_type: drive_file_id} for every truthy ``*_drive_id`` key.

    Reuses the exact suffix convention from
    ``shared.grid_design.artifact_log.sweep_state_for_artifacts`` (imported as
    ``_DRIVE_ID_SUFFIX``, not re-typed as a literal) so the derivation can never
    drift from that module's behavior. Extracted here (rather than calling
    ``sweep_state_for_artifacts`` directly) because this script needs to dedupe
    against each design's existing artifact history *before* deciding whether to
    write -- something ``sweep_state_for_artifacts`` intentionally doesn't offer,
    since its own contract is to always call ``append_design_artifact``.
    """
    return {
        key[: -len(_DRIVE_ID_SUFFIX)]: value
        for key, value in (state or {}).items()
        if key.endswith(_DRIVE_ID_SUFFIX) and value
    }


def backfill_packet(
    packet: dict[str, Any], design_repo: Repository, dry_run: bool
) -> dict[str, Any]:
    """Process a single packet. Returns an outcome dict; never raises upward
    for expected conditions (missing design_id, design not found) -- only
    unexpected errors (DB failures, malformed rows) propagate, so the caller
    can catch-and-continue per packet."""
    packet_id = packet.get("packet_id")
    state = packet.get("packet_state") or {}
    design_id = state.get("design_id")

    if not design_id:
        return {"outcome": "skipped_no_design_id"}

    drive_keys = extract_drive_id_keys(state)
    if not drive_keys:
        return {"outcome": "nothing_to_backfill", "design_id": design_id}

    # Fetch the design's current artifact history ONCE per packet, regardless of
    # how many *_drive_id keys it has, so dedup checks don't hit the DB per-key.
    design = design_repo.get(design_id)
    if design is None:
        logger.warning(
            "backfill_design_artifacts: design %s (from packet %s) not found; skipping",
            design_id,
            packet_id,
        )
        return {"outcome": "design_not_found", "design_id": design_id}

    artifacts: dict[str, Any] = dict(design.get("artifacts") or {})
    appended = 0
    failed = 0

    for artifact_type, drive_file_id in drive_keys.items():
        existing_entries = artifacts.get(artifact_type) or []
        already_present = any(
            entry.get("drive_file_id") == drive_file_id for entry in existing_entries
        )
        if already_present:
            continue

        if dry_run:
            print(
                f"[dry-run] would append: packet_id={packet_id} design_id={design_id} "
                f"artifact_type={artifact_type} drive_file_id={drive_file_id}"
            )
            appended += 1
            # Reflect the would-be write locally so a packet with several
            # *_drive_id keys mapping to the same artifact_type doesn't get
            # double-counted/double-reported within this same dry run.
            artifacts = {
                **artifacts,
                artifact_type: [{"drive_file_id": drive_file_id}] + list(existing_entries),
            }
            continue

        result = append_design_artifact(
            design_id,
            artifact_type,
            drive_file_id=drive_file_id,
            packet_id=packet_id,
            label=artifact_type,
        )
        if result is not None:
            appended += 1
            artifacts = result
        else:
            # This key already passed the local dedup check above, so
            # append_design_artifact was genuinely attempted -- a None return
            # here is an unambiguous write failure (concurrent delete, transient
            # DB error, etc.), not "nothing new to backfill". Keep it out of
            # nothing_to_backfill so operators can tell the two apart.
            failed += 1

    return {
        "outcome": "processed",
        "design_id": design_id,
        "appended": appended,
        "failed": failed,
    }


def run(*, dry_run: bool, limit: int | None) -> None:
    if not dry_run:
        print(
            "WARNING: Running LIVE -- this will write to gd_designs.artifacts. "
            "Re-run with --dry-run first to preview."
        )

    design_repo = Repository("designs")

    scanned = 0
    skipped_no_design_id = 0
    nothing_to_backfill = 0
    design_not_found = 0
    appended_total = 0
    append_failed = 0
    errored = 0

    for packet in iter_lpp_packets(limit=limit):
        scanned += 1
        packet_id = packet.get("packet_id", "<unknown>")
        try:
            result = backfill_packet(packet, design_repo, dry_run)
        except Exception:
            errored += 1
            logger.warning(
                "backfill_design_artifacts: error processing packet_id=%s; continuing",
                packet_id,
                exc_info=True,
            )
            continue

        outcome = result["outcome"]
        if outcome == "skipped_no_design_id":
            skipped_no_design_id += 1
        elif outcome == "design_not_found":
            design_not_found += 1
        elif outcome == "nothing_to_backfill":
            nothing_to_backfill += 1
        elif outcome == "processed":
            appended_total += result["appended"]
            append_failed += result.get("failed", 0)
            if result["appended"] == 0 and result.get("failed", 0) == 0:
                nothing_to_backfill += 1

    verb = "would be appended" if dry_run else "appended"
    print("")
    print("=== backfill_design_artifacts summary ===")
    print(f"Packets scanned:                          {scanned}")
    print(f"Packets skipped (no design_id):            {skipped_no_design_id}")
    print(f"Packets skipped (design_id not found):     {design_not_found}")
    print(f"Packets with nothing new to backfill:      {nothing_to_backfill}")
    print(f"Artifact entries {verb}: {appended_total}")
    print(f"Artifact entries failed to append (write errors): {append_failed}")
    print(f"Packets errored:                           {errored}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill gd_designs.artifacts from pre-existing "
            f"'{PACKET_TYPE}' agent_work_packets rows."
        )
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be appended without writing anything.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Cap the number of packets processed (for testing against a small batch).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    run(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
