"""The on-demand tier's fetch tool."""

from servers.knowledge_server.knowledge_mcp_server import fetch_knowledge_module


class FakeStore:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


def _module(slug):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary="s",
        body=f"{slug} full body",
        tags=[],
        scope="sector",
        mode="on_demand",
    )


def test_returns_the_body_for_a_known_slug():
    out = fetch_knowledge_module("sites", store=FakeStore([_module("sites")]))
    assert "sites full body" in out


def test_unknown_slug_returns_a_helpful_message_not_an_exception():
    out = fetch_knowledge_module("nope", store=FakeStore([_module("sites")]))
    assert "nope" in out and "sites" in out


def test_empty_store_says_so():
    assert "no knowledge modules" in fetch_knowledge_module("x", store=FakeStore([])).lower()


def test_a_provider_backed_module_reports_that_it_cannot_be_fetched_here():
    """A JIT body needs the caller's permissions, which this tool does not have.

    Returning an empty body would read to the model as "this module is empty",
    which is worse than a clear explanation it can act on.
    """
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="g", slug="entity-graph", title="Graph", summary="Ontology.",
                    body=None, source="graph",
                )
            ]

    text = fetch_knowledge_module("entity-graph", store=_Store())

    assert "entity-graph" in text
    assert "empty" not in text.lower()
    assert "automatically" in text.lower()


def test_a_gdoc_module_body_is_resolved():
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="d", slug="procs", title="Procedures", summary="How-tos.",
                    body=None, source="gdoc", source_ref="doc-1",
                )
            ]

    class _Gdoc:
        def body_for(self, m):
            return f"resolved {m.source_ref}"

    text = fetch_knowledge_module("procs", store=_Store(), gdoc_provider=_Gdoc())

    assert "resolved doc-1" in text


def test_a_manual_module_still_returns_its_stored_body():
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="m", slug="comms", title="Comms", summary="s", body="stored body"
                )
            ]

    assert "stored body" in fetch_knowledge_module("comms", store=_Store())
