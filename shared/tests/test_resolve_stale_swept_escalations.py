"""Tests for scripts/resolve_stale_swept_escalations.py -- closing out
escalations that run_escalation_ticket_sweep's "older than Nh with no
ticket" alert can no longer show usefully (chat_orchestrator/orchestrator/
services/escalation_service.py drops these from its bulleted list instead
of rendering a dead, unlinkable bullet -- see
test_sweep_old_escalations_alert_drops_entries_with_no_traceable_message
in chat_orchestrator/tests/services/test_escalation_service_ticketing.py).

`scripts` has no `__init__.py`, but is importable as a namespace package
given the repo root is on PYTHONPATH (see CLAUDE.md's PYTHONPATH
conventions for this repo) -- same as test_backfill_design_artifacts.py.

Fakes ``svc._escalations`` / ``svc._deliveries`` with tiny stand-in classes
(same technique test_escalation_service_ticketing.py's
test_track_as_ticket_records_the_customer_notification_delivery uses for
``_deliveries``) rather than a full fake Supabase client -- the script only
ever calls three narrow repository methods.

Async tests carry an explicit ``@pytest.mark.asyncio`` rather than relying
on chat_orchestrator/pyproject.toml's ``asyncio_mode = "auto"``: when this
file is targeted directly (as CI's ``uv run pytest ../shared -q`` does from
chat_orchestrator/), pytest can resolve the nearer repo-root pyproject.toml
instead, which has no ``asyncio_mode`` set (defaults to strict).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.escalation_service import EscalationService
from scripts import resolve_stale_swept_escalations as resolve_stale


class _Escalations:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows
        self.list_unfiled_calls: List[Dict[str, Any]] = []
        self.resolve_calls: List[str] = []

    async def list_unfiled(self, **kwargs: Any) -> List[Dict[str, Any]]:
        self.list_unfiled_calls.append(kwargs)
        return self._rows

    async def resolve(self, escalation_id: str) -> None:
        self.resolve_calls.append(escalation_id)


class _Deliveries:
    def __init__(self, has_delivery: Dict[str, bool]) -> None:
        self._has_delivery = has_delivery
        self.find_calls: List[str] = []

    async def find_by_escalation(self, escalation_id: str) -> Optional[Dict[str, Any]]:
        self.find_calls.append(escalation_id)
        return {"external_message_id": 1} if self._has_delivery.get(escalation_id) else None


def _service(
    rows: List[Dict[str, Any]], has_delivery: Dict[str, bool]
) -> EscalationService:
    svc = EscalationService(supabase_url="http://test.supabase.co", supabase_key="key")
    svc._escalations = _Escalations(rows)  # type: ignore[assignment]
    svc._deliveries = _Deliveries(has_delivery)  # type: ignore[assignment]
    return svc


# ---------------------------------------------------------------------------
# find_stale_untraceable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_stale_untraceable_keeps_only_rows_with_no_delivery_receipt():
    rows = [
        {"id": "esc-linked", "created_at": "2026-08-01T00:00:00+00:00"},
        {"id": "esc-orphan", "created_at": "2026-08-02T00:00:00+00:00"},
    ]
    svc = _service(rows, has_delivery={"esc-linked": True, "esc-orphan": False})

    stale = await resolve_stale.find_stale_untraceable(svc, max_age_hours=24, limit=200)

    assert [r["id"] for r in stale] == ["esc-orphan"]


@pytest.mark.asyncio
async def test_find_stale_untraceable_returns_nothing_when_every_row_is_linked():
    rows = [{"id": "esc-linked", "created_at": "2026-08-01T00:00:00+00:00"}]
    svc = _service(rows, has_delivery={"esc-linked": True})

    stale = await resolve_stale.find_stale_untraceable(svc, max_age_hours=24, limit=200)

    assert stale == []


@pytest.mark.asyncio
async def test_find_stale_untraceable_queries_open_unfiled_with_the_given_bounds():
    svc = _service([], has_delivery={})

    await resolve_stale.find_stale_untraceable(svc, max_age_hours=48, limit=50)

    call = svc._escalations.list_unfiled_calls[0]  # type: ignore[attr-defined]
    assert call["state"] == "open"
    assert call["exclude_reasons"] == ("safety_escalation",)
    assert call["limit"] == 50


# ---------------------------------------------------------------------------
# run() -- dry-run vs --apply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_dry_run_reports_matches_but_resolves_nothing(capsys, monkeypatch):
    rows = [{"id": "esc-orphan", "created_at": "2026-08-02T00:00:00+00:00"}]
    svc = _service(rows, has_delivery={"esc-orphan": False})
    monkeypatch.setattr(resolve_stale, "EscalationService", lambda: svc)

    await resolve_stale.run(apply=False, max_age_hours=24, limit=200)

    assert svc._escalations.resolve_calls == []  # type: ignore[attr-defined]
    out = capsys.readouterr().out
    assert "esc-orphan" in out
    assert "Dry run only" in out


@pytest.mark.asyncio
async def test_run_apply_resolves_every_match(monkeypatch):
    rows = [
        {"id": "esc-orphan-1", "created_at": "2026-08-02T00:00:00+00:00"},
        {"id": "esc-orphan-2", "created_at": "2026-08-03T00:00:00+00:00"},
    ]
    svc = _service(
        rows, has_delivery={"esc-orphan-1": False, "esc-orphan-2": False}
    )
    monkeypatch.setattr(resolve_stale, "EscalationService", lambda: svc)

    await resolve_stale.run(apply=True, max_age_hours=24, limit=200)

    assert svc._escalations.resolve_calls == ["esc-orphan-1", "esc-orphan-2"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_apply_does_not_resolve_linked_escalations(monkeypatch):
    rows = [{"id": "esc-linked", "created_at": "2026-08-02T00:00:00+00:00"}]
    svc = _service(rows, has_delivery={"esc-linked": True})
    monkeypatch.setattr(resolve_stale, "EscalationService", lambda: svc)

    await resolve_stale.run(apply=True, max_age_hours=24, limit=200)

    assert svc._escalations.resolve_calls == []  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_run_refuses_when_database_is_not_configured(monkeypatch):
    for var in ("CHAT_DB_URL", "SUPABASE_URL", "CHAT_DB_SERVICE_KEY", "SUPABASE_KEY"):
        monkeypatch.delenv(var, raising=False)
    unconfigured = EscalationService(supabase_url="", supabase_key="")
    monkeypatch.setattr(resolve_stale, "EscalationService", lambda: unconfigured)

    with pytest.raises(SystemExit):
        await resolve_stale.run(apply=False, max_age_hours=24, limit=200)


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_parse_args_defaults_to_dry_run():
    args = resolve_stale.parse_args([])
    assert args.apply is False
    assert args.max_age_hours == resolve_stale.DEFAULT_MAX_AGE_HOURS
    assert args.limit == resolve_stale.DEFAULT_LIMIT


def test_parse_args_accepts_apply_and_overrides():
    args = resolve_stale.parse_args(["--apply", "--max-age-hours", "72", "--limit", "10"])
    assert args.apply is True
    assert args.max_age_hours == 72
    assert args.limit == 10
