# LLM-First Alert Correlation and Delivery Design

## Goal

Make one LLM judgment the authoritative semantic decision for every unique
auto-correlated alert, while retaining deterministic correlation rules as
evidence, enriching the judgment with live grid and operational history, and
making Telegram suppression strictly fail-open.

The change applies to `/chat/notify` requests whose `ticket_id` is blank or
`"auto"`. Unticketed passthrough notifications (`ticket_id` omitted) and
explicit ticket-reference comment/close requests keep their current contract.

## Approved product decisions

1. The LLM is called once for every unique auto-correlated alert, including
   exact signature matches and alerts for grids with no open tickets.
2. Existing deterministic correlation code continues to run, but it produces
   findings for the LLM rather than a final `new`/`amend`/`duplicate` decision.
3. The LLM sees current inverter output, battery voltage, and all three phase
   voltages when generation is managed. When generation is not managed, it
   sees that explicit fact instead of a telemetry error.
4. The LLM also sees prior delivered alert messages, current open tickets,
   and timestamped messages from the grid's exact O&M Telegram topic, bounded
   to the correlation window and prompt budget.
5. Optional-context failures do not stop ticket processing or delivery.
6. The LLM returns the grid impact, material-change judgment, ticket-change
   judgment, likely user action, and Telegram-send judgment as separate JSON
   fields.
7. Telegram is suppressed only after a valid explicit LLM decision not to
   send. LLM, validation, or required-context failures force delivery.
8. Even a valid LLM suppression is overridden when fresh readings show
   L1=L2=L3=`0 V` and the last successfully delivered alert for the grid is
   older than eight hours, or no prior delivery exists.
9. The existing `ticketing.correlation` prompt is edited in place. No second
   prompt ID or parallel correlation prompt is introduced.

## Current behavior and gaps

`AlertCorrelator.decide()` currently follows a short-circuit ladder:
dedup-key replay, feature flag, no candidates, deterministic signature
duplicate/amend, and only then an LLM call. Consequently, the LLM never sees
the most common repeated-alert paths and never judges their operational
materiality.

The LLM prompt currently receives operational grid facts, open candidate
summaries, and live telemetry only when the deterministic ladder reaches the
LLM. The live helper exposes total inverter output and battery voltage, but
the underlying VRM result's L1/L2/L3 voltages are discarded. Open candidate
descriptions are also available from ticket backends but are not carried into
`CandidateSummary` or the prompt.

The bundled prompt has a second gap: its frontmatter declares only
`sections: [system_instructions]`, while Root Cause Rules, Failure Topology,
Component Taxonomy, and Examples are separate level-one headings. Prompt
rendering moves those headings into `context_text`, but
`get_correlation_instructions()` returns only `system_text`; the model is
therefore not receiving those policies today despite their presence in the
file.

Telegram delivery is decided later by `_amend_delivery()` and
`_duplicate_delivery()`. A duplicate is always silent; an amendment is silent
unless a component was added, severity escalated, or a power-chain cascade was
folded. This means code, not the LLM, currently makes the final suppression
decision.

The two existing sources of send history are insufficient as an authoritative
eight-hour clock. `chat_messages` logging is best-effort and skipped when the
target has no existing session. `message_deliveries` is ticket/escalation
owned and does not cover unticketed or ticket-failure alert sends. Neither is
a complete grid-alert delivery ledger.

## Architecture

### One semantic judgment per unique alert

The auto-correlation path becomes:

1. resolve the canonical grid target;
2. acquire the existing per-grid correlation lock;
3. assemble a typed, best-effort `AlertJudgmentContext`;
4. call the LLM once with that context and the incoming alert;
5. parse and validate a typed `AlertJudgment`;
6. apply the requested ticket action, subject to structural safety checks;
7. calculate the final Telegram send decision using the fail-open gate;
8. schedule Telegram delivery and persist its actual outcome.

The LLM call owns the semantic judgment. Code still owns authentication,
timeouts, schema validation, candidate-reference validation, ticket-backend
operations, idempotency, safe rendering, and fail-open behavior.

