"""Tests for shared.prompts.skills -- the user-designed skills catalog
(Phase 3 of docs/superpowers/plans/2026-08-06-user-designed-skills.md).
"""

from __future__ import annotations

import time
from types import SimpleNamespace

from shared.prompts.skills import (
    SKILL_PIN_PREFIX,
    Skill,
    SkillCatalogStore,
    render_skill_catalog,
    select_skills_for_context,
    skill_prompt_id,
)


def _skill(slug="find_tickets", title="Find Tickets", summary="Finds open tickets.", staff_only=True):
    return Skill(id=f"id-{slug}", slug=slug, title=title, summary=summary, staff_only=staff_only)


def test_skill_prompt_id_prefixes_the_skill_id():
    assert skill_prompt_id("11111111-1111-1111-1111-111111111111") == (
        "skill:11111111-1111-1111-1111-111111111111"
    )


def test_skill_pin_prefix_is_the_literal_prefix_used():
    assert SKILL_PIN_PREFIX == "skill:"


def test_skill_status_defaults_to_active():
    assert _skill().status == "active"


class TestSelectSkillsForContext:
    def test_staff_only_skill_visible_to_staff(self):
        skills = [_skill(staff_only=True)]

        assert select_skills_for_context(skills, is_staff=True) == skills

    def test_staff_only_skill_hidden_from_customer(self):
        skills = [_skill(staff_only=True)]

        assert select_skills_for_context(skills, is_staff=False) == []

    def test_non_staff_only_skill_visible_to_everyone(self):
        skills = [_skill(staff_only=False)]

        assert select_skills_for_context(skills, is_staff=True) == skills
        assert select_skills_for_context(skills, is_staff=False) == skills

    def test_mixed_list_filters_only_staff_only_entries(self):
        staff_skill = _skill(slug="staff_thing", staff_only=True)
        open_skill = _skill(slug="open_thing", staff_only=False)

        result = select_skills_for_context([staff_skill, open_skill], is_staff=False)

        assert result == [open_skill]


class TestRenderSkillCatalog:
    def test_empty_list_returns_none(self):
        assert render_skill_catalog([]) is None

    def test_renders_own_labeled_block_distinct_from_knowledge(self):
        # Must never be mistaken for or merged with knowledge.render_inlined's
        # "# Available Knowledge" block -- see this module's docstring.
        block = render_skill_catalog([_skill()])

        assert block.startswith("# Available Skills")
        assert "Available Knowledge" not in block

    def test_shows_title_and_summary_only_never_steps(self):
        # Title, not slug: a skill's title is the author-chosen, editable
        # name (see this module's render_skill_catalog docstring for why
        # this deliberately differs from knowledge modules' slug-keyed
        # catalog).
        block = render_skill_catalog([_skill(title="Find Tickets", summary="Finds open tickets.")])

        assert "Find Tickets" in block
        assert "Finds open tickets." in block

    def test_sorted_by_title_for_deterministic_output(self):
        skills = [_skill(slug="a", title="Zeta Skill"), _skill(slug="b", title="Alpha Skill")]

        block = render_skill_catalog(skills)

        assert block.index("Alpha Skill") < block.index("Zeta Skill")


