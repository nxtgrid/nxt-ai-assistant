"""Tests for scripts/backfill_escalation_org_hashtag.py -- a one-time
backfill of escalations.org_hashtag on rows created before PR #192, so the
daily sweep alert can label them with an org instead of a bare UUID.

`scripts` has no `__init__.py` but is importable as a namespace package
given the repo root is on PYTHONPATH (see CLAUDE.md; same as
test_resolve_stale_swept_escalations.py). Async tests carry an explicit
@pytest.mark.asyncio for the reason that file's docstring gives.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from scripts import backfill_escalation_org_hashtag as backfill


class _Query:
    def __init__(self, client: "_Client", table: str):
        self.c = client
        self.table = table
        self.mode = "select"
        self.payload: Dict[str, Any] | None = None
        self.filters: List[tuple] = []

    def select(self, *_a):
        return self

    def update(self, payload):
        self.mode = "update"
        self.payload = payload
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def limit(self, _n):
        return self

    def execute(self):
        self.c.calls.append((self.table, self.mode, self.payload, self.filters))
        if self.mode == "update":
            return _Resp([])
        return _Resp(self.c.responses.pop(0))


class _Resp:
    def __init__(self, data):
        self.data = data


class _Client:
    def __init__(self, responses):
        self.calls: List[tuple] = []
        self.responses = responses

    def table(self, name):
        return _Query(self, name)


class _Svc:
    def __init__(self, responses):
        self._supabase_url = "http://test.supabase.co"
        self._supabase_key = "key"
        self._client = _Client(responses)

    def _get_raw_client(self):
        return self._client


@pytest.mark.asyncio
async def test_dry_run_reports_but_writes_nothing(capsys, monkeypatch):
    svc = _Svc(
        responses=[
            [{"id": "esc-1", "chat_session_id": "s-1", "org_hashtag": None}],
            [{"metadata": {"organization_short_name": "Acme Energy"}}],
        ]
    )
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=False)

    assert [c for c in svc._client.calls if c[1] == "update"] == []
    out = capsys.readouterr().out
    assert "esc-1" in out and "#AcmeEnergy" in out and "Dry run" in out


@pytest.mark.asyncio
async def test_apply_writes_org_hashtag_from_session_metadata(monkeypatch):
    svc = _Svc(
        responses=[
            [{"id": "esc-1", "chat_session_id": "s-1", "org_hashtag": None}],
            [{"metadata": {"organization_short_name": "Acme Energy"}}],
        ]
    )
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=True)

    updates = [c for c in svc._client.calls if c[1] == "update"]
    assert len(updates) == 1
    table, _mode, payload, filters = updates[0]
    assert table == "escalations"
    assert payload == {"org_hashtag": "#AcmeEnergy"}
    assert ("eq", "id", "esc-1") in filters
    assert ("is", "org_hashtag", "null") in filters  # idempotent guard


@pytest.mark.asyncio
async def test_apply_skips_rows_whose_session_has_no_org(capsys, monkeypatch):
    svc = _Svc(
        responses=[
            [{"id": "esc-2", "chat_session_id": "s-2", "org_hashtag": None}],
            [{"metadata": {}}],
        ]
    )
    monkeypatch.setattr(backfill, "EscalationService", lambda: svc)

    await backfill.run(apply=True)

    assert [c for c in svc._client.calls if c[1] == "update"] == []
    assert "skip" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_run_refuses_when_db_not_configured(monkeypatch):
    class _Bare:
        _supabase_url = ""
        _supabase_key = ""

    monkeypatch.setattr(backfill, "EscalationService", lambda: _Bare())
    with pytest.raises(SystemExit):
        await backfill.run(apply=False)


def test_parse_args_defaults_to_dry_run():
    assert backfill.parse_args([]).apply is False
    assert backfill.parse_args(["--apply"]).apply is True