An exact retry carrying an already-recorded `dedup_key` is not a new alert.
It replays the stored judgment and applied outcome rather than calling the LLM
or mutating a ticket twice. The emergency correlation kill switch remains an
intentional operational exception: disabling the feature bypasses semantic
suppression and takes the existing create-new-and-send fail-open path.

### Context assembly boundary

Add a focused context assembler under the ticketing service rather than
growing `app.py` or placing additional I/O inside prompt rendering. It returns
the data and a per-source availability manifest:

```python
class ContextStatus(str, Enum):
    available = "available"
    empty = "empty"
    unmanaged = "unmanaged"
    failed = "failed"
    timed_out = "timed_out"


class ContextSourceResult(BaseModel):
    status: ContextStatus
    item_count: int = 0
    detail: str = ""


class AlertJudgmentContext(BaseModel):
    deterministic_findings: list[DeterministicFinding]
    open_tickets: list[OpenTicketContext]
    telemetry: AlertTelemetry
    prior_alerts: list[PriorAlertMessage]
    om_messages: list[OMChatMessage]
    availability: dict[str, ContextSourceResult]
```

Independent providers run concurrently with their own timeouts. Their
exceptions are caught at the provider boundary and converted to `failed` or
`timed_out`; the assembler itself returns a context object whenever possible.
The existing seven-day (`168h`) correlation window is shared by candidate,
prior-alert, and O&M-history reads. Prompt data is capped at 15 tickets, 20
prior alert messages, and 50 O&M messages. Message bodies are capped at 500
characters and ticket descriptions at 2,000 characters before prompt
assembly.

`empty` is a successful read returning no data. `unmanaged` is a successful
managed-generation lookup that says NXT does not manage the plant. Neither is
a degradation and neither independently prevents suppression. `failed` and
`timed_out` are degradations and make suppression unsafe.

### Deterministic findings, not decisions

Replace `find_deterministic_decision()` with a pure findings function. It
evaluates the same facts without returning a final disposition:

- exact signature and exact component match;
- exact signature with no component key;
- exact signature on a newly affected component;
- warning-to-urgent severity increase;
- signature overlap per candidate;
- component-kind match or cross-kind relationship;
- known root-cause and power-chain indicators.

Each `DeterministicFinding` names the candidate reference, finding kind,
facts used, and a factual explanation. Findings must not contain imperative
phrasing such as "choose duplicate" or pre-populate the final ticket action.
They are serialized in their own prompt section so the LLM can use or reject
them explicitly.

The lock-timeout helper must not resurrect the old deterministic final
decision. On lock timeout the process attempts the LLM with a best-effort
unlocked snapshot, marks the lock source as failed, and therefore forces
Telegram delivery. Ticket execution then uses the judgment only when its
target can still be validated; otherwise it creates a new ticket fail-open.

### Telemetry contract

Extend the existing request-local telemetry lookup rather than introduce a
second VRM fetch. The customer client's live operation returns:

```python
class LiveAlertTelemetry(TypedDict):
    generation_management: Literal["managed", "unmanaged", "unknown"]
    output_kw: float | None
    battery_voltage_v: float | None
    l1_voltage_v: float | None
    l2_voltage_v: float | None
    l3_voltage_v: float | None
    observed_at: str | None
    fresh: bool
```

Managed-generation state and the VRM site ID are resolved in one database
read. An unmanaged plant returns `generation_management="unmanaged"` without
calling VRM. A missing grid/site mapping or lookup error returns `unknown`,
not `unmanaged`, so infrastructure failure cannot masquerade as a business
fact.

For managed plants, inverter output and phase voltages come from the existing
current inverter-voltage call; battery voltage comes from the existing
battery-status call. `observed_at` is the gateway report timestamp used by
the current 30-minute freshness rule. Stale phase/output data is marked
`fresh=false` and is not eligible for the zero-voltage override. Battery
voltage retains its current independent availability semantics.

The request-local lookup remains cached: prompt assembly, Jira issue-type
selection, Telegram rendering, and the delivery override consume the same
observation without duplicate VRM calls.

### Open-ticket context and ticket actions

`CandidateSummary` gains the ticket description. Both correlation-store and
backend-discovered candidates populate it from the canonical ticket record or
backend summary. The LLM receives reference, backend, title, bounded
description, status, age, severity, affected equipment, occurrence count,
signatures, and root-cause kind.

