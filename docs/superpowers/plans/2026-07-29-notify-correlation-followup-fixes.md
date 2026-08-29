# /notify Correlation Follow-Up Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three behaviors observed in production after the 2026-07-28 alert-noise-root-cause fix (PR #36) shipped: (a) a burst of concurrent alerts on one grid can still file duplicate tickets, (b) a non-urgent alert amendment can incorrectly escalate a ticket to urgent, and (c) every ticket amendment posts a brand-new Telegram reply instead of updating one running message.

**Architecture:** No change to the correlation decision pipeline itself. This plan repairs three narrower defects: an unanchored severity regex, a lock-timeout fallback that skips correlation instead of degrading gracefully, and a Telegram delivery mode that always sends instead of sometimes editing.

**Tech Stack:** Python 3.11, FastAPI, asyncio, Supabase/postgrest, Telegram Bot API, pytest.

---

## Background: what was diagnosed

Verified against the code on `main` at commit `1030366f` (the build that produced the 2026-07-29 Belel/GridW Telegram screenshots):

1. **`derive_severity()` is unanchored.** `alert_facts.py:90-96` is documented as
   reading "the n8n `! Urgent:`/`! Warning:` convention" but implements it with
   `_SEVERITY_PATTERN = re.compile(r"\burgent\b")` / `_WARNING_PATTERN =
   re.compile(r"\bwarning\b")` matched with `.search()` — i.e. it matches the
   bare word "urgent" *anywhere* in the subject, not just as the leading
   marker. The same file already has the correct anchored pattern
   (`_LEADING_MARKER`, `alert_facts.py:33`, used by `normalize_subject`) — this
   function just doesn't use it. `alert.severity` computed this way feeds
   directly into `severity_increased_to_urgent` in `apply_amendment`
   (`correlation_render.py:345-348`), which is the only thing that flips a
   ticket to escalated/urgent on an amend. A DCU/BMS-type fault description
   that happens to mention "urgent" anywhere in its free text will wrongly
   escalate the ticket it's merged into.
2. **The grid-lock timeout fallback skips correlation entirely.**
   `_acquire_grid_correlation_lock` (`app.py:1096-1122`) correctly serializes
   `decide()` + ticket-creation + correlation-record per grid within this
   single-process deployment (`instance_count: 1`, no `--workers`, confirmed in
   `.do/app.example.yaml` and `chat_orchestrator/project.yml`). But when the
   45s budget (`correlation_rules.py:38`) is exceeded, `app.py:1533-1541` falls
   through to `_file_uncorrelated_ticket` (`app.py:1251-1290`), which never
   calls the correlator and never checks `open_candidates_for_grid` — it
   blindly files a new ticket. The 45s ceiling is reachable on its own terms
   (a single holder's worst case is already LLM 12s + ticket-backend HTTP
   calls bounded up to 30s each) and trivially reachable under a burst, where
   N alerts on one grid queue linearly behind the same lock. A burst of DCU
   alerts on Belel is exactly this scenario.
3. **Amendments always post a new reply message.** `_amend_delivery`
   (`app.py:1454-1480`) posts a fresh `reply_to_message_id`-anchored message
   for every `component_added` amendment; only `escalated` is treated as
   worth a genuinely new top-level post. There is no message-editing call
   anywhere in `shared/utils/telegram_send.py` — every send is a new message.
   Three components joining one ticket in three minutes currently means three
   separate Telegram messages under the original alert.

---

## Global Constraints

- `/chat/notify`'s ticket-resolution step (which includes the grid lock) is
  deliberately synchronous in the request/response cycle — see the comment at
  `app.py:1932-1935`. Any change to lock/timeout behavior must keep the
  caller's worst-case wait bounded and documented; it must not become
  unbounded.
- Every alert must still result in either a new ticket or an update to an
  existing one. Nothing in this plan may introduce a path where an alert is
  dropped.
- An urgent severity increase must still reach Telegram as a top-level post,
  under every code path this plan touches.
- Editing a Telegram message must never lose an update if the edit fails
  (deleted message, migrated chat, API error): fall back to sending a new
  message rather than dropping the notification.
