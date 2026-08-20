# Editable Workflows Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make an existing workflow's steps re-runnable one-by-one from the "Edit
workflow" modal (with untouched steps greyed out and preserved as-is), add a "Does not
repeat" schedule option, and make schedule edits on an existing workflow — including
removal — actually take effect.

**Architecture:** Three independent slices, in dependency order. (1) Fix
`SkillBuilderService.set_skill_schedule` to write `next_run_at` (a live bug: no skill
schedule has ever fired) and accept a one-time "Does not repeat" run; add
`remove_skill_schedule`. (2) Thread each row's existing `user_schedules` entry into the
edit modal so the Schedule section shows and can clear it. (3) Give
`render_builder` an `initial_steps` parameter it actually uses: the "pending tail" —
stored steps not yet re-run this session — is computed as
`initial_steps[len(state["steps"]):]`, a derived slice rather than tracked state, which
makes it self-correcting under the existing Rewind mechanism for free.

**Tech Stack:** Python, NiceGUI (`anansi_app/nicegui_app`), Supabase/Postgres
(`chat_db.user_schedules`, `chat_db.skills`), pytest.

**Spec:** `docs/superpowers/specs/2026-08-20-editable-workflows-design.md`

---

## File Structure

| File | Change |
|---|---|
| `anansi_app/services/skill_builder_service.py` | `set_skill_schedule`: write `next_run_at`, accept `"Does not repeat"`. New `remove_skill_schedule` method. |
| `anansi_app/nicegui_app/pages/skills.py` | `REPEAT_OPTIONS` gains `"Does not repeat"`. New `schedule_form_defaults` pure function. `_render_row`/`_open_editor` thread the row's `schedule` dict through. `_open_editor` always mounts the builder (with `initial_steps`) and gains a schedule-removal Save branch. |
| `anansi_app/nicegui_app/pages/skill_builder.py` | `render_builder` stores `initial_steps` in state. New `_render_pending_step`. `_rebuild_transcript`/`_refresh_transcript`/`_render_step` account for the pending tail. `_derive_steps_payload` appends it. |
| `anansi_app/tests/test_skill_builder_service.py` | Extend the schedule tests; add `remove_skill_schedule` tests. |
| `anansi_app/tests/test_skills_page.py` | Add `schedule_form_defaults` tests and `_derive_steps_payload` pending-tail tests (this file already tests `skill_builder.py`'s pure functions — see its existing `_step_had_tool_error` tests). |

No new test files, no migration — `user_schedules.status` has no CHECK constraint, so
a new `"cancelled"` value needs no schema change.

**Running tests** (matches `.github/workflows/ci.yml`'s `Validate` job exactly — run
this, not a bare `pytest`, so a local pass means a CI pass):

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
```

Run from the repo root (`/Users/vaibha/Downloads/git/nxt-ai-assistant/nxt-ai-assistant`).

---

## Task 1: Fix `set_skill_schedule` to write `next_run_at`

Every skill schedule saved through this modal has `next_run_at = NULL` forever, so
`process_due_skill_schedules`'s `.lte("next_run_at", now)` never matches it — no skill
schedule has ever fired. This task is the fix, isolated from the "Does not repeat" work
in Task 2 so it lands as its own reviewable, revertable change.

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py:319-398` (`set_skill_schedule`)
- Test: `anansi_app/tests/test_skill_builder_service.py:610-638`

- [ ] **Step 1: Extend the existing test to assert `next_run_at` is written**

Replace `test_set_skill_schedule_derives_cron_from_frequency` (currently
`anansi_app/tests/test_skill_builder_service.py:610-638`) with:

```python
def test_set_skill_schedule_derives_cron_from_frequency():
    captured = {}

    class _Table:
        def upsert(self, payload, **_k):
            captured.update(payload)
            return self

        def execute(self):
            class _R:
                data = [captured]

            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    result = SkillBuilderService(client=_Client()).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="2026-09-01 08:00",
        frequency="Weekly", actor="ops@example.com",
    )

    assert result["success"] is True
    assert captured["skill_id"] == "1"
    assert captured["anchor_entity_type"] == "grid"
    assert captured["cron_expression"].startswith("0 8 ")
    assert captured["command"] is None
    # Regression test for the dead-schedule bug: without this, next_run_at
    # is never written and process_due_skill_schedules's `.lte("next_run_at",
    # now)` filter never matches the row -- it would never fire.
    assert captured["next_run_at"] == "2026-09-01T08:00:00+00:00"
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py::test_set_skill_schedule_derives_cron_from_frequency -q
```

Expected: `FAIL` — `KeyError: 'next_run_at'` (the current `payload` dict never sets
that key).

- [ ] **Step 3: Write the minimal implementation**

In `anansi_app/services/skill_builder_service.py`, the `payload` dict inside
`set_skill_schedule` (currently lines ~377-389) gains one line:

```python
        payload = {
            "skill_id": skill_id,
            "command": None,
            "chat_id": f"skill:{skill_id}",
            "created_by_user_id": actor,
            "created_by_email": actor,
            "anchor_entity_type": anchor_entity_type,
            "cron_expression": recurrence["cron_expression"],
            "schedule_type": recurrence.get("schedule_type", "recurring"),
            "timezone": recurrence.get("timezone", "UTC"),
            "next_run_at": when.isoformat(),
            "is_active": True,
            "status": "active",
        }
```

(`when` is already computed a few lines above, from `first_run` — it was parsed and
validated but never actually used until now.)

- [ ] **Step 4: Run the test to verify it passes**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py -q
```

Expected: `PASS` — all tests in the file, including the two existing
`test_set_skill_schedule_rejects_*` tests, which don't touch this line.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "fix(skills): write next_run_at when scheduling a skill

Every skill schedule saved through the editor modal has had next_run_at
left NULL, so process_due_skill_schedules's due-row query never matched
it -- no skill schedule has ever actually fired. when was already parsed
and validated from first_run, just never used."
```

---

## Task 2: `set_skill_schedule` accepts "Does not repeat" as a one-time run

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py:312-398` (the `SUPPORTED_ANCHORS`
  comment and `set_skill_schedule`)
- Test: `anansi_app/tests/test_skill_builder_service.py`

- [ ] **Step 1: Write the failing tests**

Add to `anansi_app/tests/test_skill_builder_service.py`, after
`test_set_skill_schedule_derives_cron_from_frequency`:

```python
def test_set_skill_schedule_accepts_does_not_repeat_as_a_one_time_run():
    captured = {}

    class _Table:
        def upsert(self, payload, **_k):
            captured.update(payload)
            return self

        def execute(self):
            class _R:
                data = [captured]

            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    result = SkillBuilderService(client=_Client()).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="2026-09-01 08:00",
        frequency="Does not repeat", actor="ops@example.com",
    )

    assert result["success"] is True
    assert captured["cron_expression"] is None
    assert captured["schedule_type"] == "once"
    assert captured["next_run_at"] == "2026-09-01T08:00:00+00:00"
    assert captured["is_active"] is True


def test_set_skill_schedule_rejects_an_unrecognized_frequency():
    result = SkillBuilderService(client=None).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="2026-09-01 08:00",
        frequency="Every full moon", actor="x",
    )
    assert result["success"] is False
    assert "Every full moon" in result["error"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py::test_set_skill_schedule_accepts_does_not_repeat_as_a_one_time_run -q
```

Expected: `FAIL` — today `_build_recurrence(when, "Does not repeat")` returns `None`,
`recurrence = None or {}` is `{}`, and `not recurrence.get("cron_expression")` is
`True`, so this returns `{"success": False, "error": "Could not derive a schedule from
'Does not repeat'"}` instead of succeeding.

(`test_set_skill_schedule_rejects_an_unrecognized_frequency` already passes against
today's code — it's a regression guard for Step 3's refactor, not a red test on its
own. Confirm it still passes after Step 3, not before.)

- [ ] **Step 3: Write the implementation**

Replace the body of `set_skill_schedule` in
`anansi_app/services/skill_builder_service.py` from the `recurrence = ...` line through
the `payload = {...}` construction with:

```python
        from nicegui_app.pages.broadcast import _build_recurrence

        recurrence = _build_recurrence(when, frequency)
        if recurrence is None:
            if frequency != "Does not repeat":
                return {
                    "success": False,
                    "error": f"Could not derive a schedule from '{frequency}'",
                }
            cron_expression, schedule_type, tz = None, "once", "UTC"
        else:
            cron_expression = recurrence["cron_expression"]
            schedule_type = recurrence.get("schedule_type", "recurring")
            tz = recurrence.get("timezone", "UTC")

        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}

        payload = {
            "skill_id": skill_id,
            "command": None,
            "chat_id": f"skill:{skill_id}",
            "created_by_user_id": actor,
            "created_by_email": actor,
            "anchor_entity_type": anchor_entity_type,
            "cron_expression": cron_expression,
            "schedule_type": schedule_type,
            "timezone": tz,
            "next_run_at": when.isoformat(),
            "is_active": True,
            "status": "active",
        }
```

Also update the docstring's second paragraph (currently starting "Reuses broadcast.py's
_build_recurrence...") to drop the now-false claim that `frequency` must exclude "Does
not repeat":

```python
        """Schedule a skill to fan out across every eligible entity.

        Reuses broadcast.py's _build_recurrence rather than deriving cron a
        second way -- the two must agree on what "Weekly" means. `frequency`
        is one of its REPEAT_OPTIONS: "Does not repeat" (a real one-time run
        -- see below), "Weekly", "Every other week", "Monthly (same date)"
        or "Monthly (same weekday)".

        "Does not repeat" stores cron_expression=None, schedule_type="once".
        _advance_skill_schedule (anansi_app/scripts/broadcast_scheduler.py)
        already treats any schedule_type outside ("recurring", "biweekly")
        as one-time: it dispatches once and flips is_active=False,
        status="completed" -- no dispatcher-side change needed for this.
        """
```

Also update the class-level comment directly above `SUPPORTED_ANCHORS` (currently lines
312-317, starting "Must match anansi_app/nicegui_app/pages/broadcast.py's
_build_recurrence, minus 'Does not repeat'..."), which is now stale:

```python
    # Anchors this UI exposes for fan-out. entity_fanout.py is the runtime
    # source of truth for what's actually wired up; this CHECK-mirroring set
    # is just this service's own input validation.
    SUPPORTED_ANCHORS = ("grid", "organization")
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py -q
```

Expected: `PASS` — all tests, including Task 1's `next_run_at` assertion and both
pre-existing reject tests (unsupported anchor, unparseable first_run).

- [ ] **Step 5: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "feat(skills): allow a one-time (Does not repeat) skill schedule

set_skill_schedule previously rejected 'Does not repeat' outright, on the
reasoning that a one-off run didn't need a persistent schedule row. It
does when editing needs to stop an existing repeat, or an operator wants
a real future one-time run. Stores schedule_type='once',
cron_expression=None -- the dispatcher already knows how to fire that
once and deactivate it."
```

---

## Task 3: New `remove_skill_schedule` method

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py` (new method, placed directly
  after `set_skill_schedule`, before `_unique_slug`)
- Test: `anansi_app/tests/test_skill_builder_service.py`

- [ ] **Step 1: Write the failing tests**

Add to `anansi_app/tests/test_skill_builder_service.py`, after the two tests added in
Task 2:

```python
def test_remove_skill_schedule_deactivates_the_row():
    captured = {}

    class _Table:
        def update(self, payload, **_k):
            captured.update(payload)
            return self

        def eq(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = [captured]

            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    result = SkillBuilderService(client=_Client()).remove_skill_schedule(
        "1", actor="ops@example.com"
    )

    assert result["success"] is True
    assert captured["is_active"] is False
    assert captured["status"] == "cancelled"


def test_remove_skill_schedule_without_a_client():
    result = SkillBuilderService(client=None).remove_skill_schedule("1", actor="x")
    assert result["success"] is False


def test_remove_skill_schedule_survives_a_query_failure():
    class _Client:
        def table(self, _n):
            raise RuntimeError("db down")

    result = SkillBuilderService(client=_Client()).remove_skill_schedule("1", actor="x")
    assert result["success"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py::test_remove_skill_schedule_deactivates_the_row -q
```

Expected: `FAIL` — `AttributeError: 'SkillBuilderService' object has no attribute
'remove_skill_schedule'`.

- [ ] **Step 3: Write the implementation**

Add to `anansi_app/services/skill_builder_service.py`, directly after
`set_skill_schedule`'s closing `return {"success": True, "schedule": (response.data or
[payload])[0]}` and before `def _unique_slug`:

```python
    def remove_skill_schedule(self, skill_id: str, actor: str) -> Dict[str, Any]:
        """Deactivate a skill's schedule -- the editor modal's "Not
        scheduled" save path, when a schedule existed before (see
        nicegui_app/pages/skills.py's _open_editor).

        status="cancelled" distinguishes an operator turning this off from
        a one-time schedule that simply finished
        (status="completed", set by _advance_skill_schedule in
        anansi_app/scripts/broadcast_scheduler.py) -- both end up
        is_active=False and are equally invisible to
        process_due_skill_schedules; the status text is only for a human
        reading the row.
        """
        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}
        try:
            response = (
                self.client.table("user_schedules")
                .update({"is_active": False, "status": "cancelled"})
                .eq("skill_id", skill_id)
                .execute()
            )
        except Exception as e:
            logger.exception("Error removing schedule for skill %s", skill_id)
            return {"success": False, "error": str(e)}
        logger.info("Schedule removed for skill %s (by %s)", skill_id, actor)
        return {"success": True, "schedule": (response.data or [None])[0]}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skill_builder_service.py -q
