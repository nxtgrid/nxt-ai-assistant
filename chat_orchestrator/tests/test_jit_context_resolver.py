"""JIT context resolution: selection, concurrency, fail-open, rendering."""

import asyncio

import pytest

from orchestrator.services.jit_context_resolver import JitContextResolver
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.prompts.types import RequestScope


def _module(slug, source="graph", mode="pinned", scope="sector"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary=f"About {slug}.",
        body=None, scope=scope, mode=mode, source=source,
    )


class _FakeStore:
    def __init__(self, modules, pins):
        self._modules = modules
        self._pins = pins

    def all_modules(self):
        return self._modules

    def overrides_for(self, _prompt_id):
        return self._pins


class _FakeProvider:
    def __init__(self, source, text=None, raises=None, delay=0.0):
        self.source = source
        self._text = text
        self._raises = raises
        self._delay = delay
        self.calls = 0

    async def resolve(self, module, ctx):
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise self._raises
        return self._text


def _resolver(modules, pins, providers):
    registry = ProviderRegistry()
    for p in providers:
        registry.register(p)
    return JitContextResolver(store=_FakeStore(modules, pins), registry=registry)


@pytest.mark.asyncio
async def test_resolves_a_pinned_jit_module():
    provider = _FakeProvider("graph", text="Entity types: Meter, DCU.")
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "Entity types: Meter, DCU." in text
    assert used == ["graph-overview"]
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_ignores_unpinned_modules():
    provider = _FakeProvider("graph", text="X")
    resolver = _resolver([_module("graph-overview")], {}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_ignores_non_jit_modules():
    """A manual module's body is PromptLibrary's job, not the resolver's."""
    manual = KnowledgeModule(
        id="m", slug="m", title="M", summary="s", body="stored", source="manual"
    )
    provider = _FakeProvider("manual", text="should not be called")
    resolver = _resolver([manual], {"m": True}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_a_raising_provider_does_not_break_the_others():
    good = _FakeProvider("graph", text="Graph body")
    bad = _FakeProvider("directory", raises=RuntimeError("boom"))
    resolver = _resolver(
        [_module("graph-overview"), _module("directory", source="directory")],
        {"graph-overview": True, "directory": True},
        [good, bad],
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "Graph body" in text
    assert used == ["graph-overview"]


@pytest.mark.asyncio
async def test_a_provider_returning_none_contributes_nothing():
    provider = _FakeProvider("episodic", text=None)
    resolver = _resolver(
        [_module("episodic", source="episodic")], {"episodic": True}, [provider]
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_a_slow_provider_times_out_without_blocking():
    slow = _FakeProvider("graph", text="late", delay=5.0)
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [slow])
    resolver.timeout_seconds = 0.05

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_module_with_no_registered_provider_is_skipped():
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_scope_gate_still_applies():
    """A site-scoped module stays out of a conversation about another site."""
    provider = _FakeProvider("graph", text="Alpha graph")
    resolver = _resolver(
        [_module("graph-overview", scope="site:Alpha")], {"graph-overview": True}, [provider]
    )

    text, _ = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope(grid="Beta"))
    )

    assert text == ""
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_on_demand_jit_module_renders_a_catalog_line_not_a_body():
    provider = _FakeProvider("graph", text="full body")
    resolver = _resolver(
        [_module("graph-overview", mode="on_demand")], {"graph-overview": True}, [provider]
    )

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "graph-overview" in text
    assert "About graph-overview." in text
    assert "full body" not in text
    assert provider.calls == 0
    assert used == ["graph-overview"]


def test_default_registry_includes_the_directory_provider(monkeypatch):
    # DirectoryProvider() with no args builds a real AuthService() (a live
    # postgres connection needing AUTH_DB_*) -- stub the source
    # build_default_registry's local `from shared.auth import
    # get_auth_service` resolves against, so registration succeeds without
    # one. build_default_registry itself must already tolerate a
    # DirectoryProvider that can't construct (that's the whole point of its
    # try/except) -- this test is about wiring, not auth.
    import shared.auth

    monkeypatch.setattr(shared.auth, "get_auth_service", lambda: object())

    from orchestrator.services.jit_context_resolver import build_default_registry

    assert "directory" in build_default_registry().sources()


@pytest.mark.asyncio
async def test_fetch_jit_context_returns_empty_when_nothing_registered():
    from orchestrator.graphs.nodes.prepare_context import _fetch_jit_context

    text, used = await _fetch_jit_context("staff.system", None)
    assert text == ""
    assert used == []


@pytest.mark.asyncio
async def test_fetch_jit_context_never_raises(monkeypatch):
    # NOT `import orchestrator.graphs.nodes.prepare_context as pc`: that's an
    # attribute walk, and orchestrator/graphs/nodes/__init__.py's own
    # `from .prepare_context import prepare_context` overwrites the nodes
    # package's `prepare_context` *module* attribute with the `prepare_context`
    # *function* (same name, package re-export). importlib goes through
    # sys.modules instead and gets the real module either way.
    import importlib

    pc = importlib.import_module("orchestrator.graphs.nodes.prepare_context")

    def _boom(*_a, **_k):
        raise RuntimeError("store exploded")

    monkeypatch.setattr(pc, "get_jit_resolver", _boom)
    text, used = await pc._fetch_jit_context("staff.system", None)
    assert text == ""
    assert used == []
