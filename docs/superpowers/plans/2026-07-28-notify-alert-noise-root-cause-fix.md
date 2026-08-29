# /notify Alert Noise Root-Cause Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `/notify` alert correlation from posting Telegram updates that announce no change ("Added another component (0 affected components)", "Added MPPT J47M (16 affected components)" three rounds running), and stop identical alerts from filing separate tickets.

**Architecture:** Telegram delivery becomes a function of *state change* (a component actually joined the ticket, or the ticket escalated) instead of the correlator's decision label. Underneath that, the deterministic correlation rungs are repaired: component extraction is fixed to match this fleet's real device naming, an exact-signature rung is added for alerts with no identifiable component, and every fail-open ticket-creation path records a correlation row so the ticket it files can be correlated against later.

**Tech Stack:** Python 3.11, FastAPI, asyncio, Supabase/postgrest, Jira Cloud REST API v3, pytest.

---

## Background: what was diagnosed

Verified against the code on `main` (the build that produced the 2026-07-28 GridV/GridW Telegram screenshots):

1. **Component extraction fails on real alert text.** `_MPPT_PATTERN` in
   `alert_facts.py:44` is `mppt\s+([A-Za-z0-9]+)\s*\[(.*?)\]` — it requires the
   bracket immediately after the id. The fleet names devices
   `MPPT <ID> <MODEL> [<n>]`, e.g.
   `'Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]'`, so the regex does not
   match and every such alert reaches the correlator with
   `component_kind=""`, `component_key=""`. The existing tests pass only
   because they use synthetic text (`"mppt A3 [Kudi] performance dropped"`).
2. **The deterministic duplicate rung can therefore never fire.**
   `_find_signature_duplicate` (`correlator.py:240`) returns `None` immediately
   when `component_kind` is blank, so every re-fire goes to the LLM, which
   answers "amend".
3. **Nothing checks whether an amend changed anything before notifying.**
   `merge_affected_key` (`correlation_store.py:265`) knows whether it appended a
   new entry or merely bumped `count` on an existing one, but its return value is
   discarded at `correlation_render.py:225`; `AmendmentResult` has no "did
   anything change" field; `_amend_delivery` (`app.py:1395`) posts
   unconditionally. This is the direct cause of the repeated
   "Added MPPT <key> (16 affected components)" posts.
4. **"(0 affected components)" is two empty values rendered without a guard.**
   The label falls back to `"another component"` when the decision carries no
   `affected_key`, and the count is 0 because the ticket was filed from a
   keyless alert (`_record_new_correlation` seeds `affected_keys=[]`,
   `app.py:1202`). A `decided_by="replay"` decision also hardcodes
   `affected_key=None` (`correlator.py:415`).
5. **The documented "rung 2" exact-signature match was never wired in.**
   `CorrelationStore.get_by_signature` (`correlation_store.py:190`) has no
   production caller. So two byte-identical keyless alerts (the GridV
   combiner alerts that became OPS-3363 and OPS-3365) have zero deterministic
   grouping and depend entirely on an LLM judgment that falls back to "new" on
   any hiccup.
6. **Five of seven `_create_notify_ticket` call sites never record a correlation
   row.** Only `app.py:1536` and `app.py:1577` follow up with
   `_record_new_correlation`. The lock-timeout (`app.py:1464`), `decision is
   None` (`app.py:1496`) and execution-exception (`app.py:1649`) paths file
   tickets that the correlation store can never see again — each one is a
   guaranteed future duplicate.
7. **The grid lock shares the LLM's 12s budget** while candidate assembly does
   up to 15 *sequential* Jira `get_status` calls inside it, so concurrent alerts
   on a busy grid fall out to "file a new ticket".

Findings 1–6 are proven from code. Finding 7 is a mechanism, not yet a
confirmed cause — Task 0 settles which of 5/6/7 produced OPS-3365.

---

## Global Constraints

- Every alert must still result in either a new ticket or an update to an
  existing one. Nothing in this plan may introduce a path where an alert is
  dropped.
- Suppressing a *Telegram post* is not suppressing an alert: the ticket comment,
  the occurrence bump and the correlation row are still written. The ticket is
  the record; Telegram is the alarm.
- Any urgent severity increase must still reach Telegram as a top-level post,
  under every code path this plan touches.
- No database migration. No new LLM call. No rate limiter or digest job.
- `AmendmentResult`'s new field must have a default so existing test
  constructors keep working.
- Per `CLAUDE.md`: run `pre-commit run --all-files` before claiming anything is
  committed, and `git add -f` any new file under a `tests/` directory.

---

## File structure

- `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py` — `merge_affected_key` reports whether the key was new; case-insensitive key matching; delete the dead `get_by_signature`.
- `chat_orchestrator/orchestrator/services/ticketing/correlation_render.py` — carry "component added" through `AmendmentResult`.
- `chat_orchestrator/orchestrator/api/app.py` — deliver on state change; short-circuit replays; record a correlation row on every fallback ticket creation.
- `chat_orchestrator/orchestrator/services/ticketing/alert_facts.py` — component extraction that matches real fleet device names.
- `chat_orchestrator/orchestrator/services/ticketing/correlator.py` — exact-signature rung for keyless alerts; parallel candidate status confirmation.
- `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py` — separate grid-lock budget from the LLM budget.
- Tests: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`, `test_correlation_render.py`, `test_alert_facts.py`, `test_correlator.py`, `chat_orchestrator/tests/api/test_notify_ticketing.py`.

---

### Task 0: Read the audit trail before changing behaviour

The system already records every decision in `ticket_correlation_events`
(`ticket_ref`, `decided_by`, `reason`, `candidate_refs`, `llm_raw`). This task
is read-only and decides whether Task 2 and Task 7 are needed at all.

**Files:** none (Supabase SQL editor against the chat DB).

- [ ] **Step 1: Find out how OPS-3365 was decided**

```sql
select created_at, ticket_ref, decision, decided_by, confidence,
       left(reason, 200) as reason, candidate_refs
from ticket_correlation_events
where grid_name = 'GridV'
  and created_at >= '2026-07-28T10:00:00Z'
order by created_at;
```

Read the row written at ~16:45 local (the second combiner alert):

- `decided_by = 'no_candidates'` → OPS-3363 had no correlation row or was not
  returned as a candidate. Tasks 5 and 6 are the fix.
- `decided_by = 'fallback'` with a reason containing `LLM call failed` or the
  handler logged `grid-correlation lock timeout` → Task 7 matters. Note that a
  lock timeout does **not** write an event row, so an *absent* row at 16:45
  with a ticket existing is itself the signature of a lock timeout.
- `decided_by = 'llm'` with `decision = 'new'` → the model itself said new;
  Task 5 (deterministic signature rung) is the fix and the prompt needs no
  change.
- `decided_by = 'fallback'` with a reason containing `confidence` → the model
  correlated but below the 0.75 floor; Task 5 still fixes it deterministically.

- [ ] **Step 2: Find out whether the GridW re-announcements were LLM or replay**

```sql
select created_at, ticket_ref, decision, decided_by,
       alert->>'component_kind' as kind,
       alert->>'component_key' as key,
       alert->>'signature' as signature
