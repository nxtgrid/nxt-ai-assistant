"""Unit tests for SkillBuilderService (Phase 4 of
docs/superpowers/plans/2026-08-06-user-designed-skills.md).

A minimal fluent fake stands in for the real Supabase client -- the same
style as tests/test_supabase_reader_run_usage.py, generalized to the
eq/is_/gte/order/update/select/execute verbs get_builder_messages and
archive_from_message_index use.

Covers the acceptance criteria from the plan's Phase 4 section: rewinding to
a step leaves the right messages visible (asserted on what
get_builder_messages returns, not on any UI), and archived messages never
resurface.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from services.skill_builder_service import SkillBuilderService

SESSION_TEXT_ID = "api_dm_abc123"
SESSION_UUID = "11111111-1111-1111-1111-111111111111"


class _FakeQuery:
    def __init__(self, table: "_FakeTable", op: str, payload: Optional[dict] = None) -> None:
        self._table = table
        self._op = op
        self._payload = payload
        self._filters: List[tuple] = []
        self._order: Optional[tuple] = None

    def select(self, *_args, **_kwargs) -> "_FakeQuery":
        return self

    def eq(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("is", col, val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeQuery":
        self._filters.append(("gte", col, val))
        return self

    def limit(self, _n: int) -> "_FakeQuery":
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeQuery":
        self._order = (col, desc)
        return self

    def _matches(self, row: Dict[str, Any]) -> bool:
        for kind, col, val in self._filters:
            if kind == "eq" and row.get(col) != val:
                return False
            if kind == "is":
                is_null = row.get(col) is None
                if val == "null" and not is_null:
                    return False
            if kind == "gte" and not (row.get(col) is not None and row[col] >= val):
                return False
        return True

    def execute(self) -> SimpleNamespace:
        if self._op == "select":
            rows = [r for r in self._table.rows if self._matches(r)]
            if self._order is not None:
                col, desc = self._order
                rows.sort(key=lambda r: r.get(col), reverse=desc)
            return SimpleNamespace(data=rows)

        if self._op == "update":
            matched = [r for r in self._table.rows if self._matches(r)]
            for r in matched:
                r.update(self._payload or {})
            return SimpleNamespace(data=matched)

        raise AssertionError(f"Unhandled op: {self._op}")


class _FakeTable:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self.rows = rows

    def select(self, *args, **kwargs) -> _FakeQuery:
        return _FakeQuery(self, "select").select(*args, **kwargs)

    def update(self, payload: dict) -> _FakeQuery:
        return _FakeQuery(self, "update", payload)

    def insert(self, payload: dict) -> "_FakeInsertQuery":
        return _FakeInsertQuery(self, payload)


class _FakeInsertQuery:
    def __init__(self, table: "_FakeTable", payload: dict) -> None:
        self._table = table
        self._payload = payload

    def execute(self) -> SimpleNamespace:
        row = {"id": f"generated-{len(self._table.rows)}", **self._payload}
        self._table.rows.append(row)
        return SimpleNamespace(data=[row])


class _FakeClient:
    def __init__(
        self,
        chat_sessions: List[Dict[str, Any]],
        chat_messages: List[Dict[str, Any]],
        skills: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        self._tables = {
            "chat_sessions": _FakeTable(chat_sessions),
            "chat_messages": _FakeTable(chat_messages),
            "skills": _FakeTable(skills or []),
        }

    def table(self, name: str) -> _FakeTable:
        return self._tables[name]


def _service(
    chat_sessions: List[Dict[str, Any]],
    chat_messages: List[Dict[str, Any]],
    skills: Optional[List[Dict[str, Any]]] = None,
) -> SkillBuilderService:
    service = SkillBuilderService.__new__(SkillBuilderService)  # bypass real DB init
    service.client = _FakeClient(chat_sessions, chat_messages, skills)
    return service


def _session_row() -> Dict[str, Any]:
    return {"id": SESSION_UUID, "session_id": SESSION_TEXT_ID}


def _message(index: int, content: str, archived_at: Optional[str] = None) -> Dict[str, Any]:
    return {
        "id": f"msg-{index}",
        "session_id": SESSION_UUID,
        "role": "user",
        "content": content,
        "message_index": index,
        "archived_at": archived_at,
        "metadata": {},
    }


class TestNoClient:
    def test_get_session_uuid_returns_none(self):
        service = SkillBuilderService.__new__(SkillBuilderService)
        service.client = None

        assert service.get_session_uuid(SESSION_TEXT_ID) is None

    def test_get_builder_messages_returns_empty_list(self):
        service = SkillBuilderService.__new__(SkillBuilderService)
        service.client = None

        assert service.get_builder_messages(SESSION_TEXT_ID) == []

    def test_archive_from_message_index_returns_zero(self):
        service = SkillBuilderService.__new__(SkillBuilderService)
        service.client = None

        assert service.archive_from_message_index(SESSION_TEXT_ID, 0) == 0


class TestGetSessionUuid:
    def test_resolves_text_session_id_to_uuid(self):
        service = _service([_session_row()], [])

        assert service.get_session_uuid(SESSION_TEXT_ID) == SESSION_UUID

    def test_unknown_session_id_returns_none(self):
        service = _service([_session_row()], [])

        assert service.get_session_uuid("no-such-session") is None


class TestGetBuilderMessages:
    def test_returns_live_messages_in_order(self):
        messages = [_message(0, "step 1"), _message(1, "step 2")]
        service = _service([_session_row()], messages)

        result = service.get_builder_messages(SESSION_TEXT_ID)

        assert [m["content"] for m in result] == ["step 1", "step 2"]

    def test_excludes_archived_messages(self):
        messages = [
            _message(0, "step 1"),
            _message(1, "step 2 (rewound)", archived_at="2026-08-07T00:00:00+00:00"),
        ]
        service = _service([_session_row()], messages)

        result = service.get_builder_messages(SESSION_TEXT_ID)

        assert [m["content"] for m in result] == ["step 1"]

    def test_unresolvable_session_returns_empty_list(self):
        service = _service([], [_message(0, "orphaned")])

        assert service.get_builder_messages(SESSION_TEXT_ID) == []


class TestArchiveFromMessageIndex:
    def test_rewinding_to_step_2_of_5_leaves_2_visible_steps(self):
        # Mirrors the plan's Phase 4 acceptance criterion literally: a
        # 5-step session, rewind targeting step 3 (index 2, 0-based) so
        # steps 1-2 (indices 0-1) remain and 3-5 (indices 2-4) are archived
        # -- "rewinding to step 2" leaves the *2 steps up to and including
        # step 2* visible.
        messages = [_message(i, f"step {i + 1}") for i in range(5)]
        service = _service([_session_row()], messages)

        archived_count = service.archive_from_message_index(SESSION_TEXT_ID, 2)

        assert archived_count == 3
        remaining = service.get_builder_messages(SESSION_TEXT_ID)
        assert [m["content"] for m in remaining] == ["step 1", "step 2"]

    def test_archives_the_target_message_itself(self):
        # "Archives that message and everything after it" -- the clicked
        # step's own message is included in the cut, not just what follows.
        messages = [_message(0, "step 1"), _message(1, "step 2")]
        service = _service([_session_row()], messages)

        service.archive_from_message_index(SESSION_TEXT_ID, 1)

        remaining = service.get_builder_messages(SESSION_TEXT_ID)
        assert [m["content"] for m in remaining] == ["step 1"]

    def test_already_archived_rows_are_not_recounted(self):
        messages = [
            _message(0, "step 1"),
            _message(1, "step 2", archived_at="2026-08-01T00:00:00+00:00"),
        ]
        service = _service([_session_row()], messages)

        archived_count = service.archive_from_message_index(SESSION_TEXT_ID, 0)

        # Only index 0 was live; index 1 was already archived and is left alone.
        assert archived_count == 1

    def test_unresolvable_session_returns_zero_and_touches_nothing(self):
        messages = [_message(0, "step 1")]
        service = _service([], messages)

        assert service.archive_from_message_index(SESSION_TEXT_ID, 0) == 0
        assert messages[0]["archived_at"] is None


class TestSaveSkill:
    def _steps(self):
        return [{"index": 0, "name": "find", "instruction": "List all open tickets."}]

    def test_no_client_returns_failure(self):
        service = SkillBuilderService.__new__(SkillBuilderService)
        service.client = None

        result = service.save_skill("Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com")

        assert result["success"] is False

    def test_blank_title_is_rejected(self):
        service = _service([], [])

        result = service.save_skill("   ", "Finds tickets.", self._steps(), True, "a@b.com")

        assert result["success"] is False
        assert "title" in result["error"].lower()

    def test_no_steps_is_rejected(self):
        service = _service([], [])

        result = service.save_skill("Find Tickets", "Finds tickets.", [], True, "a@b.com")

        assert result["success"] is False
        assert "step" in result["error"].lower()

    def test_successful_save_returns_the_inserted_row(self):
        service = _service([], [])

        result = service.save_skill("Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com")

        assert result["success"] is True
        assert result["skill"]["title"] == "Find Tickets"
        assert result["skill"]["slug"] == "find-tickets"
        assert result["skill"]["staff_only"] is True
        assert result["skill"]["created_by"] == "a@b.com"
        assert result["skill"]["status"] == "active"
        assert result["skill"]["steps"] == self._steps()

    def test_slug_collision_appends_a_counter(self):
        service = _service([], [], skills=[{"slug": "find-tickets"}])

        result = service.save_skill("Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com")

        assert result["skill"]["slug"] == "find-tickets-2"

    def test_slug_is_derived_from_title_not_user_supplied(self):
        service = _service([], [])

        result = service.save_skill(
            "Weekly KPI Digest!", "Sends a digest.", self._steps(), False, "a@b.com"
        )

        assert result["skill"]["slug"] == "weekly-kpi-digest"

    def test_status_defaults_to_active(self):
        service = _service([], [])

        result = service.save_skill("Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com")

        assert result["skill"]["status"] == "active"

    def test_an_explicit_status_is_honoured(self):
        """A draft saves as a draft directly -- never active-then-corrected."""
        service = _service([], [])

        result = service.save_skill(
            "Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com", status="draft"
        )

        assert result["success"] is True
        assert result["skill"]["status"] == "draft"

    def test_an_invalid_status_is_rejected(self):
        service = _service([], [])

        result = service.save_skill(
            "Find Tickets", "Finds tickets.", self._steps(), True, "a@b.com", status="published"
        )

        assert result["success"] is False
        assert "published" in result["error"]


class TestSlugTaken:
    """Item (d): the /skill name box must warn on a clash instead of the
    silent '-2' suffix _unique_slug applies for an auto-derived title."""

    def test_taken_when_another_skill_already_uses_the_derived_slug(self):
        service = _service([], [], skills=[{"id": "other-id", "slug": "grid-health"}])

        taken, slug = service.slug_taken("Grid Health")

        assert taken is True
        assert slug == "grid-health"

    def test_not_taken_when_no_skill_uses_it(self):
        service = _service([], [], skills=[])

        taken, slug = service.slug_taken("Grid Health")

        assert taken is False
        assert slug == "grid-health"

    def test_excludes_the_skill_currently_being_edited(self):
        """Re-saving a skill under its own unchanged name must not flag
        itself as a clash."""
        service = _service([], [], skills=[{"id": "self-id", "slug": "grid-health"}])

        taken, _ = service.slug_taken("Grid Health", exclude_skill_id="self-id")

        assert taken is False

    def test_a_different_skill_at_the_same_slug_still_clashes_during_edit(self):
        service = _service(
            [], [], skills=[{"id": "other-id", "slug": "grid-health"}]
        )

        taken, _ = service.slug_taken("Grid Health", exclude_skill_id="self-id")

        assert taken is True

    def test_with_no_client_reports_not_taken(self):
        service = SkillBuilderService.__new__(SkillBuilderService)
        service.client = None

        taken, slug = service.slug_taken("Grid Health")

        assert taken is False
        assert slug == "grid-health"

    def test_a_query_failure_reports_not_taken_rather_than_blocking_save(self):
        class _Client:
            def table(self, _n):
                raise RuntimeError("db down")

        service = SkillBuilderService(client=_Client())

        taken, slug = service.slug_taken("Grid Health")

        assert taken is False
        assert slug == "grid-health"


def test_list_skills_returns_every_status():
    """The admin list shows drafts and disabled skills; the catalog does not."""
    rows = [
        {"id": "1", "slug": "a", "title": "A", "summary": "s", "steps": [{}, {}],
         "staff_only": True, "status": "active", "created_by": "x", "updated_at": "t"},
        {"id": "2", "slug": "b", "title": "B", "summary": "s", "steps": [],
         "staff_only": False, "status": "draft", "created_by": "x", "updated_at": "t"},
    ]

    class _Table:
        def select(self, _cols):
            return self

        def order(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = rows
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    service = SkillBuilderService(client=_Client())
    skills = service.list_skills()

    assert {s["slug"] for s in skills} == {"a", "b"}
    assert skills[0]["step_count"] == 2
    assert skills[1]["step_count"] == 0


def test_list_skills_returns_empty_without_a_client():
    assert SkillBuilderService(client=None).list_skills() == []


def test_update_skill_status_rejects_an_unknown_value():
    service = SkillBuilderService(client=None)
    result = service.update_skill_status("1", "published", actor="x")
    assert result["success"] is False
    assert "published" in result["error"]


def test_update_skill_status_accepts_the_four_valid_values():
    captured = {}

    class _Table:
        def update(self, payload):
            captured.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            class _R:
                data = [{"id": "1", "status": captured.get("status")}]
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    for status in ("draft", "active", "disabled", "unusable"):
        result = SkillBuilderService(client=_Client()).update_skill_status(
            "1", status, actor="ops@example.com"
        )
        assert result["success"] is True, status


def test_update_skill_writes_identity_and_status_together():
    captured = {}

    class _Table:
        def update(self, payload):
            captured.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            class _R:
                data = [dict(captured, id="1")]

            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    result = SkillBuilderService(client=_Client()).update_skill(
        "1", title="New Title", summary="New summary.", staff_only=False,
        status="active", actor="ops@example.com",
    )

    assert result["success"] is True
    assert captured["title"] == "New Title"
    assert captured["summary"] == "New summary."
    assert captured["staff_only"] is False
    assert captured["status"] == "active"


def test_update_skill_rejects_an_unknown_status():
    result = SkillBuilderService(client=None).update_skill(
        "1", title="T", summary="S", staff_only=True, status="published", actor="x"
    )
    assert result["success"] is False
    assert "published" in result["error"]


def test_update_skill_rejects_a_blank_title():
    result = SkillBuilderService(client=None).update_skill(
        "1", title="   ", summary="S", staff_only=True, status="draft", actor="x"
    )
    assert result["success"] is False
    assert "title" in result["error"].lower()


def test_update_skill_with_no_client_returns_failure():
    service = SkillBuilderService.__new__(SkillBuilderService)
    service.client = None

    result = service.update_skill(
        "1", title="T", summary="S", staff_only=True, status="draft", actor="x"
    )
    assert result["success"] is False


def test_schedule_summary_reports_cron_and_anchor():
    rows = [
        {"skill_id": "1", "cron_expression": "0 8 * * 1", "schedule_type": "recurring",
         "anchor_entity_type": "grid", "is_active": True},
    ]

    class _Table:
        def select(self, _cols):
            return self

        @property
        def not_(self):
            # The real postgrest client's `not_` is a property (verified
            # against the installed postgrest 2.31.0), chained as
            # `.not_.is_(...)` with no call on `not_` itself -- a plain
            # method here would make that chain raise AttributeError
            # ('function' object has no attribute 'is_'), which it did
            # before this fix.
            return self

        def is_(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = rows
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    summaries = SkillBuilderService(client=_Client()).schedule_summaries()

    assert summaries["1"]["cron_expression"] == "0 8 * * 1"
    assert summaries["1"]["anchor_entity_type"] == "grid"


def test_schedule_summary_is_empty_when_nothing_is_scheduled():
    class _Table:
        def select(self, _cols):
            return self

        @property
        def not_(self):
            # The real postgrest client's `not_` is a property (verified
            # against the installed postgrest 2.31.0), chained as
            # `.not_.is_(...)` with no call on `not_` itself -- a plain
            # method here would make that chain raise AttributeError
            # ('function' object has no attribute 'is_'), which it did
            # before this fix.
            return self

        def is_(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = []
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    assert SkillBuilderService(client=_Client()).schedule_summaries() == {}


def test_schedule_summary_survives_a_query_failure():
    class _Client:
        def table(self, _n):
            raise RuntimeError("db down")

    assert SkillBuilderService(client=_Client()).schedule_summaries() == {}


def test_set_skill_schedule_derives_cron_from_frequency():
    captured = {}

    class _Table:
        def upsert(self, payload, **_k):
            captured.update(payload)
            return self

        def execute(self):
            class _R:
                data = [captured]

            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    result = SkillBuilderService(client=_Client()).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="2026-09-01 08:00",
        frequency="Weekly", actor="ops@example.com",
    )

    assert result["success"] is True
    assert captured["skill_id"] == "1"
    assert captured["anchor_entity_type"] == "grid"
    assert captured["cron_expression"].startswith("0 8 ")
    assert captured["command"] is None
    # Regression test for the dead-schedule bug: without this, next_run_at
    # is never written and process_due_skill_schedules's `.lte("next_run_at",
    # now)` filter never matches the row -- it would never fire.
    assert captured["next_run_at"] == "2026-09-01T08:00:00+00:00"


def test_set_skill_schedule_rejects_an_unsupported_anchor():
    result = SkillBuilderService(client=None).set_skill_schedule(
        "1", anchor_entity_type="meter", first_run="2026-09-01 08:00",
        frequency="Weekly", actor="x",
    )
    assert result["success"] is False
    assert "meter" in result["error"]


def test_set_skill_schedule_rejects_an_unparseable_first_run():
    result = SkillBuilderService(client=None).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="next tuesday",
        frequency="Weekly", actor="x",
    )
    assert result["success"] is False
    assert "first run" in result["error"].lower()
