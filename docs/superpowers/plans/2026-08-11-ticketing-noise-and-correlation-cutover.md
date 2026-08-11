# Ticketing Noise Fixes + Alert-Correlation Schema Cutover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore alert correlation (dead in production since 2026-08-10 20:13 UTC), then fix the four operator-visible noise problems it was supposed to prevent: repeated escalation pings, alert storms splitting across tickets, ticket-status updates posting new messages instead of editing, and cascading failures (BMS → inverter → grid outage) filed as unrelated tickets.

**Architecture:** `ticket_correlations` / `ticket_correlation_events` become `ticket_id`-keyed correlation *state* only — current ticket ref, backend, summary, status, org, and grid come from `TicketRepository`; Telegram coordinates come from `DeliveryRepository`. On top of that, grouping moves from "LLM decides everything" toward "deterministic where the signature already proves it, LLM only for genuine judgment", and notification is gated on state having actually persisted, so a store outage degrades to silence rather than to a storm.

**Tech Stack:** Python 3.11, FastAPI, Supabase/PostgREST, Pydantic, loguru-style logging (`shared.utils.logging.get_logger`), `shared.llm` gateway (Gemini), pytest.

---

## Background: the production incident that frames this plan

On 2026-08-10 `0005b_ticket_schema_validate_and_contract.sql` (merged in #93) was applied to the production chat_db. It dropped, from `ticket_correlations`: `id, ticket_ref, ticket_backend, grid_name, organization_id, summary_current, status, telegram_chat_id, telegram_topic_id, telegram_message_id` — and from `ticket_correlation_events`: `ticket_ref`. `ticket_id` became the primary key.

**Task 7 of `docs/superpowers/plans/2026-07-28-anansi-ticket-schema-consolidation.md` ("Key alert correlation by canonical ticket ID") was never implemented**, so `correlation_store.py` still reads and writes every one of those columns. Since the migration ran, every `/notify` produces:

```
correlation store: open_candidates_for_grid(Akinsolu) failed: column ticket_correlations.grid_name does not exist
correlation store: record_event failed: Could not find the 'ticket_ref' column of 'ticket_correlation_events'
correlation store: upsert_correlation(OPS-3467) failed: Could not find the 'grid_name' column
correlation store: record_amendment(OPS-3427) failed: Could not find the 'summary_current' column
correlation store: record_message_id(OPS-3427) failed: Could not find the 'telegram_message_id' column
apply_amendment: correlation row for 'OPS-3400' not found after merge -- skipping render/ticket-update side effects
```

Because every store method swallows its error and returns an empty value, the failure is invisible to the caller and the observable effect is **more** noise, not less:

| Broken write | Consequence |
|---|---|
| `open_candidates_for_grid` | No stored candidates. Only `find_open_by_grid` (Jira search) survives, and those candidates carry no `signatures`, `affected_keys` or `severity` — so both deterministic rungs are dead and **every** alert reaches the LLM. |
| `record_amendment` | `severity` / `escalated_at` never persist → every urgent alert looks like a fresh warning→urgent transition → a fresh top-level "Escalated to urgent" post, forever. |
| `record_message_id` | No edit anchor is ever stored → amends can never edit in place, so they post new messages. |
| `merge_affected_key` / `get_correlation` | Silently no-op or return `None` → `apply_amendment` takes its "correlation row missing" branch, which returns `escalated=True` unconditionally for urgent alerts. |
| `grid_name` gone from a row that *is* found | `render_summary` renders `"3 MPPTs in  affected"` — the empty grid name visible in the operator's 2026-08-11 08:15 screenshot. |

Verified against `doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run`, window 06:15:27–06:16:08 UTC on 2026-08-11 (= 08:15–08:16 UTC+2): 7 Akinsolu alerts, 7 Gemini correlation calls, 7 failed `record_amendment` writes, 7 top-level Telegram posts for OPS-3427/OPS-3428.

**Nothing caught this** because `db/schema/chat_db.sql:211` still describes the pre-0005b tables (Task 11 of that plan, "Refresh and verify the complete final Chat DB public schema", is also unrun) and no test compares store payloads against the schema. Task 1 below closes that hole first, so the cutover has something to verify against.

**Operator decisions already made for this plan** (do not re-litigate):

1. Production stays as-is until this ships — no kill-switch flip, no column re-add.
2. Cascading failures **merge onto the root-cause ticket** (not a cross-link between two tickets).
3. The scroll-gap fix is the real topic-scoped watermark, migration included.

## Background: what exists today

Read these before starting — the plan builds on them rather than replacing them.

| Thing | Where | Why it matters |
|---|---|---|
| `CorrelationStore` | `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py` | Every method here is ref-keyed and touches dropped columns. Task 2 rewrites it. |
| `AlertCorrelator.decide()` | `chat_orchestrator/orchestrator/services/ticketing/correlator.py:423` | The decision ladder: replay → flag → no-candidates → signature rungs → LLM + guardrails. |
| `_find_signature_duplicate` / `_find_signature_only_duplicate` | `correlator.py:251` / `:268` | The only two deterministic rungs today. Both require an *exact* re-fire. |
| `apply_amendment()` | `correlation_render.py:172` | Executes amend/duplicate. Contains the "correlation row missing" branch that force-escalates. |
| `render_summary` / `render_description` | `correlation_render.py:80` / `:122` | Pure renders, recomputed from state every time. `render_description` owns the `[anansi:affected-*]` marker block. |
| `_amend_delivery` | `chat_orchestrator/orchestrator/api/app.py:1639` | Decides suppress / edit / fresh top-level post. Source of the bare `"Escalated to urgent"` string. |
| `_attempt_lock_free_signature_correlation` | `app.py:1806` | Grid-lock-timeout fallback. Runs only the two exact-duplicate rungs. |
| `TicketRepository` | `ticketing/repository.py:34` | Sole ticket writer. `TicketRecord` already carries `grid_name`, `status`, `summary`, `backend`. |
| `DeliveryRepository` | `ticketing/delivery_repository.py` | `record()`, `latest_for_ticket()`, `find_notification()` — the replacement for cached Telegram coordinates. |
| `TicketUpdateNotifier` | `ticketing/update_notifier.py:72` | Edit-vs-reply placement policy, `SCROLL_THRESHOLD = 5`. |
| `ChatWatermarkRepository` | `ticketing/chat_watermark.py:40` | Counts messages **chat-wide**. Task 6 makes it topic-aware. |
| `UrgentAlertContext` | `orchestrator/services/urgent_alert_context.py` | Lazy live-telemetry lookup behind the `⚡ Live output:` line. Task 9 extends it. |
| Correlation prompt | `shared/prompts/library/ticketing.correlation.prompt` | `overridable: true`, publish restricted to eng. Task 7 edits the bundled text. |
| `CorrelationPolicy` | `ticketing/correlation_rules.py:23` | `confidence_floor=0.75`, `llm_timeout_seconds=12`, `grid_lock_timeout_seconds=120`, `open_candidate_window_hours=168`. |
| Grid → Telegram target | `shared/auth/auth_service.py:1276` | Every grid resolves to `internal_telegram_group_chat_id` + `internal_telegram_group_thread_id` — one shared group, one **topic per grid**. This is why a chat-wide message count is the wrong denominator. |

**Commands**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant
```

Run tests from `chat_orchestrator/` (that is where `pyproject.toml`'s `testpaths = ["tests"]` resolves):

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing -q
```

**CRITICAL — from CLAUDE.md:** every new file under any `tests/` directory needs `git add -f`; a plain `git add` is a silent no-op that commits nothing and makes CI skip the suite. The same applies to `docs/superpowers/plans/`. Task 11 verifies this; do not skip it.

---

## File Structure

**Create:**
- `db/migrations/0016_chat_messages_topic.sql` — `chat_messages.telegram_topic_id` + index + backfill from `chat_sessions`
- `chat_orchestrator/tests/services/ticketing/test_correlation_store_schema_contract.py` — payload keys vs. checked-in schema
- `chat_orchestrator/tests/api/test_notify_alert_storm.py` — burst → one ticket, one escalation, one message

**Modify:**
- `db/schema/chat_db.sql` — correlation tables to post-0005b reality; new `chat_messages` column
- `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py` — `ticket_id` keying, drop dead methods
- `chat_orchestrator/orchestrator/services/ticketing/correlator.py` — candidate assembly, new deterministic rung, severity inference
- `chat_orchestrator/orchestrator/services/ticketing/correlation_render.py` — `ticket_id`, persist-gated escalation, description block at top, mixed-kind summaries
- `chat_orchestrator/orchestrator/services/ticketing/repository.py` — external-ticket adoption
- `chat_orchestrator/orchestrator/services/ticketing/service.py` — `update_ticket` persists canonical summary/description
- `chat_orchestrator/orchestrator/services/ticketing/chat_watermark.py` — topic-scoped counting
- `chat_orchestrator/orchestrator/services/ticketing/update_notifier.py` — pass the anchor's topic
- `chat_orchestrator/orchestrator/api/app.py` — notify wiring, escalation message content, lock-free rung parity
- `chat_orchestrator/orchestrator/services/urgent_alert_context.py` — battery voltage alongside live output
- `chat_orchestrator/orchestrator/services/supabase_client.py` — stamp topic (and group) on saved messages
- `mcp_servers/servers/customer_server/client_grid_status.py` — one VRM read returning output + battery voltage
- `shared/prompts/library/ticketing.correlation.prompt` — failure-topology rules
- `shared/config/flag_registry.py` — `ALERT_CASCADE_MERGE_ENABLED`
- existing tests: `test_correlation_store.py`, `test_correlator.py`, `test_correlation_render.py`, `test_notify_ticketing.py`, `test_chat_watermark.py`, `test_update_notifier.py`, `test_urgent_alert_context.py`

---

## Task 1: Make the checked-in schema true, and make a wrong payload fail CI

Nothing else in this plan is verifiable until the repo agrees with the database.

**Files:**
- Modify: `db/schema/chat_db.sql`
- Create: `chat_orchestrator/tests/services/ticketing/test_correlation_store_schema_contract.py`

- [ ] **Step 1: Update the correlation tables in `db/schema/chat_db.sql`**

  Apply 0005b's Step 4 to the checked-in schema: drop the ten dropped columns, make `ticket_id uuid PRIMARY KEY` on `ticket_correlations`, add `ticket_id uuid` to `ticket_correlation_events`, and replace `ticket_correlations_grid_idx` with an index on `(last_alert_at DESC)`. Leave the rest of the file alone — a full regeneration is still outstanding (consolidation plan Task 11) and is out of scope here; add a one-line comment above the correlation tables saying so.

- [ ] **Step 2: Write the schema-contract test (expect it to fail)**

  Parse `CREATE TABLE ... ticket_correlations` / `ticket_correlation_events` column names out of `db/schema/chat_db.sql`. Drive every `CorrelationStore` write method with a fake client that captures the payload dicts and `.eq()` filter columns, then assert every captured key is a real column. This is the test that would have caught the incident; keep it dependency-free (no DB, no network).

  Expected now: fails on `grid_name`, `summary_current`, `telegram_message_id`, `status`, `ticket_ref`.

- [ ] **Step 3: Commit the failing guard**

  ```bash
  git add db/schema/chat_db.sql
  git add -f chat_orchestrator/tests/services/ticketing/test_correlation_store_schema_contract.py
  git commit -m "test(ticketing): assert correlation store payloads match the checked-in schema"
  ```

---

## Task 2: Key `CorrelationStore` by `ticket_id`

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py`
- Modify: `chat_orchestrator/tests/services/ticketing/test_correlation_store.py`

- [ ] **Step 1: Rewrite the store's contract tests around `ticket_id`**

  Replace ref-keyed assertions with UUID-keyed ones. Assert explicitly that no write payload contains any of: `ticket_ref, ticket_backend, grid_name, organization_id, summary_current, status, telegram_chat_id, telegram_topic_id, telegram_message_id`.

- [ ] **Step 2: Convert the mutable-state methods**

  `get_correlation`, `record_amendment`, `merge_affected_key`, `bump_occurrence` all take `ticket_id: str` and filter `.eq("ticket_id", ticket_id)`. Delete `_correlation_filter` entirely — the `tickets`-lookup-then-fall-back-to-ref dance is exactly what silently mis-targeted rows. `record_amendment` loses its `summary_current` parameter; it now writes only `severity` and `escalated_at`, and **returns its real success/failure** (Task 5 depends on that return value).

- [ ] **Step 3: Convert `upsert_correlation`**

  Require `ticket_id`; upsert with `on_conflict="ticket_id"`. Payload keeps only: `ticket_id, root_cause_kind, primary_signature, signatures, affected_keys, summary_base, description_base, severity, last_alert_at`. Drop the internal `tickets` lookup — callers now always hold the id.

- [ ] **Step 4: Rewrite `open_candidates_for_grid` as a two-step read**

  1. `tickets`: `grid_name=eq.<grid>`, `status=in.(open,in_progress)`, `provisioning_state=eq.active`, limit `max_candidates * 2`, selecting `id, ticket_ref, backend, summary, status`.
  2. `ticket_correlations`: `ticket_id=in.(<ids>)`, `last_alert_at=gte.<since_iso>`, ordered `last_alert_at` desc, limited.

  Return each correlation row **merged with its ticket fields under the keys the correlator already reads** (`ticket_ref`, `ticket_backend`, `summary_current` ← `tickets.summary`, `status`, `grid_name`), so Task 3's changes to `_assemble_candidates` stay small. Use two explicit queries rather than a PostgREST embedded join — the two-step is guaranteed to work with the raw client already in use (`.in_()` is used elsewhere, e.g. `work_packet_service.py:232`) and it degrades cleanly when the first query returns nothing.

- [ ] **Step 5: Delete the methods the canonical schema replaces**

  Remove `record_message_id` (Telegram coordinates are `DeliveryRepository`'s job — the notify path already writes a receipt with `purpose="notification"`) and `mark_closed` (status lives on `tickets`, whose sole writer is `TicketRepository`). Move the events methods to `ticket_id` and rename `record_event_ticket_ref` → `record_event_ticket_id`. Keep `grid_name` on the *events* table: 0005b left it there deliberately as event-time evidence.

- [ ] **Step 6: Verify**

  ```bash
  cd chat_orchestrator && python -m pytest \
    tests/services/ticketing/test_correlation_store.py \
    tests/services/ticketing/test_correlation_store_schema_contract.py -q
  ruff check orchestrator/services/ticketing
  ```

  Expected: both pass. Task 1's guard turning green is the signal the cutover is real.

- [ ] **Step 7: Commit**

  ```bash
  git add chat_orchestrator/orchestrator/services/ticketing/correlation_store.py
  git add -f chat_orchestrator/tests/services/ticketing/test_correlation_store.py
  git commit -m "refactor(ticketing): key alert correlation state by canonical ticket id"
  ```

---

## Task 3: Move the correlator, renderer, and notify wiring onto `ticket_id`

**Files:**
- Modify: `correlator.py`, `correlation_render.py`, `repository.py`, `service.py`, `app.py`
- Modify: `test_correlator.py`, `test_correlation_render.py`, `test_notify_ticketing.py`

- [ ] **Step 1: Add external-ticket adoption to `TicketRepository`**

  `adopt_external(ref, backend, summary, grid_name) -> TicketRecord`: return the existing row if `ticket_ref` already maps to one, else insert with `created_via="adopted"`, `provisioning_state="active"`. Idempotent — a second call returns the same row. This is what guarantees a `ticket_id` exists for a candidate discovered only through Jira search, *before* any Anansi mutation.

- [ ] **Step 2: Carry `ticket_id` through the decision types**

  `CandidateSummary` and `CorrelationDecision` each gain `ticket_id: Optional[str]`; `ticket_ref` stays for display, links, and comments. In `_assemble_candidates`, store rows already carry the id (Task 2 Step 4); for each `find_open_by_grid` ref not in the store, call `adopt_external` and use the returned id. A candidate that cannot be resolved to an id is dropped with a warning rather than passed on — it cannot be amended safely.

- [ ] **Step 3: Delete the "correlation row missing" branch in `apply_amendment`**

  With Step 2, every amend target has a `ticket_id` and can be `upsert_correlation`-ed unconditionally, so the ~100-line Jira-only-seed branch (`correlation_render.py:236-329`) and its unconditional `escalated=True` disappear. Order inside `apply_amendment` becomes: ensure correlation row → merge affected key → bump occurrence → read state → render → push to backend → comment → `record_amendment`.

- [ ] **Step 4: Take `grid_name` from the ticket, not the correlation row**

  `render_summary` gains an explicit `grid_name: str` parameter, passed from `TicketRecord.grid_name`. This is the direct fix for the `"3 MPPTs in  affected"` render. Add a regression test asserting a non-empty grid name in the aggregate summary.

- [ ] **Step 5: Make `TicketService.update_ticket` persist canonical state**

  It currently only calls the backend (`service.py:428-444`), so `tickets.summary` goes stale after every amend — unacceptable now that `tickets.summary` is what replaced `summary_current` (and is what candidate assembly and severity inference read). Also write through `TicketRepository.update_by_ref`. Keep the backend call authoritative for success/failure.

- [ ] **Step 6: Rewire the notify handler**

  In `app.py`: `_record_new_correlation` passes the freshly created `ticket_id`; `_finalize_correlation_decision` and `_attempt_lock_free_signature_correlation` pass `ticket_id` into `apply_amendment`; the reply/edit target comes from `DeliveryRepository.latest_for_ticket(ticket_id)` instead of `AmendmentResult.telegram_message_id`; delete the `store.record_message_id` call in `_deliver_notification` (the delivery receipt already covers it). The replay path resolves `event.ticket_id` → ref via `TicketRepository`.

- [ ] **Step 7: Verify**

  ```bash
  cd chat_orchestrator && python -m pytest \
    tests/services/ticketing tests/api/test_notify_ticketing.py -q
  ruff check orchestrator/services/ticketing orchestrator/api/app.py
  ```

- [ ] **Step 8: Commit**

  ```bash
  git add chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing chat_orchestrator/tests/api/test_notify_ticketing.py
  git commit -m "refactor(ticketing): resolve correlation ticket fields through the canonical repositories"
  ```

---

## Task 4: Group an alert storm deterministically (operator problem 2)

An alert whose signature already matches an open ticket but whose component is new is *by construction* another affected component of the same issue: `derive_signature` hashes grid + `component_kind` + normalized subject, and the correlation prompt itself says "Two different MPPTs with the same symptom are amend, never duplicate". Today that case has no deterministic rung, so it depends on an LLM call that degrades to `new` on a 12 s timeout, a sub-0.75 confidence, or a lock timeout — which is how one MPPT storm on Akinsolu became OPS-3427 *and* OPS-3428.

**Files:**
- Modify: `correlator.py`, `app.py`
- Modify: `test_correlator.py`, `test_notify_ticketing.py`

- [ ] **Step 1: Write the failing tests first**

  Same grid, same signature, component key not on the ticket → deterministic `amend`, `decided_by="signature"`, `affected_key` populated, no LLM call (assert the gateway was never invoked). Same signature *and* same key → still `duplicate`. Keyless alert → still the existing rung. Same assertions again through `_attempt_lock_free_signature_correlation`.

- [ ] **Step 2: Add `_find_signature_amend(candidates, alert)`**

  Returns the first candidate carrying `alert.signature` when the alert has a `component_kind`/`component_key` that no entry in that candidate's `affected_keys` matches. Order in `decide()`: exact duplicate → keyless duplicate → **signature amend** → LLM. Set `amended_summary=""` (the renderer recomputes from post-merge state) and `confidence=1.0`.

- [ ] **Step 3: Share the rung with the lock-free path**

  Extract the three rungs plus their reason strings into one helper used by both `AlertCorrelator.decide()` and `_attempt_lock_free_signature_correlation`, so a grid-lock timeout groups a storm instead of filing a fresh ticket. This removes the duplicated reason-string blocks in `app.py:1862-1903`.

- [ ] **Step 4: Add a burst regression test**

  New file `chat_orchestrator/tests/api/test_notify_alert_storm.py`: replay the Akinsolu shape — seven urgent MPPT alerts, distinct component keys, same signature, arriving back-to-back at one grid. Assert exactly one ticket created, seven occurrences recorded, all seven components in `affected_keys`, exactly one escalation delivery, and zero LLM calls.

- [ ] **Step 5: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlator.py tests/api -q
  ```

  ```bash
  git add chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/api/test_notify_alert_storm.py chat_orchestrator/tests/services/ticketing/test_correlator.py
  git commit -m "fix(ticketing): group same-signature alerts onto one ticket without the llm"
  ```

---

## Task 5: Escalate once, and say something (operator problem 1)

Three separate defects sit under the repeated "Escalated to urgent" posts. Task 2 fixed the persistence; these are the rest.

**Files:**
- Modify: `correlation_render.py`, `correlator.py`, `app.py`
- Modify: `test_correlation_render.py`, `test_notify_ticketing.py`

- [ ] **Step 1: Write the failing tests**

  (a) Two urgent alerts on an already-urgent ticket → exactly one escalation delivery. (b) `record_amendment` returning `False` → **zero** escalation deliveries plus a WARNING (a state we could not persist must not be announced, or it will be announced again on the next alert). (c) A Jira-discovered candidate with blank stored `severity` whose summary starts `"! Urgent:"` → no escalation delivery. (d) The rendered escalation message contains the ticket ref, the grid, and the affected-component count — and exactly one `🔴`.

- [ ] **Step 2: Gate the escalation flag on the persist**

  In `apply_amendment`, capture `persisted = await store.record_amendment(...)` and set `AmendmentResult.escalated = escalate_now and persisted`. Log at WARNING when a persist failure suppresses an escalation notification, naming the ticket.

- [ ] **Step 3: Make `escalated_at` the idempotency key**

  If the correlation row already has `escalated_at`, never emit another escalation *notification* (the Highest-priority push to the backend stays idempotent and harmless). Document in the docstring that a deliberate de-escalate-then-re-escalate is out of scope.

- [ ] **Step 4: Infer severity when the store does not know it**

  Add `effective_candidate_severity(candidate)` to `correlator.py`: stored `severity` if set, else `derive_severity(candidate.summary)`, else `"urgent"` when the summary starts with `🔴`. Use it in `_apply_guardrails`, the signature rungs, and the lock-free path so a blank severity can no longer masquerade as a warning→urgent transition.

- [ ] **Step 5: Give the message content, and fix the double marker**

  In `_amend_delivery`, delete the bare `"Escalated to urgent"` branch. An escalation posts the current-state line — `escalated to urgent — {rendered_summary}` — falling back to the ticket's live summary. Strip any leading `🔴` from the rendered summary before handing it to `_format_ticket_update_notification`, which adds its own; that pair is what produced `"🔴 OPS-3428 — 🔴 ! Urgent: …"` in the operator's screenshot.

- [ ] **Step 6: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing tests/api -q
  ```

  ```bash
  git add chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing chat_orchestrator/tests/api
  git commit -m "fix(ticketing): announce an escalation once, only after it persists"
  ```

---

## Task 6: Count the topic, not the whole group (operator problem 3)

`messages_since` compares the newest message id in the **chat** against the anchor, but every grid is a *topic* inside one shared group. Production ids ran 65876→65882 in 40 seconds across five different grids, so any anchor is "more than five messages back" within seconds while the operator's own topic sat silent all day — which is why a status change to *in progress* posted a fresh reply instead of editing the card. `chat_messages` has no topic column at all today, so the fix needs one.

This task has no dependency on Tasks 1–5 and can ship first if a quick win is wanted.

**Files:**
- Create: `db/migrations/0016_chat_messages_topic.sql`
- Modify: `db/schema/chat_db.sql`, `chat_watermark.py`, `update_notifier.py`, `supabase_client.py`, `app.py`
- Modify: `test_chat_watermark.py`, `test_update_notifier.py`

- [ ] **Step 1: Migration**

  Add `chat_messages.telegram_topic_id text`; index `(group_id, telegram_topic_id, telegram_message_id DESC)`; backfill from `chat_sessions.telegram_topic_id` via `session_id`. Idempotent (`ADD COLUMN IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`) like every migration in `db/migrations/`. Mirror the column into `db/schema/chat_db.sql`.

- [ ] **Step 2: Stamp topic (and group) at the single write choke point**

  In `supabase_client.save_messages`, populate `telegram_topic_id` from the session row so every writer gets it for free. Separately, `_log_notification_to_chat_db` (`app.py:1029`) never passes `group_id`, so the bot's own alerts are currently invisible to the watermark's `chat_messages` read — pass it.

- [ ] **Step 3: Make the watermark topic-aware**

  `head(chat_id, topic_id=None)` and `messages_since(chat_id, anchor_message_id, topic_id=None)` filter `chat_messages.telegram_topic_id` and `message_deliveries.external_topic_id` when a topic is given, and keep today's chat-wide behavior when it is not. Rewrite the module docstring, which currently justifies chat-wide counting.

- [ ] **Step 4: Pass the anchor's topic from the notifier**

  `TicketUpdateNotifier._notify_inner` already reads `anchor["external_topic_id"]`; hand it to `messages_since`. Tests: traffic in a *different* topic of the same chat does not scroll the anchor (→ edit in place); traffic in the *same* topic does (→ fresh reply); an anchor with no topic keeps the old behavior.

- [ ] **Step 5: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing/test_chat_watermark.py tests/services/ticketing/test_update_notifier.py -q
  ```

  ```bash
  git add db chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing/test_chat_watermark.py chat_orchestrator/tests/services/ticketing/test_update_notifier.py
  git commit -m "fix(ticketing): measure ticket-card scroll distance within the grid's topic"
  ```

---

## Task 7: Merge cascading failures onto the root cause (operator problem 4)

Ogbinbiri filed OPS-3456 (`'#67 - No BMS' on 'Solar Charger [278]'`, 12:27) and OPS-3457 (`RESTART FAILED - Inverter Off while battery Ok >52V`, 12:31) as unrelated tickets. **The model behaved as instructed:** the prompt's Root Cause Rules only model `grid_off` / `grid_isolated`, it states that "an MPPT issue and an inverter fault on the same grid are usually unrelated", and Example 4 makes exactly this shape a `new` ticket. The prompt has no notion of a power chain. The code, separately, has no vocabulary for one — and its mixed-kind rendering would produce nonsense if the model did merge.

Per the operator's decision, the dependent alert **amends onto the root-cause ticket**. Ship it dark behind a flag and read `ticket_correlation_events` before enabling.

**Files:**
- Modify: `shared/prompts/library/ticketing.correlation.prompt`, `shared/config/flag_registry.py`, `correlator.py`, `correlation_render.py`, `app.py`
- Modify: `test_correlator.py`, `test_correlation_render.py`

- [ ] **Step 1: Add the kill switch**

  `ALERT_CASCADE_MERGE_ENABLED`, default `False`, `group="ticketing"`, `depends_on="ALERT_CORRELATION_ENABLED"`, label "Merge cascading equipment failures onto the root-cause ticket". Off = today's behavior exactly.

- [ ] **Step 2: Teach the prompt the failure topology**

  Add a `# Failure Topology` section with the chains that make a same-grid, short-window pair causally related — battery/BMS communication loss ⇒ inverter protective shutdown ⇒ grid outage; grid off ⇒ MPPT low production, DCU/base-station down, token-delivery drop; combiner or string fault ⇒ MPPT underproduction — each with a `root_cause_kind: "power_chain"` instruction, a ~30-minute same-grid window, and the direction of causation (the *earlier* ticket is the parent; a later, more severe symptom still amends onto it). Amend the existing "usually unrelated" line and Example 4 so they scope to pairs *not* on a named chain, and add a worked example built from OPS-3456/OPS-3457. Add `"power_chain"` to the `root_cause_kind` enum in the response-schema line of `_build_prompt` (`correlator.py:346`).

- [ ] **Step 3: Guard the new freedom in code**

  In `_apply_guardrails`: an amend whose `affected_key.kind` differs from every kind already on the target ticket is only allowed when `root_cause_kind == "power_chain"` **and** `ALERT_CASCADE_MERGE_ENABLED` **and** confidence ≥ `confidence_floor`; otherwise force `new` (today's outcome). `power_chain` must **not** join `_ROOT_CAUSE_KINDS_REQUIRING_PARENT` — the parent already exists as a real ticket; if no candidate represents the root cause, fall back to `new` rather than synthesising a parent.

- [ ] **Step 4: Render a merged cascade honestly**

  `render_summary` currently picks a dominant kind, which would produce `"2 Inverters in Ogbinbiri affected"` for a mixed-kind ticket. When `affected_keys` spans more than one kind, render root-cause-led instead: the ticket's own `summary_base` (severity marker preserved, upgraded to `! Urgent:` if any folded symptom is urgent) followed by `— +N dependent alert(s) (<kind labels>)`. The ticket comment for a folded symptom is prefixed `Folded in as a power_chain symptom:` before the raw alert text, so the second repair is still legible on one ticket.

- [ ] **Step 5: Deliver one message that names the link**

  A cascade merge must not be suppressed — it is a new operational fact. Post the LLM's `update_message` (falling back to the rendered summary) against the root ticket's anchor, so the operator sees one threaded update rather than two unrelated urgent pings.

- [ ] **Step 6: Tests**

  BMS→inverter fixture with the flag on → `amend` onto OPS-3456, one delivery, mixed-kind summary as specified. Flag off → `new`. Independent MPPT + inverter fault with `is_hps_on: true` and no topology claim → `new`. Cross-kind amend at confidence 0.6 → `new`. Snapshot the mixed-kind summary and the folded-comment prefix.

- [ ] **Step 7: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing tests/api -q
  python -m pytest tests/test_prompt_parity.py -q
  ```

  Note: `test_prompt_parity.py` snapshots prompt text in `prompt_checksums.json` — regenerate it in the same commit. Per CLAUDE.md, if a "bundled"/parity test fails locally for reasons unrelated to your edit, check `chat_orchestrator/.env` for live credentials before suspecting the codebase.

  ```bash
  git add shared chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing
  git commit -m "feat(ticketing): merge power-chain cascade alerts onto the root-cause ticket"
  ```

---

## Task 8: Put the affected-equipment list at the top of every description

Operator request: keep the bulleted failed-equipment list, but lead the description with it — that is what a technician opening the ticket needs first, not the raw alert text.

**Files:**
- Modify: `correlation_render.py`, `app.py`
- Modify: `test_correlation_render.py`

- [ ] **Step 1: Flip the block order in `render_description`**

  Emit `MARKER_START … MARKER_END` first, then a blank line, then `description_base`. Keep the markers and keep recomputing the whole block from state, which is what makes an amend idempotent. Update the docstring and the existing order assertions.

- [ ] **Step 2: Render the block on the *first* filing too**

  A ticket that has only ever had one alert currently gets no block at all, so the layout changes shape between its first and second alert. Seed it at creation: `_record_new_correlation` already knows the single affected key, so file the ticket with `render_description` output (a one-item list) rather than the bare `body.text`. A grid-level alert with no identifiable component keeps a bare description — there is nothing to list.

- [ ] **Step 3: Check the Jira ADF conversion**

  `jira_backend` converts description text to ADF; confirm a leading `[anansi:affected-start]` line and bullet lines survive that conversion, and that the block is not mistaken for a code fence. Add a conversion test if none covers it.

- [ ] **Step 4: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlation_render.py tests/services/ticketing/test_jira_backend.py -q
  ```

  ```bash
  git add chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing
  git commit -m "feat(ticketing): lead ticket descriptions with the affected-equipment list"
  ```

---

## Task 9: Report battery voltage wherever live output is reported

Operator request. It also pays for itself in Task 7: an alert that says *"Inverter Off while battery Ok >52V"* becomes machine-checkable once the correlator's grid facts carry the real voltage.

**Files:**
- Modify: `mcp_servers/servers/customer_server/client_grid_status.py`, `urgent_alert_context.py`
- Modify: `test_urgent_alert_context.py`, `test_notify_ticketing.py`, `test_jira_backend.py`

- [ ] **Step 1: One VRM read returning both numbers**

  `BatteryStatus.voltage_v` already exists (`platforms/base_platform.py:31`, populated from the VRM `BatterySummary` widget's `V` code). Add `get_live_telemetry(grid_name)` next to `get_live_inverter_output` (`client_grid_status.py:40`): resolve the site once, `asyncio.gather(get_current_inverter_voltage(sid), get_current_battery_status(sid), return_exceptions=True)`, apply the existing 30-minute staleness rule to each independently, and return `{output_kw, battery_voltage_v}` with either field `None` when unavailable. Keep `get_live_inverter_output` as a thin wrapper so existing callers are untouched.

- [ ] **Step 2: Widen the lazy lookup**

  Rename `LiveOutputLookup` → `LiveTelemetryLookup` (still one cached task per request, still bounded by `URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS`, still returning `None` on timeout rather than raising).

- [ ] **Step 3: Render both**

  `telegram_output_line()` → `⚡ Live output: 0.0 kW · 🔋 Battery: 51.8 V`. Each half degrades independently: output unknown keeps `⚡ Live output: unavailable`; battery unknown omits the battery clause rather than printing "unavailable" twice. One decimal place, matching the existing kW formatting.

- [ ] **Step 4: Put it in the LLM facts too**

  `llm_facts()` gains `battery_voltage_v` (omitted when unknown), so it reaches both the correlation prompt (via `get_live_facts`) and `jira_backend`'s `operational_context`.

- [ ] **Step 5: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/test_urgent_alert_context.py tests/api/test_notify_ticketing.py tests/services/ticketing/test_jira_backend.py -q
  ```

  ```bash
  git add chat_orchestrator/orchestrator mcp_servers
  git add -f chat_orchestrator/tests
  git commit -m "feat(notify): report battery voltage alongside live inverter output"
  ```

---

## Task 10: Make a degraded correlation store impossible to miss

The incident ran for roughly twelve hours emitting five WARNING lines per alert, and the only *visible* effect was extra noise. Cheap instrumentation, no new tables.

**Files:**
- Modify: `correlation_store.py`, `app.py`
- Modify: `test_correlation_store.py`

- [ ] **Step 1: Count failures and de-duplicate the log spam**

  A module-level counter keyed by `(method, error_code)`; log the full WARNING on first occurrence then at most once an hour per key, with the suppressed count. Five identical lines per alert is why this was easy to scroll past.

- [ ] **Step 2: Surface it where someone looks**

  Include `correlation_store_failures_last_hour` in the `/health` payload, and add `"correlation_degraded": true` to the `/chat/notify` response `extra` when the counter is non-zero, so the caller (n8n) can see it too.

- [ ] **Step 3: Verify and commit**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing/test_correlation_store.py tests/api -q
  ```

  ```bash
  git add chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests
  git commit -m "feat(ticketing): surface correlation-store degradation instead of only logging it"
  ```

---

## Task 11: Full verification and rollout

- [ ] **Step 1: The whole suite, the way CI runs it**

  ```bash
  cd chat_orchestrator && python -m pytest tests -q
  ```

  ```bash
  pre-commit run --all-files
  ```

  Per CLAUDE.md this is the only check that catches a new `tests/` file that a plain `git add` silently dropped. If `test-wiring` reports untracked test files, vet them for operator data and `git add -f` each one, then re-run until clean.

- [ ] **Step 2: Confirm what actually got committed**

  ```bash
  git show --stat HEAD
  git log --oneline main..HEAD
  ```

  Every new test file and this plan document must appear. `docs/superpowers/plans/` and `tests/` are both gitignored.

- [ ] **Step 3: Deploy and read the logs before declaring success**

  After the images build and App Platform picks them up:

  ```bash
  doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 500 | grep -c "correlation store:"
  ```

  Expected: `0`. Then watch one real alert storm end to end and confirm one ticket, one escalation message, and an edited card rather than a new one.

- [ ] **Step 4: Enable the cascade merge separately**

  Leave `ALERT_CASCADE_MERGE_ENABLED` off through the first deploy. Read a day of `ticket_correlation_events` (`decision`, `root_cause_kind`, `confidence`, `reason`, `llm_raw`) for would-be `power_chain` merges, then enable it once the model's judgment looks right on real traffic.

---

## Known limitations left standing

- **The per-grid correlation lock is in-process only** (`app.py:1277`). Correct at `instance_count: 1` (confirmed in `.do/app.image.example.yaml`); at more than one instance a burst can still split. The follow-up remains a `grid_correlation_leases` table with a short-TTL lease, as the original correlation plan noted.
- **`db/schema/chat_db.sql` is only partially reconciled** with post-0005b production (Task 1 covers the correlation tables and `chat_messages`; the archived legacy tables, dropped `chat_sessions` escalation columns, and `ticket_list_view` are still stale). Consolidation-plan Task 11 owns the full regeneration.
- **A deliberate de-escalation followed by a re-escalation** posts nothing the second time, by design (Task 5 Step 3).
- **No burst debounce.** Seven components arriving in 40 seconds now produce one ticket and one message edited seven times. A quiet-window debounce was considered and dropped as unnecessary once editing works.
