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


# ── set_prompt_modules: the prompt-editor counterpart to set_prompt_pins ───
# set_prompt_pins reconciles one module across many prompts; set_prompt_modules
# reconciles one prompt across many modules. Both write the same
# prompt_knowledge_overrides row, from opposite ends of the relationship.


def _modules():
    from shared.prompts.knowledge import KnowledgeModule

    return [
        KnowledgeModule(id="id-a", slug="alpha", title="Alpha", summary="a", body="A"),
        KnowledgeModule(id="id-b", slug="beta", title="Beta", summary="b", body="B"),
    ]


def test_set_prompt_modules_reconciles_both_directions(store):
    store._cache = _modules()
    store._expires = float("inf")

    # beta starts pinned to staff.system (via the module-side API); alpha isn't.
    store.set_prompt_pins("id-b", ["staff.system"], actor="ops@example.com")
    assert store.overrides_for("staff.system") == {"beta": True}

    # Reconciling staff.system to just ["alpha"] should add alpha and drop beta.
    store.set_prompt_modules("staff.system", ["alpha"], actor="ops@example.com")

    assert store.overrides_for("staff.system") == {"alpha": True}
    assert store.prompts_pinning("id-a") == ["staff.system"]
    assert store.prompts_pinning("id-b") == []


def test_set_prompt_modules_is_idempotent(store):
    store._cache = _modules()
    store._expires = float("inf")

    store.set_prompt_modules("staff.system", ["alpha"], actor="ops@example.com")
    store.set_prompt_modules("staff.system", ["alpha"], actor="ops@example.com")

    assert store.overrides_for("staff.system") == {"alpha": True}


def test_set_prompt_modules_ignores_unknown_slugs(store):
    store._cache = _modules()
    store._expires = float("inf")

    store.set_prompt_modules(
        "staff.system", ["alpha", "does-not-exist"], actor="ops@example.com"
    )

    assert store.overrides_for("staff.system") == {"alpha": True}


def test_unconfigured_store_set_prompt_modules_is_a_noop():
    KnowledgeStore(client=None).set_prompt_modules("staff.system", ["alpha"], actor="ada@x.com")


def test_all_modules_reads_source_columns():
    """The store must select source/source_ref or every module looks manual."""

    class _Result:
        data = [
            {
                "id": "1", "slug": "graph-overview", "title": "Graph", "summary": "s",
                "body": None, "tags": [], "scope": "sector", "mode": "pinned",
                "source": "graph", "source_ref": None,
            }
        ]

    class _Table:
        def __init__(self):
            self.selected = ""

        def select(self, columns):
            self.selected = columns
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            return _Result()

    class _Client:
        def __init__(self):
            self.table_obj = _Table()

        def table(self, _name):
            return self.table_obj

    client = _Client()
    store = KnowledgeStore(client=client)
    modules = store.all_modules()

    assert "source" in client.table_obj.selected
    assert "source_ref" in client.table_obj.selected
    assert modules[0].source == "graph"
    assert modules[0].is_jit is True
