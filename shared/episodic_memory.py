"""Distil recent chat history per grid / organization into episodic memory.

Writes `episodic_distillations`, which EpisodicProvider
(shared/prompts/providers_episodic.py) reads at render time. Nothing here is
on a request's critical path -- distilling during a request would put an LLM
call in front of every message.

The logic lives in `shared` rather than in scripts/distill_episodic_memory.py
because **no deployed image contains repo-root scripts/**: chat_orchestrator's
Dockerfile copies chat_orchestrator/, shared/, rag_pipeline/ and mcp_servers/;
anansi_app's copies anansi_app/ and shared/. That is the reason
episodic_distillations was still empty long after migration 0019 created it --
the script's own "Run nightly" docstring described an intention nothing could
have carried out, since nothing scheduled it and the file was not there to run.
It is now driven by anansi_app/scripts/episodic_scheduler.py, which is where
this deployment's other scheduled batch work already lives, and
scripts/distill_episodic_memory.py stays as a hand-run CLI over the same code.

The distillation prompt itself lives in
shared/prompts/library/episodic.distill.prompt, not as a string in this file --
like every other in-code LLM prompt here, it is editable through the
DB/Google-Doc override path (see shared/prompts/core.py) without a code change.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

LOOKBACK_DAYS = 30
MAX_MESSAGES = 300
TARGET_WORDS = 200


def anchors_to_refresh(candidates: List[str], existing: List[Dict[str, Any]]) -> List[str]:
    """Which anchors the batch should regenerate.

    A row with edited_by set was corrected by a human and is never
    overwritten -- an operator's correction outranks the batch.
    """
    protected = {row["anchor_id"] for row in existing if row.get("edited_by")}
    return [a for a in candidates if a not in protected]


def build_distillation_prompt(anchor_name: str, messages: List[str]) -> str:
    """The prompt that turns raw messages into durable lessons.

    Routed through PROMPTS (episodic.distill), not an f-string, so this
    prompt is editable via the DB/Google-Doc override path like every other
    in-code LLM prompt -- see shared/prompts/library/episodic.distill.prompt.
    """
    from shared.prompts import PROMPTS

    messages_text = "\n".join(f"- {m}" for m in messages)
    return PROMPTS.text(
        "episodic.distill",
        anchor_name=anchor_name,
        messages_text=messages_text,
        target_words=TARGET_WORDS,
    )


def build_client() -> Any:
    """A chat_db client, or None when credentials are absent."""
    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        return None
    from supabase import create_client

    return create_client(url, key)


async def eligible_anchor_names(anchor_type: str) -> List[str]:
    """Named, eligible anchors of this type.

    An empty list is ambiguous by design -- see shared/entity_eligibility.py.
    Callers must treat it as "the Auth DB may be down", not "zero anchors".
    """
    from shared.entity_eligibility import get_eligible_entities

    entities = await get_eligible_entities(anchor_type)
    return [e["name"] for e in entities if e.get("name")]


def select_targets(client: Any, anchor_type: str, names: List[str]) -> List[str]:
    """Names to regenerate, minus anything a human has corrected."""
    existing = (
        client.table("episodic_distillations")
        .select("anchor_id, edited_by")
        .eq("anchor_type", anchor_type)
        .execute()
        .data
        or []
    )
    return anchors_to_refresh(names, existing)


async def distill_anchor(
    client: Any,
    anchor_type: str,
    name: str,
    gateway: Any,
    model: str,
) -> Optional[int]:
    """Distil one anchor and upsert it. Returns the summary length, or None.

    None covers "no messages mentioning this anchor" and "the model returned
    nothing" -- both are ordinary outcomes for a quiet grid, not failures, so
    neither writes a row. Overwriting a good older summary with an empty one
    would lose history the next run could not recover.
    """
    from shared.llm import GenerationOptions, LLMMessage

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    rows = (
        client.table("chat_messages")
        .select("content")
        .ilike("content", f"%{name}%")
        .gte("created_at", cutoff)
        .order("created_at", desc=True)
        .limit(MAX_MESSAGES)
        .execute()
        .data
        or []
    )
    messages = [r["content"] for r in rows if r.get("content")]
    if not messages:
        return None

    response = await gateway.generate(
        [LLMMessage(role="user", text=build_distillation_prompt(name, messages))],
        GenerationOptions(model=model, temperature=0.3),
    )
    summary = (getattr(response, "text", "") or "").strip()
    if not summary:
        return None

    client.table("episodic_distillations").upsert(
        {
            "anchor_type": anchor_type,
            "anchor_id": name,
            "anchor_name": name,
            "summary": summary,
            "message_count": len(messages),
        },
        on_conflict="anchor_type,anchor_id",
    ).execute()
    return len(summary)


async def distill_anchor_type(
    anchor_type: str,
    apply: bool = False,
    client: Any = None,
    on_progress: Any = None,
) -> Dict[str, Any]:
    """Distil every eligible anchor of one type.

    Returns a result dict rather than printing, so the CLI and the scheduler
    can report the same run in their own voices. ``enumerated`` is separate
    from ``targets`` on purpose: zero enumerated anchors may mean the Auth DB
    is down, and the scheduler uses that to retry rather than to call the
    night's work done (see shared/entity_eligibility.py).
    """
    client = client if client is not None else build_client()
    if client is None:
        return {"error": "CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set"}

    names = await eligible_anchor_names(anchor_type)
    result: Dict[str, Any] = {
        "anchor_type": anchor_type,
        "enumerated": len(names),
        "targets": [],
        "written": 0,
        "skipped": [],
        "error": None,
    }
    if not names:
        return result

    result["targets"] = select_targets(client, anchor_type, names)
    if not result["targets"] or not apply:
        return result

    from shared.llm import get_default_generation_gateway
    from shared.llm.model_tiers import resolve_model
    from shared.prompts import PROMPTS

    gateway = get_default_generation_gateway()
    model = resolve_model(PROMPTS.spec("episodic.distill").model)

    for name in result["targets"]:
        try:
            written = await distill_anchor(client, anchor_type, name, gateway, model)
        except Exception:
            # One bad anchor must not cost the rest of the run -- a single
            # grid with an oversized history or a transient LLM error would
            # otherwise leave every later anchor un-distilled until tomorrow.
            LOGGER.opt(exception=True).warning(f"Distillation failed for '{name}'")
            result["skipped"].append(name)
            continue
        if written is None:
            result["skipped"].append(name)
        else:
            result["written"] += 1
        if on_progress:
            on_progress(name, written)

    return result


__all__ = [
    "LOOKBACK_DAYS",
    "MAX_MESSAGES",
    "TARGET_WORDS",
    "anchors_to_refresh",
    "build_client",
    "build_distillation_prompt",
    "distill_anchor",
    "distill_anchor_type",
    "eligible_anchor_names",
    "select_targets",
]
