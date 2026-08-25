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
from typing import Any, List, Optional, Tuple

from shared.prompts.knowledge import KnowledgeModule, select_for_prompt
from shared.prompts.providers import (
    ProviderRegistry,
    ResolutionContext,
    build_default_registry,
)
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 5.0

# Matches PromptLibrary's INLINE_BUDGET_CHARS. budget_inlined never sees these
# bodies -- a provider body has no length until it resolves, which happens
# here -- so without this one large document uncaps every prompt it is
# attached to.
JIT_BUDGET_CHARS = 20000


def budget_resolved(resolved, limit: int = JIT_BUDGET_CHARS):
    """Fit resolved bodies into the budget by dropping whole modules.

    Site-scoped material is kept first: most specific, least replaceable.
    Mirrors shared.prompts.knowledge.budget_inlined, including never cutting
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
            LOGGER.opt(exception=True).warning(
                f"JIT module lookup failed for '{prompt_id}'; continuing without"
            )
            return "", []

        chosen = [m for m in select_for_prompt(modules, pins, ctx.scope) if m.is_jit]
        if not chosen:
            return "", []

        # Every attached module is inlined in full. Authorization is not
        # weakened by dropping the old catalog branch: resolve() is the
        # authoritative gate and always was (GDocProvider.resolve calls its
        # own visible_to; graph/directory/episodic filter rows by permission
        # inside resolve), and with no summary-only catalog there is nothing
        # left that could name a module ahead of that gate.
        resolved = budget_resolved(await self._resolve_all(chosen, ctx))
        if not resolved:
            return "", []

        body = "\n\n".join(f"## {m.title}\n\n{text.strip()}" for m, text in resolved)
        return f"# Live Context\n\n{body}", [m.slug for m, _ in resolved]

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


async def resolve_jit_context_for(
    prompt_id: str,
    user_context: Optional[Any],
    grid: Optional[str] = None,
) -> Tuple[str, List[str]]:
    """Resolve provider-backed context modules for a pinning id. Fail open.

    Shared by prepare_context.py (a live conversation turn) and
    skill_runner.py (a skill run) -- both just need "this id's JIT modules,
    for this caller" and neither should re-implement the try/except.
    """
    try:
        from shared.prompts.providers import ResolutionContext

        ctx = ResolutionContext.from_user_context(user_context, grid=grid)
        return await get_jit_resolver().resolve_for_prompt(prompt_id, ctx)
    except Exception as e:
        LOGGER.warning(f"JIT context resolution failed (continuing without): {e}")
        return "", []


# build_default_registry is re-exported, not defined here: it lives in
# shared.prompts.providers so anansi_app (whose image has no `orchestrator`
# package -- see anansi_app/Dockerfile) can build the same registry for the
# Context page's preview pane. Kept importable from this path because
# prepare_context.py and this module's tests already name it.
__all__ = [
    "JitContextResolver",
    "budget_resolved",
    "build_default_registry",
    "get_jit_resolver",
    "resolve_jit_context_for",
]
