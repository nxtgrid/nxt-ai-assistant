"""Tests for the single Google Doc prompt adapter."""

from shared.prompts.gdoc import GDocStore


def test_returns_none_when_no_doc_configured():
    store = GDocStore(doc_id_for=lambda pid: None, fetch=lambda doc: "x")
    assert store.body_for("a.b") is None


def test_fetches_configured_doc():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "# System Instructions\n\nFrom the doc."

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch)
    assert store.body_for("a.b") == "# System Instructions\n\nFrom the doc."
    assert calls == ["DOC1"]


def test_caches_within_ttl():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "body"

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch, ttl_seconds=1000)
    store.body_for("a.b")
    store.body_for("a.b")
    assert calls == ["DOC1"]


def test_fetch_failure_returns_none_and_does_not_raise():
    def fetch(doc_id):
        raise RuntimeError("drive is down")

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch)
    assert store.body_for("a.b") is None


def test_empty_document_is_treated_as_absent():
    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=lambda doc: "   ")
    assert store.body_for("a.b") is None


def test_invalidate_clears_cache():
    calls = []

    def fetch(doc_id):
        calls.append(doc_id)
        return "body"

    store = GDocStore(doc_id_for=lambda pid: "DOC1", fetch=fetch, ttl_seconds=1000)
    store.body_for("a.b")
    store.invalidate()
    store.body_for("a.b")
    assert calls == ["DOC1", "DOC1"]
