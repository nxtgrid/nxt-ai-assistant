"""The on-demand tier's fetch tool."""

import pytest
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


@pytest.mark.asyncio
async def test_returns_the_body_for_a_known_slug():
    out = await fetch_knowledge_module("sites", store=FakeStore([_module("sites")]))
    assert "sites full body" in out


@pytest.mark.asyncio
async def test_unknown_slug_returns_a_helpful_message_not_an_exception():
    out = await fetch_knowledge_module("nope", store=FakeStore([_module("sites")]))
    assert "nope" in out and "sites" in out


@pytest.mark.asyncio
async def test_empty_store_says_so():
    out = await fetch_knowledge_module("x", store=FakeStore([]))
    assert "no knowledge modules" in out.lower()


@pytest.mark.asyncio
async def test_a_provider_backed_module_reports_that_it_cannot_be_fetched_here():
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

    text = await fetch_knowledge_module("entity-graph", store=_Store())

    assert "entity-graph" in text
    assert "empty" not in text.lower()
    assert "automatically" in text.lower()


@pytest.mark.asyncio
async def test_a_manual_module_still_returns_its_stored_body():
    from shared.prompts.knowledge import KnowledgeModule

    class _Store:
        def all_modules(self):
            return [
                KnowledgeModule(
                    id="m", slug="comms", title="Comms", summary="s", body="stored body"
                )
            ]

    text = await fetch_knowledge_module("comms", store=_Store())
    assert "stored body" in text


class _Store:
    def __init__(self, modules):
        self._modules = modules

    def all_modules(self):
        return self._modules


class _Provider:
    def __init__(self, visible=True, body="doc body"):
        self._visible, self._body = visible, body

    async def visible_to(self, _module, _ctx):
        return self._visible

    async def resolve(self, _module, _ctx):
        return self._body if self._visible else None


def _doc_module(slug="specs"):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id="1", slug=slug, title="Specs", summary="s", body=None,
        source="gdoc", source_ref="doc-1", doc_audience="acl_mirror",
    )


@pytest.mark.asyncio
async def test_an_allowed_caller_gets_the_document_body():
    text = await fetch_knowledge_module(
        "specs", user_email="tech@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=True),
    )

    assert "doc body" in text


@pytest.mark.asyncio
async def test_a_denied_caller_is_refused_and_told_why():
    """An empty body would read to the model as 'this module has no content'."""
    text = await fetch_knowledge_module(
        "specs", user_email="outsider@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=False),
    )

    assert "doc body" not in text
    assert "access" in text.lower()


@pytest.mark.asyncio
async def test_a_caller_with_no_email_is_refused():
    text = await fetch_knowledge_module(
        "specs", user_email=None,
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=False),
    )

    assert "doc body" not in text


@pytest.mark.asyncio
async def test_a_doc_module_is_still_fetchable_on_demand():
    """gdoc became JIT in this change; the is_jit refusal must not swallow it."""
    text = await fetch_knowledge_module(
        "specs", user_email="tech@example.com",
        store=_Store([_doc_module()]), gdoc_provider=_Provider(visible=True),
    )

    assert "cannot be fetched on demand" not in text


@pytest.mark.asyncio
async def test_a_graph_module_is_still_refused():
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="2", slug="graph-ctx", title="Graph", summary="s", body=None, source="graph"
    )
    text = await fetch_knowledge_module(
        "graph-ctx", user_email="tech@example.com", store=_Store([module])
    )

    assert "cannot be fetched on demand" in text
