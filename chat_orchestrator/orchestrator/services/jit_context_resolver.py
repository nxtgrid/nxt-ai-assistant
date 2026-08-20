"""Resolves just-in-time context modules for one request.

PromptLibrary.render() is synchronous, and graph/directory/episodic bodies
need async, permission-filtered database work. Rather than make render()
async -- it is called from dozens of sync sites including scripts and MCP
servers -- those three sources resolve here and their output is appended to
context_message.

The pins are read through KnowledgeStore.overrides_for, the same call
PromptLibrary._compose_knowledge uses. There must never be a second
mechanism for deciding what a prompt is attached to.
"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule, select_for_prompt
from shared.prompts.providers import ProviderRegistry, ResolutionContext
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0

# Matches PromptLibrary's PINNED_BUDGET_CHARS. budget_pinned never sees these
# bodies -- a provider body has no length until it resolves, which happens
# here -- so without this one large document uncaps every prompt it is pinned
# to.
JIT_BUDGET_CHARS = 20000


def budget_resolved(resolved, limit: int = JIT_BUDGET_CHARS):
    """Fit resolved bodies into the budget by dropping whole modules.

    Site-scoped material is kept first: most specific, least replaceable.
    Mirrors shared.prompts.knowledge.budget_pinned, including never cutting
    a document in half.
    """
    kept, dropped, used = [], [], 0
    for module, text in sorted(
        resolved, key=lambda pair: (not pair[0].is_site_scoped, pair[0].slug)
    ):
        if used + len(text) <= limit:
            kept.append((module, text))
            used += len(text)
        else:
            dropped.append(module)
    if dropped:
        LOGGER.warning(
            f"Live context exceeded the {limit}-char budget; dropped "
            f"{len(dropped)} module(s): {', '.join(m.slug for m in dropped)}"
        )
    return kept


class JitContextResolver:
    """Resolves the provider-backed modules a prompt pins."""

    def __init__(self, store=None, registry: Optional[ProviderRegistry] = None) -> None:
        if store is None:
            from shared.prompts.knowledge import KnowledgeStore

            store = KnowledgeStore.from_env()
        self._store = store
        self._registry = registry if registry is not None else ProviderRegistry()
        self.timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    async def resolve_for_prompt(
        self, prompt_id: str, ctx: ResolutionContext
    ) -> Tuple[str, List[str]]:
        """Return (text_block, slugs_used). Never raises."""
        try:
            modules = self._store.all_modules()
            pins = self._store.overrides_for(prompt_id)
        except Exception:
            LOGGER.warning(
                f"JIT module lookup failed for '{prompt_id}'; continuing without", exc_info=True
            )
            return "", []

        chosen = [m for m in select_for_prompt(modules, pins, ctx.scope) if m.is_jit]
        if not chosen:
            return "", []

        pinned = [m for m in chosen if m.mode == "pinned"]
        on_demand = await self._visible_only([m for m in chosen if m.mode != "pinned"], ctx)

        resolved = budget_resolved(await self._resolve_all(pinned, ctx))

        blocks: List[str] = []
        used: List[str] = []

        if resolved:
            body = "\n\n".join(f"## {m.title}\n\n{text.strip()}" for m, text in resolved)
            blocks.append(f"# Live Context\n\n{body}")
            used.extend(m.slug for m, _ in resolved)

        if on_demand:
            lines = "\n".join(
                f"- `{m.slug}` — {m.summary}" for m in sorted(on_demand, key=lambda m: m.slug)
            )
            blocks.append(
                "# Available Live Context\n\n"
                "Fetch any of these with the `get_knowledge_module` tool when relevant:\n\n"
                + lines
            )
            used.extend(m.slug for m in on_demand)

        return "\n\n".join(blocks), used

    async def _visible_only(
        self, modules: List[KnowledgeModule], ctx: ResolutionContext
    ) -> List[KnowledgeModule]:
        """Drop on-demand modules this caller may not fetch.

        A catalog line carries the module's summary, which can itself be
        sensitive -- and listing something the caller will be refused wastes
        a model turn. Providers without a visible_to (graph, directory,
        episodic) filter inside resolve() instead and pass through here.
        """
        out: List[KnowledgeModule] = []
        for module in modules:
            provider = self._registry.get(module.source)
            check = getattr(provider, "visible_to", None) if provider else None
            if check is None:
                out.append(module)
                continue
            try:
                if await asyncio.wait_for(check(module, ctx), timeout=self.timeout_seconds):
                    out.append(module)
            except Exception:
                LOGGER.warning(
                    f"Visibility check failed for '{module.slug}'; withholding",
                    exc_info=True,
                )
        return out

    async def _resolve_all(
        self, modules: List[KnowledgeModule], ctx: ResolutionContext
    ) -> List[Tuple[KnowledgeModule, str]]:
        """Resolve concurrently. A failure drops one module, never the batch."""
        pending = [(m, self._registry.get(m.source)) for m in modules]
        runnable = [(m, p) for m, p in pending if p is not None]

        for module, provider in pending:
            if provider is None:
                LOGGER.warning(
                    f"Module '{module.slug}' declares source '{module.source}' "
                    f"with no registered provider; skipping"
                )

        if not runnable:
            return []

        results = await asyncio.gather(
            *(self._resolve_one(m, p, ctx) for m, p in runnable),
            return_exceptions=True,
        )

        out: List[Tuple[KnowledgeModule, str]] = []
        for (module, _), result in zip(runnable, results):
            if isinstance(result, BaseException):
                LOGGER.warning(
                    f"Provider '{module.source}' failed for module '{module.slug}': {result}"
                )
                continue
            if result:
                out.append((module, result))
        return out

    async def _resolve_one(self, module, provider, ctx: ResolutionContext) -> Optional[str]:
        return await asyncio.wait_for(
            provider.resolve(module, ctx), timeout=self.timeout_seconds
        )


def build_default_registry() -> ProviderRegistry:
    """Every provider that can be constructed in this process.

    A provider whose dependencies are missing is omitted rather than
    registered-and-broken: a module naming it then logs one clear "no
    registered provider" warning per request instead of a stack trace.
    """
    registry = ProviderRegistry()
    try:
        from shared.prompts.providers_gdoc import GDocProvider

        registry.register(GDocProvider())
    except Exception:
        LOGGER.warning("GDocProvider unavailable", exc_info=True)
    try:
        from orchestrator.services.providers.directory_provider import DirectoryProvider

        registry.register(DirectoryProvider())
    except Exception:
        LOGGER.warning("DirectoryProvider unavailable", exc_info=True)
    try:
        from orchestrator.services.providers.graph_provider import GraphProvider

        registry.register(GraphProvider())
    except Exception:
        LOGGER.warning("GraphProvider unavailable", exc_info=True)
    try:
        from orchestrator.services.providers.episodic_provider import EpisodicProvider

        registry.register(EpisodicProvider())
    except Exception:
        LOGGER.warning("EpisodicProvider unavailable", exc_info=True)
    return registry


_RESOLVER: Optional[JitContextResolver] = None


def get_jit_resolver() -> JitContextResolver:
    """Process-wide resolver, so provider-internal caches are actually reused.

    Providers are registered here at first construction. A provider that
    cannot be built (missing credentials, missing table) is simply absent;
    modules naming it log a warning per request and contribute nothing.
    """
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = JitContextResolver(registry=build_default_registry())
    return _RESOLVER


__all__ = ["JitContextResolver", "build_default_registry", "get_jit_resolver"]
