"""Persist the approved context module and reconcile its prompt pins.

Runs only once prepare_module_approval's reply turn has already resolved to
approval (it returns a plain, non-paused StepResult on approve) -- there is
no approval reply left to interpret here, only the write.
"""

import asyncio
from typing import Any, Dict, List

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def resolve_mode(body: str) -> str:
    """New modules are on_demand.

    Pinned modules are inlined into every render of every prompt that uses
    them and share a fixed character budget, so promoting one is a deliberate
    decision an operator makes on the Context page -- never a default.
    """
    return "on_demand"


def build_module_payload(
    slug: str, title: str, summary: str, body: str, mode: str, actor: str
) -> Dict[str, Any]:
    """A knowledge_modules row for a staff-authored module."""
    return {
        "slug": slug,
        "title": title,
        "summary": summary,
        "body": body,
        "tags": [],
        "scope": "sector",
        "mode": mode,
        "source": "manual",
        "updated_by": actor,
    }


@register_step("store_module")
async def store_module(context: StepContext) -> StepResult:
    """Persist the module, then reconcile its prompt pins."""
    from shared.prompts.knowledge import KnowledgeStore

    slug = context.get_state("module_slug") or ""
    body = context.get_state("module_body") or ""
    replace = bool(context.get_state("module_replace"))
    actor = context.effective_email or "unknown"

    payload = build_module_payload(
        slug=slug,
        title=context.get_state("module_title") or "",
        summary=context.get_state("module_summary") or "",
        body=body,
        mode=context.get_state("module_mode") or resolve_mode(body),
        actor=actor,
    )

    store = await asyncio.to_thread(KnowledgeStore.from_env)
    if not store._client:  # noqa: SLF001 -- readiness check, mirrors the admin page
        return StepResult.failure("Context storage is not configured.")

    try:
        if replace:
            await asyncio.to_thread(
                lambda: store._client.table("knowledge_modules")  # noqa: SLF001
                .update(payload)
                .eq("slug", slug)
                .execute()
            )
            modules = await asyncio.to_thread(store.all_modules)
            module_id = next(m.id for m in modules if m.slug == slug)
        else:
            result = await asyncio.to_thread(
                lambda: store._client.table("knowledge_modules")  # noqa: SLF001
                .insert(payload)
                .execute()
            )
            module_id = result.data[0]["id"]
    except Exception as e:
        LOGGER.exception(f"Failed to store context module {slug!r}: {e}")
        return StepResult.failure("Could not save the context module.")

    prompt_ids: List[str] = context.get_state("module_prompt_ids") or []
    if prompt_ids:
        try:
            await asyncio.to_thread(store.set_prompt_pins, module_id, prompt_ids, actor)
        except Exception as e:
            LOGGER.warning(f"Module {slug!r} saved but pinning failed: {e}")

    await asyncio.to_thread(store.invalidate)

    attached = f" and attached to {', '.join(prompt_ids)}" if prompt_ids else ""
    return StepResult(
        data={"slug": slug, "module_id": module_id, "prompt_ids": prompt_ids},
        state_updates={"stored_module_slug": slug},
        progress_message=f"✅ Saved **{payload['title']}** as `{slug}`{attached}.",
    )
