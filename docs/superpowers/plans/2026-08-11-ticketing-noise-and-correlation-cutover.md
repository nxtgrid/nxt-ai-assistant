# Ticketing Noise Fixes + Alert-Correlation Schema Cutover — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan. Three phases, each a single review/verify/commit cycle. Steps use checkbox (`- [ ]`) syntax for tracking. Do not start Phase B before Phase A's verification gate passes — B is untestable against a store that cannot write.

**Goal:** Restore alert correlation (dead in production since 2026-08-10 20:13 UTC), then fix the four operator-visible noise problems it was supposed to prevent: repeated escalation pings, alert storms splitting across tickets, ticket-status updates posting new messages instead of editing, and cascading failures (BMS → inverter → grid outage) filed as unrelated tickets.

**Architecture:** `ticket_correlations` / `ticket_correlation_events` become `ticket_id`-keyed correlation *state* only — current ticket ref, backend, summary, status, org, and grid come from `TicketRepository`; Telegram coordinates come from `DeliveryRepository`. On top of that, grouping moves from "the LLM decides everything" toward "deterministic where the alert text already proves it, LLM only for genuine judgment", and notification is gated on state having actually persisted, so a store outage degrades to silence rather than to a storm.

**Tech Stack:** Python 3.11, FastAPI, Supabase/PostgREST, Pydantic, loguru-style logging (`shared.utils.logging.get_logger`), `shared.llm` gateway (Gemini), pytest.

**Three phases, three verification gates:**

| Phase | Theme | Fixes |
|---|---|---|
| **A** | Restore correlation | The 0005b cutover + failure visibility |
| **B** | One incident → one ticket, one message | Operator problems 1, 2, 3 + affected-equipment list at top |
| **C** | Cascade intelligence | Operator problem 4 + battery voltage, then rollout |

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
| `grid_name` gone from a row that *is* found | `render_summary` renders `"3 MPPTs in  affected"` — the empty grid name in the operator's 2026-08-11 08:15 screenshot. |

Verified against `doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run`, window 06:15:27–06:16:08 UTC on 2026-08-11 (= 08:15–08:16 UTC+2): 7 Akinsolu alerts, 7 Gemini correlation calls, 7 failed `record_amendment` writes, 7 top-level Telegram posts for OPS-3427/OPS-3428.

**Nothing caught this** because `db/schema/chat_db.sql:211` still describes the pre-0005b tables (Task 11 of that plan is also unrun) and no test compares store payloads against the schema. Phase A closes that hole first, so the cutover has something to verify against.

## Background: what the correlation audit trail proves about grouping

Read from production `ticket_correlation_events` on 2026-08-11 (grids Akinsolu and Ogbinbiri, 72 + ~40 events). Two findings here are load-bearing for Phase B — without them the obvious fix does nothing.

**1. The alert signature does not group what its docstring says it groups.**

`alert_facts.py`'s module docstring promises the signature "deliberately EXCLUDES the component key — `MPPT A3` and `MPPT A7` firing on the same grid must produce the *same* signature". In production they do not. The Akinsolu "No BMS" storm of 2026-08-08 14:40 produced **six alerts with six different signatures**:

| signature | component | device name in subject |
|---|---|---|
| `da98d1013f` | `mppt/KBUA#5` | `Solar Charger - MPPT KBUA ARTN4.4/-176/5 Cabin [5]` |
| `76ee619251` | `mppt/65SQ#0` | `Solar Charger - MPPT 65SQ ARTN4.4/-141/32 House [0]` |
| `07558ac505` | `mppt/JD65#3` | `Solar Charger - MPPT JD65 ARTN4.4/-176/5 Cabin [3]` |
| `febab56490` | `mppt/RH2W#6` | `Solar Charger - MPPT RH2W ARTN4.4/-176/5 Cabin [6]` |
| `b8402ebb3b` | `mppt/QI11#2` | `Solar Charger - MPPT QI11 ARTN4.4/+27/24 Church [2]` |
| `2255c88df9` | `mppt/LQLA#1` | `Solar Charger - MPPT LQLA ARTN4.4/27/24 Church [1]` |

Cause: `normalize_subject` strips the *derived* `component_key` (`KBUA#5`), but that is a synthesized `TOKEN#instance` string that never appears literally in the alert text (`MPPT KBUA … [5]`). The regex removes nothing, so the device token *and* its location word (`Cabin` / `House` / `Church`) survive into the hash. Consequences:

