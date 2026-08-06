"""Versioned prompt overrides with label-based publishing."""

import pytest

from shared.prompts.overrides import OverrideStore


@pytest.fixture
def store():
    return OverrideStore(client=None)  # unconfigured: every read returns None


def test_unconfigured_store_returns_no_override(store):
    assert store.body_for("a.b") is None


def test_unconfigured_store_reports_not_configured(store):
    assert store.is_configured() is False


def test_propose_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.propose("a.b", "body", note="n", actor="ada@x.com")


def test_publish_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.publish("a.b", 1, actor="ada@x.com")


def test_revert_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.revert_to_default("a.b", actor="ada@x.com")


def test_unconfigured_store_returns_no_doc_binding(store):
    assert store.doc_id_for("customer.system") is None


def test_unconfigured_store_falls_back_to_legacy_env_var_for_doc_id(store, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "DOC123")
    assert store.doc_id_for("customer.system") == "DOC123"


def test_unconfigured_store_reports_no_doc_override(store):
    assert store.doc_override_for("customer.system") is False


def test_unconfigured_store_returns_no_doc_bindings(store):
    assert store.all_doc_bindings() == {}


def test_set_doc_binding_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.set_doc_binding("a.b", "DOC1", is_override=False, actor="ada@x.com")


def test_clear_doc_binding_on_unconfigured_store_raises(store):
    with pytest.raises(RuntimeError, match="not configured"):
        store.clear_doc_binding("a.b", actor="ada@x.com")


def test_next_version_starts_at_one():
    assert OverrideStore._next_version([]) == 1


def test_next_version_increments_past_the_highest():
    assert OverrideStore._next_version([{"version": 2}, {"version": 7}]) == 8


def test_label_map_indexes_by_prompt():
    rows = [
        {"prompt_id": "a.b", "label": "production", "version": 3},
        {"prompt_id": "c.d", "label": "production", "version": 1},
    ]
    assert OverrideStore._label_map(rows) == {"a.b": 3, "c.d": 1}


def test_label_map_ignores_non_production_labels():
    rows = [{"prompt_id": "a.b", "label": "staging", "version": 9}]
    assert OverrideStore._label_map(rows) == {}


# ── against a fake supabase-like client ─────────────────────────────────────


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self):
        self.rows: list[dict] = []


class FakeQueryBuilder:
    """Minimal chainable stand-in for the supabase-py query builder.

    Every chain method returns self and just records intent; execute()
    interprets the accumulated (op, filters, payload) against the table.
    """

    def __init__(self, table: FakeTable):
        self._table = table
        self._filters: dict = {}
        self._op = "select"
        self._payload: dict = {}

    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def insert(self, row):
        self._op = "insert"
        self._payload = dict(row)
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

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def single(self):
        return self

    def execute(self):
        if self._op == "insert":
            self._table.rows.append(self._payload)
            return FakeResult([self._payload])

        if self._op == "upsert":
            key_fields = [k for k in ("prompt_id", "label") if k in self._payload]
            match = [
                r for r in self._table.rows if all(r.get(k) == self._payload[k] for k in key_fields)
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

        rows = [r for r in self._table.rows if all(r.get(k) == v for k, v in self._filters.items())]
        return FakeResult(rows)


class FakeClient:
    def __init__(self):
        self._tables: dict = {}

    def table(self, name):
        return FakeQueryBuilder(self._tables.setdefault(name, FakeTable()))


@pytest.fixture
def configured_store():
    return OverrideStore(client=FakeClient())


def test_propose_appends_the_first_version(configured_store):
    version = configured_store.propose("a.b", "body text", note="n", actor="ada@x.com")
    assert version == 1


def test_propose_increments_on_repeat_calls(configured_store):
    configured_store.propose("a.b", "v1", note="n", actor="ada@x.com")
    version = configured_store.propose("a.b", "v2", note="n", actor="ada@x.com")
    assert version == 2


def test_publish_then_body_for_returns_the_published_version(configured_store):
    version = configured_store.propose("a.b", "the body", note="n", actor="ada@x.com")
    configured_store.publish("a.b", version, actor="eve@x.com")
    body, published_version = configured_store.body_for("a.b")
    assert body == "the body"
    assert published_version == version


def test_body_for_returns_none_before_anything_is_published(configured_store):
    configured_store.propose("a.b", "draft only", note="n", actor="ada@x.com")
    assert configured_store.body_for("a.b") is None


def test_revert_to_default_drops_the_label(configured_store):
    version = configured_store.propose("a.b", "the body", note="n", actor="ada@x.com")
    configured_store.publish("a.b", version, actor="eve@x.com")
    configured_store.invalidate()
    configured_store.revert_to_default("a.b", actor="eve@x.com")
    configured_store.invalidate()
    assert configured_store.body_for("a.b") is None


def test_versions_lists_history_for_a_prompt(configured_store):
    configured_store.propose("a.b", "v1", note="first", actor="ada@x.com")
    configured_store.propose("a.b", "v2", note="second", actor="ada@x.com")
    versions = configured_store.versions("a.b")
    assert len(versions) == 2


# ── doc bindings ─────────────────────────────────────────────────────────────


def test_set_doc_binding_then_doc_id_for_returns_it(configured_store):
    configured_store.set_doc_binding("a.b", "DOC1", is_override=False, actor="ada@x.com")
    assert configured_store.doc_id_for("a.b") == "DOC1"


def test_set_doc_binding_then_doc_override_for_returns_it(configured_store):
    configured_store.set_doc_binding("a.b", "DOC1", is_override=True, actor="ada@x.com")
    assert configured_store.doc_override_for("a.b") is True


def test_doc_binding_with_override_false_is_distinct_from_no_binding(configured_store):
    """A row with is_override=False and no row at all both resolve to False,
    but only one has a doc_id_for -- override defaulting to False must not be
    confused with the doc not being attached at all."""
    configured_store.set_doc_binding("a.b", "DOC1", is_override=False, actor="ada@x.com")
    assert configured_store.doc_override_for("a.b") is False
    assert configured_store.doc_id_for("a.b") == "DOC1"


def test_set_doc_binding_twice_updates_in_place(configured_store):
    configured_store.set_doc_binding("a.b", "DOC1", is_override=False, actor="ada@x.com")
    configured_store.set_doc_binding("a.b", "DOC2", is_override=True, actor="eve@x.com")
    assert configured_store.doc_id_for("a.b") == "DOC2"
    assert configured_store.doc_override_for("a.b") is True
    assert len(configured_store.all_doc_bindings()) == 1


def test_clear_doc_binding_falls_back_to_legacy_env_var(configured_store, monkeypatch):
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "LEGACY_DOC")
    configured_store.set_doc_binding(
        "customer.system", "DOC1", is_override=True, actor="ada@x.com"
    )
    assert configured_store.doc_id_for("customer.system") == "DOC1"

    configured_store.clear_doc_binding("customer.system", actor="eve@x.com")

    assert configured_store.doc_id_for("customer.system") == "LEGACY_DOC"
    assert configured_store.doc_override_for("customer.system") is False


def test_all_doc_bindings_returns_every_binding(configured_store):
    configured_store.set_doc_binding("a.b", "DOC1", is_override=False, actor="ada@x.com")
    configured_store.set_doc_binding("c.d", "DOC2", is_override=True, actor="ada@x.com")
    assert configured_store.all_doc_bindings() == {
        "a.b": ("DOC1", False),
        "c.d": ("DOC2", True),
    }
