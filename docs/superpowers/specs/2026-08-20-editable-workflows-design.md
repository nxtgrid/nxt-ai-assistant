# Editable workflows: step re-run + real schedule editing

**Date:** 2026-08-20
**Covers:** making an existing workflow's steps re-runnable from the "Edit workflow"
modal, adding a "Does not repeat" schedule option, and making schedule edits (including
removal) actually take effect
**Depends on:** `2026-08-19-skills-lifecycle-and-function-steps-design.md` (the list +
modal this extends) and commit `d839135f` (workflow-first modal, auto-summary)
**Touches:** `anansi_app/nicegui_app/pages/skills.py`,
`anansi_app/nicegui_app/pages/skill_builder.py`, `anansi_app/services/skill_builder_service.py`

---

## Problem

Two things are broken and one thing is missing, all in the same "Edit workflow" modal:

1. **Steps aren't editable.** `_open_editor` (`skills.py:189`) never mounts the builder
   for an existing workflow — it shows "isn't supported here yet" and Save keeps
   `row["steps"]` verbatim. `render_builder`'s `initial_steps` param exists for exactly
   this and has never been wired up.
2. **No "don't repeat" option.** `REPEAT_OPTIONS` (`skills.py:20`) deliberately omits
   it, reasoning that a one-off run doesn't need a persistent schedule row. That
   reasoning breaks down the moment editing needs to *stop* an existing repeat, or an
   operator wants a real future one-time run (e.g. "pull inverter output once, next
   Tuesday") rather than a weekly grind.
3. **Every skill schedule saved through this modal has silently never fired.**
   `SkillBuilderService.set_skill_schedule` (`skill_builder_service.py:319`) builds a
   `cron_expression` but never writes `next_run_at`. `process_due_skill_schedules`
   (`broadcast_scheduler.py:189`) only picks up rows where `next_run_at <= now`.
   Confirmed against the live-prod schema dump (`db/schema/chat_db.sql:543`): no
   default, no trigger fills that column. A `NULL` `next_run_at` never satisfies
   `.lte(...)`, so nothing has ever come due. This isn't a new bug the "None" work
   introduces — it's a precondition for "None" (or anything else here) meaning
   anything at all.

## Design

### 1. Re-running steps

`_open_editor` mounts the real builder for an existing row instead of the placeholder:

```python
state_holder = await render_builder(
    user_email, builder_user_id,
    initial_steps=row.get("steps") or [],
    on_summary_update=_apply_auto_summary,
)
```

Inside `render_builder`, `state["initial_steps"]` is captured once at mount (a
snapshot, never mutated during the session). The "pending tail" — stored steps not yet
re-run — is a **derived value, not tracked state**:

```python
pending_tail = state["initial_steps"][len(state["steps"]):]
```

This is the key mechanism and it's deliberately not a mutable list that gets popped on
send. Slicing by `len(state["steps"])` makes it self-correcting under Rewind for free:
rewinding two live steps back to zero shrinks `state["steps"]` back to `[]`, and the
pending tail immediately re-expands to the full original list — no bookkeeping to keep
in sync, no desync case to test for. A brand-new workflow has `initial_steps == []`, so
`pending_tail` is always `[]` and this whole mechanism is a no-op for that path — one
code path serves both New and Edit.

**Rendering** (`_rebuild_transcript`): live steps render exactly as today, followed by
one card per `pending_tail` entry:

- Muted card: "Step N · not yet re-run", instruction text in a dimmed/italic style.
- If the stored step has a `result_preview`, a small caption: *"Previously retrieved:
  …"* (truncated same as today's transcript display).
- No Rewind button, no tool-name badges, no flag switches — there's no run to act on
  yet. Its flags travel through to Save untouched regardless (see below).
- The first pending card gets a visual "Up next" treatment (accent border/badge) since
  it's the one about to become live.
- A caption above Save, shown only when `pending_tail` is non-empty in edit mode: *"X
  of Y steps re-run — the rest will be saved unchanged."*

**Auto-fill**: at the end of `_refresh_transcript`, if `message_input.value` is
currently empty and `pending_tail` is non-empty, prefill it with
`pending_tail[0]["instruction"]`. "Currently empty" is the guard that keeps this from
fighting the two existing callers:

- After `_send()`: input was just cleared to `""` → prefill applies, primes the next
  pending step. This is the "re-run one by one from top" mechanic.
- After `_rewind()`: input was just set to the rewound step's real sent text (not its
  original stored instruction) → non-empty, prefill is skipped, Rewind's own
  "edit-and-resend exactly this" intent wins, unchanged from today.
- Initial mount: input starts empty → prefill applies in Edit mode, no-ops in New mode.

Sending (edited or not) executes for real against `chat_orchestrator` — this is
re-execution, not transcript replay. There is no "skip" control: advancing past a step
without re-running it only happens by not touching it and Saving early (see below),
matching "one by one from top" literally rather than adding a second way to do the same
thing.

