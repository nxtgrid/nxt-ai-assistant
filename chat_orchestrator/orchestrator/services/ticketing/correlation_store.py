"""CorrelationStore -- chat_db access for /notify alert correlation *state*.

Backs ``ticket_correlations`` / ``ticket_correlation_events``
(db/migrations/0003_alert_correlation.sql, keyed by ``ticket_id`` since
db/migrations/0005b_ticket_schema_validate_and_contract.sql). Same lazy-client
pattern as ``InternalTicketBackend``: accepts a ready-made raw postgrest
client or a getter callable that lazily produces one.

Every method here owns only mutable correlation *state* -- root cause,
signatures, affected components, occurrence count, escalation timestamp. It
is keyed purely by ``ticket_id``: this store never resolves a ``ticket_ref``
to an id itself. Callers that only have a ref (the correlator assembling
candidates, the notify handler seeding a fresh ticket) resolve it through
``TicketRepository`` first -- ``TicketRepository.adopt_external`` for a
candidate discovered only via backend search, or the id returned by ticket
creation for a freshly-filed one. Current ticket ref/backend/summary/status/
grid come from ``tickets`` (``TicketRepository``'s table); Telegram delivery
coordinates come from ``message_deliveries`` (``DeliveryRepository``) -- this
store caches neither.

Every method swallows and logs errors, returning a safe empty value (``None``
/ ``[]`` / ``False``) -- a correlation-store outage must degrade correlation
to "no candidates found" (i.e. file a new ticket), never a hard failure or
an unhandled exception reaching the /notify handler.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from shared.utils.logging import get_logger

from .alert_facts import same_component

LOGGER = get_logger(__name__)

#: Ticket states a correlation candidate can still be amended/duplicated
#: onto. Anything else (done) has nowhere further to receive an alert.
_OPEN_TICKET_STATUSES = ("open", "in_progress")


# ---------------------------------------------------------------------------
# Degradation visibility. The 2026-08-10 incident (0005b dropped columns this
# store still wrote to) ran for ~12 hours emitting five WARNING lines per
# alert -- easy to scroll past, and nothing outside the log noticed. Every
# `except Exception` below now also feeds this counter, so /health and
# /chat/notify's response can surface degradation without anyone having to
# be watching the logs at the time.
# ---------------------------------------------------------------------------

#: How long a failure keeps counting toward "this hour", and the minimum gap
#: between repeated WARNING lines for the same (method, error) shape.
_FAILURE_WINDOW_SECONDS = 3600.0

_failure_counts: Dict[str, int] = defaultdict(int)
_failure_last_logged_at: Dict[str, float] = {}
_failure_window_started_at: float = time.monotonic()


def _record_failure(method: str, error: Exception) -> None:
    """Count a correlation-store failure and log it at most once an hour
    per (method, error shape) -- first occurrence always logs immediately."""
    global _failure_window_started_at
    now = time.monotonic()
    if now - _failure_window_started_at > _FAILURE_WINDOW_SECONDS:
        _failure_counts.clear()
        _failure_last_logged_at.clear()
        _failure_window_started_at = now

    key = f"{method}:{type(error).__name__}"
    _failure_counts[key] += 1
    last_logged = _failure_last_logged_at.get(key)
    if last_logged is None or now - last_logged >= _FAILURE_WINDOW_SECONDS:
        suppressed = _failure_counts[key] - 1
        suffix = f" ({suppressed} more suppressed this hour)" if suppressed else ""
        LOGGER.warning("correlation store: {} failed: {}{}", method, error, suffix)
        _failure_last_logged_at[key] = now


def failures_last_hour() -> int:
    """Total correlation-store failures in the current hourly window.

    Used by ``/health`` (``correlation_store_failures_last_hour``) and
    ``/chat/notify`` (``correlation_degraded``) so a degraded store is
    visible without reading logs.
    """
    if time.monotonic() - _failure_window_started_at > _FAILURE_WINDOW_SECONDS:
        return 0
    return sum(_failure_counts.values())


@dataclass(frozen=True)
class AffectedKeyMerge:
    """Outcome of folding one component into a correlation's affected_keys.

    ``added`` is what the delivery layer needs: a merge that only bumped an
    existing entry's ``count`` changed nothing an operator has to be told
    about, so it must not produce a Telegram post.
    """

    affected_keys: List[Dict[str, Any]]
    added: bool


class CorrelationStore:
    def __init__(
        self,
        client: Optional[Any] = None,
        get_client: Optional[Callable[[], Optional[Any]]] = None,
    ) -> None:
        self._client_instance = client
        self._get_client_fn = get_client

    def _client(self) -> Optional[Any]:
        if self._client_instance is not None:
            return self._client_instance
        if self._get_client_fn is not None:
            try:
                return self._get_client_fn()
            except Exception as e:
                _record_failure("get_client", e)
                return None
        return None

    # ------------------------------------------------------------------
    # ticket_correlation_events -- full audit trail, event-time evidence.
    # grid_name/candidate snapshot/alert live here even though they are no
    # longer cached on ticket_correlations (see db/schema/chat_db.sql).
    # ------------------------------------------------------------------

    async def get_by_dedup_key(self, dedup_key: str) -> Optional[Dict[str, Any]]:
        """Look up a prior decision by ``dedup_key`` -- the idempotency backstop
        for retried /notify requests (see the /notify wiring in app.py)."""
        client = self._client()
        if client is None:
            return None
        try:
            response = (
                client.table("ticket_correlation_events")
                .select("*")
                .eq("dedup_key", dedup_key)
                .limit(1)
                .execute()
            )
        except Exception as e:
            _record_failure("get_by_dedup_key", e)
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def record_event(
        self,
        *,
        ticket_id: Optional[str],
        grid_name: str,
        source: Optional[str],
        signature: Optional[str],
        dedup_key: Optional[str],
        decision: str,
        decided_by: str,
        confidence: Optional[float],
        reason: Optional[str],
        candidate_refs: List[str],
        alert: Dict[str, Any],
        llm_raw: Optional[str],
    ) -> bool:
        """Insert an audit row for one correlation decision. Never raises --
        a race on the ``dedup_key`` unique index is a benign double-write
        (the earlier row is what a concurrent replay check would have found
        anyway), not a reason to fail the request."""
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlation_events").insert(
                {
                    "ticket_id": ticket_id,
                    "grid_name": grid_name,
                    "source": source,
                    "signature": signature,
                    "dedup_key": dedup_key,
                    "decision": decision,
                    "decided_by": decided_by,
                    "confidence": confidence,
                    "reason": reason,
                    "candidate_refs": candidate_refs,
                    "alert": alert,
                    "llm_raw": llm_raw,
                }
            ).execute()
            return True
        except Exception as e:
            _record_failure("record_event", e)
            return False

    async def record_event_ticket_id(self, dedup_key: str, ticket_id: str) -> bool:
        """Backfill the ticket a "new"-decided event actually produced.

        ``record_event`` runs inside ``AlertCorrelator._finalize``, before the
        ticket is created -- a "new" decision's event row is written with
        ``ticket_id=None`` because there's nothing to reference yet. Without
        this backfill, a later replay of the same ``dedup_key`` finds a row
        whose ``ticket_id`` is still ``None``, fails the delivery-idempotency
        guard's truthiness check, and files a second duplicate ticket.
        """
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlation_events").update(
                {"ticket_id": ticket_id}
            ).eq("dedup_key", dedup_key).execute()
            return True
        except Exception as e:
            _record_failure("record_event_ticket_id", e)
            return False

    # ------------------------------------------------------------------
    # ticket_correlations -- mutable state, keyed by ticket_id.
    # ------------------------------------------------------------------

    async def get_correlation(self, ticket_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single correlation row by ``ticket_id`` -- used by the
        amend executor (``correlation_render.apply_amendment``) to read the
        post-merge state right before rendering."""
        client = self._client()
        if client is None:
            return None
        try:
            response = (
                client.table("ticket_correlations")
                .select("*")
                .eq("ticket_id", ticket_id)
                .limit(1)
                .execute()
            )
        except Exception as e:
            _record_failure("get_correlation", e)
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def record_amendment(
        self,
        ticket_id: str,
        *,
        severity: Optional[str] = None,
        escalated: bool = False,
    ) -> bool:
        """Persist rendered state (severity, escalation) after an amendment
        executes. The rendered summary itself is no longer this store's
        concern -- ``TicketService.update_ticket`` persists it to
        ``tickets.summary`` (the canonical projection) directly.

        Returns whether the update actually matched a row -- callers (see
        ``correlation_render.apply_amendment``) gate whether to *announce*
        an escalation on this, so a silent no-op here must not be reported
        as success: state that didn't persist would otherwise be
        re-announced on every subsequent alert.
        """
        client = self._client()
        if client is None:
            return False
        payload: Dict[str, Any] = {}
        if severity:
            payload["severity"] = severity
        if escalated:
            payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
        if not payload:
            return True
        try:
            response = (
                client.table("ticket_correlations")
                .update(payload)
                .eq("ticket_id", ticket_id)
                .execute()
            )
            return bool(getattr(response, "data", None))
        except Exception as e:
            _record_failure("record_amendment", e)
            return False

    async def open_candidates_for_grid(
        self, grid_name: str, since_iso: str, limit: int = 15
    ) -> List[Dict[str, Any]]:
        """Open correlation rows for a grid within the lookback window,
        most-recent-first.

        Two explicit reads rather than a PostgREST embedded-resource join --
        this raw client already uses ``.in_()`` elsewhere
        (``work_packet_service.py``) and two plain queries degrade cleanly
        (an empty first query short-circuits before the second ever runs).
        ``tickets`` is queried first (current ref/backend/summary/status/grid
        -- ``ticket_correlations`` no longer caches any of these), ordered by
        ``updated_at`` and capped at ``limit * 2`` as a deliberate
        approximation: a grid with more than ``limit * 2`` simultaneously
        open, correlation-tracked tickets could have a genuinely-recent one
        fall outside this first pass. Ordering by ``updated_at`` (which every
        amend touches via ``TicketService.update_ticket`` ->
        ``TicketRepository.update_by_ref``) keeps that approximation
        reasonable in practice without a join.
        """
        client = self._client()
        if client is None:
            return []
        try:
            ticket_response = (
                client.table("tickets")
                .select("id, ticket_ref, backend, summary, description, status")
                .eq("grid_name", grid_name)
                .in_("status", list(_OPEN_TICKET_STATUSES))
                .eq("provisioning_state", "active")
                .order("updated_at", desc=True)
                .limit(limit * 2)
                .execute()
            )
        except Exception as e:
            _record_failure("open_candidates_for_grid.tickets", e)
            return []
        ticket_rows = getattr(ticket_response, "data", None) or []
        by_id = {row["id"]: row for row in ticket_rows if row.get("id")}
        if not by_id:
            return []

        try:
            correlation_response = (
                client.table("ticket_correlations")
                .select("*")
                .in_("ticket_id", list(by_id.keys()))
                .gte("last_alert_at", since_iso)
                .order("last_alert_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception as e:
            _record_failure("open_candidates_for_grid.correlations", e)
            return []
        correlation_rows = getattr(correlation_response, "data", None) or []

        merged: List[Dict[str, Any]] = []
        for row in correlation_rows:
            ticket = by_id.get(row.get("ticket_id"))
            if ticket is None:
                continue
            merged.append(
                {
                    **row,
                    "ticket_ref": ticket.get("ticket_ref"),
                    "ticket_backend": ticket.get("backend"),
                    "summary_current": ticket.get("summary"),
                    "description": ticket.get("description") or "",
                    "status": ticket.get("status"),
                    "grid_name": grid_name,
                }
            )
        return merged

    async def upsert_correlation(
        self,
        *,
        ticket_id: str,
        root_cause_kind: Optional[str],
        primary_signature: str,
        signatures: List[str],
        affected_keys: List[Dict[str, Any]],
        summary_base: str,
        description_base: str,
        severity: str,
    ) -> bool:
        """Create (or, on a retry, update) a ticket's correlation row.

        This seeds both newly-filed tickets and externally discovered
        tickets the first time correlation amends them. ``ticket_id`` is the
        canonical ``tickets.id`` this row belongs to -- always resolved by
        the caller (fresh from ticket creation, or via
        ``TicketRepository.adopt_external`` for a discovered candidate)
        before calling in, so this store never needs its own ``tickets``
        lookup. Upserting on ``ticket_id`` (the primary key since
        db/migrations/0005b) makes either path idempotent.
        """
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlations").upsert(
                {
                    "ticket_id": ticket_id,
                    "root_cause_kind": root_cause_kind,
                    "primary_signature": primary_signature,
                    "signatures": signatures,
                    "affected_keys": affected_keys,
                    "summary_base": summary_base,
                    "description_base": description_base,
                    "severity": severity,
                },
                on_conflict="ticket_id",
            ).execute()
            return True
        except Exception as e:
            _record_failure("upsert_correlation", e)
            return False

    async def merge_affected_key(
        self,
        ticket_id: str,
        *,
        kind: str,
        key: str,
        label: str,
        occurred_at: Optional[str] = None,
        signature: Optional[str] = None,
    ) -> Optional[AffectedKeyMerge]:
        """Idempotently add/bump ``(kind, key)`` in a correlation's
        ``affected_keys``, and fold ``signature`` into ``signatures`` if new.

        Returns an ``AffectedKeyMerge`` (the updated ``affected_keys`` list
        plus whether a genuinely new component was appended vs. an existing
        entry's count merely bumped), or ``None`` if ``ticket_id`` isn't
        found or the store errors.
        """
        client = self._client()
        if client is None:
            return None
        occurred_at = occurred_at or datetime.now(timezone.utc).isoformat()
        try:
            existing = (
                client.table("ticket_correlations")
                .select("*")
                .eq("ticket_id", ticket_id)
                .limit(1)
                .execute()
            )
            rows = getattr(existing, "data", None) or []
            if not rows:
                return None
            row = rows[0]

            affected_keys = list(row.get("affected_keys") or [])
            added = True
            for entry in affected_keys:
                if same_component(entry, kind, key):
                    entry["count"] = int(entry.get("count") or 0) + 1
                    entry["last_seen"] = occurred_at
                    added = False
                    break
            if added:
                affected_keys.append(
                    {
                        "kind": kind,
                        "key": key,
                        "label": label,
                        "first_seen": occurred_at,
                        "last_seen": occurred_at,
                        "count": 1,
                    }
                )

            update_payload: Dict[str, Any] = {"affected_keys": affected_keys}
            if signature:
                signatures = list(row.get("signatures") or [])
                if signature not in signatures:
                    signatures.append(signature)
                    update_payload["signatures"] = signatures

            client.table("ticket_correlations").update(update_payload).eq(
                "ticket_id", ticket_id
            ).execute()
            return AffectedKeyMerge(affected_keys=affected_keys, added=added)
        except Exception as e:
            _record_failure("merge_affected_key", e)
            return None

    async def bump_occurrence(
        self, ticket_id: str, occurred_at: Optional[str] = None
    ) -> bool:
        """Increment ``occurrence_count`` and refresh ``last_alert_at`` --
        called on every decision (new/amend/duplicate alike) so a
        correlation row's occurrence count always reflects total alerts
        folded into it."""
        client = self._client()
        if client is None:
            return False
        occurred_at = occurred_at or datetime.now(timezone.utc).isoformat()
        try:
            existing = (
                client.table("ticket_correlations")
                .select("occurrence_count")
                .eq("ticket_id", ticket_id)
                .limit(1)
                .execute()
            )
            rows = getattr(existing, "data", None) or []
            if not rows:
                return False
            current = int(rows[0].get("occurrence_count") or 0)
            client.table("ticket_correlations").update(
                {"occurrence_count": current + 1, "last_alert_at": occurred_at}
            ).eq("ticket_id", ticket_id).execute()
            return True
        except Exception as e:
            _record_failure("bump_occurrence", e)
            return False