- No database migration — reuse the existing `ticket_correlations.telegram_message_id`
  column as the "message to edit" pointer; no new column.
- New dataclass fields must have defaults so existing test constructors keep
  working.
- Per `CLAUDE.md`: run `pre-commit run --all-files` before claiming anything is
  committed, and `git add -f` any new file under a `tests/` directory.

---

## Task 0: Surface the swallowed Jira fallback error

**Fixes:** (d) diagnosability of "some tickets skip Jira despite `NOTIFY_TICKETS_BACKEND=auto`".

**Background:** Production logs (`doctl apps logs`, `anansi-bot` component,
2026-07-29) show four occurrences of:

```
WARNING | orchestrator.api.app:_create_notify_ticket:1189 - Notify: Jira ticket creation failed; created internal fallback %s
```

The literal `%s` is never substituted -- `shared/utils/logging.py:10` uses
`from loguru import logger`, and loguru formats messages with `str.format()`
(`{}` placeholders), not `%`-style. Worse, even fixing that would show
nothing useful: `create_ticket_with_internal_fallback`
(`chat_orchestrator/orchestrator/services/ticketing/service.py:210-229`)
catches the actual Jira exception as `primary_error` when Jira fails and the
internal fallback succeeds, but never attaches it to the returned
`TicketCreateOutcome` -- `TicketCreateOutcome.error`
(`chat_orchestrator/orchestrator/services/ticketing/backend.py:73-82`) is
only ever populated when *both* backends fail. The one piece of information
that would explain why Jira creation is failing is currently discarded
before it reaches any log.

- [ ] **Step 1: Write the failing test**

  In `chat_orchestrator/tests/services/ticketing/test_service.py` (or
  `test_service_resolve_backend.py` -- check which file already covers
  `create_ticket_with_internal_fallback`), add a test that makes a fake Jira
  backend's `create_ticket` raise `TicketBackendError("field X is required")`
  and a fake internal backend succeed. Assert the returned
  `TicketCreateOutcome.error` contains that message (not `None`) even though
  `outcome.result` is populated and `outcome.fallback_used` is `True`.

- [ ] **Step 2: Run it and verify it fails**

- [ ] **Step 3: Propagate the error**

  In `service.py`'s `create_ticket_with_internal_fallback`, change the
  fallback-succeeded return to carry the reason:

  ```python
  return TicketCreateOutcome(
      result=canonical_result,
      error=f"Jira: {primary_error}",
      fallback_used=True,
  )
  ```

- [ ] **Step 4: Fix the log call to actually log something**

  In `app.py`'s `_create_notify_ticket`, change the `%s`-style call to
  loguru's `{}`-style and include the reason:

  ```python
  if outcome.fallback_used:
      logger.warning(
          "Notify: Jira ticket creation failed; created internal fallback {} ({})",
          outcome.result.ref,
          outcome.error,
      )
  ```

  While here, grep `chat_orchestrator/` and `shared/` for other `logger.*`
  calls using `%s`/`%d`-style placeholders (this may not be the only one) --
  note any found in the commit message or a follow-up, but only fix ones
  directly adjacent to this change; a repo-wide sweep is out of scope for
  this task.

- [ ] **Step 5: Run the ticketing and notify suites**

- [ ] **Step 6: Commit** -- `fix: surface the Jira fallback reason instead of discarding it`

---

## Task 1: Anchor `derive_severity()` to the leading marker

**Fixes:** (b) non-urgent amendment escalating a ticket.

- [ ] **Step 1: Write the failing tests**

  In `chat_orchestrator/tests/services/ticketing/test_alert_facts.py`,
  `TestDeriveSeverity`, add:

  ```python
  def test_ignores_urgent_word_elsewhere_in_subject(self):
      assert (
          derive_severity("! Warning: DCU 862406008 needs urgent attention in Belel !")
          == "warning"
      )

  def test_ignores_warning_word_elsewhere_in_subject(self):
      assert (
          derive_severity("! Urgent: Battery fault, disregard prior warning !")
          == "urgent"
      )

  def test_bare_word_with_no_marker_is_unclassified(self):
      assert derive_severity("This is urgent, please check Belel") == ""
  ```

  Keep the four existing `TestDeriveSeverity` tests unchanged — the fix must
  not break `test_case_insensitive` (`"URGENT: something"`, no leading `!`).