from ticket_correlation_events
where grid_name = 'GridW'
  and created_at >= '2026-07-28T18:30:00Z'
order by created_at;
```

Expected if the diagnosis is right: `decided_by = 'llm'`, `decision = 'amend'`,
and `kind`/`key` **empty** on every row while the ticket's `affected_keys` holds
16 populated entries — that is finding 1 and finding 2 in one screenshot.

- [ ] **Step 3: Decide whether Task 2 applies**

```sql
select count(*) as replay_rows
from ticket_correlation_events
where decided_by = 'replay' and created_at >= now() - interval '7 days';
```

If this is `0`, n8n is not sending `dedup_key`; **skip Task 2** and record in the
PR description that the replay bug is latent rather than active. If it is
non-zero, Task 2 is live noise and must be done.

- [ ] **Step 4: Confirm the affected-keys state of the two noisy tickets**

```sql
select ticket_ref, status, severity, occurrence_count,
       jsonb_array_length(affected_keys) as affected_count,
       jsonb_array_length(signatures) as signature_count,
       left(summary_current, 120) as summary
from ticket_correlations
where ticket_ref in ('OPS-3352', 'OPS-3353', 'OPS-3363', 'OPS-3365');
```

Expected: OPS-3353 has `affected_count = 0` (explains "(0 affected
components)"); OPS-3352 has `affected_count = 16`; **OPS-3365 missing from the
result entirely** would confirm finding 6 (a fallback path filed it with no
correlation row).

- [ ] **Step 5: Record the findings in the plan file**

Append a short "Task 0 findings" section to this document with the four
answers, so Tasks 2 and 7 can be included or dropped with a written reason.

---

### Task 1: Only notify when a component was actually added

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py:265-331`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_render.py:154-168, 198-385`
- Modify: `chat_orchestrator/orchestrator/api/app.py:1389-1402`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlation_render.py`
- Test: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Interfaces:**
- Produces: `same_component(entry, kind, key) -> bool` in `alert_facts.py` (pure, shared by the store and the correlator).
- Produces: `CorrelationStore.merge_affected_key(...) -> Optional[AffectedKeyMerge]` where `AffectedKeyMerge(affected_keys: List[Dict[str, Any]], added: bool)`.
- Produces: `AmendmentResult.component_added: bool = False`.
- Consumes: `_amend_delivery` suppresses unless `component_added or escalated`.

- [ ] **Step 1: Write the failing store test**

Add to `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`,
using that module's existing `_make_store()` helper and `FakeRawClient.tables`:

```python
class TestMergeAffectedKeyReportsNovelty:
    @pytest.mark.asyncio
    async def test_new_key_reports_added_true(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {"ticket_ref": "TKT-1", "affected_keys": [], "signatures": []}
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A7", label="MPPT A7", signature="sig-a"
        )

        assert merge is not None
        assert merge.added is True
        assert [e["key"] for e in merge.affected_keys] == ["A7"]

    @pytest.mark.asyncio
    async def test_existing_key_reports_added_false_and_bumps_count(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {"kind": "mppt", "key": "A7", "label": "MPPT A7", "count": 1}
                ],
                "signatures": ["sig-a"],
            }
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A7", label="MPPT A7", signature="sig-a"
        )

        assert merge is not None
        assert merge.added is False
        assert merge.affected_keys[0]["count"] == 2

    @pytest.mark.asyncio
    async def test_case_differing_key_is_not_a_new_component(self):
        store, fake = _make_store()
        fake.tables["ticket_correlations"] = [
            {
                "ticket_ref": "TKT-1",
                "affected_keys": [
                    {"kind": "mppt", "key": "IYYY", "label": "MPPT IYYY", "count": 1}
                ],
                "signatures": [],
            }
        ]

        merge = await store.merge_affected_key(
            "TKT-1", kind="MPPT", key="iyyy", label="MPPT iyyy"
        )

        assert merge is not None
        assert merge.added is False
        assert len(merge.affected_keys) == 1
```

- [ ] **Step 1b: Update the four existing `TestMergeAffectedKey` tests to the new return type**

Those tests assert on the returned list directly (`len(updated) == 1`,
`updated[0]["count"] == 2`). Change each to read through the new field —
`updated.affected_keys` — leaving every other assertion identical. Example:

```python
        updated = await store.merge_affected_key(
            "TKT-1", kind="mppt", key="A3", label="MPPT A3", occurred_at="2026-01-02T00:00:00Z"
        )

        assert updated is not None
        assert len(updated.affected_keys) == 1
        assert updated.affected_keys[0]["count"] == 2
        assert updated.affected_keys[0]["first_seen"] == "2026-01-01T00:00:00Z"
        assert updated.affected_keys[0]["last_seen"] == "2026-01-02T00:00:00Z"
```

- [ ] **Step 2: Run the store tests and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlation_store.py -k MergeAffectedKeyReportsNovelty -q`

Expected: FAIL — `merge_affected_key` returns a list, which has no `.added`.

- [ ] **Step 3: Return a merge result and match keys case-insensitively**

In `alert_facts.py` (pure functions, no I/O — the natural home for component
identity, and importable by both the store and the correlator without a
private cross-module import), add:

```python
def same_component(entry: Dict[str, Any], kind: str, key: str) -> bool:
    """Component identity, compared case-insensitively.

    Derived keys come from a regex over alert text; merged keys can come from
    the correlation LLM. The two must compare equal or the same component is
    stored twice and every re-fire looks novel.
    """
    return (
        str(entry.get("kind") or "").strip().casefold() == (kind or "").strip().casefold()
        and str(entry.get("key") or "").strip().casefold() == (key or "").strip().casefold()
    )
```

In `correlation_store.py`, add above the class:

```python
from dataclasses import dataclass

from .alert_facts import same_component


@dataclass(frozen=True)
class AffectedKeyMerge:
    """Outcome of folding one component into a correlation's affected_keys.

    ``added`` is what the delivery layer needs: a merge that only bumped an
    existing entry's ``count`` changed nothing an operator has to be told
    about, so it must not produce a Telegram post.
    """

    affected_keys: List[Dict[str, Any]]
    added: bool
