"""Collision detection for context modules.

The RAG flow's chunk-level "incorporate" mode has no analogue here -- a module
is one row, so the operator's choices are replace, keep both, or cancel.

Follows the same ask-then-process-the-reply shape as
ingestion_expert/detect_duplicates.py: one step handler covers both turns,
distinguished by an `awaiting_collision_decision` flag in packet_state.
"""

import asyncio
import hashlib
import re
from typing import Any, Dict, List, Set

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

CANCEL_WORDS = {"cancel", "skip", "abort", "quit", "exit", "stop", "no", "3"}
REPLACE_WORDS = {"replace", "1"}
KEEP_BOTH_WORDS = {"keep both", "keep_both", "2"}


def hash_body(text: str) -> str:
    """SHA256 of normalized text -- lowercased, whitespace collapsed."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def classify_collision(
    slug: str, title: str, body: str, existing: List[Dict[str, Any]]
) -> str:
    """One of: identical, slug_taken, title_taken, none."""
    body_hash = hash_body(body)
    for module in existing:
        if hash_body(module.get("body", "")) == body_hash:
            return "identical"
    if any(m.get("slug") == slug for m in existing):
        return "slug_taken"
    if any(m.get("title", "").strip().lower() == title.strip().lower() for m in existing):
        return "title_taken"
    return "none"


def unique_slug(slug: str, taken: Set[str]) -> str:
    """First free ``slug``, ``slug-2``, ``slug-3``… ."""
    if slug not in taken:
        return slug
    n = 2
    while f"{slug}-{n}" in taken:
        n += 1
    return f"{slug}-{n}"


def _collision_choice_options(suggested_slug: str) -> List[str]:
    return [
        "Replace the existing module",
        f"Keep both (as {suggested_slug})",
        "Cancel",
    ]


@register_step("detect_module_duplicates")
async def detect_module_duplicates(context: StepContext) -> StepResult:
    """Check the proposed module against existing ones; ask how to proceed on collision."""
    if context.get_state("awaiting_collision_decision") and context.user_input:
        return _handle_collision_decision(context)

    from shared.prompts.knowledge import KnowledgeStore

    slug = context.get_state("module_slug") or ""
    title = context.get_state("module_title") or ""
    body = context.get_state("module_body") or ""

    store = await asyncio.to_thread(KnowledgeStore.from_env)
    modules = await asyncio.to_thread(store.all_modules)
    existing = [{"slug": m.slug, "title": m.title, "body": m.body} for m in modules]

    collision = classify_collision(slug, title, body, existing)
    LOGGER.info(f"Module collision check for {slug!r}: {collision}")

    if collision == "identical":
        match = next(m for m in existing if hash_body(m["body"]) == hash_body(body))
        return StepResult(
            data={"collision": collision, "existing_slug": match["slug"]},
            state_updates={"module_collision": collision},
            skip_remaining=True,
            progress_message=(
                f"This content is already stored as **{match['slug']}**. "
                "Nothing to add — cancelling."
            ),
        )

    if collision in ("slug_taken", "title_taken"):
        suggested = unique_slug(slug, {m["slug"] for m in existing})
        conflicting_name = slug if collision == "slug_taken" else title
        return StepResult(
            data={"collision": collision, "suggested_slug": suggested},
            state_updates={
                "module_collision": collision,
                "suggested_slug": suggested,
                "awaiting_collision_decision": True,
            },
            needs_user_input=True,
            user_prompt=(
                f'A module named "{conflicting_name}" already exists with different content.\n\n'
                f"1. Replace the existing module\n"
                f"2. Keep both (save this as {suggested})\n"
                f"3. Cancel\n\n"
                f"Reply 1, 2, or 3."
            ),
            inline_options=_collision_choice_options(suggested),
        )

    return StepResult(data={"collision": "none"}, state_updates={"module_collision": "none"})


def _handle_collision_decision(context: StepContext) -> StepResult:
    """Process the user's reply to the Replace/Keep both/Cancel prompt."""
    response = context.user_input.strip().lower()
    suggested = context.get_state("suggested_slug") or ""

    if response in CANCEL_WORDS:
        LOGGER.info("User cancelled after a module collision")
        return StepResult(
            state_updates={"awaiting_collision_decision": False},
            skip_remaining=True,
            progress_message="Cancelled — nothing was saved.",
        )

    if response in REPLACE_WORDS:
        LOGGER.info("User chose to replace the existing module")
        return StepResult(
            data={"collision_action": "replace"},
            state_updates={
                "awaiting_collision_decision": False,
                "module_replace": True,
            },
            progress_message="Will replace the existing module.",
        )

    if response in KEEP_BOTH_WORDS:
        LOGGER.info(f"User chose to keep both, saving as {suggested!r}")
        return StepResult(
            data={"collision_action": "keep_both"},
            state_updates={
                "awaiting_collision_decision": False,
                "module_slug": suggested,
                "module_replace": False,
            },
            progress_message=f"Will save as a new module: {suggested}",
        )

    return StepResult(
        needs_user_input=True,
        user_prompt="Please choose: Replace, Keep both, or Cancel. Reply 1, 2, or 3.",
        inline_options=_collision_choice_options(suggested),
    )