```

Expected: `PASS` — full file, all tests including Tasks 1-2's.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "feat(skills): add remove_skill_schedule

The editor modal has never had a way to turn off an existing skill
schedule -- set_skill_schedule only ever creates/replaces one. Needed so
clearing the Schedule section back to 'Not scheduled' on an edit actually
takes effect instead of being silently ignored."
```

---

## Task 4: `REPEAT_OPTIONS` + `schedule_form_defaults`

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py:1-27` (imports, `REPEAT_OPTIONS`) and a
  new function placed directly after `format_schedule` (currently ending at line 39)
- Test: `anansi_app/tests/test_skills_page.py`

- [ ] **Step 1: Write the failing tests**

Add to `anansi_app/tests/test_skills_page.py`, after the existing
`test_format_schedule_handles_a_missing_cron` test:

```python
def test_schedule_form_defaults_with_no_schedule():
    from nicegui_app.pages.skills import REPEAT_OPTIONS, schedule_form_defaults

    assert schedule_form_defaults(None) == {
        "anchor": "", "repeat": REPEAT_OPTIONS[0], "first_run": "",
    }


def test_schedule_form_defaults_with_an_inactive_schedule():
    """An already-completed one-time run, or one an operator previously
    removed, must read the same as no schedule at all -- Save's removal
    branch relies on this same rule to avoid firing a pointless removal
    call on a workflow that isn't really scheduled (see Task 6)."""
    from nicegui_app.pages.skills import REPEAT_OPTIONS, schedule_form_defaults

    schedule = {
        "anchor_entity_type": "grid", "cron_expression": None,
        "schedule_type": "once", "next_run_at": "2026-09-01T08:00:00+00:00",
        "is_active": False,
    }
    assert schedule_form_defaults(schedule) == {
        "anchor": "", "repeat": REPEAT_OPTIONS[0], "first_run": "",
    }