```

Then replace the body of `merge_affected_key` from the `affected_keys = ...`
line through `return affected_keys`:

```python
            affected_keys = list(row.get("affected_keys") or [])
            added = True
            for entry in affected_keys:
                if same_component(entry, kind, key):
                    entry["count"] = int(entry.get("count") or 0) + 1
                    entry["last_seen"] = occurred_at
                    added = False
                    break
            if added:
                affected_keys.append(
                    {
                        "kind": kind,
                        "key": key,
                        "label": label,
                        "first_seen": occurred_at,
                        "last_seen": occurred_at,
                        "count": 1,
                    }
                )

            update_payload: Dict[str, Any] = {"affected_keys": affected_keys}
            if signature:
                signatures = list(row.get("signatures") or [])
                if signature not in signatures:
                    signatures.append(signature)
                    update_payload["signatures"] = signatures

            client.table("ticket_correlations").update(update_payload).eq(
                "ticket_ref", ticket_ref
            ).execute()
            return AffectedKeyMerge(affected_keys=affected_keys, added=added)
```

Update the return annotation to `Optional[AffectedKeyMerge]` and the docstring's
"Returns the updated ``affected_keys`` list" sentence to describe the new type.

- [ ] **Step 4: Run the store tests and verify they pass**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlation_store.py -q`

Expected: PASS.

- [ ] **Step 5: Write the failing render test**

In `chat_orchestrator/tests/services/ticketing/test_correlation_render.py`, first
update `_FakeStore.merge_affected_key` to speak the new contract:

```python
    async def merge_affected_key(self, ticket_ref, *, kind, key, label, occurred_at=None, signature=None):
        from orchestrator.services.ticketing.correlation_store import AffectedKeyMerge

        self.merge_calls.append({"ticket_ref": ticket_ref, "kind": kind, "key": key, "label": label})
        if self.correlation is None:
            return None
        affected = list(self.correlation.get("affected_keys") or [])
        added = not any(e["kind"] == kind and e["key"] == key for e in affected)
        if added:
            affected.append({"kind": kind, "key": key, "label": label, "first_seen": "t", "last_seen": "t", "count": 1})
            self.correlation["affected_keys"] = affected
        return AffectedKeyMerge(affected_keys=affected, added=added)
```

Then add:

```python
class TestApplyAmendmentReportsNovelty:
    @pytest.mark.asyncio
    async def test_new_component_sets_component_added(self):
        store = _FakeStore(correlation=_correlation())
        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            alert=AlertFacts(subject="! Warning: MPPT A7 in Kudi !", severity="warning"),
            decision=_amend_decision(),
            raw_text="raw notify text",
        )

        assert result is not None
        assert result.component_added is True

    @pytest.mark.asyncio
    async def test_already_known_component_clears_component_added(self):
        store = _FakeStore(correlation=_correlation())
        decision = _amend_decision(
            affected_key={"kind": "mppt", "key": "A3", "label": "MPPT A3"}
        )

        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            alert=AlertFacts(subject="! Warning: MPPT A3 in Kudi !", severity="warning"),
            decision=decision,
            raw_text="raw notify text",
        )

        assert result is not None
        assert result.decision == "amend"
        assert result.component_added is False
        assert result.affected_keys_count == 1

    @pytest.mark.asyncio
    async def test_amend_without_affected_key_is_not_a_component_add(self):
        store = _FakeStore(correlation=_correlation(affected_keys=[]))

        result = await apply_amendment(
            store=store,
            ticket_service=_FakeTicketService(),
            ticket_ref="TKT-1",
            alert=AlertFacts(subject="! Urgent: Grid outage in Kudi !", severity="warning"),
            decision=_amend_decision(affected_key=None),
            raw_text="raw notify text",
        )

        assert result is not None
        assert result.component_added is False
        assert result.affected_keys_count == 0
        assert store.merge_calls == []
```

- [ ] **Step 6: Run the render tests and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlation_render.py -k ReportsNovelty -q`

Expected: FAIL — `AmendmentResult` has no attribute `component_added`.

- [ ] **Step 7: Carry the flag through `apply_amendment`**

In `correlation_render.py`, add the field to `AmendmentResult` (last, with a
default, so existing constructors keep working):

```python
@dataclass(frozen=True)
class AmendmentResult:
    """What happened when an "amend"/"duplicate" decision was executed --
    enough for the caller (the /notify handler) to decide what, if anything,
    to post to Telegram."""

    ticket_ref: str
    decision: str  # "amend" | "duplicate"
    escalated: bool
    affected_keys_count: int
    occurrence_count: int
    telegram_chat_id: Optional[str]
    telegram_topic_id: Optional[str]
    telegram_message_id: Optional[int]
    component_added: bool = False
```

Capture the merge outcome (replacing the bare `await store.merge_affected_key(...)`):

```python
    component_added = False
    affected_key = decision.affected_key or {}
    kind = str(affected_key.get("kind") or "").strip()
    key = str(affected_key.get("key") or "").strip()
    # A dict of empty strings is truthy, so the old `if decision.affected_key`
    # guard would merge a nameless ("", "") entry for any component-less alert.
    if decision.decision == "amend" and kind and key:
        label = affected_key.get("label") or f"{kind} {key}".strip()
        merge = await store.merge_affected_key(
            ticket_ref, kind=kind, key=key, label=label, signature=alert.signature or None
        )
        component_added = bool(merge is not None and merge.added)
```

Set it on the three `AmendmentResult(...)` returns that can represent a real
amend:

- the urgent Jira-only seed path (`correlation is None` branch): add
  `component_added=bool(seeded_affected_count)`.
- the final "amend" return: add `component_added=component_added`.
- leave both "duplicate" returns and the replay early-return at their
  `component_added` default of `False`.

- [ ] **Step 8: Run the render tests and verify they pass**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlation_render.py -q`

Expected: PASS.

- [ ] **Step 9: Write the failing delivery tests**

In `chat_orchestrator/tests/api/test_notify_ticketing.py`, next to the existing
`test_amend_delivery_*` tests:

