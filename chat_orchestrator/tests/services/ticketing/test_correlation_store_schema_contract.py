"""Guard against CorrelationStore writing to columns the checked-in schema
doesn't have.

This is the test that would have caught the 2026-08-10 production incident:
migration 0005b (db/migrations/0005b_ticket_schema_validate_and_contract.sql)
dropped ten columns from ``ticket_correlations``/``ticket_correlation_events``,
but ``correlation_store.py`` kept reading and writing every one of them. Every
write silently swallowed its own PostgREST "column does not exist" error and
returned an empty/false value, so the failure was invisible to every caller
and to every existing unit test (which all use a fake client that accepts any
payload) -- the only thing that would have caught it is exactly this: parsing
the real column list out of the schema and asserting write payloads/filters
never name a column that isn't there.

No database, no network -- ``db/schema/chat_db.sql`` is parsed as text, and
CorrelationStore's write methods are driven against a capturing fake client
(in the same style as ``test_correlation_store.py``'s ``FakeRawClient``, but
recording every column name touched instead of storing rows).

At the point this file was written, ``test_write_methods_never_touch_a_column_the_schema_does_not_have``
drives the *pre-cutover* (ticket_ref-keyed) CorrelationStore surface and is
expected to fail -- proving the guard actually detects the incident's real
failure mode, not merely that it exists. Once CorrelationStore is rewritten
to the ticket_id-keyed surface (this plan's Task A4), the driving calls below
are updated to the new method signatures and the same test starts passing.
That is deliberate: this is a live contract against the schema, not a frozen
snapshot of one interface.

Deliberately does not validate the ``tickets`` table: ``db/schema/chat_db.sql``
predates the 0005a/0005b consolidation and does not define it (see the NOTE
above ``ticket_correlations`` in that file) -- only the two correlation
tables this incident actually touched are in scope here.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import pytest

from orchestrator.services.ticketing.correlation_store import CorrelationStore

# This file lives at <repo_root>/chat_orchestrator/tests/services/ticketing/,
# so the schema it validates against is four parents up and back down into
# db/schema/ (see test_ticket_backend_migration.py / test_ticket_schema_expand_migration.py
# for the same convention one directory shallower).
REPO_ROOT = Path(__file__).resolve().parents[4]
SCHEMA_SQL = REPO_ROOT / "db" / "schema" / "chat_db.sql"

_CONSTRAINT_KEYWORDS = ("PRIMARY KEY", "UNIQUE", "CHECK", "FOREIGN KEY", "CONSTRAINT")


def _extract_table_columns(sql_text: str, table_name: str) -> Set[str]:
    """Parse column names out of one ``CREATE TABLE IF NOT EXISTS <table_name> (...)``
    block. Naive on purpose (this schema file has no nested parens inside
    column definitions) -- matches up to the first unindented ``);``."""
    pattern = re.compile(
        rf"CREATE TABLE IF NOT EXISTS {re.escape(table_name)} \((.*?)\n\);",
        re.DOTALL,
    )
    match = pattern.search(sql_text)
    if not match:
        raise AssertionError(f"Could not find CREATE TABLE for {table_name!r} in {SCHEMA_SQL}")

    columns: Set[str] = set()
    for line in match.group(1).splitlines():
        stripped = line.strip().rstrip(",")
        if not stripped:
            continue
        if any(stripped.upper().startswith(kw) for kw in _CONSTRAINT_KEYWORDS):
            continue
        columns.add(stripped.split()[0])
    return columns


@pytest.fixture(scope="module")
def real_columns() -> Dict[str, Set[str]]:
    sql_text = SCHEMA_SQL.read_text()
    return {
        "ticket_correlations": _extract_table_columns(sql_text, "ticket_correlations"),
        "ticket_correlation_events": _extract_table_columns(sql_text, "ticket_correlation_events"),
    }


def test_parses_the_expected_columns_as_a_sanity_check(real_columns: Dict[str, Set[str]]) -> None:
    """If this fails, the regex above stopped matching the file -- fix the
    parser before trusting anything else in this module."""
    assert "ticket_id" in real_columns["ticket_correlations"]
    assert "occurrence_count" in real_columns["ticket_correlations"]
    assert "ticket_ref" not in real_columns["ticket_correlations"]
    assert "ticket_id" in real_columns["ticket_correlation_events"]
    assert "grid_name" in real_columns["ticket_correlation_events"]


# ---------------------------------------------------------------------------
# Capturing fake client -- records every column name a write touches instead
# of storing rows, so this test can drive CorrelationStore's real methods and
# diff what they touched against what the schema actually has.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, data: Any):
        self.data = data


class _CaptureTable:
    def __init__(self, recorder: "ColumnRecorder", table_name: str):
        self._recorder = recorder
        self._table_name = table_name
        self._mode: Optional[str] = None

    def select(self, *_a, **_k) -> "_CaptureTable":
        self._mode = "select"
        return self

    def insert(self, payload: Dict[str, Any]) -> "_CaptureTable":
        self._mode = "insert"
        self._recorder.record(self._table_name, payload.keys())
        return self

    def update(self, payload: Dict[str, Any]) -> "_CaptureTable":
        self._mode = "update"
        self._recorder.record(self._table_name, payload.keys())
        return self

    def upsert(self, payload: Dict[str, Any], on_conflict: Optional[str] = None) -> "_CaptureTable":
        self._mode = "upsert"
        self._recorder.record(self._table_name, payload.keys())
        if on_conflict:
            self._recorder.record(self._table_name, [on_conflict])
        return self

    def eq(self, field: str, _value: Any) -> "_CaptureTable":
        self._recorder.record(self._table_name, [field])
        return self

    def gte(self, field: str, _value: Any) -> "_CaptureTable":
        self._recorder.record(self._table_name, [field])
        return self

    def order(self, field: str, desc: bool = False) -> "_CaptureTable":
        self._recorder.record(self._table_name, [field])
        return self

    def limit(self, _n: int) -> "_CaptureTable":
        return self

    def execute(self) -> _FakeResult:
        # A read against ticket_correlations returns one synthetic existing
        # row -- merge_affected_key/bump_occurrence read-before-write and
        # bail out on an empty result, which would skip their .update() call
        # (and the payload columns it touches) entirely. Every other table
        # (in particular "tickets", used by _correlation_filter's own
        # lookup) stays empty, so that lookup falls through to its
        # ticket_ref-based fallback -- the exact pre-cutover path this test
        # needs to exercise.
        if self._table_name == "ticket_correlations" and self._mode == "select":
            return _FakeResult([{"affected_keys": [], "signatures": [], "occurrence_count": 1}])
        return _FakeResult([])


class ColumnRecorder:
    """Accumulates every column name touched, per table."""

    def __init__(self) -> None:
        self.touched: Dict[str, Set[str]] = {}

    def record(self, table_name: str, fields: Any) -> None:
        self.touched.setdefault(table_name, set()).update(fields)

    def table(self, name: str) -> _CaptureTable:
        return _CaptureTable(self, name)


@pytest.mark.asyncio
async def test_write_methods_never_touch_a_column_the_schema_does_not_have(
    real_columns: Dict[str, Set[str]],
) -> None:
    """Drives every CorrelationStore write method and asserts every column
    name it touched (payload keys, .eq()/.gte() filter columns, on_conflict)
    on ticket_correlations/ticket_correlation_events is a real column. See
    the module docstring for why this currently fails and what makes it
    pass.
    """
    recorder = ColumnRecorder()
    store = CorrelationStore(get_client=lambda: recorder)

    await store.record_event(
        ticket_id="ticket-uuid-1",
        grid_name="Kudi",
        source="n8n",
        signature="sig-a",
        dedup_key="dk-1",
        decision="new",
        decided_by="no_candidates",
        confidence=None,
        reason="no open candidates",
        candidate_refs=[],
        alert={"subject": "s"},
        llm_raw=None,
    )
    await store.record_event_ticket_id("dk-1", "ticket-uuid-1")
    await store.upsert_correlation(
        ticket_id="ticket-uuid-1",
        root_cause_kind=None,
        primary_signature="sig-a",
        signatures=["sig-a"],
        affected_keys=[],
        summary_base="s",
        description_base="d",
        severity="warning",
    )
    await store.merge_affected_key("ticket-uuid-1", kind="mppt", key="A3", label="MPPT A3")
    await store.bump_occurrence("ticket-uuid-1")
    await store.record_amendment("ticket-uuid-1", severity="urgent", escalated=True)

    invalid: Dict[str, List[str]] = {}
    for table_name in ("ticket_correlations", "ticket_correlation_events"):
        touched = recorder.touched.get(table_name, set())
        bad = sorted(touched - real_columns[table_name])
        if bad:
            invalid[table_name] = bad

    assert not invalid, (
        f"CorrelationStore wrote to column(s) not present in {SCHEMA_SQL}: {invalid}"
    )