The judgment requests one ticket action:

- `create_new`: create a new ticket using the incoming alert as the safe base;
- `update_existing`: update the named open candidate;
- `record_occurrence`: correlate to the named candidate without changing its
  title or description.

`update_existing` separately specifies whether title and/or description
should change, a bounded proposed title, and a bounded factual description
addition. Code validates that the target is one of the offered candidates.
It never accepts an LLM-provided URL. It uses the existing correlation state
and renderers to preserve affected-component markers and severity behavior.
A requested title change becomes `amended_summary` input to the existing
summary renderer. A requested description change appends the bounded addition
to `ticket_correlations.description_base` before re-rendering the full
description, so it really changes both the canonical ticket projection and
the owning backend without replacing or erasing prior description content.
The ordinary raw-alert private comment is still added as event history.

Malformed actions, invented refs, a low-confidence mutation, or a failed
ticket operation degrade to the existing safe behavior: attempt to create a
new ticket and send the alert. If all ticket backends fail, Telegram still
sends an unlinked alert.

### LLM JSON contract

The existing `shared/prompts/library/ticketing.correlation.prompt` is updated
in place to describe the richer context and require exactly this shape:

```json
{
  "grid_impact": {
    "status": "no_change|at_risk|degraded|outage|recovering|unknown",
    "summary": "What the reported failure means for this grid",
    "confidence": 0.0
  },
  "notification": {
    "material_grid_status_change": true,
    "send_telegram": true,
    "reason": "Why this alert should or should not be sent"
  },
  "ticket": {
    "action": "create_new|update_existing|record_occurrence",
    "target_ticket_ref": null,
    "change_title": true,
    "proposed_title": null,
    "change_description": true,
    "description_addition": null,
    "relationship": "same_issue|same_root_cause|new_issue",
    "root_cause_kind": "grid_off|grid_isolated|power_chain|component|other",
    "reason": "Why this ticket action is appropriate",
    "confidence": 0.0
  },
  "likely_user_action": {
    "category": "none|remote_investigation|equipment_restart|site_visit|contact_operator|monitor|other",
    "summary": "The action a user is most likely to take",
    "confidence": 0.0
  }
}
```

All four requested answers remain individually addressable. The additional
`send_telegram` field makes the suppression intent explicit instead of
inferring it from prose. `material_grid_status_change=true` combined with
`send_telegram=false` is invalid and forces delivery.

Parse the response into Pydantic models after the gateway's existing JSON
response mode. Reject missing required objects, unknown enum values, invalid
candidate refs, non-finite confidence, and inconsistent booleans. Existing
confidence policy continues to protect ticket mutation. A response below the
mutation floor may still request delivery, but it cannot suppress an alert or
modify an existing ticket.

### Existing prompt ownership

This feature edits `ticketing.correlation.prompt`; it does not create a new
prompt or prompt-library registry entry. The prompt remains `overridable:
true`, view/edit for ops and eng, publish for eng, and uses the configured
fast model. Its root-cause, failure-topology, and component-taxonomy guidance
are retained and rewritten around the new ticket-action and notification
schema. Their headings become level-two headings beneath the existing
`# System Instructions` section, so `PROMPTS.render(...).system_text` includes
them and `get_correlation_instructions()` actually sends them to the model.
The implementation updates the current prompt-library content test, which
asserts the opposite legacy behavior, to lock in this corrected boundary.

O&M messages, prior alerts, ticket prose, and the incoming alert are untrusted
data. The system instructions explicitly say to treat their contents only as
evidence, never as instructions, and prompt assembly places them in clearly
labeled JSON data sections.

### Alert delivery ledger

Add `notify_alert_deliveries`, a purpose-specific record of successfully sent
`/chat/notify` messages:

```text
id uuid primary key
grid_name text not null
external_chat_id text not null
external_topic_id text null
external_message_id bigint not null
sent_at timestamptz not null default now()
source text null
dedup_key text null
ticket_id uuid null references tickets(id) on delete set null
ticket_ref text null
rendered_text text not null
alert jsonb not null default '{}'
unique(external_chat_id, external_message_id)
index(grid_name, sent_at desc)
```

