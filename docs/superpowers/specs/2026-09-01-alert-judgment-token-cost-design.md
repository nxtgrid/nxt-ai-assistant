# Cutting Alert-Judgment Token Cost Design

## Goal

Reduce the LLM token cost of the `/chat/notify` `ticket_id="auto"` correlation
judgment, without weakening correlation quality or the fail-open delivery
guarantee. Alert traffic is high-volume and mostly ends in suppression, so
the judgment call is several times more expensive in aggregate than answering
a user chat turn.

Three coordinated changes, in descending order of saving:

1. **Skip the LLM entirely on an exact-signature re-fire** — a deterministic
   match against an open ticket is decided without a model call at all, the
   same way the legacy `AlertCorrelator.decide()` ladder already does.
2. **Make the model emit less on the calls that remain** — the send decision
   is produced first, and the three operator-facing free-text fields shrink
   or go null when the alert is not being sent.
3. **Verify the ~3,400-token system prompt is being implicitly cached** —
   instrument the four token counts per judgment, decide from a week of data
   whether explicit caching is worth building.

Thinking budget is deliberately left untouched (see Non-goals).

## Scope

Applies only to `/chat/notify` requests whose `ticket_id` is `""` or
`"auto"` and that reach `_resolve_notify_ticket_llm_judgment`
(`ALERT_CORRELATION_ENABLED` and `ALERT_LLM_JUDGMENT_ENABLED` both on).
Passthrough notifications (`ticket_id` omitted), explicit ticket-ref
comment/close requests, and the deterministic-ladder path
(`ALERT_LLM_JUDGMENT_ENABLED` off) keep their current behaviour.

## Non-goals

- **Capping the thinking budget.** `judge()` keeps `thinking` unset, so the
  model uses its own dynamic budget. This is independent of the input context
  it is handed (unchanged here). If the per-judgment `thinking_tokens` that
  change 3 starts recording show runaway values, revisit with a *generous*
  cap (~2048), never a hard off.
- **Trimming the judgment context** (open-ticket count, description length,
  prior-alert / O&M-message limits). A separate lever, not in this change.
- **Explicit Gemini context caching.** Change 3 is measurement only. If the
  data says implicit caching is not hitting, explicit caching gets its own
  spec (it needs a cache-lifecycle module and an invalidation hook on the
  admin Prompts "publish override" path).

## Current behaviour and gaps

`_resolve_notify_ticket_llm_judgment` (`chat_orchestrator/orchestrator/api/app.py`)
does, per unique auto-correlated alert:

1. `correlator.replay_decision(dedup_key)` — the only pre-LLM exit. Keys on an
   exact `dedup_key` match, which catches a retried webhook but not a fresh
   periodic re-fire of the same fault.
2. `correlator._assemble_candidates(grid_name)`.
3. `AlertJudgmentContextAssembler.assemble(...)` — DB reads for up to 20 prior
   alerts, up to 50 O&M topic messages, live telemetry, open-ticket dumps.
4. `correlator.judge(grid_name, alert, context)` — one `gateway.generate()`
   with the ~3,400-token `ticketing.correlation` system prompt plus the
   per-alert context as the user message. Always called on any non-replay
   alert.
5. `to_legacy_correlation_decision(...)`, then an optional deterministic
   *backstop*: if the model said `new` and `ALERT_DETERMINISTIC_BACKSTOP_ENABLED`
   is on, `find_deterministic_decision` can overrule it with an exact
   signature match.
6. `decide_alert_delivery(...)` (deterministic), audit write, then
   `_file_uncorrelated_ticket` / `_finalize_correlation_decision`.
7. When the alert is suppressed, `grid_impact.summary` and
   `likely_user_action.summary` are still computed onto the delivery, then
   discarded for rendering (kept only in the audit row).

Gaps this change closes:

- **G1.** An exact-signature re-fire already on an open ticket runs the full
  context assembly and a model call, then step 5's backstop frequently throws
  the model's `ticket` section away. The legacy `decide()` path short-circuits
  this exact case *before* the model; the judgment path does not.
- **G2.** The model always writes `grid_impact.summary` (≤500),
  `notification.reason` (≤500), `likely_user_action.summary` (≤500) and
  `ticket.reason` (≤500) in full. On a suppressed alert — the common case —
  the first three are not shown to anyone.
- **G3.** `judge()` drops `GenerateResult.usage` on the floor. The gateway
  logs `tokens in/out/thinking/cached` but not attributably to correlation,
  so there is no way to tell whether the stable system-prompt prefix is being
  implicitly cached.

## Design

### Change 1 — pre-LLM exact-signature short-circuit

**Where.** `_resolve_notify_ticket_llm_judgment`, immediately after
`_assemble_candidates` succeeds (`app.py:2592`) and before the
`AlertJudgmentContextAssembler` block (`app.py:2602`).