- [ ] **Step 2: Run them and verify they fail**

  `pytest tests/services/ticketing/test_alert_facts.py -k TestDeriveSeverity -v`
  — the two new "ignores ... elsewhere" tests must fail against current
  `derive_severity` (both currently return `"urgent"`/mismatch because of the
  unanchored search).

- [ ] **Step 3: Anchor the pattern**

  In `alert_facts.py`, replace `_SEVERITY_PATTERN`/`_WARNING_PATTERN` usage in
  `derive_severity` with a single anchored pattern that allows the optional
  leading `!` (required to keep `test_case_insensitive` passing):

  ```python
  _LEADING_SEVERITY = re.compile(r"^\s*!?\s*(urgent|warning)\s*:", re.IGNORECASE)

  def derive_severity(subject: str) -> str:
      """"urgent" | "warning" | "" from the n8n "! Urgent:"/"! Warning:" convention.

      Anchored to the *leading* marker only -- a fault description that
      mentions "urgent" or "warning" elsewhere in free text must not be
      misclassified.
      """
      match = _LEADING_SEVERITY.match(subject or "")
      return match.group(1).lower() if match else ""
  ```

  Leave `_SEVERITY_PATTERN`/`_WARNING_PATTERN` in place only if another
  function in the file still uses them (grep first); delete them if
  `derive_severity` was their only caller.

- [ ] **Step 4: Run the alert-facts suite**

  `pytest tests/services/ticketing/test_alert_facts.py -v` — all green,
  including the untouched `test_case_insensitive`.

- [ ] **Step 5: Commit**

  `git add -f` is not needed (existing tracked file). Commit message: `fix:
  anchor derive_severity to the leading "! Urgent:"/"! Warning:" marker`.

---

## Task 2: Stop the grid-lock timeout from skipping correlation

**Fixes:** (a) concurrent alerts on a busy grid filing duplicate tickets.

- [ ] **Step 1: Write the failing test**

  In `chat_orchestrator/tests/api/test_notify_ticketing.py`, add a test
  alongside `test_lock_timeout_falls_back_to_plain_create` that pre-seeds an
  open candidate for the grid (via the existing `_RecordingCorrelationStore`
  fixture / `_patch_recording_store` helper — check how other tests seed
  `open_candidates_for_grid` return values, e.g. around
  `test_signature_duplicate` or similar deterministic-rung tests) with a
  matching `(signature, component_key)`, then patches
  `_acquire_grid_correlation_lock` to `_never_available` as the existing test
  does. Assert that the resolved `ref` is the *existing* candidate's ref (an
  amend/duplicate), not a freshly minted `TKT-000001`, and that
  `extra["decided_by"]` reflects a best-effort deterministic match (e.g.
  `"fallback_signature"`), not `"fallback"`.

  Also add a test for the still-no-match case (no candidate matches): confirm
  it still falls back to `_file_uncorrelated_ticket` exactly as
  `test_lock_timeout_falls_back_to_plain_create` already verifies today — this
  is a regression guard, not a new assertion.

- [ ] **Step 2: Run them and verify the new one fails**

  `pytest tests/api/test_notify_ticketing.py -k lock_timeout -v`

- [ ] **Step 3: Add a lock-free deterministic-only correlation attempt**

  In `app.py`, factor a helper (or extend the lock-timeout branch at
  `app.py:1533-1541`) that, before calling `_file_uncorrelated_ticket`:
  1. Calls `store.open_candidates_for_grid(grid_name, since_iso, limit=...)`
     directly (no lock needed — this is a read plus pure computation, not a
     decide-and-create sequence) using the same `open_candidate_window_hours`/
     `maximum_candidate_count` policy values `AlertCorrelator` uses.
  2. Runs only the deterministic rungs — `_find_signature_duplicate` /
     `_find_signature_only_duplicate` from `correlator.py` — against those
     candidates. **Do not** call the LLM here: the whole point of this path is
     to stay fast and lock-free.
  3. If a match is found, route it through `apply_amendment` exactly like the
     signature-rung path inside `AlertCorrelator.decide()` does, and record the
     correlation event the same way `_finalize` does.
  4. If no match, fall through to `_file_uncorrelated_ticket` exactly as
     today.

  Import the rung functions from `correlator.py` (they're already
  module-level, not methods) rather than duplicating the matching logic.