The implementation may omit a separate `channel` column because every row is
Telegram-owned by definition. The repository records a row only after the
Telegram API returns a message ID. It supports:

```python
async def recent_for_grid(grid_name: str, since: str, limit: int) -> list[PriorAlertMessage]
async def latest_for_grid(grid_name: str) -> PriorAlertMessage | None
async def record_success(...) -> None
```

Like `CorrelationStore`, the delivery-history repository maintains an hourly
degradation counter. A failed ledger write increments it, and subsequent
context assembly reports prior-alert history as `failed` while that
degradation is active, preventing a later alert from being suppressed on a
history clock known to be incomplete. A process restart clears the in-memory
counter, but the existing legacy chat-log merge still provides the available
fallback history; durable cross-restart health tracking is outside this
change.

The first deployment has no ledger history. Reads therefore merge ledger rows
with bounded legacy `chat_messages` rows tagged
`metadata.channel="notify_endpoint"`, deduplicating by chat/message ID. New
successful sends always write both the ledger and the existing best-effort
chat log. Ledger-write failure after a Telegram success is observable but
cannot undo delivery; because the last-send clock is then uncertain, the next
alert cannot be safely suppressed.

### O&M chat context

Add a narrow read-only repository method keyed by the already-resolved
`GridNotificationTarget.chat_id` and `topic_id`. It reads only that O&M topic,
not organization DMs, developer chat, logbook, escalation chat, or sibling
grid topics. Rows include timestamp, sender/role, and bounded content.

Prior alert messages appear in their own context section. The O&M section
excludes rows tagged as notify-endpoint alerts to avoid duplicating the same
text and spending prompt budget twice. Archived and blank messages are
excluded. A missing session or a successful zero-row query is `empty`; a
database exception is `failed`.

### Final Telegram decision

Suppression is an allow-list operation. Define `DeliveryDecision` with
`send`, `reason`, `forced_by`, and the validated judgment. Code sets
`send=false` only when all of the following are true:

1. the LLM call completed within its timeout;
2. the full response parsed and validated;
3. deterministic findings, open-ticket context, managed-generation/telemetry,
   prior-alert history, and O&M history have no `failed`/`timed_out` status;
4. `material_grid_status_change` is false;
5. `send_telegram` is false;
6. the zero-voltage/eight-hour override is false.

Any other state sends. In particular, no candidates, no history, and an
unmanaged plant are valid non-failure states and do not by themselves force a
message.

The zero-voltage override is true only when all of these hold:

- generation is managed;
- telemetry is fresh;
- L1, L2, and L3 are all present and each numerically equals `0.0`;
- the prior-delivery lookup succeeded; and
- the most recent successful grid alert is more than eight hours old, or the
  successful lookup found no prior alert.

If telemetry or history lookup fails, the general fail-open rule already
sends. A missing phase does not independently prove an all-phase outage.

When a judgment requests `update_existing`, every sent message includes that
ticket as a code-generated link. A newly created ticket is linked as today.
For `record_occurrence`, the selected ticket may be linked when a message is
forced or explicitly requested, but silence remains possible only through the
six-condition gate above.

### Audit and observability

Extend the correlation event audit to retain the structured judgment,
context-availability manifest, intended send decision, and force reasons in
addition to the raw LLM response. Actual delivery remains a separate fact in
`notify_alert_deliveries` because `_deliver_notification` runs in a background
task after the HTTP 202 response.

Add structured counters/log fields for:

- LLM called, succeeded, timed out, malformed, or replayed;
- context source status and latency;
- ticket action requested, accepted, rejected, or failed;
- Telegram intended send/suppress and force reason;
- Telegram transport success/failure;
- delivery-ledger write success/failure.

The `/chat/notify` response continues to expose correlation degradation and
adds `send_decision`, `send_forced`, and the non-sensitive force reason. It
must not expose O&M content, raw prompts, credentials, or VRM payloads.

## Failure behavior matrix

