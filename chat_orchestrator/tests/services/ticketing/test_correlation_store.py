"""Tests for CorrelationStore, the chat_db-backed state layer alert
correlation reads/writes against (ticket_correlations / ticket_correlation_events;
see db/migrations/0003_alert_correlation.sql).

Uses a small fake standing in for the raw postgrest client, in the same
style as test_internal_backend.py's FakeSupabaseClient -- supports the
predicate/order/limit chain CorrelationStore actually issues, plus
``.contains()`` for the signatures jsonb-array lookup and ``.upsert()`` for
correlation-row creation.

Every CorrelationStore method must swallow errors and return a safe empty
value (None/[]/False) -- a correlation-store outage degrades correlation to
"no candidates found" (file a new ticket), never a hard failure. That
contract is exercised via ``raise_on_execute`` on the relevant fake table.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from orchestrator.services.ticketing.correlation_store import CorrelationStore


class _FakeResult:
    def __init__(self, data: Any):
        self.data = data


class _FakeTable:
    """Fakes select/insert/update/upsert/eq/gte/contains/order/limit/execute."""

    def __init__(self, store: "FakeRawClient", name: str):
        self._store = store
        self._name = name
        self._mode: Optional[str] = None
        self._predicates: List[tuple] = []  # (op, field, value)
        self._order_field: Optional[str] = None
        self._order_desc: bool = False
        self._limit: Optional[int] = None
        self._payload: Optional[Dict[str, Any]] = None

    @property
    def _rows(self) -> List[Dict[str, Any]]:
        return self._store.tables[self._name]

    def select(self, *_a, **_k) -> "_FakeTable":
        self._mode = "select"
        return self

    def insert(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._mode = "insert"
        self._payload = payload
        return self

    def update(self, payload: Dict[str, Any]) -> "_FakeTable":
        self._mode = "update"
        self._payload = payload
        return self

    def upsert(self, payload: Dict[str, Any], on_conflict: Optional[str] = None) -> "_FakeTable":
        self._mode = "upsert"
        self._payload = payload
        return self

    def eq(self, field: str, value: Any) -> "_FakeTable":
        self._predicates.append(("eq", field, value))
        return self

    def gte(self, field: str, value: Any) -> "_FakeTable":
        self._predicates.append(("gte", field, value))
        return self

    def contains(self, field: str, value: Any) -> "_FakeTable":
        self._predicates.append(("contains", field, value))
        return self

    def order(self, field: str, desc: bool = False) -> "_FakeTable":
        self._order_field = field
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_FakeTable":
        self._limit = n
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for op, field, value in self._predicates:
            if op == "eq" and row.get(field) != value:
                return False
            if op == "gte" and (row.get(field) or "") < value:
                return False
            if op == "contains":
                haystack = row.get(field) or []
                if not isinstance(value, list) or not all(v in haystack for v in value):
                    return False
        return True

    def execute(self) -> _FakeResult:
        if self._store.raise_on_execute.get(self._name):
            raise self._store.raise_on_execute[self._name]

        if self._mode == "select":
            matches = [r for r in self._rows if self._matches(r)]
            if self._order_field:
                matches.sort(
                    key=lambda r: r.get(self._order_field) or "", reverse=self._order_desc
                )
            if self._limit is not None:
                matches = matches[: self._limit]
            return _FakeResult(matches)

        if self._mode == "insert":
            row = dict(self._payload or {})
            self._rows.append(row)
            return _FakeResult([row])

        if self._mode == "update":
            updated = []
            for row in self._rows:
                if self._matches(row):
                    row.update(self._payload or {})
                    updated.append(row)
            return _FakeResult(updated)

        if self._mode == "upsert":
            key = self._payload.get("ticket_ref")
            for row in self._rows:
                if row.get("ticket_ref") == key:
                    row.update(self._payload)
                    return _FakeResult([row])
            row = dict(self._payload)
            self._rows.append(row)
            return _FakeResult([row])

        raise AssertionError("execute() called before select/insert/update/upsert")


class FakeRawClient:
    def __init__(self) -> None:
        self.tables: Dict[str, List[Dict[str, Any]]] = {
            "ticket_correlations": [],
            "ticket_correlation_events": [],
        }
        self.raise_on_execute: Dict[str, Exception] = {}

    def table(self, name: str) -> _FakeTable:
        return _FakeTable(self, name)


def _make_store(client: Optional[FakeRawClient] = None) -> tuple[CorrelationStore, FakeRawClient]:
    fake = client or FakeRawClient()
    store = CorrelationStore(get_client=lambda: fake)
    return store, fake


class TestGetByDedupKey:
    @pytest.mark.asyncio
    async def test_finds_prior_event(self):
        store, fake = _make_store()
        fake.tables["ticket_correlation_events"].append(
            {"dedup_key": "k1", "ticket_ref": "TKT-1", "decision": "new", "decided_by": "no_candidates"}
        )

        event = await store.get_by_dedup_key("k1")

        assert event is not None
        assert event["ticket_ref"] == "TKT-1"

    @pytest.mark.asyncio
    async def test_none_when_not_found(self):
        store, _fake = _make_store()
        assert await store.get_by_dedup_key("missing") is None

    @pytest.mark.asyncio
    async def test_none_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlation_events"] = RuntimeError("db down")
        store, _ = _make_store(fake)

        assert await store.get_by_dedup_key("k1") is None

    @pytest.mark.asyncio
    async def test_none_when_no_client(self):
        store = CorrelationStore(get_client=lambda: None)
        assert await store.get_by_dedup_key("k1") is None


class TestOpenCandidatesForGrid:
    @pytest.mark.asyncio
    async def test_returns_open_tickets_for_grid_ordered_desc(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "grid_name": "Kudi", "status": "open", "last_alert_at": "2026-01-01T00:00:00Z"},
            {"ticket_ref": "TKT-2", "grid_name": "Kudi", "status": "open", "last_alert_at": "2026-01-02T00:00:00Z"},
            {"ticket_ref": "TKT-3", "grid_name": "Other", "status": "open", "last_alert_at": "2026-01-03T00:00:00Z"},
            {"ticket_ref": "TKT-4", "grid_name": "Kudi", "status": "done", "last_alert_at": "2026-01-04T00:00:00Z"},
        ]

        results = await store.open_candidates_for_grid("Kudi", since_iso="2025-01-01T00:00:00Z")

        assert [r["ticket_ref"] for r in results] == ["TKT-2", "TKT-1"]

    @pytest.mark.asyncio
    async def test_respects_since_and_limit(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": f"TKT-{i}", "grid_name": "Kudi", "status": "open", "last_alert_at": f"2026-01-0{i}T00:00:00Z"}
            for i in range(1, 6)
        ]

        results = await store.open_candidates_for_grid(
            "Kudi", since_iso="2026-01-03T00:00:00Z", limit=1
        )

        assert len(results) == 1
        assert results[0]["ticket_ref"] == "TKT-5"

    @pytest.mark.asyncio
    async def test_empty_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("db down")
        store, _ = _make_store(fake)

        assert await store.open_candidates_for_grid("Kudi", since_iso="2026-01-01") == []


class TestUpsertCorrelation:
    @pytest.mark.asyncio
    async def test_creates_new_row(self):
        store, fake = _make_store()

        ok = await store.upsert_correlation(
            ticket_ref="TKT-1",
            ticket_backend="internal",
            grid_name="Kudi",
            organization_id=7,
            root_cause_kind=None,
            primary_signature="sig-a",
            signatures=["sig-a"],
            affected_keys=[],
            summary_base="s",
            description_base="d",
            severity="warning",
            telegram_chat_id="-100",
            telegram_topic_id="5",
        )

        assert ok is True
        assert len(fake.tables["ticket_correlations"]) == 1
        row = fake.tables["ticket_correlations"][0]
        assert row["ticket_ref"] == "TKT-1"
        assert row["grid_name"] == "Kudi"
        assert row["signatures"] == ["sig-a"]

    @pytest.mark.asyncio
    async def test_upsert_updates_existing_row(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [{"ticket_ref": "TKT-1", "grid_name": "Kudi"}]

        ok = await store.upsert_correlation(
            ticket_ref="TKT-1",
            ticket_backend="internal",
            grid_name="Kudi",
            organization_id=None,
            root_cause_kind="grid_off",
            primary_signature="sig-a",
            signatures=["sig-a"],
            affected_keys=[],
            summary_base="s2",
            description_base="d2",
            severity="urgent",
            telegram_chat_id=None,
            telegram_topic_id=None,
        )

        assert ok is True
        assert len(fake.tables["ticket_correlations"]) == 1
        assert fake.tables["ticket_correlations"][0]["root_cause_kind"] == "grid_off"

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        ok = await store.upsert_correlation(
            ticket_ref="TKT-1",
            ticket_backend="internal",
            grid_name="Kudi",
            organization_id=None,
            root_cause_kind=None,
            primary_signature="sig-a",
            signatures=["sig-a"],
            affected_keys=[],
            summary_base="s",
            description_base="d",
            severity="warning",
            telegram_chat_id=None,
            telegram_topic_id=None,
        )

        assert ok is False


class TestMergeAffectedKey:
    @pytest.mark.asyncio
    async def test_appends_new_key(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "affected_keys": [], "signatures": ["sig-a"]}
        ]

        updated = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A3", label="MPPT A3", occurred_at="2026-01-01T00:00:00Z"
        )

        assert updated is not None
        assert len(updated.affected_keys) == 1
        assert updated.affected_keys[0]["kind"] == "mppt"
        assert updated.affected_keys[0]["key"] == "A3"
        assert updated.affected_keys[0]["count"] == 1
        assert updated.affected_keys[0]["first_seen"] == "2026-01-01T00:00:00Z"
        assert updated.affected_keys[0]["last_seen"] == "2026-01-01T00:00:00Z"

    @pytest.mark.asyncio
    async def test_idempotent_bumps_existing_key(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {
                        "kind": "mppt",
                        "key": "A3",
                        "label": "MPPT A3",
                        "first_seen": "2026-01-01T00:00:00Z",
                        "last_seen": "2026-01-01T00:00:00Z",
                        "count": 1,
                    }
                ],
                "signatures": [],
            }
        ]

        updated = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A3", label="MPPT A3", occurred_at="2026-01-02T00:00:00Z"
        )

        assert updated is not None
        assert len(updated.affected_keys) == 1
        assert updated.affected_keys[0]["count"] == 2
        assert updated.affected_keys[0]["first_seen"] == "2026-01-01T00:00:00Z"
        assert updated.affected_keys[0]["last_seen"] == "2026-01-02T00:00:00Z"

    @pytest.mark.asyncio
    async def test_adds_new_component_alongside_existing(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {"kind": "mppt", "key": "A3", "label": "MPPT A3", "first_seen": "t", "last_seen": "t", "count": 1}
                ],
                "signatures": [],
            }
        ]

        updated = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A7", label="MPPT A7", occurred_at="2026-01-02T00:00:00Z"
        )

        assert updated is not None
        assert len(updated.affected_keys) == 2
        assert {u["key"] for u in updated.affected_keys} == {"A3", "A7"}

    @pytest.mark.asyncio
    async def test_signature_appended_when_new(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "affected_keys": [], "signatures": ["sig-a"]}
        ]

        await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A3", label="MPPT A3", signature="sig-b"
        )

        assert fake.tables["ticket_correlations"][0]["signatures"] == ["sig-a", "sig-b"]

    @pytest.mark.asyncio
    async def test_none_when_ticket_not_found(self):
        store, _fake = _make_store()
        assert await store.merge_affected_key("TKT-999", kind="mppt", key="A3", label="MPPT A3") is None

    @pytest.mark.asyncio
    async def test_none_on_error(self):
        fake = FakeRawClient()
        fake.tables["ticket_correlations"] = [{"ticket_ref": "TKT-1", "affected_keys": [], "signatures": []}]
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.merge_affected_key("TKT-1", kind="mppt", key="A3", label="MPPT A3") is None


class TestMergeAffectedKeyReportsNovelty:
    @pytest.mark.asyncio
    async def test_new_key_reports_added_true(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "affected_keys": [], "signatures": []}
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A7", label="MPPT A7", signature="sig-a"
        )

        assert merge is not None
        assert merge.added is True
        assert [e["key"] for e in merge.affected_keys] == ["A7"]

    @pytest.mark.asyncio
    async def test_existing_key_reports_added_false_and_bumps_count(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {"kind": "mppt", "key": "A7", "label": "MPPT A7", "count": 1}
                ],
                "signatures": ["sig-a"],
            }
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A7", label="MPPT A7", signature="sig-a"
        )

        assert merge is not None
        assert merge.added is False
        assert merge.affected_keys[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_case_differing_key_is_not_a_new_component(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {"kind": "mppt", "key": "IYYY", "label": "MPPT IYYY", "count": 1}
                ],
                "signatures": [],
            }
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="MPPT", key="iyyy", label="MPPT iyyy"
        )

        assert merge is not None
        assert merge.added is False
        assert len(merge.affected_keys) == 1


class TestBumpOccurrence:
    @pytest.mark.asyncio
    async def test_increments_count_and_last_alert_at(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "occurrence_count": 1, "last_alert_at": "2026-01-01T00:00:00Z"}
        ]

        ok = await store.bump_occurrence("TKT-1", occurred_at="2026-01-02T00:00:00Z")

        assert ok is True
        row = fake.tables["ticket_correlations"][0]
        assert row["occurrence_count"] == 2
        assert row["last_alert_at"] == "2026-01-02T00:00:00Z"

    @pytest.mark.asyncio
    async def test_false_when_not_found(self):
        store, _fake = _make_store()
        assert await store.bump_occurrence("TKT-999") is False

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeRawClient()
        fake.tables["ticket_correlations"] = [{"ticket_ref": "TKT-1", "occurrence_count": 1}]
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.bump_occurrence("TKT-1") is False


class TestRecordMessageId:
    @pytest.mark.asyncio
    async def test_sets_message_id(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [{"ticket_ref": "TKT-1"}]

        ok = await store.record_message_id("TKT-1", 12345)

        assert ok is True
        assert fake.tables["ticket_correlations"][0]["telegram_message_id"] == 12345

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.record_message_id("TKT-1", 1) is False


class TestMarkClosed:
    @pytest.mark.asyncio
    async def test_sets_status_done(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [{"ticket_ref": "TKT-1", "status": "open"}]

        ok = await store.mark_closed("TKT-1")

        assert ok is True
        assert fake.tables["ticket_correlations"][0]["status"] == "done"

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.mark_closed("TKT-1") is False


class TestRecordEvent:
    @pytest.mark.asyncio
    async def test_inserts_event_row(self):
        store, fake = _make_store()

        ok = await store.record_event(
            ticket_ref="TKT-1",
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

        assert ok is True
        assert len(fake.tables["ticket_correlation_events"]) == 1
        row = fake.tables["ticket_correlation_events"][0]
        assert row["dedup_key"] == "dk-1"
        assert row["decision"] == "new"

    @pytest.mark.asyncio
    async def test_false_on_error_never_raises(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlation_events"] = RuntimeError("down")
        store, _ = _make_store(fake)

        ok = await store.record_event(
            ticket_ref=None,
            grid_name="Kudi",
            source="n8n",
            signature=None,
            dedup_key=None,
            decision="new",
            decided_by="fallback",
            confidence=None,
            reason="error",
            candidate_refs=[],
            alert={},
            llm_raw=None,
        )

        assert ok is False

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        store = CorrelationStore(get_client=lambda: None)
        ok = await store.record_event(
            ticket_ref=None,
            grid_name="Kudi",
            source="n8n",
            signature=None,
            dedup_key=None,
            decision="new",
            decided_by="fallback",
            confidence=None,
            reason=None,
            candidate_refs=[],
            alert={},
            llm_raw=None,
        )
        assert ok is False


class TestRecordEventTicketRef:
    @pytest.mark.asyncio
    async def test_backfills_ticket_ref_by_dedup_key(self):
        store, fake = _make_store()
        fake.tables["ticket_correlation_events"] = [
            {"dedup_key": "alert-42", "ticket_ref": None, "decision": "new"}
        ]

        ok = await store.record_event_ticket_ref("alert-42", "TKT-000123")

        assert ok is True
        assert fake.tables["ticket_correlation_events"][0]["ticket_ref"] == "TKT-000123"

    @pytest.mark.asyncio
    async def test_returns_false_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlation_events"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.record_event_ticket_ref("alert-42", "TKT-000123") is False

    @pytest.mark.asyncio
    async def test_false_when_no_client(self):
        store = CorrelationStore(get_client=lambda: None)
        assert await store.record_event_ticket_ref("alert-42", "TKT-000123") is False


class TestGetCorrelation:
    @pytest.mark.asyncio
    async def test_returns_row_by_ref(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "grid_name": "Kudi"},
            {"ticket_ref": "TKT-2", "grid_name": "Other"},
        ]

        row = await store.get_correlation("TKT-1")

        assert row is not None
        assert row["grid_name"] == "Kudi"

    @pytest.mark.asyncio
    async def test_none_when_not_found(self):
        store, _fake = _make_store()
        assert await store.get_correlation("TKT-999") is None

    @pytest.mark.asyncio
    async def test_none_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.get_correlation("TKT-1") is None

    @pytest.mark.asyncio
    async def test_none_when_no_client(self):
        store = CorrelationStore(get_client=lambda: None)
        assert await store.get_correlation("TKT-1") is None


class TestRecordAmendment:
    @pytest.mark.asyncio
    async def test_updates_summary_current(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "summary_current": "old", "escalated_at": None}
        ]

        ok = await store.record_amendment("TKT-1", summary_current="new summary")

        assert ok is True
        assert fake.tables["ticket_correlations"][0]["summary_current"] == "new summary"
        assert fake.tables["ticket_correlations"][0]["escalated_at"] is None

    @pytest.mark.asyncio
    async def test_escalated_sets_escalated_at(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "summary_current": "old", "escalated_at": None}
        ]

        ok = await store.record_amendment("TKT-1", summary_current="new", escalated=True)

        assert ok is True
        assert fake.tables["ticket_correlations"][0]["escalated_at"] is not None

    @pytest.mark.asyncio
    async def test_urgent_amendment_persists_effective_severity(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "summary_current": "old",
                "severity": "warning",
                "escalated_at": None,
            }
        ]

        ok = await store.record_amendment(
            "TKT-1",
            summary_current="🔴 ! Urgent: issue",
            severity="urgent",
            escalated=True,
        )

        assert ok is True
        assert fake.tables["ticket_correlations"][0]["severity"] == "urgent"

    @pytest.mark.asyncio
    async def test_false_on_error(self):
        fake = FakeRawClient()
        fake.raise_on_execute["ticket_correlations"] = RuntimeError("down")
        store, _ = _make_store(fake)

        assert await store.record_amendment("TKT-1", summary_current="x") is False
