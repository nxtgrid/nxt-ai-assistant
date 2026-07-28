"""CorrelationStore -- chat_db access for /notify alert correlation state.

Backs ``ticket_correlations`` / ``ticket_correlation_events``
(db/migrations/0003_alert_correlation.sql). Same lazy-client pattern as
``InternalTicketBackend``: accepts a ready-made raw postgrest client or a
getter callable that lazily produces one.

Every method swallows and logs errors, returning a safe empty value (``None``
/ ``[]`` / ``False``) -- a correlation-store outage must degrade correlation
to "no candidates found" (i.e. file a new ticket), never a hard failure or
an unhandled exception reaching the /notify handler.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from shared.utils.logging import get_logger

from .alert_facts import same_component

LOGGER = get_logger(__name__)


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
            except Exception:
                LOGGER.warning("correlation store: get_client() raised", exc_info=True)
                return None
        return None

    # ------------------------------------------------------------------
    # ticket_correlation_events
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
            LOGGER.warning("correlation store: get_by_dedup_key(%s) failed: %s", dedup_key, e)
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def record_event(
        self,
        *,
        ticket_ref: Optional[str],
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
                    "ticket_ref": ticket_ref,
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
            LOGGER.warning("correlation store: record_event failed: %s", e)
            return False

    async def record_event_ticket_ref(self, dedup_key: str, ticket_ref: str) -> bool:
        """Backfill the ticket a "new"-decided event actually produced.

        ``record_event`` runs inside ``AlertCorrelator._finalize``, before the
        ticket is created -- a "new" decision's event row is written with
        ``ticket_ref=None`` because there's nothing to reference yet. Without
        this backfill, a later replay of the same ``dedup_key`` finds a row
        whose ``ticket_ref`` is still ``None``, fails the delivery-idempotency
        guard's truthiness check, and files a second duplicate ticket.
        """
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlation_events").update(
                {"ticket_ref": ticket_ref}
            ).eq("dedup_key", dedup_key).execute()
            return True
        except Exception as e:
            LOGGER.warning(
                "correlation store: record_event_ticket_ref(%s) failed: %s", dedup_key, e
            )
            return False

    # ------------------------------------------------------------------
    # ticket_correlations
    # ------------------------------------------------------------------

    async def get_correlation(self, ticket_ref: str) -> Optional[Dict[str, Any]]:
        """Fetch a single correlation row by ``ticket_ref`` -- used by the
        amend executor (``correlation_render.apply_amendment``) to read the
        post-merge state right before rendering."""
        client = self._client()
        if client is None:
            return None
        try:
            response = (
                client.table("ticket_correlations")
                .select("*")
                .eq("ticket_ref", ticket_ref)
                .limit(1)
                .execute()
            )
        except Exception as e:
            LOGGER.warning("correlation store: get_correlation(%s) failed: %s", ticket_ref, e)
            return None
        rows = getattr(response, "data", None) or []
        return rows[0] if rows else None

    async def record_amendment(
        self,
        ticket_ref: str,
        *,
        summary_current: str,
        severity: Optional[str] = None,
        escalated: bool = False,
    ) -> bool:
        """Persist rendered state after an amendment executes."""
        client = self._client()
        if client is None:
            return False
        payload: Dict[str, Any] = {"summary_current": summary_current}
        if severity:
            payload["severity"] = severity
        if escalated:
            payload["escalated_at"] = datetime.now(timezone.utc).isoformat()
        try:
            client.table("ticket_correlations").update(payload).eq(
                "ticket_ref", ticket_ref
            ).execute()
            return True
        except Exception as e:
            LOGGER.warning("correlation store: record_amendment(%s) failed: %s", ticket_ref, e)
            return False

    async def open_candidates_for_grid(
        self, grid_name: str, since_iso: str, limit: int = 15
    ) -> List[Dict[str, Any]]:
        """Open correlation rows for a grid within the lookback window,
        most-recent-first."""
        client = self._client()
        if client is None:
            return []
        try:
            response = (
                client.table("ticket_correlations")
                .select("*")
                .eq("grid_name", grid_name)
                .eq("status", "open")
                .gte("last_alert_at", since_iso)
                .order("last_alert_at", desc=True)
                .limit(limit)
                .execute()
            )
        except Exception as e:
            LOGGER.warning(
                "correlation store: open_candidates_for_grid(%s) failed: %s", grid_name, e
            )
            return []
        return getattr(response, "data", None) or []

    async def upsert_correlation(
        self,
        *,
        ticket_ref: str,
        ticket_backend: str,
        grid_name: str,
        organization_id: Optional[int],
        root_cause_kind: Optional[str],
        primary_signature: str,
        signatures: List[str],
        affected_keys: List[Dict[str, Any]],
        summary_base: str,
        description_base: str,
        severity: str,
        telegram_chat_id: Optional[str],
        telegram_topic_id: Optional[str],
    ) -> bool:
        """Create (or, on a retry, update) a ticket's correlation row.

        This seeds both newly-filed tickets and externally discovered tickets
        the first time correlation amends them. Upserting on ``ticket_ref``
        makes either path idempotent at the UNIQUE constraint.
        """
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlations").upsert(
                {
                    "ticket_ref": ticket_ref,
                    "ticket_backend": ticket_backend,
                    "grid_name": grid_name,
                    "organization_id": organization_id,
                    "root_cause_kind": root_cause_kind,
                    "primary_signature": primary_signature,
                    "signatures": signatures,
                    "affected_keys": affected_keys,
                    "summary_base": summary_base,
                    "summary_current": summary_base,
                    "description_base": description_base,
                    "severity": severity,
                    "telegram_chat_id": telegram_chat_id,
                    "telegram_topic_id": telegram_topic_id,
                },
                on_conflict="ticket_ref",
            ).execute()
            return True
        except Exception as e:
            LOGGER.warning("correlation store: upsert_correlation(%s) failed: %s", ticket_ref, e)
            return False

    async def merge_affected_key(
        self,
        ticket_ref: str,
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
        entry's count merely bumped), or ``None`` if the ticket_ref isn't
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
                .eq("ticket_ref", ticket_ref)
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
                "ticket_ref", ticket_ref
            ).execute()
            return AffectedKeyMerge(affected_keys=affected_keys, added=added)
        except Exception as e:
            LOGGER.warning("correlation store: merge_affected_key(%s) failed: %s", ticket_ref, e)
            return None

    async def bump_occurrence(
        self, ticket_ref: str, occurred_at: Optional[str] = None
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
                .eq("ticket_ref", ticket_ref)
                .limit(1)
                .execute()
            )
            rows = getattr(existing, "data", None) or []
            if not rows:
                return False
            current = int(rows[0].get("occurrence_count") or 0)
            client.table("ticket_correlations").update(
                {"occurrence_count": current + 1, "last_alert_at": occurred_at}
            ).eq("ticket_ref", ticket_ref).execute()
            return True
        except Exception as e:
            LOGGER.warning("correlation store: bump_occurrence(%s) failed: %s", ticket_ref, e)
            return False

    async def record_message_id(self, ticket_ref: str, message_id: int) -> bool:
        """Stamp the Telegram message id of a ticket's first post, so a
        later amend can reply to it."""
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlations").update(
                {"telegram_message_id": message_id}
            ).eq("ticket_ref", ticket_ref).execute()
            return True
        except Exception as e:
            LOGGER.warning("correlation store: record_message_id(%s) failed: %s", ticket_ref, e)
            return False

    async def mark_closed(self, ticket_ref: str) -> bool:
        """Mark a correlation row's cached ``status`` as done -- used when
        candidate assembly discovers (via the backend's own ``get_status``)
        that a ticket has actually closed, so it stops surfacing as an open
        candidate. The ticket backend remains the authoritative source of
        truth; this is only the correlation layer's cached mirror."""
        client = self._client()
        if client is None:
            return False
        try:
            client.table("ticket_correlations").update({"status": "done"}).eq(
                "ticket_ref", ticket_ref
            ).execute()
            return True
        except Exception as e:
            LOGGER.warning("correlation store: mark_closed(%s) failed: %s", ticket_ref, e)
            return False