- [ ] **Step 4: Widen the lock's own budget**

  In `correlation_rules.py`, raise `grid_lock_timeout_seconds` from `45` to
  `120`, with a comment explaining the reasoning: a single holder's own worst
  case (LLM 12s + bounded ticket-backend HTTP calls) can already approach 45s,
  and `/chat/notify`'s ticket-resolution step is synchronous in the caller's
  request cycle (`app.py:1932-1935`) — 120s keeps a multi-alert burst queued
  and correlated rather than bailing out, while still bounding the caller's
  worst-case wait to a fixed, documented ceiling. Update the existing test
  that asserts the exact timeout value passed to the lock
  (`test_lock_timeout_falls_back_to_plain_create` reads
  `DEFAULT_CORRELATION_POLICY.grid_lock_timeout_seconds`, so it doesn't need a
  literal change, just re-verify it still passes).

- [ ] **Step 5: Run the ticketing and notify suites**

  `pytest tests/services/ticketing/ tests/api/test_notify_ticketing.py -v`

- [ ] **Step 6: Commit**

  `fix: attempt deterministic correlation before filing a blind ticket on
  grid-lock timeout, and widen the lock's own budget`

---

## Task 3: Edit amendments in place; only escalation posts a new message

**Fixes:** (c) one Telegram message per ticket instead of one per amendment.

- [ ] **Step 1: Write the failing test for the new send-layer primitive**

  In `shared/tests/test_telegram_send.py`, add tests for a new
  `edit_telegram_message(bot_token, chat_id, message_id, text, *,
  parse_mode=None) -> bool`:
  - Successful edit returns `True` and calls Telegram's
    `editMessageText` with `chat_id`, `message_id`, `text`, `parse_mode`.
  - Telegram's "message is not modified" 400 response (this happens when the
    new text is byte-identical to the old — a legitimate no-op, not a
    failure) returns `True`.
  - Any other non-2xx/error response returns `False` and does not raise.

- [ ] **Step 2: Run it and verify it fails**

  `pytest ../shared/tests/test_telegram_send.py -k edit -v` (adjust path per
  actual pytest rootdir/conftest setup — confirm with the existing
  `test_telegram_send.py` invocation pattern first).

- [ ] **Step 3: Implement `edit_telegram_message`**

  In `shared/utils/telegram_send.py`, add the function following the same
  session/error-handling shape as `send_telegram_message_raw`
  (`telegram_send.py:119`) but calling `.../editMessageText`. Treat
  `description` containing "message is not modified" on a 400 response as a
  success (idempotent no-op), matching how `is_markdown_parse_error`
  (`telegram_send.py:104`) already special-cases a specific Telegram error
  string.

- [ ] **Step 4: Run the telegram_send suite**

  Confirm the whole file is green, not just the new tests.

- [ ] **Step 5: Commit** — `feat: add edit_telegram_message to the Telegram send layer`

- [ ] **Step 6: Write the failing tests for the render layer**

  In `chat_orchestrator/tests/services/ticketing/test_correlation_render.py`,
  add assertions to (or extend) the existing `apply_amendment` amend-path
  tests: `AmendmentResult` gets a new `rendered_summary: str = ""` field that
  carries the exact `new_summary` text already computed and pushed to the
  ticket backend at `correlation_render.py:341,367-372`. Assert it's populated
  on both the amend path (`correlation_render.py:383-393`) and the
  Jira-only-seed urgent path (`correlation_render.py:311-321`, using
  `final_summary`).

- [ ] **Step 7: Run them and verify they fail**

- [ ] **Step 8: Add the field**

  Add `rendered_summary: str = ""` to `AmendmentResult`
  (`correlation_render.py:154-168`) and populate it at both construction
  sites noted above.