```python
def test_amend_delivery_is_silent_when_no_component_was_added():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3352",
        confidence=0.9,
        decided_by="llm",
        reason="same root cause",
        affected_key={"kind": "mppt", "key": "J47M", "label": "MPPT J47M"},
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3352"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3352",
        decision="amend",
        escalated=False,
        affected_keys_count=16,
        occurrence_count=42,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3352", backend="jira")
    )

    assert delivery.suppress is True
    assert delivery.text_override is None


def test_amend_delivery_never_announces_a_nameless_component():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3353",
        confidence=0.9,
        decided_by="llm",
        reason="grid level",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3353"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3353",
        decision="amend",
        escalated=False,
        affected_keys_count=0,
        occurrence_count=9,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is True


def test_amend_delivery_posts_escalation_without_a_component_add():
    from orchestrator.api.app import _amend_delivery

    decision = CorrelationDecision(
        decision="amend",
        ticket_ref="OPS-3353",
        confidence=0.9,
        decided_by="llm",
        reason="urgent now",
        affected_key=None,
        root_cause_kind=None,
        update_message="",
        amended_summary="",
        candidate_refs=["OPS-3353"],
        llm_raw="{}",
    )
    amendment = AmendmentResult(
        ticket_ref="OPS-3353",
        decision="amend",
        escalated=True,
        affected_keys_count=0,
        occurrence_count=9,
        telegram_chat_id="-100555",
        telegram_topic_id="42",
        telegram_message_id=555,
        component_added=False,
    )

    delivery = _amend_delivery(
        decision, amendment, NotificationTicket(ref="OPS-3353", backend="jira")
    )

    assert delivery.suppress is False
    assert delivery.top_level is True
    assert delivery.text_override == "Escalated to urgent"
```

Update the existing `test_amend_delivery_*` test that asserts
`"Added MPPT A7 (2 affected components)"` so its `AmendmentResult` passes
`component_added=True`; that message must still be produced for a genuine add.

- [ ] **Step 10: Run the delivery tests and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -k amend_delivery -q`

Expected: FAIL — the current `_amend_delivery` always sets `text_override` and
never sets `suppress`.

- [ ] **Step 11: Gate delivery on state change**

Replace `_amend_delivery` in `app.py`:

```python
def _amend_delivery(
    decision: Any, amendment: Any, ticket: NotificationTicket
) -> NotificationDelivery:
    """Post only what an operator needs to act on.

    An amend that merely re-listed a component already on the ticket changed
    nothing operationally -- the ticket still records the occurrence and the
    raw alert comment, but Telegram stays quiet. Only a component genuinely
    joining the ticket, or an escalation, is worth a message.
    """
    escalated = bool(amendment is not None and amendment.escalated)
    component_added = bool(amendment is not None and amendment.component_added)

    if not (component_added or escalated):
        return NotificationDelivery(suppress=True)

    if component_added:
        label = (decision.affected_key or {}).get("label") or "a new component"
        count = amendment.affected_keys_count if amendment is not None else 1
        message = f"Added {label} ({count} affected component{'s' if count != 1 else ''})"
    else:
        message = "Escalated to urgent"

    if escalated:
        return NotificationDelivery(text_override=message, top_level=True, ticket=ticket)
    reply_to = amendment.telegram_message_id if amendment is not None else None
    return NotificationDelivery(text_override=message, reply_to_message_id=reply_to, ticket=ticket)
```

- [ ] **Step 11b: Skip the ticket-summary read for a suppressed delivery**

Right after the `_amend_delivery`/`_duplicate_delivery` call in
`_resolve_notify_ticket_auto`, the handler unconditionally runs
`ticket_summary=await _ticket_summary(ticket_service, ref)` — a Jira round-trip,
made while holding the grid lock, on the exact path that is now silent. Replace
that `dataclasses.replace(...)` call with:

```python
                delivery = dataclasses.replace(
                    delivery,
                    alert_context=alert_context,
                    ticket_summary=(
                        "" if delivery.suppress else await _ticket_summary(ticket_service, ref)
                    ),
                    stored_ticket_severity=decision.ticket_severity,
                )
```

`ticket_summary` only feeds `_is_effectively_urgent`, which only matters when
something is actually being posted.

- [ ] **Step 12: Run the notify suite and verify it passes**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py chat_orchestrator/tests/services/ticketing -q`

Expected: PASS.

- [ ] **Step 13: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlation_store.py \
        chat_orchestrator/orchestrator/services/ticketing/correlation_render.py \
        chat_orchestrator/orchestrator/api/app.py \
        chat_orchestrator/tests/services/ticketing/test_correlation_store.py \
        chat_orchestrator/tests/services/ticketing/test_correlation_render.py \
        chat_orchestrator/tests/api/test_notify_ticketing.py
git commit -m "fix: notify telegram only when an amend changes ticket state"
```

---

### Task 2: Make dedup_key replays delivery-idempotent

**Gated on Task 0 Step 3.** Skip this task if there are zero `decided_by='replay'`
rows in the last 7 days, and say so in the PR description.

A replay means this exact `dedup_key` was already processed and already
notified. Today it re-bumps the occurrence counter, re-comments on the ticket,
and re-posts to Telegram — and because the replay decision hardcodes
`affected_key=None`, that post is exactly
`"Added another component (0 affected components)"`.

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py:1590-1600` (immediately after `decision` is known to be non-`None`)
- Test: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Interfaces:**
- Consumes: `CorrelationDecision.decided_by`, `.ticket_ref`, `.ticket_severity`; `correlator._is_urgent_severity_increase`.
- Produces: `NotificationDelivery(suppress=True)` for a replayed decision that is not an urgent escalation.

- [ ] **Step 1: Write the failing test**

Add a class to `test_notify_ticketing.py` alongside
`TestResolveNotifyTicketAutoAmend`, using the module's existing
`_FakeCorrelator` / `_decision` / `fake_apply_amendment` / `_notify_body` /
`_target` helpers:

```python
class TestResolveNotifyTicketAutoReplay:
    async def test_replayed_amend_does_not_renotify_or_recomment(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, _result_holder = fake_apply_amendment
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="OPS-3353",
            confidence=0.9,
            decided_by="replay",
            reason="replayed prior decision (dedup_key match)",
            affected_key=None,
            ticket_severity="warning",
        )
        body = _notify_body(ticket_id="auto", dedup_key="alert-42")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3353"
        assert extra["decided_by"] == "replay"
        assert delivery is not None
        assert delivery.suppress is True
        # The prior run already amended the ticket and already posted.
        assert calls == []

    async def test_replayed_urgent_escalation_still_applies(
        self, fake_apply_amendment, monkeypatch
    ):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        calls, result_holder = fake_apply_amendment
        result_holder["result"] = AmendmentResult(
            ticket_ref="OPS-3353",
            decision="amend",
            escalated=True,
            affected_keys_count=0,
            occurrence_count=4,
            telegram_chat_id="-100555",
            telegram_topic_id="42",
            telegram_message_id=123,
        )
        _FakeCorrelator.decision_to_return = _decision(
            decision="amend",
            ticket_ref="OPS-3353",
            confidence=0.9,
            decided_by="replay",
            reason="replayed prior decision (dedup_key match)",
            affected_key=None,
            ticket_severity="warning",
        )
        body = _notify_body(
            ticket_id="auto",
            dedup_key="alert-42",
            alert={"subject": "! Urgent: Grid outage", "severity": "urgent"},
        )

        ref, error, _extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert ref == "OPS-3353"
        assert len(calls) == 1
        assert delivery is not None
        assert delivery.suppress is False
        assert delivery.top_level is True
```