- `_find_signature_duplicate` only ever matches the **same** MPPT re-firing (which is exactly what the trail shows: `exact signature+component match`, one per device).
- Two different MPPTs with the same fault are never deterministically related, so every one of them goes to the LLM — which scattered the six across **OPS-3427 (JD65#3, LQLA#1, QI11#2)** and **OPS-3428 (65SQ#0, KBUA#5, RH2W#6)**, 3 and 3. That is operator problem 2, and no confidence floor or lock timeout was involved.
- A "same signature, different component ⇒ amend" rung — the obvious fix — would almost never fire until normalization is corrected. **The two must ship together.**

**2. Component detection misses Victron "Solar Charger" devices, and the keyless rung then over-groups them.**

`_MPPT_PATTERN` requires the literal word `MPPT`. On Ogbinbiri, `'#67 - No BMS' on 'Solar Charger [278]'` and `… 'Solar Charger [279]'` both parsed as **no component at all** — and because they then share one signature (`007f06d35b`), `_find_signature_only_duplicate` classified charger **279 as a duplicate of 278's ticket**. Two distinct failed devices, one recorded, the other silently dropped: no `affected_keys` entry, nothing in the description's equipment list. The same miss hit Akinsolu's `Solar Charger - VT6Y … House 4 [8]` (signature `edf04832ad`, `comp=/`).

This is the mirror image of finding 1, and it is why fixing normalization *without* fixing detection would make things worse: coarser signatures plus keyless alerts means more silent duplicate-collapsing.

**3. The cascade misgrouping is squarely the prompt, twice, with the model's reasoning on record.**

- Ogbinbiri, 2026-08-08 10:31:43 — `RESTART FAILED - Inverter Off while battery Ok >52V … causing Grid outage` → `new`, confidence **0.9**, reason: *"The incoming alert describes a total grid outage due to an inverter failure, which is distinct from the existing BMS communication issue (OPS-3456) and the batt…"*. The model **saw** OPS-3456 (filed 4 minutes earlier) and explicitly rejected it.
- Akinsolu, 2026-07-29 19:00:57 — same alert shape → `new`, confidence **0.95**, reason: *"The existing ticket relates to battery equalization, which is a maintenance issue, whereas this new alert indicates a critical grid outage due to an inverter fault"*.

High confidence, correct-by-the-prompt reasoning. The prompt models only `grid_off` / `grid_isolated`, states that an MPPT issue and an inverter fault on the same grid are usually unrelated, and Example 4 makes exactly this shape a `new` ticket. Phase C changes the instructions, not the model.

**4. Sibling tickets are self-perpetuating.** Once OPS-3427 *and* OPS-3428 both exist, every later Akinsolu alert lists both as candidates and the LLM picks between them non-deterministically (candidate order flips between events). Phase B stops new splits; the existing sibling pairs (OPS-3427/3428, OPS-3456/3457) need a human merge-and-close. Out of scope for the code.

## Background: what exists today

| Thing | Where | Why it matters |
|---|---|---|
| `CorrelationStore` | `chat_orchestrator/orchestrator/services/ticketing/correlation_store.py` | Every method is ref-keyed and touches dropped columns. Phase A rewrites it. |
| `AlertCorrelator.decide()` | `ticketing/correlator.py:423` | The ladder: replay → flag → no-candidates → signature rungs → LLM + guardrails. |
| `_find_signature_duplicate` / `_find_signature_only_duplicate` | `correlator.py:251` / `:268` | The only two deterministic rungs. Both require an *exact* re-fire. |
| `normalize_subject` / `derive_component` / `derive_signature` | `ticketing/alert_facts.py:128` / `:100` / `:156` | Findings 1 and 2 above live here. Pure functions, no I/O — cheap to test. |
| `apply_amendment()` | `correlation_render.py:172` | Executes amend/duplicate. Contains the row-missing branch that force-escalates. |
| `render_summary` / `render_description` | `correlation_render.py:80` / `:122` | Pure renders, recomputed from state. `render_description` owns the `[anansi:affected-*]` block. |
| `_amend_delivery` | `orchestrator/api/app.py:1639` | Decides suppress / edit / fresh top-level post. Source of the bare `"Escalated to urgent"`. |
| `_attempt_lock_free_signature_correlation` | `app.py:1806` | Grid-lock-timeout fallback. Runs only the two exact-duplicate rungs. |
| `TicketRepository` | `ticketing/repository.py:34` | Sole ticket writer. `TicketRecord` already carries `grid_name`, `status`, `summary`, `backend`. |
| `DeliveryRepository` | `ticketing/delivery_repository.py` | `record()`, `latest_for_ticket()`, `find_notification()` — replaces cached Telegram coordinates. |
| `TicketUpdateNotifier` | `ticketing/update_notifier.py:72` | Edit-vs-reply placement, `SCROLL_THRESHOLD = 5`. |
| `ChatWatermarkRepository` | `ticketing/chat_watermark.py:40` | Counts messages **chat-wide**. Phase B makes it topic-aware. |
| `UrgentAlertContext` | `orchestrator/services/urgent_alert_context.py` | Lazy live-telemetry lookup behind `⚡ Live output:`. Phase C extends it. |
| Correlation prompt | `shared/prompts/library/ticketing.correlation.prompt` | `overridable: true`, publish restricted to eng. Phase C edits the bundled text. |
| `CorrelationPolicy` | `ticketing/correlation_rules.py:23` | `confidence_floor=0.75`, `llm_timeout_seconds=12`, `grid_lock_timeout_seconds=120`, `open_candidate_window_hours=168`. |
| Grid → Telegram target | `shared/auth/auth_service.py:1276` | Every grid resolves to one shared group + a **topic per grid** — why a chat-wide message count is the wrong denominator. |

**Operator decisions already made** (do not re-litigate): production stays as-is until this ships (no kill-switch flip, no column re-add); cascading failures **merge onto the root-cause ticket** rather than cross-linking; the scroll-gap fix is the real topic-scoped watermark, migration included.

**Commands**

```bash
cd /Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant
```

Run tests from `chat_orchestrator/` (where `pyproject.toml`'s `testpaths = ["tests"]` resolves):

```bash
cd chat_orchestrator && python -m pytest tests/services/ticketing -q
```

**CRITICAL — from CLAUDE.md:** every new file under any `tests/` directory needs `git add -f`; a plain `git add` is a silent no-op that commits nothing and makes CI skip the suite. Same for `docs/superpowers/plans/`. Each phase's gate checks this; do not skip it.

---

## File Structure

**Create:**
- `db/migrations/0016_chat_messages_topic.sql` — `chat_messages.telegram_topic_id` + index + backfill from `chat_sessions`
- `chat_orchestrator/tests/services/ticketing/test_correlation_store_schema_contract.py` — payload keys vs. checked-in schema
- `chat_orchestrator/tests/api/test_notify_alert_storm.py` — burst → one ticket, one escalation, one message

**Modify:** `db/schema/chat_db.sql`; `ticketing/{correlation_store,correlator,correlation_render,alert_facts,repository,service,chat_watermark,update_notifier}.py`; `orchestrator/api/app.py`; `orchestrator/services/urgent_alert_context.py`; `orchestrator/services/supabase_client.py`; `mcp_servers/servers/customer_server/client_grid_status.py`; `shared/prompts/library/ticketing.correlation.prompt`; `shared/config/flag_registry.py`; and the existing tests for each.

---

# Phase A — Restore correlation on the post-0005b schema

One review/verify/commit cycle. Nothing in Phase B or C is meaningfully testable until this lands: a store that cannot read candidates or persist severity makes every grouping and escalation test a test of the fallback path.

**Files:** `db/schema/chat_db.sql`, `correlation_store.py`, `correlator.py`, `correlation_render.py`, `repository.py`, `service.py`, `app.py`, + `test_correlation_store.py`, `test_correlator.py`, `test_correlation_render.py`, `test_notify_ticketing.py`, and the new schema-contract test.

- [ ] **A1: Make the checked-in schema true**

  Apply 0005b's Step 4 to `db/schema/chat_db.sql`: drop the ten columns, make `ticket_id uuid PRIMARY KEY` on `ticket_correlations`, add `ticket_id uuid` to `ticket_correlation_events`, replace `ticket_correlations_grid_idx` with an index on `(last_alert_at DESC)`. Leave the rest of the file alone — a full regeneration is consolidation-plan Task 11's job — and add a one-line comment saying so.

- [ ] **A2: Write the schema-contract guard (expect it to fail)**

  New test: parse the two `CREATE TABLE` blocks out of `db/schema/chat_db.sql`, drive every `CorrelationStore` write method with a fake client that captures payload dicts and `.eq()` filter columns, assert every captured key is a real column. No DB, no network. This is the test that would have caught the incident. Expected now: fails on `grid_name`, `summary_current`, `telegram_message_id`, `status`, `ticket_ref`.

- [ ] **A3: Rewrite the store's own tests around `ticket_id`**

  Replace ref-keyed assertions with UUID-keyed ones, and assert no write payload contains any of `ticket_ref, ticket_backend, grid_name, organization_id, summary_current, status, telegram_chat_id, telegram_topic_id, telegram_message_id`.

- [ ] **A4: Convert `CorrelationStore` to `ticket_id`**

  - `get_correlation`, `record_amendment`, `merge_affected_key`, `bump_occurrence` take `ticket_id: str` and filter `.eq("ticket_id", …)`. Delete `_correlation_filter` — the lookup-then-fall-back-to-ref dance is exactly what mis-targeted rows.
  - `record_amendment` loses `summary_current`, writes only `severity` / `escalated_at`, and **returns its real success or failure** (B3 depends on that).
  - `upsert_correlation` requires `ticket_id`, upserts `on_conflict="ticket_id"`, and keeps only `ticket_id, root_cause_kind, primary_signature, signatures, affected_keys, summary_base, description_base, severity, last_alert_at`.
  - `open_candidates_for_grid` becomes two explicit reads: `tickets` (`grid_name=eq.<grid>`, `status=in.(open,in_progress)`, `provisioning_state=eq.active`, selecting `id, ticket_ref, backend, summary, status`), then `ticket_correlations` (`ticket_id=in.(<ids>)`, `last_alert_at=gte.<since>`, ordered desc, limited). Return each correlation row **merged with its ticket fields under the keys the correlator already reads** (`ticket_ref`, `ticket_backend`, `summary_current` ← `tickets.summary`, `status`, `grid_name`) so A6 stays small. Two queries, not a PostgREST embedded join — `.in_()` is already used with this client (`work_packet_service.py:232`) and it degrades cleanly when the first query is empty.
  - Delete `record_message_id` (Telegram coordinates are `DeliveryRepository`'s; the notify path already writes a `purpose="notification"` receipt) and `mark_closed` (status lives on `tickets`, whose sole writer is `TicketRepository`). Move the events methods to `ticket_id`, rename `record_event_ticket_ref` → `record_event_ticket_id`, and keep `grid_name` on the events table — 0005b left it there deliberately as event-time evidence.

- [ ] **A5: Add external-ticket adoption to `TicketRepository`**

  `adopt_external(ref, backend, summary, grid_name) -> TicketRecord`: return the existing row if `ticket_ref` maps to one, else insert with `created_via="adopted"`, `provisioning_state="active"`. Idempotent. This guarantees a `ticket_id` exists for a Jira-search-discovered candidate *before* any Anansi mutation.

- [ ] **A6: Carry `ticket_id` through the correlator and renderer**

  - `CandidateSummary` and `CorrelationDecision` each gain `ticket_id: Optional[str]`; `ticket_ref` stays for display, links, and comments. In `_assemble_candidates`, store rows already carry the id; every `find_open_by_grid` ref not in the store goes through `adopt_external`. A candidate with no resolvable id is dropped with a warning — it cannot be amended safely.
  - **Delete the "correlation row missing" branch** (`correlation_render.py:236-329`). With an id always present the row can be upserted unconditionally, so ~100 lines and their unconditional `escalated=True` disappear. Order becomes: ensure row → merge affected key → bump occurrence → read state → render → push to backend → comment → `record_amendment`.
  - `render_summary` takes an explicit `grid_name: str` from `TicketRecord.grid_name` — the direct fix for `"3 MPPTs in  affected"`. Add a regression test asserting a non-empty grid name.

- [ ] **A7: Make `TicketService.update_ticket` persist canonical state**

  It currently only calls the backend (`service.py:428-444`), so `tickets.summary` goes stale after every amend — unacceptable now that `tickets.summary` is what replaced `summary_current` and is what candidate assembly and severity inference read. Write through `TicketRepository.update_by_ref` as well; keep the backend call authoritative for success/failure.

- [ ] **A8: Rewire the notify handler**

  `_record_new_correlation` passes the freshly created `ticket_id`; `_finalize_correlation_decision` and `_attempt_lock_free_signature_correlation` pass `ticket_id` into `apply_amendment`; the reply/edit target comes from `DeliveryRepository.latest_for_ticket(ticket_id)` instead of `AmendmentResult.telegram_message_id`; delete the `store.record_message_id` call in `_deliver_notification`; the replay path resolves `event.ticket_id` → ref via `TicketRepository`.

- [ ] **A9: Make a degraded store impossible to miss**

  The incident ran ~12 hours emitting five WARNING lines per alert, and its only *visible* effect was extra noise. Add a module-level counter keyed by `(method, error_code)`; log the full WARNING on first occurrence then at most hourly per key with the suppressed count. Include `correlation_store_failures_last_hour` in the `/health` payload, and add `"correlation_degraded": true` to the `/chat/notify` response `extra` when the counter is non-zero so n8n can see it too.

- [ ] **A10: Phase A gate**

  ```bash
  cd chat_orchestrator && python -m pytest tests/services/ticketing tests/api/test_notify_ticketing.py -q
  ruff check orchestrator/services/ticketing orchestrator/api/app.py
  ```

  A2's guard turning green is the signal the cutover is real. Then commit:

  ```bash
  git add db chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests/services/ticketing chat_orchestrator/tests/api/test_notify_ticketing.py
  git commit -m "refactor(ticketing): key alert correlation by canonical ticket id"
  ```

---

# Phase B — One incident, one ticket, one message

One review/verify/commit cycle covering operator problems 1, 2, and 3 plus the affected-equipment request. These belong together: the signature fix and the component-detection fix are interdependent (see findings 1 and 2 — shipping either alone makes grouping worse), and the escalation and placement fixes are what turn correct grouping into a single readable message.

**Files:** `alert_facts.py`, `correlator.py`, `correlation_render.py`, `chat_watermark.py`, `update_notifier.py`, `supabase_client.py`, `app.py`, `db/migrations/0016_chat_messages_topic.sql`, `db/schema/chat_db.sql`, + `test_alert_facts.py`, `test_correlator.py`, `test_correlation_render.py`, `test_chat_watermark.py`, `test_update_notifier.py`, `test_notify_ticketing.py`, `test_notify_alert_storm.py`.

- [ ] **B1: Fix the signature so one fault on N devices is one shape**

  Write the failing tests first, from the real subjects in finding 1: all six Akinsolu `No BMS` MPPT subjects must produce **one** signature, and both Ogbinbiri `Solar Charger [278]` / `[279]` subjects must too — while a *different* fault text on the same device still differs.

  In `normalize_subject`, stop relying on the synthesized `component_key` appearing verbatim. Mask device identity structurally instead: replace a trailing quoted device segment (`on '<anything>'` → `on '#'`, the VRM `ALERT - '<grid>': '<fault>' on '<device>'` shape) and substitute `_MPPT_PATTERN` / `_DCU_PATTERN` matches with `mppt #` / `dcu #`. Keep the existing key-removal as a fallback for subjects where the key *does* appear literally. The fault text survives, so grouping stays inside one fault type; device identity is `component_key`'s job, which is the point.

  Note the deploy effect in the docstring: historical `signatures` arrays were computed with the old algorithm, so the first alert of each family after deploy will not match its own ticket's stored signature — it goes to the LLM once, then converges as `merge_affected_key` folds the new signature in.

- [ ] **B2: Detect Victron "Solar Charger" devices as MPPTs**

  `_MPPT_PATTERN` requires the literal word `MPPT`, so `'Solar Charger [278]'` and `'Solar Charger - VT6Y … House 4 [8]'` parse as component-less and get swallowed by the keyless duplicate rung (finding 2). Add a `Solar Charger[ - <TOKEN>]… [<n>]` pattern, tried after `_MPPT_PATTERN` and before `_DCU_PATTERN`, yielding kind `mppt` with key `TOKEN#n` when a token is present and `n` when it is not. Tests: `[278]` → `mppt/278`; `VT6Y … [8]` → `mppt/VT6Y#8`; a subject with neither still returns `("", "", "")`.

- [ ] **B3: Add the deterministic "same shape, new component" rung**

  With B1 in place this is what actually collapses a storm onto one ticket. `_find_signature_amend(candidates, alert)` returns the first candidate carrying `alert.signature` when the alert has a `component_kind`/`component_key` that no entry in that candidate's `affected_keys` matches. Ladder order in `decide()`: exact duplicate → keyless duplicate → **signature amend** → LLM. `amended_summary=""` (the renderer recomputes), `confidence=1.0`, `decided_by="signature"`. Extract the three rungs and their reason strings into one helper shared with `_attempt_lock_free_signature_correlation`, so a grid-lock timeout groups too and the duplicated reason-string blocks at `app.py:1862-1903` go away. Assert in tests that the LLM gateway is never invoked on this path.

- [ ] **B4: Escalate once, only after it persists, and say something**

  Four defects, one behaviour:
  - `apply_amendment` captures `persisted = await store.record_amendment(...)` and sets `AmendmentResult.escalated = escalate_now and persisted`, logging at WARNING when a persist failure suppresses a notification. State we could not persist must not be announced, or it will be announced again on the next alert.
  - If the correlation row already has `escalated_at`, never emit another escalation *notification* (the Highest-priority push stays idempotent and harmless). Document that a deliberate de-escalate-then-re-escalate is out of scope.
  - Add `effective_candidate_severity(candidate)` to `correlator.py` — stored `severity`, else `derive_severity(candidate.summary)`, else `"urgent"` when the summary starts with `🔴` — and use it in `_apply_guardrails`, the signature rungs, and the lock-free path, so a blank severity on a Jira-discovered candidate can no longer masquerade as a warning→urgent transition.
  - In `_amend_delivery`, delete the contentless `"Escalated to urgent"` branch: post `escalated to urgent — {rendered_summary}`, falling back to the ticket's live summary, and strip any leading `🔴` before `_format_ticket_update_notification` adds its own — that pair produced `"🔴 OPS-3428 — 🔴 ! Urgent: …"`.

- [ ] **B5: Lead every description with the affected-equipment list**

  `render_description` emits `MARKER_START … MARKER_END` first, then a blank line, then `description_base`. Keep the markers and keep recomputing the whole block from state — that is what makes an amend idempotent. Seed it at first filing too (`_record_new_correlation` already knows the single affected key), so the layout does not change shape between a ticket's first and second alert; a grid-level alert with no identifiable component keeps a bare description. Confirm the Jira ADF conversion handles a leading `[anansi:affected-start]` line and bullet lines without treating them as a code fence; add a conversion test if none covers it.

- [ ] **B6: Count the topic, not the whole group**

  `messages_since` compares the newest id in the **chat** against the anchor, but every grid is a *topic* in one shared group — production ids ran 65876→65882 in 40 seconds across five grids, so any anchor is "more than five messages back" within seconds while the operator's own topic sat silent all day. That is why *in progress* posted a fresh reply.
  - Migration `0016_chat_messages_topic.sql`: add `chat_messages.telegram_topic_id text`, index `(group_id, telegram_topic_id, telegram_message_id DESC)`, backfill from `chat_sessions.telegram_topic_id` via `session_id`. Idempotent, like every migration in `db/migrations/`. Mirror into `db/schema/chat_db.sql`.
  - Populate it in `supabase_client.save_messages` from the session row — one choke point, every writer benefits. Separately, `_log_notification_to_chat_db` (`app.py:1029`) never passes `group_id`, so the bot's own alerts are invisible to the watermark's `chat_messages` read; pass it.
  - `head(chat_id, topic_id=None)` / `messages_since(chat_id, anchor_message_id, topic_id=None)` filter `chat_messages.telegram_topic_id` and `message_deliveries.external_topic_id` when a topic is given, keeping chat-wide behaviour when it is not. Rewrite the module docstring, which currently justifies chat-wide counting. `TicketUpdateNotifier._notify_inner` already reads `anchor["external_topic_id"]` — hand it through.
  - Tests: traffic in a *different* topic of the same chat does not scroll the anchor (→ edit in place); same-topic traffic does (→ fresh reply); a no-topic anchor keeps today's behaviour.

- [ ] **B7: Burst regression test**

  New `chat_orchestrator/tests/api/test_notify_alert_storm.py`, built from the real Akinsolu subjects: six `'#67 - No BMS'` MPPT alerts on distinct devices plus the component-less `Solar Charger - VT6Y … [8]`, arriving back-to-back on one grid. Assert exactly one ticket, seven occurrences, all seven components in `affected_keys` (VT6Y included, thanks to B2), exactly one escalation delivery, one Telegram message edited in place rather than seven posts, the equipment list at the top of the description, and zero LLM calls.

- [ ] **B8: Phase B gate**

  ```bash
  cd chat_orchestrator && python -m pytest tests -q
  ruff check orchestrator shared
  ```

  ```bash
  git add db chat_orchestrator/orchestrator
  git add -f chat_orchestrator/tests
  git commit -m "fix(ticketing): group one fault on many devices into one ticket and one message"
  ```

---

# Phase C — Cascade intelligence, then rollout

One review/verify/commit cycle. Battery voltage ships here rather than as its own change because it is what makes the cascade decidable: the alert text is literally *"Inverter Off while battery Ok >52V"*, and once `llm_facts` carries the real voltage the model can check that claim instead of guessing.

**Files:** `client_grid_status.py`, `urgent_alert_context.py`, `ticketing.correlation.prompt`, `flag_registry.py`, `correlator.py`, `correlation_render.py`, `app.py`, + `test_urgent_alert_context.py`, `test_correlator.py`, `test_correlation_render.py`, `test_jira_backend.py`, `test_notify_ticketing.py`.

- [ ] **C1: Report battery voltage wherever live output is reported**

  `BatteryStatus.voltage_v` already exists (`platforms/base_platform.py:31`, from the VRM `BatterySummary` widget's `V` code). Add `get_live_telemetry(grid_name)` beside `get_live_inverter_output` (`client_grid_status.py:40`): resolve the site once, `asyncio.gather(get_current_inverter_voltage(sid), get_current_battery_status(sid), return_exceptions=True)`, apply the existing 30-minute staleness rule to each field independently, return `{output_kw, battery_voltage_v}` with either field `None` when unavailable. Keep `get_live_inverter_output` as a thin wrapper so existing callers are untouched. Rename `LiveOutputLookup` → `LiveTelemetryLookup` (still one cached task per request, still bounded by `URGENT_ALERT_LIVE_OUTPUT_TIMEOUT_SECONDS`, still `None` on timeout rather than raising). `telegram_output_line()` → `⚡ Live output: 0.0 kW · 🔋 Battery: 51.8 V`, each half degrading independently: unknown output keeps `⚡ Live output: unavailable`, unknown battery omits the clause rather than printing "unavailable" twice. `llm_facts()` gains `battery_voltage_v` (omitted when unknown), reaching both the correlation prompt and `jira_backend`'s `operational_context`.

- [ ] **C2: Add the kill switch**

  `ALERT_CASCADE_MERGE_ENABLED`, default `False`, `group="ticketing"`, `depends_on="ALERT_CORRELATION_ENABLED"`, label "Merge cascading equipment failures onto the root-cause ticket". Off = today's behaviour exactly.

- [ ] **C3: Teach the prompt the failure topology**

  Add a `# Failure Topology` section naming the chains that make a same-grid, short-window pair causally related — battery/BMS communication loss ⇒ inverter protective shutdown ⇒ grid outage; grid off ⇒ MPPT low production, DCU/base-station down, token-delivery drop; combiner or string fault ⇒ MPPT underproduction — each with a `root_cause_kind: "power_chain"` instruction, a ~30-minute same-grid window, and the direction of causation (the *earlier* ticket is the parent; a later, more severe symptom still amends onto it). Tell the model to use the live `battery_voltage_v` fact when the alert makes a battery claim. Rescope the existing "usually unrelated" line and Example 4 to pairs *not* on a named chain, and add a worked example built from OPS-3456/OPS-3457 with the model's own 0.9-confidence rejection as the counter-example. Add `"power_chain"` to the `root_cause_kind` enum in `_build_prompt`'s response-schema line (`correlator.py:346`).

- [ ] **C4: Guard the new freedom in code**

  In `_apply_guardrails`, an amend whose `affected_key.kind` differs from every kind already on the target ticket is allowed only when `root_cause_kind == "power_chain"` **and** `ALERT_CASCADE_MERGE_ENABLED` **and** confidence ≥ `confidence_floor`; otherwise force `new` (today's outcome). `power_chain` must **not** join `_ROOT_CAUSE_KINDS_REQUIRING_PARENT` — the parent already exists as a real ticket, and if no candidate represents the root cause, fall back to `new` rather than synthesising a parent.

- [ ] **C5: Render and deliver a merged cascade honestly**

  `render_summary` picks a dominant kind today, which would produce `"2 Inverters in Ogbinbiri affected"` for a mixed-kind ticket. When `affected_keys` spans more than one kind, render root-cause-led instead: the ticket's `summary_base` (severity marker preserved, upgraded to `! Urgent:` if any folded symptom is urgent) followed by `— +N dependent alert(s) (<kind labels>)`. Prefix a folded symptom's ticket comment with `Folded in as a power_chain symptom:` so the second repair stays legible on one ticket — this is the mitigation for the one real cost of merging over linking. A cascade merge must not be suppressed: post the LLM's `update_message` (falling back to the rendered summary) against the root ticket's anchor, so the operator gets one threaded update instead of two unrelated urgent pings.

- [ ] **C6: Tests**

  BMS→inverter fixture with the flag on → `amend` onto OPS-3456, one delivery, mixed-kind summary as specified. Flag off → `new`. Independent MPPT + inverter fault with `is_hps_on: true` and no topology claim → `new`. Cross-kind amend at confidence 0.6 → `new`. Snapshot the mixed-kind summary and the folded-comment prefix. Battery-voltage line: both halves present, each half missing.

- [ ] **C7: Phase C gate**

  ```bash
  cd chat_orchestrator && python -m pytest tests -q
  python -m pytest tests/test_prompt_parity.py -q
  ```

  `test_prompt_parity.py` snapshots prompt text in `prompt_checksums.json` — regenerate it in the same commit. Per CLAUDE.md, if a "bundled"/parity test fails locally for reasons unrelated to your edit, check `chat_orchestrator/.env` for live credentials before suspecting the codebase.

  ```bash
  git add shared chat_orchestrator/orchestrator mcp_servers
  git add -f chat_orchestrator/tests
  git commit -m "feat(ticketing): merge power-chain cascades and report battery voltage"
  ```

- [ ] **C8: Rollout**

  ```bash
  pre-commit run --all-files
  git show --stat HEAD && git log --oneline main..HEAD
  ```

  `pre-commit run --all-files` is the only check that catches a new `tests/` file a plain `git add` silently dropped; if `test-wiring` reports untracked test files, vet them for operator data, `git add -f` each, and re-run until clean. Every new test file and this plan document must appear in the log.

  After the images build and App Platform picks them up:

  ```bash
  doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 500 | grep -c "correlation store:"
  ```

  Expected: `0`. Then watch one real storm end to end — one ticket, one escalation message, an edited card rather than a new one. Leave `ALERT_CASCADE_MERGE_ENABLED` **off** through this deploy; read a day of `ticket_correlation_events` (`decision`, `root_cause_kind`, `confidence`, `reason`, `llm_raw`) for would-be `power_chain` merges, then enable it once the model's judgment looks right on real traffic.

---

## Known limitations left standing

- **Existing sibling tickets need a human.** OPS-3427/OPS-3428 and OPS-3456/OPS-3457 are duplicate pairs for one fault each; Phase B stops new splits but does not merge old ones. Close one of each pair by hand, or every later alert on those grids keeps choosing between them.
- **Signature change is a one-time discontinuity.** After B1 deploys, the first alert of each family does not match its own ticket's stored signature and takes the LLM path once before converging.
- **The per-grid correlation lock is in-process only** (`app.py:1277`). Correct at `instance_count: 1` (confirmed in `.do/app.image.example.yaml`); above that a burst can still split. Follow-up remains a `grid_correlation_leases` table with a short-TTL lease.
- **`db/schema/chat_db.sql` stays partially reconciled** with production (A1 covers the correlation tables, B6 the new column; archived legacy tables, dropped `chat_sessions` escalation columns, and `ticket_list_view` are still stale). Consolidation-plan Task 11 owns the full regeneration.
- **A deliberate de-escalation followed by re-escalation** posts nothing the second time, by design (B4).
- **No burst debounce.** Seven components in 40 seconds produce one ticket and one message edited seven times; a quiet-window debounce was considered and dropped as unnecessary once editing works.
