"""JIT context resolution: selection, concurrency, fail-open, rendering."""

import asyncio

import pytest

from unittest.mock import patch

from orchestrator.services.jit_context_resolver import JitContextResolver, resolve_jit_context_for
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.prompts.types import RequestScope


def _module(slug, source="graph", scope="sector"):
    return KnowledgeModule(
        id=slug, slug=slug, title=slug.title(), summary=f"About {slug}.",
        body=None, scope=scope, source=source,
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
async def test_resolves_an_attached_jit_module():
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
async def test_every_attached_jit_module_is_resolved_and_inlined_in_full():
    """The old on-demand tier rendered a summary and never called resolve().

    Attaching a module now means its resolved content reaches the prompt, so
    the provider must actually be called and its body must appear.
    """
    provider = _FakeProvider("graph", text="full body")
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [provider])

    text, used = await resolver.resolve_for_prompt(
        "staff.system", ResolutionContext(scope=RequestScope())
    )

    assert "full body" in text
    assert "get_knowledge_module" not in text
    assert provider.calls == 1
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

    # _fetch_jit_context now delegates to resolve_jit_context_for (see
    # resolve_jit_context_for's own fail-open test above) -- patch the
    # resolver where that function actually looks it up, not on
    # prepare_context itself, which no longer imports get_jit_resolver at all.
    import orchestrator.services.jit_context_resolver as jcr

    monkeypatch.setattr(jcr, "get_jit_resolver", _boom)
    text, used = await pc._fetch_jit_context("staff.system", None)
    assert text == ""
    assert used == []


def test_budget_resolved_keeps_everything_that_fits():
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    a = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    b = KnowledgeModule(id="2", slug="b", title="B", summary="s", source="gdoc")

    kept = budget_resolved([(a, "x" * 10), (b, "y" * 10)], limit=100)

    assert [m.slug for m, _ in kept] == ["a", "b"]


def test_budget_resolved_drops_whole_modules_not_fragments():
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    a = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    b = KnowledgeModule(id="2", slug="b", title="B", summary="s", source="gdoc")

    kept = budget_resolved([(a, "x" * 60), (b, "y" * 60)], limit=100)

    assert len(kept) == 1
    assert len(kept[0][1]) == 60


def test_budget_resolved_keeps_site_scoped_material_first():
    """Most specific, least replaceable -- same rule as budget_inlined."""
    from orchestrator.services.jit_context_resolver import budget_resolved
    from shared.prompts.knowledge import KnowledgeModule

    general = KnowledgeModule(id="1", slug="a", title="A", summary="s", source="gdoc")
    site = KnowledgeModule(
        id="2", slug="z", title="Z", summary="s", source="gdoc", scope="site:ABC"
    )

    kept = budget_resolved([(general, "x" * 60), (site, "y" * 60)], limit=100)

    assert [m.slug for m, _ in kept] == ["z"]


def test_budget_resolved_on_an_empty_list_is_empty():
    from orchestrator.services.jit_context_resolver import budget_resolved

    assert budget_resolved([], limit=100) == []


class _GatedProvider:
    """A provider that enforces its own access check inside resolve().

    Faithful to GDocProvider, which calls its own visible_to() at the top of
    resolve() and returns None when denied. That is the load-bearing detail:
    resolve() is the authoritative gate. The resolver used to pre-filter
    on-demand modules through visible_to() before rendering a catalog line;
    with no catalog left, resolve() is the only gate there is, so a fake
    that returned a body regardless of its own check would be testing a
    contract no real provider has.
    """

    source = "gdoc"

    def __init__(self, visible):
        self._visible = visible

    async def visible_to(self, _module, _ctx):
        return self._visible

    async def resolve(self, module, ctx):
        if not await self.visible_to(module, ctx):
            return None
        return "body"


def _gated_module(slug="secret-doc"):
    from shared.prompts.knowledge import KnowledgeModule

    return KnowledgeModule(
        id="1", slug=slug, title="T", summary="A sensitive summary.",
        body=None, source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )


def _catalog_resolver(provider, module):
    """Distinct from the module-level _resolver(modules, pins, providers)
    above -- same shape, different argument order, kept separate rather than
    forcing this block's call sites to conform to an unrelated signature."""
    return _resolver([module], {module.slug: True}, [provider])


@pytest.mark.asyncio
async def test_a_denied_module_contributes_nothing():
    """Neither its body nor its name may reach a caller who cannot see it."""
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    module = _gated_module()
    resolver = _catalog_resolver(_GatedProvider(visible=False), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" not in text
    assert "A sensitive summary." not in text
    assert used == []


@pytest.mark.asyncio
async def test_an_allowed_module_is_inlined_in_full():
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    module = _gated_module()
    resolver = _catalog_resolver(_GatedProvider(visible=True), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "body" in text
    assert used == ["secret-doc"]


@pytest.mark.asyncio
async def test_a_provider_that_raises_while_checking_access_fails_closed():
    """A provider whose own gate blows up must contribute nothing.

    graph/directory/episodic filter database rows by permission inside
    resolve(); gdoc checks Drive. Whichever way it fails, the resolver drops
    that one module rather than serving unfiltered content.
    """
    from shared.prompts.providers import ResolutionContext
    from shared.prompts.types import RequestScope

    class _Boom:
        source = "gdoc"

        async def resolve(self, _module, _ctx):
            raise RuntimeError("drive down")

    module = _gated_module()
    resolver = _catalog_resolver(_Boom(), module)

    text, used = await resolver.resolve_for_prompt(
        "customer.system", ResolutionContext(scope=RequestScope(), user_email="a@b.com")
    )

    assert "secret-doc" not in text
    assert used == []


@pytest.mark.asyncio
async def test_resolve_jit_context_for_delegates_to_the_process_wide_resolver():
    provider = _FakeProvider("graph", text="Entity types: grid, meter.")
    resolver = _resolver([_module("graph-overview")], {"graph-overview": True}, [provider])

    with patch(
        "orchestrator.services.jit_context_resolver.get_jit_resolver", return_value=resolver
    ):
        text, used = await resolve_jit_context_for("skill:abc", user_context=None, grid=None)

    assert "Entity types: grid, meter." in text
    assert used == ["graph-overview"]


@pytest.mark.asyncio
async def test_resolve_jit_context_for_fails_open_on_error():
    with patch(
        "orchestrator.services.jit_context_resolver.get_jit_resolver",
        side_effect=RuntimeError("boom"),
    ):
        text, used = await resolve_jit_context_for("skill:abc", user_context=None, grid=None)

    assert text == ""
    assert used == []
