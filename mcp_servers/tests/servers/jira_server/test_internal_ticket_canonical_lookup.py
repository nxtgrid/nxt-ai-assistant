"""Regression test for the internal-ticket / canonical-schema mismatch.

The ticket schema consolidation (db/migrations/0005a) moved internal tickets
from ``internal_tickets``/``internal_ticket_comments`` into the canonical
``tickets``/``ticket_comments`` tables, and every writer (InternalTicketBackend)
was updated to match. This Jira MCP server's internal-ticket detection was not:
it kept querying the retired ``internal_tickets`` table, which nothing writes
to any more. The lookup always returned no row, so every internal ticket
(TKT-*) fell through to the Jira-only path and the server tried to look it up
on the real Jira API instead.
"""

import json
import os
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.jira_server import jira_mcp_server as jira_module  # noqa: E402

pytestmark = pytest.mark.asyncio


def _parse(result):
    assert len(result) == 1
    return json.loads(result[0].text)


class _FakeQuery:
    """Minimal select/insert/update chain over a live (mutable) row list."""

    def __init__(self, rows, mode, payload=None):
        self._rows = rows
        self._preds = []
        self._mode = mode
        self._payload = payload
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, col, val):
        self._preds.append(lambda r: r.get(col) == val)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self._mode == "insert":
            row = dict(self._payload)
            row.setdefault("id", f"generated-{len(self._rows)}")
            self._rows.append(row)
            return SimpleNamespace(data=[row])

        matches = [r for r in self._rows if all(p(r) for p in self._preds)]

        if self._mode == "update":
            for r in matches:
                r.update(self._payload)
            return SimpleNamespace(data=matches)

        if self._limit is not None:
            matches = matches[: self._limit]
        return SimpleNamespace(data=matches)


class _FakeTableProxy:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_args, **_kwargs):
        return _FakeQuery(self._rows, "select")

    def insert(self, payload):
        return _FakeQuery(self._rows, "insert", payload)

    def update(self, payload):
        return _FakeQuery(self._rows, "update", payload)


class _FakeDb:
    """Seeded with live table -> rows lists so inserts/updates are observable."""

    def __init__(self, tables: dict[str, list[dict]]):
        self._tables = tables

    def table(self, name):
        self._tables.setdefault(name, [])
        return _FakeTableProxy(self._tables[name])


@pytest.fixture
def fake_tables(monkeypatch):
    tables: dict[str, list[dict]] = {"tickets": [], "ticket_comments": []}
    db = _FakeDb(tables)
    monkeypatch.setattr(jira_module.client, "_get_chat_supabase", lambda: db)
    yield tables


async def test_internal_ticket_row_reads_canonical_tickets_table(fake_tables):
    fake_tables["tickets"].append(
        {
            "id": "uuid-1",
            "ticket_ref": "TKT-000001",
            "backend": "internal",
            "summary": "Meter offline",
            "status": "open",
        }
    )

    row = await jira_module.client._internal_ticket_row("TKT-000001")

    assert row is not None
    assert row["id"] == "uuid-1"
    assert row["summary"] == "Meter offline"


async def test_internal_ticket_row_never_matches_a_jira_backed_ticket(fake_tables):
    """Backend must come from the stored column, never be inferred from the
    ref's shape -- a Jira row must not be treated as internal even if looked
    up by the same code path."""
    fake_tables["tickets"].append(
        {"id": "uuid-2", "ticket_ref": "OPS-100", "backend": "jira", "summary": "Jira ticket"}
    )

    row = await jira_module.client._internal_ticket_row("OPS-100")

    assert row is None


async def test_get_internal_ticket_joins_comments_by_ticket_id(fake_tables):
    fake_tables["tickets"].append(
        {"id": "uuid-1", "ticket_ref": "TKT-000001", "backend": "internal", "summary": "S"}
    )
    fake_tables["ticket_comments"].extend(
        [
            {
                "ticket_id": "uuid-1",
                "author": "staff",
                "body": "note on this ticket",
                "is_public": False,
                "source": "staff",
                "created_at": "2026-07-01T00:00:00Z",
            },
            {
                "ticket_id": "uuid-other",
                "author": "staff",
                "body": "note on a different ticket",
                "is_public": False,
                "source": "staff",
                "created_at": "2026-07-01T00:00:00Z",
            },
        ]
    )

    row = await jira_module.client.get_internal_ticket("TKT-000001")

    assert row is not None
    assert [c["body"] for c in row["comments"]] == ["note on this ticket"]