`_decision` does not currently pass `ticket_severity`; add it to that helper's
`defaults` dict with a `""` default so both tests can override it.

- [ ] **Step 2: Run it and verify it fails**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -k replayed_decision -q`

Expected: FAIL — `apply_amendment` runs and the assertion inside the fake fires.

- [ ] **Step 3: Short-circuit non-escalating replays**

In `_resolve_notify_ticket_auto`, directly after the `if decision is None:`
fallback block and before the `try:` that branches on `decision.decision`:

```python
        from orchestrator.services.ticketing.correlator import _is_urgent_severity_increase

        if (
            decision.decided_by == "replay"
            and decision.ticket_ref
            and not _is_urgent_severity_increase(alert.severity, decision.ticket_severity)
        ):
            # This dedup_key was already decided, already applied to the
            # ticket, and already posted. Re-running the amend would double
            # the comment and the Telegram message. An urgent severity
            # increase is the one thing that still has to get through, so it
            # deliberately falls past this guard into the normal amend path.
            logger.info(
                "Notify: replayed dedup_key for %r -- suppressing duplicate delivery",
                decision.ticket_ref,
            )
            return (
                decision.ticket_ref,
                None,
                {
                    "decision": decision.decision,
                    "correlated_with": decision.ticket_ref,
                    "confidence": decision.confidence,
                    "decided_by": decision.decided_by,
                },
                NotificationDelivery(
                    suppress=True,
                    alert_context=alert_context,
                    stored_ticket_severity=decision.ticket_severity,
                ),
            )
```

- [ ] **Step 4: Run it and verify it passes**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/api/test_notify_ticketing.py
git commit -m "fix: make replayed notify decisions delivery-idempotent"
```

---

### Task 3: Extract components from real fleet device names

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/alert_facts.py:44-100`
- Test: `chat_orchestrator/tests/services/ticketing/test_alert_facts.py`

**Interfaces:**
- Produces: `derive_component(subject, text) -> Tuple[kind, key, label]` matching `MPPT <ID>` with or without a following bracket, and disambiguating instance numbers.

- [ ] **Step 1: Write the failing tests using the fleet's real alert text**

Add to `TestDeriveComponent` in `test_alert_facts.py`:

```python
    def test_solar_charger_mppt_with_model_between_id_and_bracket(self):
        subject = (
            "! Urgent: Turn off Combiner: ALERT - 'GridV': "
            "'#26 - Charger terminal overheated' on "
            "'Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]' !"
        )
        kind, key, label = derive_component(subject, "")
        assert kind == "mppt"
        assert key == "PNXG#27"
        assert label == "MPPT PNXG#27"

    def test_same_charger_id_different_instance_is_a_different_component(self):
        first = derive_component("Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]", "")
        second = derive_component("Solar Charger - MPPT PNXG ARTN4.50/100/10 [28]", "")
        assert first[1] != second[1]

    def test_mppt_id_without_any_bracket(self):
        subject = "! Warning: MPPT IYYY in GridW seems to perform lower than other MPPTs !"
        kind, key, label = derive_component(subject, "")
        assert kind == "mppt"
        assert key == "IYYY"
        assert label == "MPPT IYYY"

    def test_prose_after_mppt_is_not_treated_as_an_id(self):
        kind, key, label = derive_component("! Warning: MPPT performance drop detected !", "")
        assert (kind, key, label) == ("", "", "")
```

- [ ] **Step 2: Run them and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_alert_facts.py -k "solar_charger or without_any_bracket or different_instance or prose_after" -q`

Expected: FAIL — the first three return `("", "", "")` because the current
regex needs the bracket adjacent to the id.

- [ ] **Step 3: Replace the MPPT pattern with an id-shaped scan**

In `alert_facts.py`, replace the `_MPPT_PATTERN` definition and the MPPT branch
of `derive_component`:

```python
# n8n's original "Build Alert Actions1" pattern only matched "MPPT <id> [<x>]"
# with the bracket adjacent to the id. Real device names put the model between
# them ("Solar Charger - MPPT PNXG ARTN4.50/100/10 [27]") and some alerts have
# no bracket at all, so scan for the first id-shaped token after "MPPT" and
# keep a trailing numeric bracket as an instance discriminator.
_MPPT_PATTERN = re.compile(
    r"\bmppt\b[\s:\-]+([A-Za-z0-9]+)([^\[\]]{0,40}\[(\d{1,4})\])?", re.IGNORECASE
)
_DCU_PATTERN = re.compile(r"dcu\s+(\d{9}|[a-fA-F0-9]{16})", re.IGNORECASE)


def _looks_like_component_id(token: str) -> bool:
    """An id carries a digit or is a short all-caps code -- "performance" is
    prose that happens to follow the word MPPT, "A3"/"PNXG"/"IYYY" are ids."""
    if any(character.isdigit() for character in token):
        return True
    return token.isupper() and 2 <= len(token) <= 12
```

and in `derive_component`, replace the `mppt_match` block:

```python
    for haystack in (subject or "", text or ""):
        for mppt_match in _MPPT_PATTERN.finditer(haystack):
            token = mppt_match.group(1)
            if not _looks_like_component_id(token):
                continue
            instance = mppt_match.group(3)
            key = f"{token}#{instance}" if instance else token
            return "mppt", key, f"MPPT {key}"

        dcu_match = _DCU_PATTERN.search(haystack)
        if dcu_match:
            key = dcu_match.group(1)
            kind = "base_station" if len(key) > 10 else "dcu"
            label = "Base Station" if kind == "base_station" else "DCU"
            return kind, key, f"{label} {key}"

    return "", "", ""
```