**What.** Call `find_deterministic_decision(candidates, alert)` — the same
function the legacy ladder (`correlator.decide()`) and the post-LLM backstop
already use. It returns a `CorrelationDecision` (`confidence=1.0`,
`decided_by="signature"`) for its three rungs (exact signature+component
duplicate, keyless signature-only duplicate, signature-amend onto a new
component), or `None`.

- **Non-`None`** → run the exact two-step the legacy `_resolve_notify_ticket_auto`
  path already runs for a deterministic decision, and return:
  1. `await correlator._finalize(target.grid_name, alert, body.dedup_key, decision)`
     — writes the `ticket_correlation_events` audit row (`decided_by="signature"`,
     `confidence=1.0`, `llm_raw=None`, `judgment=None`) and returns `decision`
     unchanged.
  2. `return await _finalize_correlation_decision(body, target, alert,
     alert_context, store, ticket_service, decision)` — executes the
     `amend`/`duplicate` via `apply_amendment` (occurrence-counter bump,
     in-place Telegram edit / escalation for amend, normally-suppressed
     `_duplicate_delivery` for duplicate) and produces the `(ref, None, extra,
     delivery)` 4-tuple, where `extra` already carries `decision`,
     `correlated_with`, `confidence`, `decided_by`.

  No context assembly, no `AlertJudgmentContextAssembler`, no `judge()` call.
- **`None`** → unchanged: assemble context → `judge()` → backstop →
  `decide_alert_delivery` → file/finalise.

This reuses `_finalize` + `_finalize_correlation_decision` verbatim rather
than open-coding a `record_event` call or a bespoke `NotificationDelivery`,
so occurrence counting, the downtime-floor / fail-open un-suppression of a
duplicate, and the response contract all stay identical to the legacy path.
`judgment_valid` / `send_decision` / `send_force_reasons` keys are simply
absent from `extra` on this path (there was no judgment) — the n8n caller
already treats them as optional.

**Rationale.** This is not new authority — the legacy ladder and the existing
backstop already trust a `confidence=1.0` exact signature match over the
model. Moving the check *ahead* of the model removes, for that case: one
`gateway.generate()` call, the ~3,400-token prompt, and the DB reads for 20
prior alerts + 50 O&M messages + telemetry. The post-LLM backstop stays in
place for the no-match path as defence-in-depth.

**Gated by `ALERT_DETERMINISTIC_BACKSTOP_ENABLED`.** The pre-LLM check runs
only when that flag is on (its default). With it off the flag's stated
contract is "the judgment is the last word" — so an exact re-fire is judged,
not pre-empted, exactly as the post-LLM backstop is also skipped. One flag,
one meaning: *a deterministic signature match takes precedence over the
model* — whether that match is applied before the call or after it.

**Interaction with `replay_decision`.** Unchanged and still first — a
`dedup_key` replay is even cheaper (no candidate assembly). The signature
check runs only when replay misses.

### Change 2 — conditional output verbosity

The send decision *is* the model's output, produced in the same call, so the
only way to make the model write less when suppressing is to have it commit
to `send_telegram` before it writes any summary. Generation is left-to-right,
so field order in the required JSON is the mechanism.

**Prompt** (`shared/prompts/library/ticketing.correlation.prompt`, edited in
place — no new prompt id):

- Reorder the required output object so `notification` (`send_telegram`,
  `reason`) is **first**, ahead of `grid_impact`, `ticket`,
  `likely_user_action`. Update the "Give four independent answers" sentence to
  match the new order.
- Add an explicit verbosity rule:
  - When `send_telegram` is `false`: `notification.reason` = `null`;
    `likely_user_action` = `{ "category": "none", "summary": null,
    "confidence": <c> }`; `grid_impact.summary` ≤ 12 words — an audit tag,
    shown to no one.
  - When `send_telegram` is `true`: all three written in full for the
    operator, exactly as today.
- `ticket.reason`: cap at ~25 words in all cases; a `record_occurrence` may
  be a terse phrase.
- Update the inline JSON skeleton (currently lines 64-69) and the Example
  blocks so the bundled prompt still demonstrates a parseable object in the
  new field order.

**Schema** (`chat_orchestrator/orchestrator/services/ticketing/alert_judgment.py`):

- `NotificationJudgment.reason: str | None` — was `str`, `min_length=1`,
  `max_length=500`. Now `Field(default=None, max_length=500)`.
- `LikelyUserAction.summary: str | None` — same change.
- `GridImpact.summary` — unchanged (`str`, `min_length=1`, `max_length=500`);
  brevity is prompt-enforced, not schema-enforced, so a wordy suppressed
  summary costs a few tokens but never fails a judgment.
- `parse_alert_judgment` guardrails:
  - Keep `impact.material_status_change and not notification.send_telegram`
    → `inconsistent_notification`.
  - Add `notification.send_telegram and not _has_text(notification.reason)`
    → `missing_notification_reason` (a sent alert must still carry a
    rationale). A `judgment` object is passed through on this rejection, per
    the existing `_invalid` salvage contract.
  - No guardrail forces null/brevity when `send_telegram` is false — the
    prompt asks for it; over-writing is not a correctness failure.

