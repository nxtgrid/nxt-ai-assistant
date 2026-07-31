"""Regression tests for the canonical (escalations-table) path of
get_my_open_issues.

This is a multi-tenant isolation boundary: escalations has no
organization_id column, so the canonical lookup resolves it via a separate
chat_sessions query rather than a single embedded-filter query. These tests
exist specifically to prove that resolution never leaks another
organization's open issues -- the failure mode this guards is the highest
severity in the whole ticket-schema cutover.
"""

import os
import sys
from typing import Any, Dict, List, Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.customer_mcp_server import (  # noqa: E402
    _get_my_open_issues_canonical,
)


class _FakeResponse:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], raise_on_execute: Optional[Exception] = None):
        self._rows = rows
        self._filters: List[tuple] = []
        self._raise = raise_on_execute

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n):
        return self

    def execute(self):
        if self._raise is not None:
            raise self._raise
        matches = []
        for row in self._rows:
            ok = True
            for op, col, val in self._filters:
                if op == "eq" and row.get(col) != val:
                    ok = False
                elif op == "in" and row.get(col) not in val:
                    ok = False
            if ok:
                matches.append(row)
        return _FakeResponse(matches)


class _FakeClient:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]], raise_on: Optional[str] = None):
        self._tables = tables
        self._raise_on = raise_on

    def table(self, name):
        raise_exc = RuntimeError("db down") if name == self._raise_on else None
        return _FakeQuery(self._tables.get(name, []), raise_on_execute=raise_exc)


def test_only_resolves_issues_for_the_requesting_organization():
    """The core isolation guarantee: a session belonging to a different org
    must never surface another org's escalation, even though escalations
    itself carries no organization_id to filter on directly."""
    client = _FakeClient(
        {
            "chat_sessions": [
                {"id": "session-org7-a", "organization_id": 7},
                {"id": "session-org9-a", "organization_id": 9},
            ],
            "escalations": [
                {
                    "id": "esc-org7",
                    "chat_session_id": "session-org7-a",
                    "state": "open",
                    "question_text": "org7 issue",
                },
                {
                    "id": "esc-org9",
                    "chat_session_id": "session-org9-a",
                    "state": "open",
                    "question_text": "org9 issue",
                },
            ],
        }
    )

    rows = _get_my_open_issues_canonical(client, organization_id=7)

    assert rows is not None
    ids = {r["id"] for r in rows}
    assert ids == {"esc-org7"}
    assert "esc-org9" not in ids


def test_filters_to_open_and_processing_states_only():
    client = _FakeClient(
        {
            "chat_sessions": [{"id": "session-1", "organization_id": 7}],
            "escalations": [
                {"id": "esc-open", "chat_session_id": "session-1", "state": "open"},
                {"id": "esc-processing", "chat_session_id": "session-1", "state": "processing"},
                {"id": "esc-resolved", "chat_session_id": "session-1", "state": "resolved"},
                {"id": "esc-tracked", "chat_session_id": "session-1", "state": "tracked"},
            ],
        }
    )

    rows = _get_my_open_issues_canonical(client, organization_id=7)

    assert rows is not None
    ids = {r["id"] for r in rows}
    assert ids == {"esc-open", "esc-processing"}


def test_returns_empty_list_when_org_has_no_sessions():
    client = _FakeClient({"chat_sessions": [], "escalations": []})

    rows = _get_my_open_issues_canonical(client, organization_id=7)

    assert rows == []


def test_returns_none_on_error_so_caller_falls_back_to_legacy():
    client = _FakeClient(
        {"chat_sessions": [{"id": "session-1", "organization_id": 7}]},
        raise_on="escalations",
    )

    assert _get_my_open_issues_canonical(client, organization_id=7) is None


def test_returns_none_when_session_lookup_itself_fails():
    client = _FakeClient({}, raise_on="chat_sessions")

    assert _get_my_open_issues_canonical(client, organization_id=7) is None