def test_schedule_form_defaults_for_a_weekly_schedule():
    from nicegui_app.pages.skills import schedule_form_defaults

    schedule = {
        "anchor_entity_type": "grid", "cron_expression": "0 8 * * 1",
        "schedule_type": "recurring", "next_run_at": "2026-09-01T08:00:00+00:00",
        "is_active": True,
    }
    assert schedule_form_defaults(schedule) == {
        "anchor": "grid", "repeat": "Weekly", "first_run": "2026-09-01 08:00",
    }


def test_schedule_form_defaults_for_a_biweekly_schedule():
    from nicegui_app.pages.skills import schedule_form_defaults

    schedule = {
        "anchor_entity_type": "organization", "cron_expression": "0 8 * * 1",
        "schedule_type": "biweekly", "next_run_at": "2026-09-01T08:00:00+00:00",
        "is_active": True,
    }
    assert schedule_form_defaults(schedule)["repeat"] == "Every other week"


def test_schedule_form_defaults_for_a_monthly_same_date_schedule():
    from nicegui_app.pages.skills import schedule_form_defaults

    schedule = {
        "anchor_entity_type": "grid", "cron_expression": "0 8 15 * *",
        "schedule_type": "recurring", "next_run_at": "2026-09-15T08:00:00+00:00",
        "is_active": True,
    }
    assert schedule_form_defaults(schedule)["repeat"] == "Monthly (same date)"


def test_schedule_form_defaults_for_a_monthly_same_weekday_schedule():
    from nicegui_app.pages.skills import schedule_form_defaults

    schedule = {
        "anchor_entity_type": "grid", "cron_expression": "0 8 * * 1#3",
        "schedule_type": "recurring", "next_run_at": "2026-09-15T08:00:00+00:00",
        "is_active": True,
    }
    assert schedule_form_defaults(schedule)["repeat"] == "Monthly (same weekday)"


