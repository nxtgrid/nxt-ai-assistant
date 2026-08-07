"""Dispatch a scheduled skill run across every eligible entity.

Phase 5 of docs/superpowers/plans/2026-08-06-user-designed-skills.md.

The scheduler (anansi_app/scripts/broadcast_scheduler.py) recognizes a due
user_schedules row with skill_id set and calls this module's entry point
(via POST /skills/dispatch-schedule, orchestrator/api/app.py) instead of
its own single-chat command dispatch -- "the scheduler starts the run and
hands off... it does not execute steps" (the plan's Phase 5, item 1). This
module IS the hand-off target: it does everything the scheduler itself
cannot. Entity fan-out and per-run authorization both need direct Auth DB
access (AUTH_DB_HOST/USER/PASSWORD), which only chat_orchestrator has
configured -- anansi_app's own .env.example has none of those, so
broadcast_scheduler.py could not do this work even if it tried.

For each eligible entity this calls the conversation graph directly
in-process (build_full_conversation_graph + invoke_full_graph), not a
second HTTP hop back into chat_orchestrator's own /chat endpoint -- this
module already runs inside chat_orchestrator, and calling the graph
directly is the only way to read expert_error/expert_executed off the
final state; the public process_webhook_with_graph wrapper narrows its
return to a (text, tool_results, reply_markup, tokens) tuple that doesn't
expose either.

Ownership split with skill_runner.py: skill_runner.py executes the skill
and delivers SUCCESS messages progressively as flagged steps complete (see
its _ResponseBuffer) -- it has no notion of run history or staff-vs-
customer failure routing. This module owns everything that happens once
the graph call returns: logging the outcome to user_schedule_logs (Phase 5,
item 3) and routing a FAILURE's notification to the target chat (staff-
facing groups only) or the escalation channel (everyone else, Phase 5,
item 4) -- reusing the same org resolution already done for authorization,
rather than a second lookup.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from orchestrator.experts import entity_fanout
from orchestrator.models.schemas import UserContext
from orchestrator.services.supabase_client import get_supabase_client
from orchestrator.utils.session_id import generate_session_id
from shared.utils.logging import get_logger
from shared.utils.telegram_send import send_telegram_message

LOGGER = get_logger(__name__)

# Minimum time between two runs of the same (skill, grid) pair fired by the
# alert trigger (Phase 5, item 6) -- NOT applied to the cron-scheduled path,
# which is naturally rate-limited by its own cron_expression. Read by
# app.py's handle_notify; defined here since it's a skill-dispatch concern.
ALERT_TRIGGER_MIN_INTERVAL_SECONDS = 5 * 60


async def dispatch_skill_schedule(schedule_id: str) -> Dict[str, Any]:
    """Entry point: run a due user_schedules row's skill across every
    eligible entity of its anchor_entity_type.

    Returns a summary dict ({"dispatched": N, "skipped": N, "failed": N,
    "reason": Optional[str]}) for the caller (the /skills/dispatch-schedule
    endpoint, ultimately the scheduler) to log -- never raises; every
    failure mode here is either a per-entity skip/failure (recorded in
    user_schedule_logs) or a whole-tick abort (recorded via the returned
    "reason"), not an exception the caller needs to handle specially.
    """
    supabase = get_supabase_client()

    schedule = await supabase.get_user_schedule(schedule_id)
    if not schedule or not schedule.get("skill_id"):
        LOGGER.error(f"dispatch_skill_schedule: schedule {schedule_id} not found or not a skill")
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": "schedule not found"}

    skill_id = schedule["skill_id"]
    anchor_entity_type = schedule["anchor_entity_type"]

    skill = await supabase.get_skill(skill_id)
    if not skill:
        LOGGER.error(f"dispatch_skill_schedule: skill {skill_id} not found")
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": "skill not found"}

    if skill.get("status") != "active":
        LOGGER.warning(f"dispatch_skill_schedule: skill {skill_id} is {skill.get('status')}")
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": f"skill is {skill.get('status')}"}

    # Creator liveness (Phase 5, item 2, first bullet): a skill whose
    # creator's account is gone stops running everywhere, not just this
    # tick. Checked once per skill, not per target entity -- it's a
    # property of the skill, not of any one chat.
    creator_email = skill.get("created_by")
    from shared.auth.auth_service import get_auth_service

    auth_service = get_auth_service()
    creator_live = await auth_service.is_account_email_live(creator_email) if creator_email else False

    if creator_live is False:
        await supabase.set_skill_status(
            skill_id, "unusable", f"creator account {creator_email!r} deleted or missing"
        )
        return {
            "dispatched": 0,
            "skipped": 0,
            "failed": 0,
            "reason": f"creator {creator_email} is not live -- skill marked unusable",
        }
    if creator_live is None:
        # Could not determine (Auth DB unreachable) -- skip this tick only,
        # do NOT mark the skill unusable on a transient failure.
        LOGGER.warning(f"dispatch_skill_schedule: could not verify creator liveness for {skill_id}")
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": "could not verify creator liveness"}

    creator_permissions = await auth_service.get_user_permissions(email=creator_email)

    entities = await entity_fanout.get_eligible_entities(anchor_entity_type)
    if not entities:
        # Safety property from agent_worker.py's _reconcile_expert: 0 rows
        # means "the Auth DB may be down", not "there are zero entities" --
        # skip the tick rather than acting on an empty set.
        LOGGER.warning(
            f"dispatch_skill_schedule: 0 eligible {anchor_entity_type}s for skill {skill_id}, skipping tick"
        )
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": f"0 eligible {anchor_entity_type}s"}

    dispatched = 0
    skipped = 0
    failed = 0

    for entity in entities:
        anchor_metadata = entity_fanout.build_anchor_metadata(anchor_entity_type, entity)
        outcome = await _dispatch_to_one_entity(
            schedule=schedule,
            skill=skill,
            creator_permissions=creator_permissions,
            anchor_metadata=anchor_metadata,
        )
        if outcome == "dispatched":
            dispatched += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1

    return {"dispatched": dispatched, "skipped": skipped, "failed": failed, "reason": None}


async def dispatch_skill_alert_trigger(grid_name: str, chat_id: str, topic_id: Optional[str]) -> Dict[str, Any]:
    """Wake every skill whose trigger is a notify alert and whose anchor
    matches this resolved grid (Phase 5, item 6).

    Called from app.py's handle_notify AFTER grid resolution and AFTER the
    alert-correlation decision -- firing before the correlation decision
    would re-run skills on duplicate re-fires of the same alert, precisely
    the noise this trigger exists to avoid.

    Unlike dispatch_skill_schedule (which fans one schedule out across
    every eligible entity), this targets exactly the one grid the alert
    concerns -- entity_fanout's eligibility query has no part in this path;
    the "entity" is already resolved by the caller. Only anchor_entity_type
    "grid" schedules are ever considered, matching what an alert can even
    be about.

    Rate-limited per (schedule, grid): one run per
    ALERT_TRIGGER_MIN_INTERVAL_SECONDS, mirroring the old user-agent path's
    own rate limit for this trigger.
    """
    supabase = get_supabase_client()
    schedules = await supabase.get_notify_trigger_schedules(anchor_entity_type="grid")
    if not schedules:
        return {"dispatched": 0, "skipped": 0, "failed": 0, "reason": None}

    anchor_metadata = {
        "grid_name": grid_name,
        "telegram_chat_id": chat_id,
        "telegram_topic_id": topic_id,
        "organization_id": None,
        "organization_name": "",
    }

    dispatched = 0
    skipped = 0
    failed = 0

    for schedule in schedules:
        if await _rate_limited(schedule["id"], grid_name):
            skipped += 1
            continue

        skill = await supabase.get_skill(schedule["skill_id"])
        if not skill or skill.get("status") != "active":
            skipped += 1
            continue

        # Deferred until here: constructing AuthService opens a real DB
        # connection (AUTH_DB_HOST/USER/PASSWORD), which a rate-limited or
        # already-inactive skill has no reason to pay for.
        from shared.auth.auth_service import get_auth_service

        auth_service = get_auth_service()
        creator_email = skill.get("created_by")
        creator_live = (
            await auth_service.is_account_email_live(creator_email) if creator_email else False
        )
        if creator_live is False:
            await supabase.set_skill_status(
                schedule["skill_id"],
                "unusable",
                f"creator account {creator_email!r} deleted or missing",
            )
            skipped += 1
            continue
        if creator_live is None:
            skipped += 1
            continue

        creator_permissions = await auth_service.get_user_permissions(email=creator_email)
        outcome = await _dispatch_to_one_entity(
            schedule=schedule,
            skill=skill,
            creator_permissions=creator_permissions,
            anchor_metadata=anchor_metadata,
        )
        if outcome == "dispatched":
            dispatched += 1
        elif outcome == "skipped":
            skipped += 1
        else:
            failed += 1

    return {"dispatched": dispatched, "skipped": skipped, "failed": failed, "reason": None}


async def _rate_limited(schedule_id: str, anchor_entity_id: str) -> bool:
    """True if this (schedule, entity) pair ran within the last
    ALERT_TRIGGER_MIN_INTERVAL_SECONDS. A malformed/unparseable last-run
    timestamp fails open (treated as "not rate limited") rather than
    silently blocking every future alert for this pair forever.
    """
    from datetime import datetime as _dt

    supabase = get_supabase_client()
    last_run_at = await supabase.get_last_skill_schedule_run_at(schedule_id, anchor_entity_id)
    if not last_run_at:
        return False
    try:
        last_run = _dt.fromisoformat(last_run_at.replace("Z", "+00:00"))
    except ValueError:
        return False
    from datetime import timezone as _tz

    elapsed = (_dt.now(_tz.utc) - last_run).total_seconds()
    return elapsed < ALERT_TRIGGER_MIN_INTERVAL_SECONDS


async def _dispatch_to_one_entity(
    *,
    schedule: Dict[str, Any],
    skill: Dict[str, Any],
    creator_permissions: Any,
    anchor_metadata: Dict[str, Any],
) -> str:
    """Authorize and run the skill for one fan-out target. Returns
    "dispatched" | "skipped" | "failed" -- always logs a user_schedule_logs
    row itself before returning, so the caller never needs to.
    """
    from shared.auth.auth_service import get_auth_service

    supabase = get_supabase_client()
    skill_id = schedule["skill_id"]
    # "grid_name" vs "organization_name" is what actually distinguishes the
    # two anchor_metadata shapes entity_fanout.build_anchor_metadata
    # produces (see its docstring) -- both carry organization_id (a grid's
    # *owning* org, not the grid itself), so that key alone can't tell them
    # apart and picking it first would mislabel every grid run with its
    # org's id instead of the grid's own name.
    if "grid_name" in anchor_metadata:
        entity_id = anchor_metadata.get("grid_name") or ""
        entity_name = anchor_metadata.get("grid_name") or ""
    else:
        entity_id = str(anchor_metadata.get("organization_id") or "")
        entity_name = anchor_metadata.get("organization_name") or ""

    chat_id = anchor_metadata.get("telegram_chat_id")
    topic_id = anchor_metadata.get("telegram_topic_id")
    if not chat_id:
        await supabase.log_skill_schedule_run(
            schedule["id"],
            "skipped",
            anchor_entity_id=entity_id,
            anchor_entity_name=entity_name,
            error_message="no Telegram chat configured for this entity",
        )
        return "skipped"

    auth_service = get_auth_service()
    chat_org_id = await auth_service.get_organization_from_chat(chat_id, topic_id)

    # Per-run authorization (Phase 5, item 2): proceed only if the creator's
    # org matches this chat's org, or the creator is staff. The chat is
    # still authoritative for what PERMISSIONS the run gets (resolve_auth.py
    # re-resolves those fresh, per-chat) -- this check only gates WHETHER a
    # given chat is in scope for this creator's skill at all.
    creator_org_ids = set(getattr(creator_permissions, "organization_ids", []) or [])
    creator_is_staff = bool(getattr(creator_permissions, "is_staff", False))
    if not creator_is_staff and (not chat_org_id or chat_org_id not in creator_org_ids):
        await supabase.log_skill_schedule_run(
            schedule["id"],
            "skipped",
            anchor_entity_id=entity_id,
            anchor_entity_name=entity_name,
            error_message="creator's organization does not match this chat",
        )
        return "skipped"

    from shared.auth.auth_service import STAFF_ORG_ID

    is_staff_facing_chat = chat_org_id == str(STAFF_ORG_ID)

    user_context = UserContext(
        user_id=f"skill_schedule:{skill_id}",
        user_email=schedule.get("created_by_email") or skill.get("created_by") or "",
        source="telegram",
        chat_id=chat_id,
        topic_id=str(topic_id) if topic_id is not None else None,
        is_group=True,
    )
    session_id = generate_session_id(source="telegram", chat_id=chat_id, topic_id=user_context.topic_id)
    metadata = {
        "scheduled_execution": True,
        "skill_id": skill_id,
        "skill_inputs": schedule.get("skill_inputs") or {},
    }

    from orchestrator.graphs.full_conversation_graph import (
        build_full_conversation_graph,
        invoke_full_graph,
    )

    try:
        graph = build_full_conversation_graph()
        final_state = await invoke_full_graph(
            graph=graph,
            user_input=f"[Scheduled skill run: {skill.get('title')}]",
            user_context=user_context,
            session_id=session_id,
            metadata=metadata,
        )
    except Exception as e:
        LOGGER.exception(f"dispatch_skill_schedule: graph invocation failed for {skill_id}: {e}")
        await _deliver_failure(
            is_staff_facing_chat, chat_id, user_context.topic_id, skill.get("title"), str(e)
        )
        await supabase.log_skill_schedule_run(
            schedule["id"],
            "failed",
            anchor_entity_id=entity_id,
            anchor_entity_name=entity_name,
            error_message=str(e),
        )
        return "failed"

    expert_error = final_state.get("expert_error")
    if expert_error:
        await _deliver_failure(
            is_staff_facing_chat, chat_id, user_context.topic_id, skill.get("title"), str(expert_error)
        )
        await supabase.log_skill_schedule_run(
            schedule["id"],
            "failed",
            anchor_entity_id=entity_id,
            anchor_entity_name=entity_name,
            error_message=str(expert_error),
        )
        return "failed"

    await supabase.log_skill_schedule_run(
        schedule["id"],
        "success",
        anchor_entity_id=entity_id,
        anchor_entity_name=entity_name,
        result_message=(final_state.get("final_response") or "")[:4000],
    )
    return "dispatched"


async def _deliver_failure(
    is_staff_facing_chat: bool,
    chat_id: str,
    topic_id: Optional[str],
    skill_title: Optional[str],
    error_text: str,
) -> None:
    """Failure delivery routing (Phase 5, item 4): a staff-facing chat sees
    its own failures directly; every other chat's failure goes to the
    escalation channel instead, never to the chat itself.
    """
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return

    text = f"⚠️ Skill '{skill_title}' failed: {error_text}"
    if is_staff_facing_chat:
        await send_telegram_message(bot_token, chat_id, text, topic_id=topic_id)
        return

    escalation_chat_id = os.getenv("ESCALATION_TELEGRAM_CHAT_ID", "")
    if not escalation_chat_id:
        LOGGER.warning("dispatch_skill_schedule: no ESCALATION_TELEGRAM_CHAT_ID configured")
        return
    await send_telegram_message(
        bot_token,
        escalation_chat_id,
        f"{text}\n(target chat: {chat_id}, not staff-facing -- not sent there)",
    )


__all__ = ["dispatch_skill_schedule", "ALERT_TRIGGER_MIN_INTERVAL_SECONDS"]
