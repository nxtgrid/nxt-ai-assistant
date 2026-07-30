"""KnowledgeStore's prompt_knowledge_overrides read/write path.

This is the module-authoring counterpart to the per-prompt override reads
already covered in test_prompt_knowledge_wiring.py: here a knowledge module
is pinned to specific prompts directly, from the Knowledge Modules page.
"""

import pytest

from shared.prompts.knowledge import KnowledgeStore

# ── a minimal chainable stand-in for the supabase-py query builder ─────────
# Mirrors the fake used in test_prompt_overrides.py, trimmed to the
# select/upsert/delete/eq/execute surface prompt_knowledge_overrides needs.


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self):
        self.rows: "list[dict]" = []


class FakeQueryBuilder:
    def __init__(self, table: FakeTable):
        self._table = table
        self._filters: dict = {}
        self._op = "select"
        self._payload: dict = {}

    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def upsert(self, row):
        self._op = "upsert"
        self._payload = dict(row)
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    def execute(self):
        if self._op == "upsert":
            key_fields = ("prompt_id", "module_id")
            match = [
                r
                for r in self._table.rows
                if all(r.get(k) == self._payload[k] for k in key_fields)
            ]
            if match:
                match[0].update(self._payload)
            else:
                self._table.rows.append(dict(self._payload))
            return FakeResult([self._payload])

        if self._op == "delete":
            self._table.rows = [
                r
                for r in self._table.rows
                if not all(r.get(k) == v for k, v in self._filters.items())
            ]
            return FakeResult([])

        rows = [
            r for r in self._table.rows if all(r.get(k) == v for k, v in self._filters.items())
        ]
        return FakeResult(rows)


class FakeClient:
    def __init__(self):
        self._tables: dict = {}

    def table(self, name):
        return FakeQueryBuilder(self._tables.setdefault(name, FakeTable()))


@pytest.fixture
def store():
    return KnowledgeStore(client=FakeClient())


def test_prompts_pinning_is_empty_by_default(store):
    assert store.prompts_pinning("mod-1") == []


def test_set_prompt_pins_adds_new_pins(store):
    store.set_prompt_pins("mod-1", ["a.b", "c.d"], actor="ada@x.com")
    assert sorted(store.prompts_pinning("mod-1")) == ["a.b", "c.d"]


def test_set_prompt_pins_removes_deselected_pins(store):
    store.set_prompt_pins("mod-1", ["a.b", "c.d"], actor="ada@x.com")
    store.set_prompt_pins("mod-1", ["a.b"], actor="ada@x.com")
    assert store.prompts_pinning("mod-1") == ["a.b"]


def test_set_prompt_pins_is_scoped_to_its_own_module(store):
    store.set_prompt_pins("mod-1", ["a.b"], actor="ada@x.com")
    store.set_prompt_pins("mod-2", ["c.d"], actor="ada@x.com")
    assert store.prompts_pinning("mod-1") == ["a.b"]
    assert store.prompts_pinning("mod-2") == ["c.d"]


def test_set_prompt_pins_is_idempotent():
    store = KnowledgeStore(client=FakeClient())
    store.set_prompt_pins("mod-1", ["a.b"], actor="ada@x.com")
    store.set_prompt_pins("mod-1", ["a.b"], actor="ada@x.com")
    assert store.prompts_pinning("mod-1") == ["a.b"]


def test_unconfigured_store_prompts_pinning_returns_empty():
    assert KnowledgeStore(client=None).prompts_pinning("mod-1") == []


def test_unconfigured_store_set_prompt_pins_is_a_noop():
    KnowledgeStore(client=None).set_prompt_pins("mod-1", ["a.b"], actor="ada@x.com")
