"""Distil recent chat history per grid / organization into episodic memory.

Run nightly. Reads chat_messages, writes episodic_distillations. Read at
render time by EpisodicProvider -- nothing here is on a request's critical
path.

Reuses orchestrator.experts.entity_fanout for "every eligible grid /
organization" rather than adding a fifth enumeration, the same decision
0013_skill_scheduling.sql made.

The distillation prompt itself lives in
shared/prompts/library/episodic.distill.prompt, not as a string in this
file -- like every other in-code LLM prompt in this codebase, it is editable
through the DB/Google-Doc override path (see shared/prompts/core.py)
without a code change.

Usage:
    python scripts/distill_episodic_memory.py --anchor-type grid
    python scripts/distill_episodic_memory.py --anchor-type grid --apply
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--anchor-type", choices=["grid", "organization"], required=True)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    import asyncio

    from supabase import create_client

    from shared.config.db_credentials import chat_db_service_key, chat_db_url

    url, key = chat_db_url(), chat_db_service_key()
    if not (url and key):
        print("CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set", file=sys.stderr)
        return 1
    client = create_client(url, key)

    from orchestrator.experts.entity_fanout import get_eligible_entities

    candidates = asyncio.run(get_eligible_entities(args.anchor_type))
    names = [e["name"] for e in candidates if e.get("name")]

    existing = (
        client.table("episodic_distillations")
        .select("anchor_id, edited_by")
        .eq("anchor_type", args.anchor_type)
        .execute()
        .data
        or []
    )
    targets = anchors_to_refresh(names, existing)

    if not targets:
        print("Nothing to refresh.")
        return 0

    print(f"{len(targets)} anchor(s) to refresh: {', '.join(targets)}")
    if not args.apply:
        print("\nDry run. Re-run with --apply to generate and write.")
        return 0

    from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
    from shared.llm.model_tiers import resolve_model
    from shared.prompts import PROMPTS

    gateway = get_default_generation_gateway()
    model = resolve_model(PROMPTS.spec("episodic.distill").model)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()

    for name in targets:
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
            print(f"  {name}: no messages, skipped")
            continue

        prompt = build_distillation_prompt(name, messages)
        response = asyncio.run(
            gateway.generate(
                [LLMMessage(role="user", text=prompt)],
                GenerationOptions(model=model, temperature=0.3),
            )
        )
        summary = (getattr(response, "text", "") or "").strip()
        if not summary:
            print(f"  {name}: model returned nothing, skipped")
            continue

        client.table("episodic_distillations").upsert(
            {
                "anchor_type": args.anchor_type,
                "anchor_id": name,
                "anchor_name": name,
                "summary": summary,
                "message_count": len(messages),
            },
            on_conflict="anchor_type,anchor_id",
        ).execute()
        print(f"  {name}: {len(messages)} messages -> {len(summary)} chars")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