- [ ] **Step 4: Run the whole alert-facts suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_alert_facts.py -q`

Expected: PASS, including the pre-existing `"mppt A3 [Kudi] performance
dropped"` and `"! Warning: MPPT performance drop detected !"` + `"mppt B1
[Kudi] below threshold"` cases (`[Kudi]` is non-numeric so it is not treated as
an instance, and the prose token is skipped in favour of the text's real id).

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/alert_facts.py chat_orchestrator/tests/services/ticketing/test_alert_facts.py
git commit -m "fix: extract mppt ids from real fleet device names"
```

**Expected one-time migration effect:** components already stored on open
tickets were written by the LLM as bare ids (`"PNXG"`, `"IYYY"`). A derived key
that now carries an instance suffix (`"PNXG#27"`) will not match the stored
entry, so each such component produces one — and only one — additional
"Added ..." post the first time it re-fires after deploy, then settles. Grids
whose ids never carry a numeric bracket (`"IYYY"`) are unaffected. This is not
worth a backfill; it self-heals within one alert cycle.

---

### Task 4: Compare component identity case-insensitively in the correlator

The store now matches keys case-insensitively (Task 1). The correlator's
deterministic duplicate check must use the same comparison, or an
LLM-supplied `"MPPT"`/`"iyyy"` entry will never match a derived
`"mppt"`/`"IYYY"` alert.

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py:240-254`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlator.py`

- [ ] **Step 1: Write the failing test**

Add to `TestSignatureDuplicate` in `test_correlator.py`:

```python
    @pytest.mark.asyncio
    async def test_case_differing_stored_key_still_matches(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = _mppt_alert()
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-42",
                "grid_name": "Kudi",
                "status": "open",
                "signatures": [alert.signature],
                "affected_keys": [{"kind": "MPPT", "key": "a3", "label": "MPPT a3"}],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("Kudi", alert)

        assert decision.decision == "duplicate"
        assert decision.decided_by == "signature"
        assert gateway.calls == []
```

`_mppt_alert()` produces `component_kind="mppt"`, `component_key="A3"`; adjust
the stored key casing in the test if that helper's default id changes.

- [ ] **Step 2: Run it and verify it fails**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py -k case_differing_stored_key -q`

Expected: FAIL — the decision is `"amend"` via the LLM, and `gateway.calls` is
non-empty.

- [ ] **Step 3: Use a shared case-insensitive comparison**

In `correlator.py`, extend the existing `alert_facts` import:

```python
from .alert_facts import AlertFacts, same_component
```

and in `_find_signature_duplicate`:

```python
    for candidate in candidates:
        if alert.signature not in (candidate.signatures or []):
            continue
        for entry in candidate.affected_keys or []:
            if same_component(entry, alert.component_kind, alert.component_key):
                return candidate
    return None
```

- [ ] **Step 4: Run the correlator suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py -q`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlator.py chat_orchestrator/tests/services/ticketing/test_correlator.py
git commit -m "fix: compare correlation component keys case-insensitively"
```

---

### Task 5: Add the missing exact-signature rung for keyless alerts

Two byte-identical alerts with no identifiable component (the GridV
combiner alerts) currently have no deterministic path at all — grouping them is
left entirely to the LLM. The correlator's own docstring calls this "rung 2";
`CorrelationStore.get_by_signature` was written for it and never wired in. This
task implements the rung against the already-assembled, already-status-confirmed
candidate list, which needs no extra query, and deletes the dead store method.

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py:240-262, 445-482`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py:190-212` (delete `get_by_signature`)
- Test: `chat_orchestrator/tests/services/ticketing/test_correlator.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py` (remove the `get_by_signature` test)

**Interfaces:**
- Produces: `_find_signature_only_duplicate(candidates, alert) -> Optional[CandidateSummary]` — a candidate carrying this exact signature, used only when the alert has no component key.

- [ ] **Step 1: Write the failing test**

```python
class TestKeylessSignatureDuplicate:
    @pytest.mark.asyncio
    async def test_identical_keyless_alert_is_a_duplicate_without_the_llm(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = enrich_alert_facts(
            AlertFacts(
                subject=(
                    "! Urgent: Turn off Combiner: ALERT - 'GridV': "
                    "'#26 - Charger terminal overheated' on 'Combiner Box 4' !"
                ),
                severity="urgent",
            ),
            grid_name="GridV",
        )
        correlator, store, _ts, gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-3363",
                "grid_name": "GridV",
                "status": "open",
                "severity": "urgent",
                "signatures": [alert.signature],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("GridV", alert)

        assert decision.decision == "duplicate"
        assert decision.ticket_ref == "OPS-3363"
        assert decision.decided_by == "signature"
        assert gateway.calls == []

    @pytest.mark.asyncio
    async def test_keyless_signature_match_still_escalates_on_urgency(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        alert = enrich_alert_facts(
            AlertFacts(subject="! Urgent: Grid outage in GridV !", severity="urgent"),
            grid_name="GridV",
        )
        correlator, store, _ts, _gateway = _make_correlator()
        store.correlations.append(
            {
                "ticket_ref": "OPS-3363",
                "grid_name": "GridV",
                "status": "open",
                "severity": "warning",
                "signatures": [alert.signature],
                "affected_keys": [],
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
        )

        decision = await correlator.decide("GridV", alert)

        assert decision.decision == "amend"
        assert decision.ticket_ref == "OPS-3363"
```

Add `from orchestrator.services.ticketing.alert_facts import enrich_alert_facts`
to the test module's imports if it is not already there.

- [ ] **Step 2: Run them and verify they fail**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py -k KeylessSignatureDuplicate -q`

Expected: FAIL — the LLM gateway is called and the decision comes back
`decided_by="llm"`.

- [ ] **Step 3: Implement the rung**

In `correlator.py`, add next to `_find_signature_duplicate`:

```python
def _find_signature_only_duplicate(
    candidates: List[CandidateSummary], alert: AlertFacts
) -> Optional[CandidateSummary]:
    """Rung 2: for an alert with no identifiable component, an open candidate
    carrying this exact signature *is* the same alert re-firing -- there is no
    component key that could make it a distinct affected component. Without
    this, identical grid-level and unparsed-device alerts depend entirely on an
    LLM judgment that falls back to "new" (a duplicate ticket) on any hiccup."""
    if not alert.signature or alert.component_kind:
        return None
    for candidate in candidates:
        if alert.signature in (candidate.signatures or []):
            return candidate
    return None
```

In `decide()`, replace the single `duplicate = _find_signature_duplicate(...)`
lookup with:

```python
        duplicate = _find_signature_duplicate(candidates, alert) or (
            _find_signature_only_duplicate(candidates, alert)
        )
```

The existing severity-increase handling below it already converts an urgent
increase into an `"amend"`, so the second test passes without further change.

- [ ] **Step 4: Delete the dead store method**

Remove `CorrelationStore.get_by_signature` (`correlation_store.py:190-212`) and
its test in `test_correlation_store.py`. It has no production caller and its
docstring advertises a pipeline rung that did not exist, which is how this gap
stayed invisible.

- [ ] **Step 5: Run the ticketing suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlator.py \
        chat_orchestrator/orchestrator/services/ticketing/correlation_store.py \
        chat_orchestrator/tests/services/ticketing/test_correlator.py \
        chat_orchestrator/tests/services/ticketing/test_correlation_store.py
git commit -m "feat: group identical keyless alerts by signature without the llm"
```

---

### Task 6: Record a correlation row for every fallback ticket

A ticket filed by a fail-open path has no `ticket_correlations` row, so it is
invisible to `open_candidates_for_grid` forever and can only be rediscovered
through Jira's label search with empty `signatures`/`affected_keys` — which
means it can never be a deterministic match. Every fallback therefore seeds the
next duplicate.

**Files:**
- Modify: `chat_orchestrator/orchestrator/api/app.py:1415-1435, 1455-1475, 1490-1505, 1640-1660`
- Test: `chat_orchestrator/tests/api/test_notify_ticketing.py`

**Interfaces:**
- Produces: `_file_uncorrelated_ticket(body, target, backend_override, alert, alert_context, store, decided_by) -> tuple[Optional[str], Optional[Dict[str, Any]], NotificationDelivery]`.

- [ ] **Step 1: Write the failing test**

`CorrelationStore` is imported *inside* `_resolve_notify_ticket_auto`, so
patching the module attribute works exactly the way the existing
`_patch_correlator` fixture patches `AlertCorrelator`:

```python
class _RecordingCorrelationStore:
    """Captures upsert_correlation without needing a Supabase client."""

    instances: List["_RecordingCorrelationStore"] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.upserts: List[Dict[str, Any]] = []
        _RecordingCorrelationStore.instances.append(self)

    async def upsert_correlation(self, **kwargs: Any) -> bool:
        self.upserts.append(kwargs)
        return True

    async def open_candidates_for_grid(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        return []

    async def record_message_id(self, *args: Any, **kwargs: Any) -> bool:
        return True


@asynccontextmanager
async def _never_acquires_lock(grid_name: str, timeout_seconds: float):
    yield False


class TestFallbackTicketsAreCorrelatable:
    async def test_lock_timeout_ticket_still_gets_a_correlation_row(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        _RecordingCorrelationStore.instances = []
        monkeypatch.setattr(
            "orchestrator.services.ticketing.correlation_store.CorrelationStore",
            _RecordingCorrelationStore,
        )
        monkeypatch.setattr(
            "orchestrator.api.app._acquire_grid_correlation_lock", _never_acquires_lock
        )
        body = _notify_body(ticket_id="auto", text="! Warning: MPPT A7 in Kudi !")

        ref, error, extra, delivery = await _resolve_notify_ticket_full(body, _target())

        assert error is None
        assert extra["decided_by"] == "fallback"
        assert delivery is not None
        assert delivery.suppress is False
        upserts = [u for s in _RecordingCorrelationStore.instances for u in s.upserts]
        assert [u["ticket_ref"] for u in upserts] == [ref]
```

Add `from contextlib import asynccontextmanager` to the test module's imports.
The class attribute is reset inside the test rather than via another autouse
fixture.

- [ ] **Step 2: Run it and verify it fails**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -k lock_timeout_ticket -q`

Expected: FAIL — `upserts` is empty; the lock-timeout path never records.

- [ ] **Step 3: Move alert enrichment above the kill switch**

In `_resolve_notify_ticket_auto`, the enriched `alert` is currently built after
the `ALERT_CORRELATION_ENABLED` check. Move the `base_alert`/`alert =
enrich_alert_facts(...)` block (and the imports it needs) to the top of the
function, before the flag check. `enrich_alert_facts` is pure and does no I/O,
so this is safe on the kill-switch path.

- [ ] **Step 4: Add one helper and use it on every fallback path**

```python
async def _file_uncorrelated_ticket(
    body: "NotifyRequest",
    target: "GridNotificationTarget",
    backend_override: str,
    alert: "AlertFacts",
    alert_context: UrgentAlertContext,
    store: Any,
    decided_by: str,
) -> "tuple[Optional[str], Optional[Dict[str, Any]], NotificationDelivery]":
    """File a plain ticket on a fail-open path *and* record its correlation row.

    Without the row the ticket is invisible to ``open_candidates_for_grid``
    forever, so the next identical alert cannot correlate with it and files yet
    another ticket. Recording is best-effort inside ``_record_new_correlation``
    -- a store outage still leaves the ticket filed and the alert delivered.
    """
    summary = _notify_ticket_subject(body)
    result, error = await _create_notify_ticket(
        body, target, backend_override, alert_context=alert_context
    )
    if error is not None:
        return None, {"ticket_error": error}, _ticket_failure_delivery(alert_context)
    if store is not None:
        await _record_new_correlation(store, target, alert, result, None, summary, body.text)
    return (
        result.ref,
        {
            "decision": "new",
            "correlated_with": None,
            "confidence": None,
            "decided_by": decided_by,
        },
        _new_ticket_delivery(_notification_ticket_from_result(result), alert_context),
    )
```

Replace the four fallback blocks with calls to it, keeping each site's existing
`decided_by` value and returning `(ref, None, meta, delivery)`:

- kill switch (`ALERT_CORRELATION_ENABLED` off): `decided_by="flag_off"`,
  `store=CorrelationStore(get_client=_raw_supabase_client)` — construct the
  store before the flag check so the row is still recorded while correlation is
  disabled.
- lock timeout: `decided_by="fallback"`.
- `decision is None`: `decided_by="fallback"`.
- correlation-execution exception: `decided_by="fallback"`.

Leave the two paths that already call `_record_new_correlation` (the LLM "new"
branch and the root-cause parent) exactly as they are.

- [ ] **Step 5: Run the notify suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/api/test_notify_ticketing.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/api/app.py chat_orchestrator/tests/api/test_notify_ticketing.py
git commit -m "fix: record correlation rows for fail-open notify tickets"
```

---

### Task 7: Stop the grid lock from timing out under load

**Gated on Task 0 Step 1.** Do this task if OPS-3365 had no event row (lock
timeout) or an `LLM call failed` reason. Otherwise it is a latent robustness
fix — still worth doing, but say in the PR description that it was not the
observed cause.

`_acquire_grid_correlation_lock` gets `llm_timeout_seconds` (12s), while the
lock holder runs one Jira JQL search plus up to 15 *sequential* `get_status`
calls (15s timeout each) before the LLM call even starts. A second alert on the
same grid cannot realistically get the lock, so it files its own ticket.

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py:28-38`
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlator.py:589-607`
- Modify: `chat_orchestrator/orchestrator/api/app.py:1450-1460`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlator.py`
- Test: `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`

- [ ] **Step 1: Write the failing test**

```python
class TestCandidateStatusConcurrency:
    @pytest.mark.asyncio
    async def test_status_lookups_run_concurrently(self, monkeypatch):
        monkeypatch.setenv("ALERT_CORRELATION_ENABLED", "true")
        correlator, store, ticket_service, _gateway = _make_correlator()
        for index in range(8):
            store.correlations.append(
                {
                    "ticket_ref": f"OPS-{index}",
                    "grid_name": "Kudi",
                    "status": "open",
                    "signatures": [],
                    "affected_keys": [],
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )

        in_flight = 0
        peak = 0

        async def _slow_status(ref):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            await asyncio.sleep(0.05)
            in_flight -= 1
            return TicketStatus(ref=ref, status="Open", is_done=False, summary="")

        ticket_service.get_status = _slow_status

        candidates = await correlator._assemble_candidates("Kudi")

        assert len(candidates) == 8
        assert peak > 1
```

Import `asyncio` and `TicketStatus` in the test module if they are not already
imported, and construct `TicketStatus` with whatever field names
`ticketing/backend.py` defines.

- [ ] **Step 2: Run it and verify it fails**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing/test_correlator.py -k status_lookups_run_concurrently -q`

Expected: FAIL — `peak == 1`, the lookups are sequential.

- [ ] **Step 3: Confirm candidate status concurrently**

In `correlator.py`, replace the sequential confirmation loop in
`_assemble_candidates`:

```python
        semaphore = asyncio.Semaphore(5)

        async def _confirm(candidate: CandidateSummary):
            async with semaphore:
                try:
                    return await self._ticket_service.get_status(candidate.ref)
                except Exception:
                    LOGGER.warning(
                        "Candidate status lookup raised for %r", candidate.ref, exc_info=True
                    )
                    return None

        ordered = list(by_ref.values())
        statuses = await asyncio.gather(*(_confirm(c) for c in ordered))

        confirmed: List[CandidateSummary] = []
        for candidate, status in zip(ordered, statuses):
            if status is not None and status.is_done:
                if candidate.ref in store_refs:
                    await self._store.mark_closed(candidate.ref)
                continue
            if status is None and candidate.ref not in store_refs:
                continue
            if status is None:
                LOGGER.warning("Preserving cached candidate %r: status unavailable", candidate.ref)
            confirmed.append(candidate)
```

Note this also fixes the two `LOGGER.warning("... {!r} ...", ref)` calls in that
block, which use `str.format` placeholders against a `%`-style logger and
therefore print literal `{!r}` today.

- [ ] **Step 4: Give the lock its own budget**

In `correlation_rules.py`:

```python
@dataclass(frozen=True)
class CorrelationPolicy:
    """Application-versioned safety bounds for one correlation decision."""

    confidence_floor: float = 0.75
    llm_timeout_seconds: float = 12
    # The lock is held across candidate assembly (Jira search + status
    # confirmation) *and* the LLM call, so it must outlast the LLM budget --
    # sharing it made every concurrent alert on a busy grid file its own
    # ticket.
    grid_lock_timeout_seconds: float = 45
    open_candidate_window_hours: int = 168
    maximum_candidate_count: int = 15
```

In `app.py`, in `_resolve_notify_ticket_auto`:

```python
    timeout_seconds = DEFAULT_CORRELATION_POLICY.grid_lock_timeout_seconds
```

Add to `test_correlation_rules.py`:

```python
def test_grid_lock_budget_exceeds_llm_budget():
    policy = DEFAULT_CORRELATION_POLICY
    assert policy.grid_lock_timeout_seconds > policy.llm_timeout_seconds
```

- [ ] **Step 5: Run the ticketing and notify suites**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing chat_orchestrator/tests/api/test_notify_ticketing.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/ticketing/correlator.py \
        chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py \
        chat_orchestrator/orchestrator/api/app.py \
        chat_orchestrator/tests/services/ticketing/test_correlator.py \
        chat_orchestrator/tests/services/ticketing/test_correlation_rules.py
git commit -m "perf: confirm alert candidates concurrently and widen the grid lock"
```

---

### Task 8: Verify the whole change set

**Files:**
- Verify: all modified modules and test files.

- [ ] **Step 1: Run the full orchestrator suite**

Run: `chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests -q`

Expected: PASS. If the environment blocks loopback sockets, rerun the identical
command with loopback permission and report that separately from any real
failure.

- [ ] **Step 2: Run the repo-wide hooks (not just ruff/pytest)**

Run: `pre-commit run --all-files`

Expected: PASS. If `test-wiring` reports untracked files under a `tests/`
directory, vet each one for operator data and then `git add -f` it explicitly —
a plain `git add` on a new test file is a silent no-op in this repo.

- [ ] **Step 3: Re-run the hook and the ticketing suites after any force-add**

```bash
pre-commit run --all-files
chat_orchestrator/.venv/bin/python -m pytest chat_orchestrator/tests/services/ticketing chat_orchestrator/tests/api/test_notify_ticketing.py -q
```

Expected: both clean.

- [ ] **Step 4: Confirm what actually got committed**

```bash
git show --stat HEAD
git status --short
```

Expected: every file listed in the task commits is present in the history; a
clean worktree.

- [ ] **Step 5: Commit the plan artifact**

```bash
git add -f docs/superpowers/plans/2026-07-28-notify-alert-noise-root-cause-fix.md
git commit -m "docs: plan notify alert noise root-cause fix"
```

`docs/superpowers/plans/` is gitignored in this repo, so the `-f` is required or
the file silently never reaches the remote.

---

## Post-deploy verification

Run these against the chat DB 24 hours after deploy — they are the numeric
version of "did the noise stop".

- [ ] **Amend posts that announced nothing should be gone.** Every amend that
  reaches Telegram must now correspond to a component that was genuinely new:

```sql
select date_trunc('hour', created_at) as hour, decided_by, decision, count(*)
from ticket_correlation_events
where grid_name in ('GridW', 'GridV')
  and created_at >= now() - interval '24 hours'
group by 1, 2, 3
order by 1;
```

Expect a visible shift from `decided_by='llm'` toward `decided_by='signature'`
for repeat fires — that is the deterministic rung doing the work the LLM was
doing, at zero cost and zero noise.

- [ ] **No new correlation-less tickets.** This should return zero rows:

```sql
select t.ticket_ref
from internal_tickets t
left join ticket_correlations c on c.ticket_ref = t.ticket_ref
where t.created_at >= now() - interval '24 hours'
  and c.ticket_ref is null;
```

(For a Jira-backed deployment, compare the refs returned by the correlation
events' `ticket_ref` column against `ticket_correlations` instead.)

- [ ] **Component extraction is actually landing.** Keyless alerts should become
rare rather than universal:

```sql
select coalesce(nullif(alert->>'component_kind', ''), '(none)') as kind, count(*)
from ticket_correlation_events
where created_at >= now() - interval '24 hours'
group by 1 order by 2 desc;
```

Before the fix this is ~100% `(none)` for these grids. If it stays that way
after Task 3, the device naming differs again from what the screenshots showed
and the regex needs another real sample — do not guess at it.