| Condition | Ticket behavior | Telegram behavior |
|---|---|---|
| Valid judgment, `send_telegram=true` | Apply validated ticket action | Send |
| Valid judgment, material change | Apply validated ticket action | Send |
| Valid judgment, explicit suppression, all context healthy | Apply validated ticket action | Suppress unless outage override |
| All phases fresh zero and last send >8h/none | Apply validated ticket action | Send |
| One context provider fails/times out | Continue; apply only a still-valid action | Send |
| LLM timeout/transport/malformed JSON | Create new ticket if possible | Send |
| LLM invents/targets a closed ticket | Create new ticket if possible | Send |
| Ticket update/create fails | Preserve audit; do not block delivery | Send unlinked or with an accurately labeled related-ticket link |
| Telegram transport fails | Ticket/audit remain | Log and count failure; no false delivery-ledger row |
| Delivery-ledger write fails after send | Ticket/audit remain | Message is already sent; next suppression is unsafe |
| Exact dedup-key retry | Replay prior applied judgment | Do not double-send or double-mutate |

## Rollout and compatibility

Ship behind a versioned `ALERT_LLM_JUDGMENT_ENABLED` flag and a separate
`ALERT_LLM_SUPPRESSION_ENFORCED` flag while retaining
`ALERT_CORRELATION_ENABLED` as the master kill switch. The existing prompt is
edited in place, so the new typed parser and a legacy-decision adapter must
ship in the same change: when judgment is disabled, the current short-circuit
ladder stays active and any alert that reaches the LLM maps the new ticket
object back to the existing `CorrelationDecision`. This keeps legacy control
flow behavior-compatible without requiring a second prompt.

When judgment is enabled and suppression enforcement is disabled, all unique
auto-correlated alerts use the new call/context path but Telegram always
sends; the model's would-send result is audit-only. When both flags are
enabled, the full suppression gate becomes active.

Roll out first with shadow suppression: call the new pipeline and audit its
send recommendation, but continue sending every alert. After production data
shows valid context completeness, parse rate, latency, and ticket-action
quality, enable enforcement so only validated explicit suppressions are
withheld. The eight-hour outage override and all fail-open paths are active in
both stages.

Prompt publication follows the existing prompt-library workflow. Because a
published database override can supersede the bundled prompt text, rollout
must update or retire any live override for `ticketing.correlation`; otherwise
deploying the edited file alone will not change the effective production
instructions.

## Verification

Tests must prove:

1. the LLM is called for exact signature/component matches, keyless signature
   matches, new-component signature matches, and zero-candidate alerts;
2. deterministic matches appear as findings and never short-circuit or become
   final decisions on their own;
3. open ticket descriptions, live output, battery voltage, phase voltages,
   prior alerts, and timestamped O&M messages reach the existing prompt;
4. unmanaged generation is represented explicitly and does not count as a
   failed context provider;
5. each context provider can independently fail or time out while the LLM and
   ticket pipeline continue;
6. any provider failure, LLM failure, schema failure, low-confidence
   suppression, or inconsistent material/send combination forces delivery;
7. only a valid `material=false` plus `send=false` judgment with healthy
   context suppresses;
8. fresh L1=L2=L3 zero overrides that suppression after eight hours or with no
   prior send, but not before eight hours, with stale data, a missing phase, or
   a non-zero phase;
9. the selected existing ticket or new ticket is rendered as a trusted link;
10. successful Telegram sends create ledger rows and suppressed/failed sends
    do not;
11. exact dedup-key retries neither re-call the LLM nor double-mutate/deliver;
12. prompt text keeps the `ticketing.correlation` ID, permissions, topology
    guidance, and adds the approved JSON/output and untrusted-data rules;
13. context caps and redaction prevent oversized prompts and instruction-like
    O&M content from escaping its data section;
14. the existing correlation, ticket rendering, notify endpoint, telemetry,
    prompt-library, and schema-contract suites remain green.

## Out of scope

- Changing passthrough or explicit-ticket `/chat/notify` behavior.
- Asking the LLM to generate or choose ticket URLs.
- Replacing the current ticket backends or Telegram transport.
- Backfilling all historical Telegram alerts when no reliable message record
  exists.
- Using DMs, developer chat, logbook, escalation chat, or sibling grid topics
  as O&M correlation evidence.
- Treating missing/stale phase readings as proof that all phases are at zero.
- Removing the emergency correlation kill switch.