class TestSkillCatalogStore:
    def test_no_client_returns_empty_list(self):
        store = SkillCatalogStore(client=None)

        assert store.all_skills() == []

    def test_fetches_and_parses_rows_into_skill_objects(self):
        rows = [
            {
                "id": "id-1",
                "slug": "find_tickets",
                "title": "Find Tickets",
                "summary": "Finds open tickets.",
                "staff_only": True,
            }
        ]
        client = _fake_supabase_client(rows)
        store = SkillCatalogStore(client=client)

        skills = store.all_skills()

        assert len(skills) == 1
        assert skills[0].slug == "find_tickets"
        assert skills[0].staff_only is True

    def test_query_filters_to_active_status_only(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client)

        store.all_skills()

        assert client.last_query.filters.get("status") == "active"

    def test_active_only_false_fetches_every_status(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client)

        store.all_skills(active_only=False)

        # The fake's last_query is only ever set inside .eq(...) (see
        # _FakeQuery below) -- staying None proves no .eq("status", ...)
        # call happened at all, not just that it wasn't the last one.
        assert client.last_query is None

    def test_active_only_true_is_still_the_default(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client)

        store.all_skills()

        assert client.last_query.filters.get("status") == "active"

    def test_active_only_and_all_are_cached_separately(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client, ttl_seconds=300)

        store.all_skills(active_only=True)
        store.all_skills(active_only=False)

        assert client.query_count == 2

    def test_result_is_cached_within_ttl(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client, ttl_seconds=300)

        store.all_skills()
        store.all_skills()

        assert client.query_count == 1

    def test_invalidate_forces_a_fresh_fetch(self):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client, ttl_seconds=300)

        store.all_skills()
        store.invalidate()
        store.all_skills()

        assert client.query_count == 2

    def test_expired_cache_forces_a_fresh_fetch(self, monkeypatch):
        client = _fake_supabase_client([])
        store = SkillCatalogStore(client=client, ttl_seconds=1)
        real_time = time.time

        store.all_skills()
        monkeypatch.setattr(time, "time", lambda: real_time() + 10)
        store.all_skills()

        assert client.query_count == 2

    def test_query_failure_degrades_to_empty_not_raise(self):
        client = _fake_supabase_client([])

        def boom(*_a, **_k):
            raise RuntimeError("db unreachable")

        client.table = boom
        store = SkillCatalogStore(client=client)

        assert store.all_skills() == []

    def test_from_env_with_no_credentials_has_no_client(self, monkeypatch):
        monkeypatch.setattr("shared.config.db_credentials.chat_db_url", lambda: "")
        monkeypatch.setattr("shared.config.db_credentials.chat_db_service_key", lambda: "")

        store = SkillCatalogStore.from_env()

        assert store.all_skills() == []


# ── a minimal fake supabase-py query builder ────────────────────────────────


class _FakeQuery:
    def __init__(self, rows, client):
        self._rows = rows
        self._client = client
        self.filters: dict = {}

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        self._client.last_query = self
        return self

    def execute(self):
        self._client.query_count += 1
        return SimpleNamespace(data=self._rows)


class _FakeClient:
    def __init__(self, rows):
        self._rows = rows
        self.query_count = 0
        self.last_query = None

    def table(self, _name):
        return _FakeQuery(self._rows, self)


def _fake_supabase_client(rows):
    return _FakeClient(rows)


def test_catalog_query_filters_to_active_only():
    """A draft must never reach a model's context. This is the only gate."""
    captured = {}

    class _Table:
        def select(self, _cols):
            return self

        def eq(self, key, value):
            captured[key] = value
            return self

        def execute(self):
            class _R:
                data = []
            return _R()

    class _Client:
        def table(self, _name):
            return _Table()

    SkillCatalogStore(client=_Client()).all_skills()

    assert captured == {"status": "active"}


def test_a_draft_row_is_not_returned_by_the_catalog():
    class _Table:
        def __init__(self):
            self._status = None

        def select(self, _cols):
            return self

        def eq(self, key, value):
            if key == "status":
                self._status = value
            return self

        def execute(self):
            rows = [
                {"id": "1", "slug": "a", "title": "A", "summary": "s", "staff_only": True,
                 "status": "active"},
                {"id": "2", "slug": "b", "title": "B", "summary": "s", "staff_only": True,
                 "status": "draft"},
            ]
            class _R:
                data = [
                    {k: v for k, v in r.items() if k != "status"}
                    for r in rows
                    if r["status"] == self._status
                ]
            return _R()

    class _Client:
        def table(self, _name):
            return _Table()

    skills = SkillCatalogStore(client=_Client()).all_skills()

    assert [s.slug for s in skills] == ["a"]
