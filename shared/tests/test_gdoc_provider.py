"""Google Doc-backed context modules."""

from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers_gdoc import GDocProvider


def _module(source_ref="doc-abc"):
    return KnowledgeModule(
        id="d", slug="procedures", title="Procedures", summary="How-tos.",
        body=None, source="gdoc", source_ref=source_ref,
    )


def test_resolves_a_doc_body():
    provider = GDocProvider(fetch=lambda doc_id: f"body of {doc_id}")
    assert provider.body_for(_module()) == "body of doc-abc"


def test_a_module_without_a_source_ref_resolves_to_none():
    provider = GDocProvider(fetch=lambda doc_id: "never")
    assert provider.body_for(_module(source_ref=None)) is None


def test_a_failing_fetch_resolves_to_none():
    def _boom(_doc_id):
        raise RuntimeError("403")

    assert GDocProvider(fetch=_boom).body_for(_module()) is None


def test_an_empty_doc_resolves_to_none():
    assert GDocProvider(fetch=lambda _d: "   ").body_for(_module()) is None


def test_results_are_cached_per_doc():
    calls = []

    def _fetch(doc_id):
        calls.append(doc_id)
        return "body"

    provider = GDocProvider(fetch=_fetch, ttl_seconds=300)
    provider.body_for(_module())
    provider.body_for(_module())
    assert calls == ["doc-abc"]


def test_invalidate_forces_a_refetch():
    calls = []

    def _fetch(doc_id):
        calls.append(doc_id)
        return "body"

    provider = GDocProvider(fetch=_fetch, ttl_seconds=300)
    provider.body_for(_module())
    provider.invalidate()
    provider.body_for(_module())
    assert calls == ["doc-abc", "doc-abc"]