**Consumers — no code change required, verified:**

- `app.py` delivery enrichment already null-safe:
  `(impact.summary or "").strip()`, `action.summary or ""`
  (`app.py:2706-2716`).
- `to_legacy_correlation_decision` reads `ticket.reason` only, never
  `notification.reason` / `likely_user_action.summary`
  (`correlator.py:777`).
- `decide_alert_delivery` reads `judgment.notification.send_telegram` only
  (`alert_delivery_policy.py:280`).
- Audit `judgment=judgment.judgment.model_dump(mode="json")` serialises
  `None` fields without complaint.

### Change 3 — implicit-cache verification

The judgment call is already shaped for Gemini implicit caching: a stable
`system` message (`PROMPTS.render("ticketing.correlation").system_text`,
~3,400 tokens, above the Flash ~1,024-token minimum) as the leading prefix,
all per-alert data in the `user` message. No structural change needed;
Change 2's reorder is inside the system-prompt blob and is a one-time version
bump.

**Instrument:**

- Add `usage: Usage | None = None` to `AlertJudgmentResult`
  (`alert_judgment.py`). Populate it in `AlertCorrelator.judge()` from
  `response.usage`; leave `None` on the timeout / `llm_failed` paths.
- In `_resolve_notify_ticket_llm_judgment`, after `judge()` returns, emit one
  structured log line:
  `logger.info("alert_judgment_tokens grid={} in={} out={} thinking={} cached={} valid={}", ...)`
  drawn from `judgment.usage`. Greppable in `doctl` run logs, attributable to
  correlation (unlike the gateway's generic `Gemini ...` metrics line).

**Decide (operational, ~1 week after deploy):** grep the log line.

- `cached` regularly ≈ system-prompt size on non-first alerts → implicit
  caching is working; close this out.
- `cached` regularly ≈ 0 → alert cadence is below the implicit-cache TTL;
  open a follow-up spec for explicit `client.caches.create()` caching with a
  publish-override bust hook.

Persisting the counts to `ticket_correlation_events` is intentionally *not*
done here — it needs a column migration (which does not auto-apply to prod,
per `db-migrations-need-manual-apply`) for a one-week measurement. If durable
analytics are wanted later, add a `token_usage jsonb` column then.

## Testing

New / updated tests, all under `chat_orchestrator/tests/` (existing tracked
files where possible; any new file needs `git add -f` per `CONTRIBUTING.md`):

**Change 1** — `tests/api/test_notify_ticketing.py`,
`tests/api/test_notify_alert_storm.py`:

- Exact signature+component match against an open ticket → response
  `decision="duplicate"`, delivery suppressed, **mock gateway `.generate`
  asserted not called**, one `record_event` row with `decided_by="signature"`.
- Same-fault new-component signature → `decision="amend"`,
  `_finalize_correlation_decision` reached, gateway not called.
- Exact match + urgent severity increase → amend/escalate path, gateway not
  called.
- No signature match → `judge()` is called, existing behaviour unchanged
  (regression guard).
- `dedup_key` replay still short-circuits ahead of the signature check.

**Change 2** — `tests/services/ticketing/test_alert_judgment.py`:

- `notification.reason = null` accepted when `send_telegram=false`; rejected
  (`missing_notification_reason`) when `send_telegram=true`.
- `likely_user_action.summary = null` accepted; round-trips through
  `model_dump(mode="json")`.
- Existing `inconsistent_notification` / `inconsistent_site_status` /
  ticket-guardrail cases still pass.
- Prompt-contract test: the bundled prompt's JSON skeleton and every Example
  block parse under the reordered schema.

**Change 3** — `tests/services/ticketing/test_correlator.py`,
`tests/services/ticketing/test_alert_judgment.py`:

- `judge()` populates `AlertJudgmentResult.usage` from the gateway response;
  `None` on the timeout / failure paths.

Full `chat_orchestrator` correlation suite stays green;
`pre-commit run --all-files` before pushing.

## Risks

- **Reorder shifts send/no-send quality.** The model commits to
  `send_telegram` before emitting the impact prose. Mitigation: the thinking
  phase still reasons over the whole context before any output token; the
  `material_status_change` ↔ `send_telegram` ↔ site-status guardrails still
  cross-check; compare decisions on a batch of recorded alerts before/after.
- **Short-circuit suppresses a real change.** Only fires on `confidence=1.0`
  exact signature (± component) matches to an *already open* ticket — the
  same matches the legacy path and backstop already act on. An urgent
  severity increase routes to amend/escalate, not suppress.
- **Implicit caching may not hit.** That is the reason Change 3 is
  measure-first rather than assumed.
