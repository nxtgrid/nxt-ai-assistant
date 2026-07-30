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
