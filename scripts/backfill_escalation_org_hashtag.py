#!/usr/bin/env python3
"""Backfill escalations.org_hashtag on rows the daily sweep alert would
otherwise label with a bare id:<uuid>.

PR #192 made _record_canonical_escalation persist customer_username /
customer_email / org_hashtag for new escalations. Rows created before that
deploy have all three NULL, so run_escalation_ticket_sweep's "older than 24h
with no ticket" alert falls back to the canonical id. customer_username /
customer_email are not recoverable for those rows, but the org is: it's in
chat_sessions.metadata->>'organization_short_name'. This fills org_hashtag
from there, which (with the label-precedence change in the same PR as this
script) is enough for the alert to show '#Org' instead of the UUID.

Scope: state='open' AND ticket_id IS NULL AND org_hashtag IS NULL. Idempotent
-- the UPDATE is guarded on org_hashtag IS NULL, and a second run re-queries
and finds nothing. Already-populated rows are never touched.

Usage:
    python scripts/backfill_escalation_org_hashtag.py [--apply]

Defaults to a DRY RUN. Pass --apply to write.

Requires CHAT_DB_URL / CHAT_DB_SERVICE_KEY (same as the running bot) and
PYTHONPATH=<repo-root> (see CLAUDE.md's scripts/ conventions):

    PYTHONPATH=. CHAT_DB_URL=... CHAT_DB_SERVICE_KEY=... \\
        python scripts/backfill_escalation_org_hashtag.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from orchestrator.services.escalation_service import (
    EscalationService,
    _org_hashtag_from_short_name,
)
from shared.utils.logging import get_logger

logger = get_logger(__name__)


async def find_rows_missing_org(svc: EscalationService) -> list[dict[str, Any]]:
    """open, unfiled, org_hashtag IS NULL -- the rows the sweep alert would
    otherwise label with a bare id:<uuid>."""
    raw = svc._get_raw_client()
    resp = (
        raw.table("escalations")
        .select("id, chat_session_id, org_hashtag")
        .eq("state", "open")
        .is_("ticket_id", "null")
        .is_("org_hashtag", "null")
        .execute()
    )
    return list(getattr(resp, "data", None) or [])


async def org_hashtag_for_session(svc: EscalationService, session_uuid: str) -> str | None:
    raw = svc._get_raw_client()
    resp = (
        raw.table("chat_sessions")
        .select("metadata")
        .eq("id", session_uuid)
        .limit(1)
        .execute()
    )
    rows = list(getattr(resp, "data", None) or [])
    if not rows:
        return None
    short_name = (rows[0].get("metadata") or {}).get("organization_short_name")
    return _org_hashtag_from_short_name(short_name)


async def run(*, apply: bool) -> None:
    svc = EscalationService()
    if not (svc._supabase_url and svc._supabase_key):
        raise SystemExit(
            "CHAT_DB_URL / CHAT_DB_SERVICE_KEY must be set -- refusing to run "
            "against no database."
        )

    rows = await find_rows_missing_org(svc)
    print(f"Found {len(rows)} open, unfiled escalation(s) with no org_hashtag.")
    skipped = 0
    for r in rows:
        tag = await org_hashtag_for_session(svc, r["chat_session_id"])
        if not tag:
            print(
                f"  skip      id={r['id']}  session={r['chat_session_id']}  (no org short name)"
            )
            skipped += 1
            continue
        verb = "set      " if apply else "would set"
        print(f"  {verb} id={r['id']}  org_hashtag={tag!r}")
        if apply:
            svc._get_raw_client().table("escalations").update(
                {"org_hashtag": tag}
            ).eq("id", r["id"]).is_("org_hashtag", "null").execute()

    updated = len(rows) - skipped
    if apply:
        print(f"\nUpdated {updated} row(s); skipped {skipped}.")
    else:
        print(
            f"\nDry run only -- nothing changed. {updated} row(s) would be updated, "
            f"{skipped} skipped. Re-run with --apply to write."
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill escalations.org_hashtag from the session's org short name."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without this flag, only prints what would change.",
    )
    return parser.parse_args(argv)


def main() -> None:
    args = parse_args()
    asyncio.run(run(apply=args.apply))


if __name__ == "__main__":
    main()