def test_schedule_form_defaults_for_a_one_time_schedule():
    from nicegui_app.pages.skills import schedule_form_defaults

    schedule = {
        "anchor_entity_type": "grid", "cron_expression": None,
        "schedule_type": "once", "next_run_at": "2026-09-01T08:00:00+00:00",
        "is_active": True,
    }
    assert schedule_form_defaults(schedule) == {
        "anchor": "grid", "repeat": "Does not repeat", "first_run": "2026-09-01 08:00",
    }
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skills_page.py -k schedule_form_defaults -q
```

Expected: `FAIL` — `ImportError: cannot import name 'schedule_form_defaults'`.

- [ ] **Step 3: Write the implementation**

In `anansi_app/nicegui_app/pages/skills.py`, change the import line (currently line 11)
from:

```python
from typing import Any, Dict, List
```

to:

```python
from typing import Any, Dict, List, Optional
```

Replace `REPEAT_OPTIONS` (currently lines 15-20, including its comment) with:

```python
# Matches nicegui_app.pages.broadcast._build_recurrence's REPEAT_OPTIONS
# exactly, including "Does not repeat" -- a real one-time run (see
# SkillBuilderService.set_skill_schedule), not just "no schedule at all".
REPEAT_OPTIONS = [
    "Does not repeat",
    "Weekly",
    "Every other week",
    "Monthly (same date)",
    "Monthly (same weekday)",
]
```

Add, directly after `format_schedule` (currently ending at line 39, right before
`def derive_fallback_title`):

```python
def schedule_form_defaults(schedule: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """anchor / repeat / first_run string values to preselect when opening
    Edit on a workflow that may already have a user_schedules row.

    An inactive row (a one-time run that already completed, or one an
    operator previously removed via remove_skill_schedule) reads identically
    to no schedule at all -- both here and in _open_editor's Save logic,
    which must agree on the same "is this really scheduled" question or it
    will fire a pointless removal call on a workflow that was never actually
    scheduled to begin with.
    """
    if not schedule or not schedule.get("is_active"):
        return {"anchor": "", "repeat": REPEAT_OPTIONS[0], "first_run": ""}

    anchor = schedule.get("anchor_entity_type") or ""

    first_run = ""
    next_run_at = schedule.get("next_run_at")
    if next_run_at:
        try:
            from datetime import datetime

            parsed = datetime.fromisoformat(str(next_run_at).replace("Z", "+00:00"))
            first_run = parsed.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            first_run = ""

    # Only has to round-trip what _build_recurrence itself produces --
    # skill schedules have exactly one writer -- not arbitrary hand-written
    # cron.
    schedule_type = schedule.get("schedule_type")
    cron = schedule.get("cron_expression") or ""
    if schedule_type == "biweekly":
        repeat = "Every other week"
    elif schedule_type != "recurring" or not cron:
        repeat = "Does not repeat"
    else:
        fields = cron.split()
        dow = fields[4] if len(fields) > 4 else ""
        dom = fields[2] if len(fields) > 2 else "*"
        if "#" in dow:
            repeat = "Monthly (same weekday)"
        elif dom != "*":
            repeat = "Monthly (same date)"
        else:
            repeat = "Weekly"

    return {"anchor": anchor, "repeat": repeat, "first_run": first_run}
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skills_page.py -q
```

Expected: `PASS` — full file.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py anansi_app/tests/test_skills_page.py
git commit -m "feat(skills): add Does not repeat to REPEAT_OPTIONS + schedule_form_defaults

REPEAT_OPTIONS now matches broadcast.py's list exactly. schedule_form_defaults
reverse-maps an existing user_schedules row back to the anchor/repeat/first_run
values the edit modal's Schedule section should preselect -- not wired into
the modal yet, that's Task 5."
```

---

## Task 5: Thread `schedule` through the list into the edit modal, prefill the Schedule section

`schedule_form_defaults` (Task 4) has no caller yet. This task wires it in. This is
NiceGUI widget-construction code with no existing precedent for isolated unit testing
in this codebase (`test_skills_page.py` only ever tests the pure helpers, never `render`/
`_render_row`/`_open_editor` themselves) — verification here is the full suite (a
regression guard) plus a manual check.

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py` (`refresh`, `_render_row`,
  `_open_editor`'s signature and Schedule-widget construction)

- [ ] **Step 1: Thread `schedule` from `refresh()` through `_render_row`**

Replace `refresh()`'s body (currently `skills.py:146-159`) — only the last two lines
change:

```python
    async def refresh() -> None:
        skills = await run.io_bound(service.list_skills)
        schedules = await run.io_bound(service.schedule_summaries)
        rows = build_skill_rows(skills, schedules)

        container.clear()
        with container:
            if not rows:
                ui.label("No workflows yet. Create one to get started.").classes(
                    "text-gray-500 italic"
                )
                return
            for row in rows:
                _render_row(row, schedules.get(row["id"]), service, refresh, user_email)
```

Replace `_render_row`'s signature and its Edit button (currently `skills.py:171-186`):

```python
def _render_row(row, schedule, service, refresh, user_email) -> None:
    with ui.card().classes("w-full"):
        with ui.row().classes("w-full items-center justify-between"):
            with ui.column().classes("gap-0"):
                ui.label(row["title"]).classes("text-base font-medium")
                ui.label(row["summary"]).classes("text-sm text-gray-600")
                ui.label(
                    f"{row['step_count']} steps · {row['audience']} · "
                    f"{row['schedule']} · {row['created_by']}"
                ).classes("text-xs text-gray-500")
            with ui.row().classes("items-center gap-2"):
                ui.badge(row["status"], color=STATUS_COLORS.get(row["status"], "grey"))
                ui.button(
                    "Edit",
                    on_click=lambda r=row, s=schedule: _open_editor(
                        r, s, service, refresh, user_email
                    ),
                ).props("flat dense")
```

(`s=schedule` as a lambda default arg matters here exactly like `r=row` already does —
without it every row's Edit button would close over whichever `schedule` the loop
variable held on its *last* iteration, not each row's own.)

- [ ] **Step 2: Update the "New workflow" button's call site**

Replace (currently `skills.py:161-166`):

```python
    with ui.row().classes("w-full justify-end mb-2"):
        ui.button(
            "New workflow",
            icon="add",
            on_click=lambda: _open_editor(None, service, refresh, user_email),
        ).props("color=primary")
```

with:

```python
    with ui.row().classes("w-full justify-end mb-2"):
        ui.button(
            "New workflow",
            icon="add",
            on_click=lambda: _open_editor(None, None, service, refresh, user_email),
        ).props("color=primary")
```

- [ ] **Step 3: Update `_open_editor`'s signature and prefill the Schedule widgets**

Change `_open_editor`'s signature (currently `skills.py:189`) from:

```python
async def _open_editor(row, service, refresh, user_email) -> None:
```

to:

```python
async def _open_editor(row, schedule, service, refresh, user_email) -> None:
```

Replace the Schedule section's widget construction (currently `skills.py:312-331`):

```python
                ui.separator()
                ui.label(
                    "Schedule -- runs once per entity of the chosen type."
                ).classes("text-xs text-gray-500")
                with ui.row().classes("w-full gap-2"):
                    anchor_select = ui.select(
                        {
                            "": "Not scheduled",
                            "grid": "Per grid",
                            "organization": "Per organization",
                        },
                        value="",
                        label="Fan out across",
                    ).classes("flex-grow")
                    repeat_select = ui.select(
                        REPEAT_OPTIONS,
                        value=REPEAT_OPTIONS[0],
                        label="Repeat",
                    ).classes("flex-grow")
                first_run = ui.input("First run (YYYY-MM-DD HH:MM)").classes("w-full")
```

with:

```python
                ui.separator()
                ui.label(
                    "Schedule -- runs once per entity of the chosen type."
                ).classes("text-xs text-gray-500")
                schedule_defaults = schedule_form_defaults(schedule)
                with ui.row().classes("w-full gap-2"):
                    anchor_select = ui.select(
                        {
                            "": "Not scheduled",
                            "grid": "Per grid",
                            "organization": "Per organization",
                        },
                        value=schedule_defaults["anchor"],
                        label="Fan out across",
                    ).classes("flex-grow")
                    repeat_select = ui.select(
                        REPEAT_OPTIONS,
                        value=schedule_defaults["repeat"],
                        label="Repeat",
                    ).classes("flex-grow")
                first_run = ui.input(
                    "First run (YYYY-MM-DD HH:MM)", value=schedule_defaults["first_run"]
                ).classes("w-full")
```

- [ ] **Step 4: Run the full suite to confirm nothing broke, plus an import sanity check**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
cd anansi_app && python -c "import nicegui_app.pages.skills" && cd ..
```

Expected: all tests `PASS`; the import command prints nothing and exits 0 (a syntax or
name error in the edits above would raise on import).

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py
git commit -m "feat(skills): prefill the Schedule section when editing a workflow

Opening Edit on a scheduled workflow previously always showed blank
schedule fields regardless of what was actually configured -- there was
no way to see or change it short of guessing. Now threads each row's
user_schedules entry through and prefills via schedule_form_defaults.
Save-side removal (clearing to 'Not scheduled') is Task 6."
```

---

## Task 6: Save removes a schedule when cleared to "Not scheduled"

Same UI-glue caveat as Task 5: verified by the full suite + a manual check, no isolated
unit test (the branch condition itself — the `is_active` gate — is already covered
indirectly by Task 4's `schedule_form_defaults` tests, since both must agree on the same
rule).

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py` (`_save()`, inside `_open_editor`)

- [ ] **Step 1: Add the removal branch**

Replace the schedule-handling block in `_save()` (currently `skills.py:451-468`, right
before `ui.notify(f"Saved '{title}'.", type="positive")`):

```python
                if anchor_select.value:
                    schedule_result = await run.io_bound(
                        lambda: service.set_skill_schedule(
                            skill_id,
                            anchor_entity_type=anchor_select.value,
                            first_run=first_run.value,
                            frequency=repeat_select.value,
                            actor=user_email,
                        )
                    )
                    if not schedule_result.get("success"):
                        ui.notify(
                            f"Saved, but scheduling failed: {schedule_result.get('error')}",
                            type="warning",
                        )
                        dialog.close()
                        await refresh()
                        return
```

with:

```python
                if anchor_select.value:
                    schedule_result = await run.io_bound(
                        lambda: service.set_skill_schedule(
                            skill_id,
                            anchor_entity_type=anchor_select.value,
                            first_run=first_run.value,
                            frequency=repeat_select.value,
                            actor=user_email,
                        )
                    )
                    if not schedule_result.get("success"):
                        ui.notify(
                            f"Saved, but scheduling failed: {schedule_result.get('error')}",
                            type="warning",
                        )
                        dialog.close()
                        await refresh()
                        return
                elif schedule is not None and schedule.get("is_active"):
                    # Had an ACTIVE schedule; the author explicitly cleared
                    # it to "Not scheduled" -- remove it rather than
                    # silently leaving the old row running. is_active gates
                    # this the same way schedule_form_defaults gates the
                    # prefill (see its docstring): without it, saving a
                    # workflow whose one-time run already completed would
                    # fire a pointless removal call every single time.
                    removal_result = await run.io_bound(
                        lambda: service.remove_skill_schedule(skill_id, actor=user_email)
                    )
                    if not removal_result.get("success"):
                        ui.notify(
                            f"Saved, but removing the schedule failed: "
                            f"{removal_result.get('error')}",
                            type="warning",
                        )
                        dialog.close()
                        await refresh()
                        return
```

(`skill_id` is already defined above this point in both the `row` and new-workflow
branches; `schedule` is the parameter added to `_open_editor`'s signature in Task 5.)

- [ ] **Step 2: Run the full suite to confirm nothing broke, plus an import sanity check**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
cd anansi_app && python -c "import nicegui_app.pages.skills" && cd ..
```

Expected: all tests `PASS`; the import command exits 0.

- [ ] **Step 3: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py
git commit -m "feat(skills): removing a schedule on edit now actually removes it

Clearing 'Fan out across' back to 'Not scheduled' on a workflow that had
an active schedule previously did nothing -- the Save handler only ever
knew how to create/replace a schedule, never take one away. Calls the
new remove_skill_schedule (Task 3), gated on is_active so an already-
completed one-time run doesn't trigger a pointless removal on every save."
```

---

## Task 7: `_derive_steps_payload` appends the untouched pending tail

This is the core data-shape change behind "re-run one by one from top, everything else
saved unchanged." Pure function, fully TDD-able, no NiceGUI involved.

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skill_builder.py:228-259` (`_derive_steps_payload`)
- Test: `anansi_app/tests/test_skills_page.py`

- [ ] **Step 1: Write the failing tests**

Add to `anansi_app/tests/test_skills_page.py`, after the existing
`test_a_step_with_no_tool_calls_is_not_flagged` test:

```python
def _make_live_step(content: str) -> dict:
    return {"user_message": {"content": content, "message_index": 0}, "response_messages": []}


def test_derive_steps_payload_with_no_pending_tail_is_unchanged():
    """New-workflow path (initial_steps=[]) -- must keep behaving exactly as
    before this change."""
    from nicegui_app.pages.skill_builder import _derive_steps_payload

    state = {
        "steps": [_make_live_step("do a")],
        "flags": {0: {"allow_write": False, "is_response_step": False}},
        "initial_steps": [],
    }
    steps = _derive_steps_payload(state)
    assert len(steps) == 1
    assert steps[0]["instruction"] == "do a"
    assert steps[0]["is_response_step"] is True  # only step -> forced last


def test_derive_steps_payload_appends_an_untouched_pending_tail():
    from nicegui_app.pages.skill_builder import _derive_steps_payload

    stored = [
        {"index": 0, "name": "a", "instruction": "do a", "allow_write": False,
         "is_response_step": False, "had_tool_error": False, "result_preview": "got a"},
        {"index": 1, "name": "b", "instruction": "do b", "allow_write": True,
         "is_response_step": False, "had_tool_error": False, "result_preview": "got b"},
        {"index": 2, "name": "c", "instruction": "do c", "allow_write": False,
         "is_response_step": True, "had_tool_error": False, "result_preview": "got c"},
    ]
    state = {"steps": [], "flags": {}, "initial_steps": stored}

    steps = _derive_steps_payload(state)

    assert len(steps) == 3
    assert [s["instruction"] for s in steps] == ["do a", "do b", "do c"]
    assert [s["index"] for s in steps] == [0, 1, 2]
    assert steps[1]["allow_write"] is True  # passed through verbatim
    assert steps[2]["is_response_step"] is True


def test_derive_steps_payload_mixes_live_and_pending():
    from nicegui_app.pages.skill_builder import _derive_steps_payload

    stored = [
        {"index": 0, "name": "a", "instruction": "do a (stored)", "allow_write": False,
         "is_response_step": False, "had_tool_error": False, "result_preview": ""},
        {"index": 1, "name": "b", "instruction": "do b (stored)", "allow_write": False,
         "is_response_step": False, "had_tool_error": False, "result_preview": ""},
    ]
    state = {
        "steps": [_make_live_step("do a (re-run)")],
        "flags": {0: {"allow_write": False, "is_response_step": False}},
        "initial_steps": stored,
    }

    steps = _derive_steps_payload(state)

    assert len(steps) == 2
    assert steps[0]["instruction"] == "do a (re-run)"  # the live re-run wins
    assert steps[1]["instruction"] == "do b (stored)"  # untouched, preserved verbatim
    assert steps[1]["index"] == 1
    assert steps[1]["is_response_step"] is True  # now the combined-last step


def test_derive_steps_payload_preserves_a_function_step_in_the_pending_tail():
    """A P3 function-kind step can't be produced or re-run by this chat
    builder -- it must ride through untouched, not be dropped or crash."""
    from nicegui_app.pages.skill_builder import _derive_steps_payload

    stored = [
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis", "is_response_step": True},
    ]
    state = {"steps": [], "flags": {}, "initial_steps": stored}

    steps = _derive_steps_payload(state)

    assert steps[0]["kind"] == "function"
    assert steps[0]["handler"] == "fetch_grafana_kpis"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skills_page.py -k derive_steps_payload -q
```

Expected: `test_derive_steps_payload_appends_an_untouched_pending_tail`,
`test_derive_steps_payload_mixes_live_and_pending`, and
`test_derive_steps_payload_preserves_a_function_step_in_the_pending_tail` `FAIL` (today's
`_derive_steps_payload` never reads `state["initial_steps"]`, so it returns `[]`, `[<1
step>]`, and `[]` respectively instead of the expected 3, 2, and 1 elements).
`test_derive_steps_payload_with_no_pending_tail_is_unchanged` already passes — it's a
regression guard for Step 3, not a red test itself.

- [ ] **Step 3: Write the implementation**

Replace `_derive_steps_payload` in `anansi_app/nicegui_app/pages/skill_builder.py`
(currently lines 228-259) with:

```python
def _derive_steps_payload(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build the skills.steps-shaped payload from the current transcript +
    per-step flags -- see
    chat_orchestrator/orchestrator/experts/skill_validation.py's module
    docstring for the canonical shape. Used for /skills/validate,
    /skills/summarize, and Save, so all three always see the same steps.

    Appends whatever's left of state["initial_steps"] beyond the live
    transcript -- the "pending tail" (steps from a reopened workflow not yet
    re-run this session) -- verbatim, renumbered only. This is what makes
    "open Edit, Save without touching anything" reproduce the stored steps
    byte-for-byte, and what lets a step kind this builder can't produce
    (e.g. a P3 "function" step) survive an edit untouched instead of being
    dropped.
    """
    live_count = len(state["steps"])
    pending_tail = state.get("initial_steps", [])[live_count:]
    step_count = live_count + len(pending_tail)

    steps = []
    for index, step in enumerate(state["steps"]):
        instruction = step["user_message"].get("content") or ""
        _read_text, output_var = _parse_output_binding(instruction)
        flags = state["flags"].get(index, {"allow_write": False, "is_response_step": False})
        is_last = index == step_count - 1
        steps.append(
            {
                "index": index,
                "name": output_var or f"step_{index + 1}",
                "instruction": instruction,
                "output_var": output_var,
                "allow_write": flags["allow_write"],
                # The final step (of the combined live + pending sequence)
                # is always an implicit response step even if not flagged --
                # see the plan's "Run-mode output" section.
                "is_response_step": is_last or flags["is_response_step"],
                "had_tool_error": _step_had_tool_error(step),
                # Builder-only context for /skills/summarize (item b) -- what
                # the step's tools actually returned, not just the intent
                # the instruction states. Ignored by /skills/validate.
                "result_preview": _step_response_text(step)[:_RESULT_PREVIEW_CHARS],
            }
        )

    for offset, stored_step in enumerate(pending_tail):
        index = live_count + offset
        is_last = index == step_count - 1
        kept = dict(stored_step)
        kept["index"] = index
        kept["is_response_step"] = is_last or kept.get("is_response_step", False)
        steps.append(kept)

    return steps
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests/test_skills_page.py -q
```

Expected: `PASS` — full file, including the pre-existing `_step_had_tool_error` tests
and Task 4's `schedule_form_defaults` tests.

- [ ] **Step 5: Commit**

```bash
git add anansi_app/nicegui_app/pages/skill_builder.py anansi_app/tests/test_skills_page.py
git commit -m "feat(skills): _derive_steps_payload preserves an un-re-run pending tail

The 'pending tail' -- state[\"initial_steps\"] beyond however many steps
have actually been (re-)sent this session -- is a derived slice, not
tracked state, so it self-corrects under Rewind for free: rewinding live
steps back to zero re-expands the tail to the full original list with no
bookkeeping. A brand-new workflow has initial_steps=[], so this is a
no-op there -- one code path serves both New and Edit."
```

---

## Task 8: `render_builder` renders and auto-fills the pending tail

Same UI-glue caveat as Tasks 5-6: NiceGUI widget code, verified by the full suite
(which now includes Task 7's pending-tail tests, exercised through the same
`_derive_steps_payload` this task's rendering logic mirrors) plus an import sanity
check. `initial_steps` is still not reachable from `skills.py` after this task —
that's Task 9.

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skill_builder.py` (`render_builder`'s docstring
  and state init, `_rebuild_transcript`, `_render_step`, `_refresh_transcript`; new
  `_render_pending_step`)

- [ ] **Step 1: Update `render_builder`'s docstring and state init**

Replace the `initial_steps` paragraph in `render_builder`'s docstring (currently
`skill_builder.py:279-289`, starting "`initial_steps` is accepted for the editor
modal's future..."):

```python
    `initial_steps` is the stored `skills.steps` list when this builder is
    reopened to edit an existing workflow (`[]` for a brand-new one). It is
    captured once into `state["initial_steps"]` at mount and never mutated;
    "how much of it is still pending" is a *derived* value
    (`state["initial_steps"][len(state["steps"]):]`), not tracked state --
    slicing by the live step count is what makes this self-correcting under
    Rewind for free (archiving live steps back to zero re-expands the
    pending tail to the full original list with no bookkeeping needed).
    Each pending step renders as an inert, greyed "not yet re-run" card
    (`_render_pending_step`) until its instruction is actually (re-)sent, at
    which point it graduates into a normal live step sourced from the real
    transcript exactly like any other. See `_derive_steps_payload` for how a
    still-pending tail is preserved verbatim into the saved payload.
```

Replace the `state` dict literal (currently `skill_builder.py:303-311`):

```python
    state: Dict[str, Any] = {
        "session_id": None,
        "steps": [],
        "flags": {},  # step index -> {"allow_write": bool, "is_response_step": bool}
        "validation_errors": [],
        "sending": False,
        "summary": "",
        "summary_user_edited": False,
    }
```

with:

```python
    state: Dict[str, Any] = {
        "session_id": None,
        "steps": [],
        "initial_steps": initial_steps or [],
        "flags": {},  # step index -> {"allow_write": bool, "is_response_step": bool}
        "validation_errors": [],
        "sending": False,
        "summary": "",
        "summary_user_edited": False,
    }
```

- [ ] **Step 2: Render the pending tail after the live transcript**

Replace `_rebuild_transcript` (currently `skill_builder.py:367-375`):

```python
    def _rebuild_transcript() -> None:
        transcript.clear()
        errors_by_step: Dict[int, List[Dict[str, Any]]] = {}
        for err in state["validation_errors"]:
            errors_by_step.setdefault(err["step_index"], []).append(err)

        with transcript:
            for index, step in enumerate(state["steps"]):
                _render_step(index, step, errors_by_step.get(index, []))
```

with:

```python
    def _rebuild_transcript() -> None:
        transcript.clear()
        errors_by_step: Dict[int, List[Dict[str, Any]]] = {}
        for err in state["validation_errors"]:
            errors_by_step.setdefault(err["step_index"], []).append(err)

        live_count = len(state["steps"])
        pending_tail = state["initial_steps"][live_count:]

        with transcript:
            for index, step in enumerate(state["steps"]):
                _render_step(index, step, errors_by_step.get(index, []))

            for offset, stored_step in enumerate(pending_tail):
                _render_pending_step(live_count + offset, stored_step, is_up_next=(offset == 0))

            # Only meaningful for a reopened workflow -- initial_steps is
            # always [] for a brand-new one, so pending_tail is always []
            # there too and this never renders.
            if state["initial_steps"] and pending_tail:
                total = len(state["initial_steps"])
                ui.label(
                    f"{total - len(pending_tail)} of {total} steps re-run -- "
                    f"the rest will be saved unchanged."
                ).classes("text-caption text-grey-6")
```

- [ ] **Step 3: Add `_render_pending_step`**

Add directly after `_render_step`'s closing (currently ending at `skill_builder.py:423`,
right before `async def _send`):

```python
    def _render_pending_step(index: int, stored_step: Dict[str, Any], is_up_next: bool) -> None:
        """A step from initial_steps not yet re-run in this edit session --
        greyed out, nothing to act on yet. Graduates into a normal
        _render_step card the moment its instruction is actually (re-)sent;
        see _refresh_transcript's auto-fill and the pending-tail slice in
        _rebuild_transcript."""
        card_classes = "w-full bg-grey-2"
        if is_up_next:
            card_classes += " border-l-4 border-primary"
        with ui.card().classes(card_classes):
            label = f"Step {index + 1} · not yet re-run"
            if is_up_next:
                label += " · up next"
            ui.label(label).classes("text-bold text-grey-6")
            ui.label(stored_step.get("instruction") or "").classes(
                "text-body1 text-grey-6"
            ).style("font-style: italic")
            preview = (stored_step.get("result_preview") or "").strip()
            if preview:
                ui.label(f"Previously retrieved: {preview[:200]}").classes(
                    "text-caption text-grey-5"
                )
```

- [ ] **Step 4: A live step is only "the final step" when nothing is pending after it**

Replace `_render_step`'s first two lines (currently `skill_builder.py:377-379`):

```python
    def _render_step(index: int, step: Dict[str, Any], errors: List[Dict[str, Any]]) -> None:
        is_last = index == len(state["steps"]) - 1
        flags = state["flags"][index]
```

with:

```python
    def _render_step(index: int, step: Dict[str, Any], errors: List[Dict[str, Any]]) -> None:
        pending_tail = state["initial_steps"][len(state["steps"]):]
        is_last = index == len(state["steps"]) - 1 and not pending_tail
        flags = state["flags"][index]
```

(Without this, a live step immediately before a pending tail would have its "Also
return this response" toggle wrongly force-disabled as if it were the workflow's last
step.)

- [ ] **Step 5: Auto-fill the input with the next pending step's instruction**

Replace the tail of `_refresh_transcript` (currently `skill_builder.py:362-365`):

```python
        else:
            state["validation_errors"] = []

        _rebuild_transcript()
```

with:

```python
        else:
            state["validation_errors"] = []

        _rebuild_transcript()

        # Prime the input with the next not-yet-re-run step, but only when
        # nothing else already claimed the box: _send() just cleared it to
        # "" (prefill applies below), _rewind() just set it to the rewound
        # step's real sent text (non-empty -- Rewind's own "edit and resend
        # this" intent wins instead, unchanged from before this feature).
        pending_tail = state["initial_steps"][len(state["steps"]):]
        if pending_tail and not message_input.value:
            message_input.value = pending_tail[0].get("instruction") or ""
```

- [ ] **Step 6: Run the full suite to confirm nothing broke, plus an import sanity check**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
cd anansi_app && python -c "import nicegui_app.pages.skill_builder" && cd ..
```

Expected: all tests `PASS` (including Task 7's, which exercise the same pending-tail
slice this task's rendering mirrors); the import command exits 0.

- [ ] **Step 7: Commit**

```bash
git add anansi_app/nicegui_app/pages/skill_builder.py
git commit -m "feat(skills): render_builder shows and auto-advances a pending step tail

Each stored step not yet re-run this session renders as a greyed 'not
yet re-run' card; the one at the front is marked 'up next' and its
instruction primes the input box, so sending it (edited or not) re-runs
it for real and the next pending card unlocks. Not reachable from the
editor modal yet -- that's Task 9."
```

---

## Task 9: Wire `_open_editor` to mount the builder for existing rows

The last piece: `_open_editor` mounts `render_builder` for New and Edit alike, passing
`initial_steps` for Edit, and Save no longer has a "no builder mounted" fallback to
special-case. This is what actually removes the "isn't supported here yet" message from
the screenshot.

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py` (`_open_editor`'s module docstring,
  the Workflow-card section, and Save's steps derivation)

- [ ] **Step 1: Update `_open_editor`'s docstring**

Replace the paragraph starting "The Workflow card mounts the same chat-driven builder..."
(currently `skills.py:200-208`):

```python
    The Workflow card mounts the same chat-driven builder the standalone
    /skill-builder page used to (render_builder, extracted for exactly this
    reuse) -- but only for a *new* workflow. Reopening an existing one's
    steps for further chat-driven editing is a known gap: render_builder's
    initial_steps parameter exists for this and is not wired up yet (see
    its docstring), so editing an existing workflow here covers identity,
    status and schedule only, not its step transcript.
```

with:

```python
    The Workflow card mounts the same chat-driven builder the standalone
    /skill-builder page used to (render_builder, extracted for exactly this
    reuse), for New and Edit alike. Editing seeds it with the workflow's
    stored steps via initial_steps: each renders as an inert "not yet
    re-run" card until the author actually re-sends it, so a re-run is a
    real execution against live tools, not a replay of old transcript --
    see render_builder's docstring for the pending-tail mechanism, and
    _derive_steps_payload for how anything left un-re-run is preserved
    verbatim at Save.
```

- [ ] **Step 2: Always mount the builder, with `initial_steps` for an existing row**

Replace the Workflow-card section (currently `skills.py:352-367`):

```python
        # Now fill the Workflow card placeholder created above.
        if row:
            with steps_card:
                ui.label("Workflow").classes("text-subtitle2")
                ui.label(
                    "Editing an existing workflow's steps isn't supported here yet -- "
                    "this save will keep its current steps unchanged."
                ).classes("text-xs text-gray-500")
            state_holder: Dict[str, Any] = {"steps": None}
        else:
            with steps_card:
                ui.label("Workflow").classes("text-subtitle2")
                builder_user_id = f"{user_email}:{uuid.uuid4()}"
                state_holder = await render_builder(
                    user_email, builder_user_id, on_summary_update=_apply_auto_summary
                )
```

with:

```python
        # Now fill the Workflow card placeholder created above. Mounted for
        # both New and Edit alike -- render_builder's initial_steps handles
        # the difference (empty for New, the stored steps for Edit; see its
        # docstring and _derive_steps_payload for how a partially-re-run
        # edit is preserved).
        with steps_card:
            ui.label("Workflow").classes("text-subtitle2")
            if row:
                ui.label(
                    "Each step below is re-runnable, one at a time from the top -- "
                    "grey cards haven't been re-run in this session yet and are "
                    "saved unchanged if you don't get to them."
                ).classes("text-xs text-gray-500")
            builder_user_id = f"{user_email}:{uuid.uuid4()}"
            state_holder = await render_builder(
                user_email,
                builder_user_id,
                initial_steps=(row.get("steps") or []) if row else [],
                on_summary_update=_apply_auto_summary,
            )
```

- [ ] **Step 3: Remove Save's dead "no builder mounted" fallback**

Replace the steps-derivation block in `_save()` (currently `skills.py:401-410`):

```python
                if state_holder.get("steps") is None:
                    # Editing an existing workflow: the Workflow card above
                    # didn't mount a builder, so nothing to derive -- keep
                    # it as is.
                    steps = row.get("steps") or []
                else:
                    steps = _derive_steps_payload(state_holder)
                    if not steps:
                        ui.notify("Send at least one message to build a step.", type="negative")
                        return
```

with:

```python
                steps = _derive_steps_payload(state_holder)
                if not steps:
                    ui.notify("Send at least one message to build a step.", type="negative")
                    return
```

(`state_holder.get("steps")` can no longer be `None` — the builder is always mounted
now, per Step 2 — so this always goes through `_derive_steps_payload`, which for an
untouched edit returns `row["steps"]`'s pending tail verbatim, Task 7's `_derive_steps_payload`
change is what makes this a safe no-op.)

- [ ] **Step 4: Run the full suite to confirm nothing broke, plus an import sanity check**

```bash
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests -q
cd anansi_app && python -c "import nicegui_app.pages.skills" && cd ..
```

Expected: all tests `PASS`; the import command exits 0.

- [ ] **Step 5: Manual smoke test** (needs `CHAT_DB_URL`/`CHAT_DB_SERVICE_KEY` and
  Google OAuth configured locally — skip if unavailable in this environment and rely on
  the automated suite plus code review instead)

```bash
cd anansi_app && python -m nicegui_app.main
```

Open `/skills`, click Edit on a workflow that has at least two steps and, ideally, an
existing schedule. Confirm: the Workflow card shows grey "not yet re-run" cards for
every stored step, the first marked "up next", with its instruction pre-filling the
input box; the Schedule section shows the workflow's actual current values instead of
blanks; clicking Save immediately (nothing touched) reopens to the identical state;
sending the pre-filled first step re-runs it for real and the second card's instruction
takes over the input box.

- [ ] **Step 6: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py
git commit -m "feat(skills): make an existing workflow's steps editable

Removes the 'isn't supported here yet' placeholder -- _open_editor now
mounts render_builder for an existing row too, seeded with its stored
steps via initial_steps (Task 8). Each step is re-runnable one at a
time from the top; anything not re-run is preserved unchanged at Save
(Task 7)."
```