- [ ] **Step 9: Run the render suite**

- [ ] **Step 10: Commit** — `feat: carry the rendered ticket summary through AmendmentResult`

- [ ] **Step 11: Write the failing tests for delivery mode**

  In `chat_orchestrator/tests/api/test_notify_ticketing.py`, extend the
  `_amend_delivery` tests (around line 1795-1930):
  - `test_amend_delivery_names_new_component_and_distinct_total` (or a new
    test) must assert the returned `NotificationDelivery` has
    `edit_message_id == 555` (the ticket's tracked `telegram_message_id`) and
    `text_override == amendment.rendered_summary`, not the old
    `f"Added {label} (...)"` phrasing. Keep `reply_to_message_id == 555` too,
    as the edit-failure fallback anchor.
  - A new test for the escalation branch: `_amend_delivery` with
    `escalated=True` must set `record_message_id_for_ticket_ref=ticket.ref`
    (it doesn't today) so a future amend's edit target moves to the new
    top-level post, not the stale original message.
  - In `test_notify_ticketing.py`'s `_deliver_notification`-level tests
    (search for the `FakeTelegramSend`/`_send` fixtures around line
    1420-1450), add: when `delivery.edit_message_id` is set and the fake edit
    call returns `True`, no new `send`/reply call happens. When it returns
    `False`, a fallback `send` call happens using `reply_to_message_id` as the
    anchor, and `record_message_id_for_ticket_ref` behavior is unaffected.

- [ ] **Step 12: Run them and verify they fail**

- [ ] **Step 13: Add `edit_message_id` to `NotificationDelivery`**

  Add `edit_message_id: Optional[int] = None` to `NotificationDelivery`
  (`app.py:1381-1398`).

- [ ] **Step 14: Update `_amend_delivery`**

  In `app.py:1454-1480`:
  - `component_added` branch: use `amendment.rendered_summary` (fall back to
    the existing `f"Added {label} (...)"` phrasing only if `rendered_summary`
    is blank, e.g. the Jira-only-seed path where a full ticket summary isn't
    available) as `text_override`; set `edit_message_id=reply_to` alongside
    the existing `reply_to_message_id=reply_to`.
  - `escalated` branch: keep `top_level=True` (new message, unchanged), and
    add `record_message_id_for_ticket_ref=ticket.ref` so the escalation post
    becomes the new edit target for subsequent amends.

- [ ] **Step 15: Wire the edit attempt into `_deliver_notification`**

  In `app.py`, near the existing send call (`app.py:1016-1023`): if
  `delivery is not None and delivery.edit_message_id`, call
  `edit_telegram_message` first. On success, skip the send call entirely (log
  and return — no new `message_id` to record, the ticket's
  `telegram_message_id` is unchanged, so skip the
  `record_message_id_for_ticket_ref` write too). On failure, fall through to
  the existing `send_telegram_message_with_fallback` call unchanged (which
  already uses `reply_to_message_id` as its anchor).

- [ ] **Step 16: Run the full notify suite**

  `pytest tests/api/test_notify_ticketing.py tests/services/ticketing/ -v`

- [ ] **Step 17: Commit** — `feat: edit the ticket's Telegram message in place for amendments; only escalation posts a new message`

---

## Task 4: Full verification

- [ ] **Step 1: Run the complete chat_orchestrator suite**

  `pytest chat_orchestrator/ -v` (or the project's standard invocation) —
  confirm no regressions outside the touched modules.

- [ ] **Step 2: `pre-commit run --all-files`**

  Per `CLAUDE.md`: this is mandatory before claiming anything is committed.
  Any new file under a `tests/` directory needs `git add -f` (vet for
  operator data first) — this plan doesn't add new test *files*, only extends
  existing ones, so this should be a formality, but do not skip the check.

- [ ] **Step 3: Re-run the touched suites once more to confirm clean**

- [ ] **Step 4: `git add -f` this plan file and commit it**

  This plan file lives under `docs/superpowers/plans/`, which is gitignored
  like `tests/` — force-add it.
