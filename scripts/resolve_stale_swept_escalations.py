#!/usr/bin/env python3
"""Resolve escalations that the daily sweep alert can no longer show usefully.

An escalation qualifies as "stale and untraceable" when all of these hold:
  - state == "open" and no ticket attached (never filed, same as the sweep's
    own eligible/old-unfiled queries)
  - older than --max-age-hours (default 24h, matching the sweep's own
    max_age_hours threshold)
  - has no "escalation"-purpose row in message_deliveries at all -- i.e.
    EscalationService.run_escalation_ticket_sweep's "older than Nh with no
    ticket" alert (chat_orchestrator/orchestrator/services/escalation_service.py)
    would drop this exact row from its bulleted list rather than show it,
    because there is no Telegram message left to link to.

These specific rows are also stuck from the *product* side: with no
delivery receipt there is no Telegram message to click through to, so the
"Track as ticket & close" / "Close silently" / "Close & inform customer"
buttons that live on that message (shared/utils/telegram_buttons.py) are
unreachable. There is no other supported way to close them out.

This script closes them the same way those buttons would: it calls
EscalationRepository.resolve(), the exact method "Close silently" already
uses, moving state -> "resolved" and setting resolved_at. It does **not**
delete any row -- the record stays in Supabase for audit/history, and is
fully undoable with EscalationRepository.reopen(<id>) (state -> "open",
resolved_at -> NULL) if a match should not have been resolved.

Usage:
    python scripts/resolve_stale_swept_escalations.py [--apply] [--max-age-hours N] [--limit N]

Defaults to a DRY RUN: prints every match, changes nothing. Pass --apply to
actually resolve them. This intentionally does NOT default to live writes
the way some other scripts/ here do (e.g. backfill_design_artifacts.py) --
these are real customer support records, so preview-first is the safer
default.

Requires CHAT_DB_URL / CHAT_DB_SERVICE_KEY (same as the running bot) --
no other configuration, and no Telegram credentials.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from orchestrator.services.escalation_service import EscalationService
from shared.utils.logging import get_logger

logger = get_logger(__name__)

DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_LIMIT = 200


async def find_stale_untraceable(
    svc: EscalationService,
    *,
    max_age_hours: int,
    limit: int,
) -> list[dict[str, Any]]:
    """Same age/state/ticket filter the sweep's own "old escalations" query
    uses, narrowed to rows with no resolvable Telegram delivery receipt --
    the same condition run_escalation_ticket_sweep now checks per-entry to
    decide whether to render a [View] link or drop the bullet."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    candidates = await svc._escalations.list_unfiled(
        state="open",
        created_before=cutoff,
        exclude_reasons=("safety_escalation",),
        limit=limit,
    )
    stale = []
    for row in candidates:
        delivery = await svc._deliveries.find_by_escalation(row["id"])
        if not delivery:
            stale.append(row)
    return stale


async def run(*, apply: bool, max_age_hours: int, limit: int) -> None:
    svc = EscalationService()
    if not (svc._supabase_url and svc._supabase_key):
        raise SystemExit(
            "CHAT_DB_URL / CHAT_DB_SERVICE_KEY (or SUPABASE_URL / SUPABASE_KEY) "
            "must be set -- refusing to run against no database."
        )

    stale = await find_stale_untraceable(svc, max_age_hours=max_age_hours, limit=limit)

    print(
        f"Found {len(stale)} stale, untraceable escalation(s): open, unfiled, "
        f"older than {max_age_hours}h, no Telegram delivery receipt."
    )
    if not stale:
        return

    verb = "Would resolve" if not apply else "Resolving"
    for row in stale:
        print(f"  {verb}: id={row['id']}  created_at={row['created_at']}")
        if apply:
            try:
                await svc._escalations.resolve(row["id"])
            except Exception:
                logger.warning(
                    "resolve_stale_swept_escalations: failed to resolve {}",
                    row["id"],
                    exc_info=True,
                )

    if apply:
        print(
            f"\nResolved {len(stale)} escalation(s) (state -> 'resolved', row kept). "
            "Undo any of these with EscalationRepository.reopen(<id>) if needed."
        )
    else:
        print("\nDry run only -- nothing was changed. Re-run with --apply to resolve these.")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Resolve stale, untraceable escalations -- the same orphans "
            "run_escalation_ticket_sweep's alert now drops instead of "
            "showing a dead, unlinkable bullet."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually resolve the matches. Without this flag, only prints what would happen.",
    )
    parser.add_argument(
        "--max-age-hours",
        type=int,
        default=DEFAULT_MAX_AGE_HOURS,
        help=f"Only consider escalations older than this (default: {DEFAULT_MAX_AGE_HOURS}, "
        "matching the sweep's own threshold).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Cap how many candidate escalations are queried (default: {DEFAULT_LIMIT}).",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    asyncio.run(run(apply=args.apply, max_age_hours=args.max_age_hours, limit=args.limit))


if __name__ == "__main__":
    main()