async def test_add_internal_comment_resolves_ticket_id_before_inserting(fake_tables):
    fake_tables["tickets"].append(
        {"id": "uuid-1", "ticket_ref": "TKT-000001", "backend": "internal", "summary": "S"}
    )

    ok = await jira_module.client.add_internal_comment("TKT-000001", "hello from staff")

    assert ok is True
    assert len(fake_tables["ticket_comments"]) == 1
    inserted = fake_tables["ticket_comments"][0]
    assert inserted["ticket_id"] == "uuid-1"
    assert inserted["body"] == "hello from staff"
    assert inserted["is_public"] is False
    assert inserted["source"] == "staff"


async def test_add_internal_comment_returns_false_for_unknown_ref(fake_tables):
    ok = await jira_module.client.add_internal_comment("TKT-999999", "hello")

    assert ok is False
    assert fake_tables["ticket_comments"] == []


class _FakeHttpResponse:
    """Async context manager standing in for aiohttp's response object."""

    def __init__(self, status: int) -> None:
        self.status = status

    async def __aenter__(self) -> "_FakeHttpResponse":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class _FakeHttpSession:
    """Async context manager standing in for aiohttp.ClientSession.

    close_internal_ticket now closes tickets over HTTP (through the
    orchestrator's TicketService, not a direct DB write -- see its
    docstring), so these tests exercise the HTTP call rather than a
    Supabase fake.
    """

    def __init__(self, status: int = 200) -> None:
        self._status = status
        self.posted_urls: list[str] = []
        self.posted_headers: list[dict] = []

    async def __aenter__(self) -> "_FakeHttpSession":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    def post(self, url, **kwargs):
        self.posted_urls.append(url)
        self.posted_headers.append(kwargs.get("headers"))
        return _FakeHttpResponse(self._status)


async def test_close_internal_ticket_posts_to_the_orchestrator(monkeypatch):
    monkeypatch.setenv("CHAT_ORCHESTRATOR_URL", "http://orchestrator.internal")
    monkeypatch.setenv("API_KEY", "secret-key")
    fake_session = _FakeHttpSession(status=200)
    monkeypatch.setattr(jira_module.aiohttp, "ClientSession", lambda: fake_session)

    ok = await jira_module.client.close_internal_ticket("TKT-000001")

    assert ok is True
    assert fake_session.posted_urls == [
        "http://orchestrator.internal/internal/tickets/TKT-000001/close"
    ]
    assert fake_session.posted_headers == [{"X-Api-Key": "secret-key"}]


async def test_close_internal_ticket_returns_false_on_http_error(monkeypatch):
    monkeypatch.setenv("CHAT_ORCHESTRATOR_URL", "http://orchestrator.internal")
    fake_session = _FakeHttpSession(status=500)
    monkeypatch.setattr(jira_module.aiohttp, "ClientSession", lambda: fake_session)

    ok = await jira_module.client.close_internal_ticket("TKT-000001")

    assert ok is False


async def test_close_internal_ticket_returns_false_without_orchestrator_url(monkeypatch):
    monkeypatch.delenv("CHAT_ORCHESTRATOR_URL", raising=False)

    ok = await jira_module.client.close_internal_ticket("TKT-000001")

    assert ok is False


async def test_get_issue_tool_returns_internal_ticket_without_touching_jira(
    fake_tables, monkeypatch
):
    """End-to-end regression for the reported bug: get_issue on a TKT-* ref
    must resolve via the canonical tables and never fall through to the Jira
    API (which would 404 -- Jira has never heard of an internal ref)."""
    fake_tables["tickets"].append(
        {
            "id": "uuid-1",
            "ticket_ref": "TKT-000001",
            "backend": "internal",
            "summary": "Meter offline",
            "description": "desc",
            "status": "open",
            "ticket_type": "Task",
            "assignee_email": None,
            "created_at": "2026-07-01T00:00:00Z",
            "updated_at": "2026-07-01T00:00:00Z",
            "labels": [],
            "grid_name": "Grid A",
        }
    )

    async def _boom(*_args, **_kwargs):
        raise AssertionError("must not call the Jira API for an internal ticket ref")

    monkeypatch.setattr(jira_module.client, "get_issue", _boom)

    result = await jira_module._tool_get_issue({"issue_key": "TKT-000001"})

    data = _parse(result)
    assert data["backend"] == "internal"
    assert data["key"] == "TKT-000001"
    assert data["summary"] == "Meter offline"