**`_derive_steps_payload`** (used by validate, summarize, and Save alike, so all three
stay consistent) appends `pending_tail` after the derived live steps, renumbering
`index` but passing every other key through unchanged — `name`, `instruction`,
`output_var`, `allow_write`, `is_response_step`, `had_tool_error`, `result_preview`,
and `kind` if present. That last one is what makes a P3 function step in an existing
workflow safe: this builder can't produce or re-run one, but it can't touch one it
didn't produce either — it rides through the pending tail untouched, same as any other
step the user didn't get to.

Two existing pieces of "last step" logic need to key off the *combined* count
(`len(state["steps"]) + len(pending_tail)`), not just live steps, or they misfire
whenever a pending tail exists:

- `_derive_steps_payload`'s `is_last` (forces `is_response_step=True` on the final
  step) — must land on the true final step of the combined sequence.
- `_render_step`'s response-switch disable — a live step must only be treated as "the
  final step, toggle locked" when nothing pending follows it.

**Save**: with the builder always mounted for an edit, `state_holder.get("steps")` is
never `None` again, so `_open_editor`'s `if state_holder.get("steps") is None: steps =
row.get("steps")` fallback in `skills.py` is dead code — removed. `_derive_steps_payload`
is the single source of truth for both paths now. Opening Edit and clicking Save
without touching anything reproduces `row["steps"]` byte-for-byte (every step is 100%
pending tail) — the same safe no-op as today, just arrived at through the general
mechanism instead of a special case.

`can_promote_to_active`'s existing `had_tool_error` check keeps working unmodified: it
inspects the combined derived list, so a previously-failed step still blocks
`active` until it's actually re-run clean — carrying forward the exact rule the P3 doc
already established for the fresh-build case.

### 2. Schedule editing

`_open_editor` gains a `schedule: Optional[Dict[str, Any]]` parameter — the existing
`user_schedules` row for this skill, or `None`. `refresh()` already computes the full
`schedules` dict before flattening it for the list display (`build_skill_rows`); it now
threads `schedules.get(skill["id"])` through `_render_row` into `_open_editor` instead
of discarding it, so no extra query is needed.

`REPEAT_OPTIONS` becomes `["Does not repeat", "Weekly", "Every other week", "Monthly
(same date)", "Monthly (same weekday)"]` — matching `broadcast.py`'s list and ordering
exactly, including the new default-first "Does not repeat" for a brand-new schedule
(a safer default than defaulting to recurring).

**Prefill** — a new pure function, testable without NiceGUI or a DB:

```python
def schedule_form_defaults(schedule: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """anchor / repeat / first_run string values to preselect when opening Edit."""
```

- No schedule (or inactive) → `{"anchor": "", "repeat": REPEAT_OPTIONS[0], "first_run": ""}`
  — today's blank defaults, unchanged.
- Active schedule → `anchor` from `anchor_entity_type`; `first_run` formatted from
  `next_run_at` (`"%Y-%m-%d %H:%M"`) if set, else `""`; `repeat` reverse-mapped from
  `schedule_type` + `cron_expression`:
  - `schedule_type == "biweekly"` → "Every other week"
  - `schedule_type == "once"` or no `cron_expression` → "Does not repeat"
  - `schedule_type == "recurring"`: inspect the cron's weekday field — contains `#` →
    "Monthly (same weekday)"; day-of-month field isn't `*` → "Monthly (same date)";
    otherwise → "Weekly"

  This only has to round-trip what `_build_recurrence` itself produces — skill
  schedules have exactly one writer — so it doesn't need to handle arbitrary
  hand-written cron.

Leaving the Schedule section untouched on an edit reproduces the same `first_run`
string `schedule_form_defaults` prefilled, so `_build_recurrence` derives the identical
cron it had before — schedule fields are a no-op-when-untouched Save, same principle
as steps.

**Save**:

```python
if anchor_select.value:
    # existing upsert path; set_skill_schedule now accepts "Does not repeat" too
    ...
elif schedule is not None and schedule.get("is_active"):
    # had an ACTIVE one, explicitly cleared it -> remove, don't silently ignore.
    # is_active gates this deliberately: schedule_summaries()
    # (skill_builder_service.py:187) doesn't filter is_active, so `schedule`
    # can be a row that already
    # completed or was previously cancelled. schedule_form_defaults already
    # treats that the same as "no schedule" for prefill -- this check has to
    # agree, or saving a workflow whose one-time run already fired would fire
    # a pointless removal call every single time.
    result = await run.io_bound(lambda: service.remove_skill_schedule(row["id"], actor=user_email))
    if not result.get("success"):
        ui.notify(f"Saved, but removing the schedule failed: {result.get('error')}", type="warning")
# else: no active schedule before, none now -> unchanged no-op
```

### 3. `SkillBuilderService` changes

`set_skill_schedule` accepts `"Does not repeat"` instead of rejecting it, and always
writes `next_run_at` — the fix from Problem #3, applied uniformly:

```python
recurrence = _build_recurrence(when, frequency)
if recurrence is None:
    if frequency != "Does not repeat":
        return {"success": False, "error": f"Could not derive a schedule from '{frequency}'"}
    cron_expression, schedule_type = None, "once"
else:
    cron_expression, schedule_type = recurrence["cron_expression"], recurrence.get("schedule_type", "recurring")

payload = {
    ...  # unchanged: skill_id, command=None, chat_id, created_by_*, anchor_entity_type
    "cron_expression": cron_expression,
    "schedule_type": schedule_type,
    "timezone": recurrence.get("timezone", "UTC") if recurrence else "UTC",
    "next_run_at": when.isoformat(),   # new — this line is the actual fix
    "is_active": True,
    "status": "active",
}
```

For "Does not repeat", `next_run_at = when` and `cron_expression = None` puts the row
in exactly the shape `_advance_skill_schedule` (`broadcast_scheduler.py:276`) already
knows how to handle — `schedule_type not in ("recurring", "biweekly")` fires the
dispatch once and flips `is_active=False, status="completed"`. No dispatcher-side
change needed; that half of the machinery was already built for the general `/schedule`
command path and just needed a skill-created row to reach it correctly formed.

New method, same dict-result convention as the rest of the service:

```python
def remove_skill_schedule(self, skill_id: str, actor: str) -> Dict[str, Any]:
    """Deactivate a skill's schedule (operator turned it off, not a completed
    one-time run — status distinguishes the two, though both are is_active=False
    and equally invisible to process_due_skill_schedules)."""
```

Sets `is_active=False, status="cancelled"` on the matching `user_schedules` row.
`user_schedules.status` has no CHECK constraint (unlike `skills.status`), so
`"cancelled"` needs no migration.

## Failure modes

| failure | behaviour |
|---|---|
| Edit opened, nothing touched, Save clicked | byte-for-byte unchanged steps and schedule (both mechanisms are no-ops when untouched) |
| A pending step's stored `kind` is `"function"` | rides through untouched in the pending tail; never rendered as re-runnable, never sent as chat |
| Promote to `active` with an unre-run step that has `had_tool_error=True` | blocked, same message as today ("Rewind and re-run them before activating") — now also reachable by simply not re-running a step during edit |
| `first_run` blank on an edit with no schedule change | reuses the prefilled value from `schedule_form_defaults`, not an error |
| `anchor_select` cleared to "Not scheduled" where no schedule existed | no-op, unchanged |
| Edit opened on a workflow whose one-time schedule already fired (`is_active=False, status="completed"`) | shown as "Not scheduled" (same as never having had one); Save does not call `remove_skill_schedule` |
| `remove_skill_schedule` fails mid-save | skill identity/steps already saved; user sees a warning naming the schedule failure specifically, not a generic save error |

## Testing

Pure-function tests, extending `test_skills_page.py` / `test_skill_builder_service.py`'s
existing conventions (no new fixtures/infra):

- `schedule_form_defaults`: no schedule → today's blanks; each `schedule_type`/cron
  shape reverse-maps to the right `REPEAT_OPTIONS` entry, including the `once`/no-cron
  → "Does not repeat" case; an `is_active=False` row (completed or cancelled) → same
  blanks as no schedule at all, not treated as a live schedule to display.
- Save's removal branch: an `is_active=False` `schedule` does not trigger
  `remove_skill_schedule` — this is the regression test for the self-review fix above.
- `_derive_steps_payload`: pending tail appended with keys preserved verbatim; combined
  (not live-only) count drives `is_last`/forced `is_response_step`; a `kind: "function"`
  entry in the tail survives round-trip unmodified.
- Pending-tail derivation itself: `initial_steps[len(state["steps"]):]` behaves
  correctly at the three shape boundaries — empty `initial_steps` (new workflow), fully
  consumed (`len(state["steps"]) >= len(initial_steps)`), and partially consumed.
- `set_skill_schedule`: "Does not repeat" now succeeds instead of erroring, and the
  captured upsert payload has `cron_expression=None`, `schedule_type="once"`, and
  `next_run_at` set; the pre-existing "Weekly" test extends to also assert
  `next_run_at` is present (this is the regression test for Problem #3).
- `remove_skill_schedule`: happy path sets `is_active=False`; no-client / query-failure
  paths return `{"success": False, ...}` rather than raising.

Per `CLAUDE.md`: any new test file under `tests/` needs `git add -f` (the directory is
gitignored — a plain `git add` silently drops it, and `pytest` still finds it on disk
so the suite looks green with nothing actually committed), and `pre-commit run
--all-files` before claiming done.

## Sequencing

1. `SkillBuilderService.set_skill_schedule` `next_run_at` fix + `"Does not repeat"`
   branch + `remove_skill_schedule`. Ships alone, covered by service-level tests,
   independent of the UI change.
2. `REPEAT_OPTIONS` + `schedule_form_defaults` + threading `schedule` through
   `_render_row`/`_open_editor`, wired to the Save logic above.
3. `render_builder`'s pending-tail mechanism (rendering, auto-fill, `_derive_steps_payload`
   changes) + wiring `initial_steps` from `_open_editor`. Largest and riskiest piece;
   last so 1–2 are already solid.
