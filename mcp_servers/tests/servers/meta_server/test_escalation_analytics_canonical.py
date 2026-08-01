"""Regression tests for the canonical (escalations-table) analytics queries
in meta_mcp_server.py.

escalations has no organization_id column, so every one of these resolves an
organization filter as a separate chat_sessions lookup rather than an
embedded-filter query -- these tests exist to prove that resolution behaves
correctly (org scoping, date window, extra predicates, ordering/limit) and
degrades safely (returns None, not a wrong/partial answer) on any failure.
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

# meta_mcp_server imports vl_convert (chart rendering) at module scope, which
# isn't installed in this dev environment (pre-existing gap, unrelated to
# this module's actual logic under test) -- stub it so the import succeeds.
sys.modules.setdefault("vl_convert", MagicMock())

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("CHAT_DB_URL", "https://example.test.supabase.co")
os.environ.setdefault("CHAT_DB_SERVICE_KEY", "test-key")

from servers.meta_server.meta_mcp_server import (  # noqa: E402
    _canonical_escalated_session_ids,
    _canonical_escalations_query,
)

_START = datetime(2026, 1, 1, tzinfo=timezone.utc)
_END = _START + timedelta(days=7)


class _FakeResponse:
    def __init__(self, data: List[Dict[str, Any]]):
        self.data = data


class _FakeQuery:
    def __init__(self, rows: List[Dict[str, Any]], raise_on_execute: Optional[Exception] = None):
        self._rows = rows
        self._filters: List[tuple] = []
        self._not_null: List[str] = []
        self._order: Optional[tuple] = None
        self._limit_n: Optional[int] = None
        self._raise = raise_on_execute

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col, vals):
        self._filters.append(("in", col, list(vals)))
        return self

    def gte(self, *_a, **_k):
        return self

    def lt(self, *_a, **_k):
        return self

    @property
    def not_(self):
        return self

    def is_(self, col, _val):
        self._not_null.append(col)
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit_n = n
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
            for col in self._not_null:
                if row.get(col) is None:
                    ok = False
            if ok:
                matches.append(row)
        if self._order is not None:
            col, desc = self._order
            matches.sort(key=lambda r: r.get(col), reverse=desc)
        if self._limit_n is not None:
            matches = matches[: self._limit_n]
        return _FakeResponse(matches)


class _FakeClient:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]], raise_on: Optional[str] = None):
        self._tables = tables
        self._raise_on = raise_on

    def table(self, name):
        raise_exc = RuntimeError("db down") if name == self._raise_on else None
        return _FakeQuery(self._tables.get(name, []), raise_on_execute=raise_exc)


class TestCanonicalEscalationsQuery:
    def test_no_org_filter_returns_all_rows_in_window(self):
        client = _FakeClient({"escalations": [{"reason": "could_not_answer"}]})

        rows = _canonical_escalations_query(client, _START, _END, None, "reason")

        assert rows == [{"reason": "could_not_answer"}]

    def test_org_filter_only_returns_rows_for_that_orgs_sessions(self):
        client = _FakeClient(
            {
                "chat_sessions": [
                    {"id": "session-org7", "organization_id": 7},
                    {"id": "session-org9", "organization_id": 9},
                ],
                "escalations": [
                    {"chat_session_id": "session-org7", "reason": "could_not_answer"},
                    {"chat_session_id": "session-org9", "reason": "out_of_scope"},
                ],
            }
        )

        rows = _canonical_escalations_query(
            client, _START, _END, 7, "chat_session_id, reason"
        )

        assert rows == [{"chat_session_id": "session-org7", "reason": "could_not_answer"}]

    def test_returns_empty_list_when_org_has_no_sessions(self):
        client = _FakeClient({"chat_sessions": [], "escalations": [{"reason": "x"}]})

        rows = _canonical_escalations_query(client, _START, _END, 7, "reason")

        assert rows == []

    def test_extra_eq_filter_applied(self):
        client = _FakeClient(
            {
                "escalations": [
                    {"reason": "staff_action_required", "action_type": "wallet_credit"},
                    {"reason": "out_of_scope", "action_type": None},
                ]
            }
        )

        rows = _canonical_escalations_query(
            client, _START, _END, None, "action_type", extra_eq=("reason", "staff_action_required")
        )

        assert rows == [{"reason": "staff_action_required", "action_type": "wallet_credit"}]

    def test_require_not_null_filter_applied(self):
        client = _FakeClient(
            {
                "escalations": [
                    {"created_at": "t1", "resolved_at": "t2"},
                    {"created_at": "t1", "resolved_at": None},
                ]
            }
        )

        rows = _canonical_escalations_query(
            client, _START, _END, None, "created_at, resolved_at", require_not_null="resolved_at"
        )

        assert rows == [{"created_at": "t1", "resolved_at": "t2"}]

    def test_order_by_and_limit_applied(self):
        client = _FakeClient(
            {
                "escalations": [
                    {"created_at": "2026-01-01"},
                    {"created_at": "2026-01-03"},
                    {"created_at": "2026-01-02"},
                ]
            }
        )

        rows = _canonical_escalations_query(
            client, _START, _END, None, "created_at", order_by=("created_at", True), limit=2
        )

        assert [r["created_at"] for r in rows] == ["2026-01-03", "2026-01-02"]

    def test_returns_none_on_error_so_caller_falls_back_to_legacy(self):
        client = _FakeClient({}, raise_on="escalations")

        assert _canonical_escalations_query(client, _START, _END, None, "reason") is None

    def test_returns_none_when_session_lookup_fails(self):
        client = _FakeClient({}, raise_on="chat_sessions")

        assert _canonical_escalations_query(client, _START, _END, 7, "reason") is None


class TestCanonicalEscalatedSessionIds:
    def test_returns_the_set_of_chat_session_ids(self):
        client = _FakeClient(
            {
                "escalations": [
                    {"chat_session_id": "s1"},
                    {"chat_session_id": "s2"},
                    {"chat_session_id": "s1"},
                ]
            }
        )

        result = _canonical_escalated_session_ids(client, _START, _END, None)

        assert result == {"s1", "s2"}

    def test_returns_none_on_error(self):
        client = _FakeClient({}, raise_on="escalations")

        assert _canonical_escalated_session_ids(client, _START, _END, None) is None
