"""KnowledgeStore's prompt_knowledge_overrides read/write path.

This is the module-authoring counterpart to the per-prompt override reads
already covered in test_prompt_knowledge_wiring.py: here a knowledge module
is pinned to specific prompts directly, from the Knowledge Modules page.
"""

import pytest

from shared.prompts.knowledge import KNOWLEDGE_STORE, KnowledgeStore

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
        self._select_columns = "*"

    def select(self, columns="*", **_kwargs):
        self._op = "select"
        self._select_columns = columns
        return self

    def upsert(self, row):
        self._op = "upsert"
        self._payload = dict(row)
        return self

    def insert(self, row):
        self._op = "insert"
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

        if self._op == "insert":
            row = dict(self._payload)
            row.setdefault("id", f"fake-id-{len(self._table.rows)}")
            self._table.rows.append(row)
            return FakeResult([row])

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
        # Real PostgREST only returns the requested columns. Project here too
        # -- otherwise a row inserted with extra columns (updated_by,
        # is_active, ...) would hand KnowledgeModule(**row) kwargs it has no
        # field for, something a real select() could never do.
        if self._select_columns and self._select_columns != "*":
            names = [c.strip() for c in self._select_columns.split(",")]
            rows = [{k: r.get(k) for k in names} for r in rows]
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
                "body": None, "tags": [], "scope": "sector",
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


def test_the_store_selects_the_audience_columns():
    """KnowledgeModule(**row) requires the select and the dataclass to match.

    Miss a column here and every doc module looks unaudienced, which the
    provider reads as 'not acl_mirror' -- i.e. it would fail *open*.
    """

    class _Result:
        data = []

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
    KnowledgeStore(client=client).all_modules()

    assert "doc_audience" in client.table_obj.selected
    assert "doc_audience_set_by" in client.table_obj.selected
    assert "source_tab" in client.table_obj.selected


# ── ensure_singleton_modules: bootstrap the code-defined provider rows ─────
# directory/graph/episodic have no UI path to create them (no "add a
# provider module" control -- see knowledge_modules.py's source_select,
# which only ever offers manual/gdoc) and no seed script ever inserts them
# either. Without this, DirectoryProvider/GraphProvider/EpisodicProvider are
# fully wired but permanently unreachable: nothing ever creates the row an
# operator would pin to a prompt.


def test_ensure_singleton_modules_creates_all_three_when_none_exist(store):
    results = store.ensure_singleton_modules(actor="ops@example.com")

    assert results == {"directory": "created", "graph": "created", "episodic": "created"}
    sources = {m.source for m in store.all_modules()}
    assert sources == {"directory", "graph", "episodic"}


def test_ensure_singleton_modules_is_idempotent(store):
    store.ensure_singleton_modules(actor="ops@example.com")
    results = store.ensure_singleton_modules(actor="ops@example.com")

    assert results == {"directory": "exists", "graph": "exists", "episodic": "exists"}
    assert len([m for m in store.all_modules() if m.source == "directory"]) == 1


def test_ensure_singleton_modules_only_creates_whats_missing(store):
    store._client.table("knowledge_modules").insert(
        {
            "slug": "graph", "title": "Graph (hand-edited)", "summary": "s",
            "body": None, "scope": "global", "source": "graph",
            "is_active": True,
        }
    ).execute()

    results = store.ensure_singleton_modules(actor="ops@example.com")

    assert results["graph"] == "exists"
    assert results["directory"] == "created"
    assert results["episodic"] == "created"
    # The pre-existing row survives untouched -- ensure never overwrites.
    graph_rows = [m for m in store.all_modules() if m.source == "graph"]
    assert len(graph_rows) == 1
    assert graph_rows[0].title == "Graph (hand-edited)"


def test_ensure_singleton_modules_created_rows_exist_but_are_not_attached_to_any_prompt(store):
    store.ensure_singleton_modules(actor="ops@example.com")

    directory = next(m for m in store.all_modules() if m.source == "directory")
    assert directory.slug == "directory"
    assert directory.scope == "global"
    assert directory.body is None
    assert directory.summary  # a blank summary helps nobody find it in the picker
    # Existence is not attachment: this method never writes
    # prompt_knowledge_overrides, so a bootstrapped module reaches no prompt
    # until someone ticks it.
    assert store.overrides_for("staff.system") == {}

    graph = next(m for m in store.all_modules() if m.source == "graph")
    assert graph.slug == "entity-graph"  # matches the P1 seed script / P4 rollout checklist


def test_ensure_singleton_modules_fails_open_per_source():
    """A CHECK-constraint rejection for one source must not sink the others.

    Simulates migration 0017 not yet applied for just 'graph' -- the most
    likely real-world failure this method exists to survive.
    """

    class _FlakyTable:
        def __init__(self):
            self.rows: "list[dict]" = []

        def select(self, *_a, **_k):
            return self

        def eq(self, *_a, **_k):
            return self

        def insert(self, row):
            if row.get("source") == "graph":
                raise RuntimeError("knowledge_modules_source_chk violated")
            self.rows.append(dict(row))
            return self

        def execute(self):
            return FakeResult(list(self.rows))

    class _FlakyClient:
        def __init__(self):
            self._table = _FlakyTable()

        def table(self, _name):
            return self._table

    store = KnowledgeStore(client=_FlakyClient())
    results = store.ensure_singleton_modules(actor="ops@example.com")

    assert results["directory"] == "created"
    assert results["episodic"] == "created"
    assert results["graph"].startswith("failed:")


def test_unconfigured_store_ensure_singleton_modules_is_a_noop():
    assert KnowledgeStore(client=None).ensure_singleton_modules(actor="ops@example.com") == {}


def test_knowledge_store_singleton_exists_and_is_a_knowledge_store():
    assert isinstance(KNOWLEDGE_STORE, KnowledgeStore)
