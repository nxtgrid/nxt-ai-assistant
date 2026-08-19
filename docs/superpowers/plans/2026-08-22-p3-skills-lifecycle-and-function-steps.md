# P3 — Skills Lifecycle and Function Steps Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## ⚠️ Before Task 1 — check the prior stages

**Stage 3 of 4.** Prior stages are P1 (`2026-08-20-p1-resolvable-context-modules.md`) and P2 (`2026-08-21-p2-procedures-to-context-modules.md`).

This plan touches **no code that P1 or P2 touch** — they work on
`shared/prompts/knowledge.py` and the Context page; this works on `skills`,
`skill_runner.py` and the Skill Builder page. It can run in parallel with either.

Still read their PRs for two things:

```bash
gh pr list --search "context modules" --state all
gh pr view <PR#> --json title,body,state,mergedAt
```

| what to check | why it matters here |
|---|---|
| the highest applied migration number in `db/migrations/` | Task 1 takes the next free number; P1 claims up to three |
| whether P2 changed `Procedure.id` to a slug | only matters if you touch `embed_and_store`; this plan does not |
| whether `pre-commit` gained hooks | affects Task 16's verification command |

```bash
ls db/migrations/ | tail -5
```

**Neither P1 nor P2 needs to have merged for this plan to run.**

---

**Goal:** Turn the Skill Builder from a single ephemeral page into a managed list of named, draftable, schedulable skills, and let skill steps call the registered Python handlers that expert workflows already use.

**Architecture:** `skills.status` gains `draft`, which `SkillCatalogStore` already filters out. The builder moves into a modal opened from a new `/skills` list page, gaining identity, status and schedule sections — the schedule reusing `broadcast.py`'s existing `_build_recurrence` and the fan-out machinery already in `0013_skill_scheduling.sql`. A `kind: "function"` step maps onto `ParsedStep(step_type="function")`, which `WorkflowExecutor` already dispatches, gated by an explicit per-handler opt-in.

**Tech Stack:** Python 3.12+, Supabase, NiceGUI, pytest, pre-commit.

**Spec:** `docs/superpowers/specs/2026-08-19-skills-lifecycle-and-function-steps-design.md`

---

## Critical Context for the Implementer

### Most of this already exists in the backend

- `skills.status` accepts `active | disabled | unusable` (`0011_skills.sql`)
- `SkillCatalogStore.all_skills()` already filters `.eq("status", "active")`
  (`shared/prompts/skills.py:118`), so a `draft` is invisible to the model with
  **zero code change** — that is the entire security model for drafts
- `user_schedules.skill_id` + `anchor_entity_type` fan a skill across every
  eligible grid or organization, logging per-entity outcomes to
  `user_schedule_logs` (`0013_skill_scheduling.sql`)
- `ParsedStep.step_type` is already `"llm" | "function"`
  (`workflow_executor.py:268`) and `WorkflowExecutor` already dispatches both

The gap is the UI, plus one line in `build_parsed_steps` that hardcodes
`step_type="llm"`.

### The experts split 5/4 — do not convert all nine

Tallied from the bundled `experts.definitions`:

| expert | `[llm]` | `[function:]` | action |
|---|---|---|---|
| grid_analyst, grid_monitor, site_visit_tracker, signing, community_sizing | 0 | 0 | convert (Phase 5) |
| context_expert | 2 | 7 | leave as code |
| grids_technical_reviewer | 2 | 9 | leave as code |
| ingestion_expert | 4 | 9 | leave as code |
| package_generator | 2 | 12 | leave as code |

The four pipelines encode real external ordering constraints —
`package_generator` sleeps 60s waiting on AppSheet between steps. Exposing them to
reordering in a chat builder is not a feature. **Do not convert them.**

### The expert definitions are not in the bundled file

Production resolves `experts.definitions` from a DB or Google Doc override.
Editing `shared/prompts/library/experts.definitions.prompt` has no effect there.
Verify before Phase 5:

```bash
python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('experts.definitions')[1:])"
```

`grid_analyst` is already struck through (disabled) in the live doc, making it the
safest first conversion.

### `.gitignore` denies `tests/`

`git add -f` every new test file. `pre-commit run --all-files` before claiming done.

---

## File Structure

**Phase 1 — draft status**
- Create: `db/migrations/00NN_skill_draft_status.sql`
- Test: `shared/tests/test_skills_catalog.py`

**Phase 2 — the list page**
- Create: `anansi_app/nicegui_app/pages/skills.py`
- Create: `anansi_app/tests/test_skills_page.py`
- Modify: `anansi_app/services/skill_builder_service.py` — list/update/schedule reads
- Modify: `anansi_app/nicegui_app/layout.py`, `main.py` — route

**Phase 3 — the modal**
- Modify: `anansi_app/nicegui_app/pages/skill_builder.py` — extract the builder into a reusable component
- Modify: `anansi_app/nicegui_app/pages/skills.py`

**Phase 4 — function steps**
- Modify: `chat_orchestrator/orchestrator/experts/skill_runner.py:90-124`
- Modify: `chat_orchestrator/orchestrator/experts/skill_validation.py`
- Modify: `chat_orchestrator/orchestrator/experts/step_registry.py` — `exposed_to_builder`
- Test: `chat_orchestrator/tests/experts/test_skill_step_bindings.py`

**Phase 5 — expert conversion**
- Modify: the live `experts.definitions` override (manual)
- Create: `scripts/convert_expert_to_skill.py`

---

# Phase 1 — Draft status

### Task 1: Migration

**Files:**
- Create: `db/migrations/00NN_skill_draft_status.sql` (next free number — check `ls db/migrations/`)

- [ ] **Step 1: Write the migration**

```sql
-- 00NN_skill_draft_status.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md.
--
-- Adds 'draft' so the builder can save unfinished work without it entering
-- anyone's context. No code change is needed for the invisibility itself:
-- SkillCatalogStore.all_skills() already filters .eq("status", "active").
--
-- Existing rows are unaffected -- this only widens what is allowed.

BEGIN;

ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_status_chk;

ALTER TABLE skills
    ADD CONSTRAINT skills_status_chk
        CHECK (status IN ('draft', 'active', 'disabled', 'unusable'));

COMMIT;
```

- [ ] **Step 2: Mirror into the schema file**

Update the `skills_status_chk` constraint in `db/schema/chat_db.sql` (line ~899) to
the four-value version.

- [ ] **Step 3: Apply to production and verify**

```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'skills_status_chk';
```
Expected: the definition includes `'draft'`

- [ ] **Step 4: Commit**

```bash
git add db/migrations/ db/schema/chat_db.sql
git commit -m "feat(skills): allow draft status"
```

---

### Task 2: Assert drafts stay out of context

**Files:**
- Test: `shared/tests/test_skills_catalog.py`

This is the security-relevant behaviour of the whole draft feature. Assert it
directly rather than trusting the existing filter by inspection.

- [ ] **Step 1: Write the test**

Append to `shared/tests/test_skills_catalog.py`:

```python
def test_catalog_query_filters_to_active_only():
    """A draft must never reach a model's context. This is the only gate."""
    captured = {}

    class _Table:
        def select(self, _cols):
            return self

        def eq(self, key, value):
            captured[key] = value
            return self

        def execute(self):
            class _R:
                data = []
            return _R()

    class _Client:
        def table(self, _name):
            return _Table()

    SkillCatalogStore(client=_Client()).all_skills()

    assert captured == {"status": "active"}


def test_a_draft_row_is_not_returned_by_the_catalog():
    class _Table:
        def __init__(self):
            self._status = None

        def select(self, _cols):
            return self

        def eq(self, key, value):
            if key == "status":
                self._status = value
            return self

        def execute(self):
            rows = [
                {"id": "1", "slug": "a", "title": "A", "summary": "s", "staff_only": True,
                 "status": "active"},
                {"id": "2", "slug": "b", "title": "B", "summary": "s", "staff_only": True,
                 "status": "draft"},
            ]
            class _R:
                data = [
                    {k: v for k, v in r.items() if k != "status"}
                    for r in rows
                    if r["status"] == self._status
                ]
            return _R()

    class _Client:
        def table(self, _name):
            return _Table()

    skills = SkillCatalogStore(client=_Client()).all_skills()

    assert [s.slug for s in skills] == ["a"]
```

- [ ] **Step 2: Run it**

Run: `python -m pytest shared/tests/test_skills_catalog.py -v`
Expected: PASS — this asserts existing behaviour, so it should pass immediately.
If it fails, the filter is not what the spec assumed; stop and re-read
`shared/prompts/skills.py:118` before continuing.

- [ ] **Step 3: Commit**

```bash
git add shared/tests/test_skills_catalog.py
git commit -m "test(skills): assert drafts never reach model context"
```

---

# Phase 2 — The list page

### Task 3: Reading skills for the list

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py`
- Test: `anansi_app/tests/test_skill_builder_service.py`

- [ ] **Step 1: Write the failing test**

Append to `anansi_app/tests/test_skill_builder_service.py`:

```python
def test_list_skills_returns_every_status():
    """The admin list shows drafts and disabled skills; the catalog does not."""
    rows = [
        {"id": "1", "slug": "a", "title": "A", "summary": "s", "steps": [{}, {}],
         "staff_only": True, "status": "active", "created_by": "x", "updated_at": "t"},
        {"id": "2", "slug": "b", "title": "B", "summary": "s", "steps": [],
         "staff_only": False, "status": "draft", "created_by": "x", "updated_at": "t"},
    ]

    class _Table:
        def select(self, _cols):
            return self

        def order(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = rows
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    service = SkillBuilderService(client=_Client())
    skills = service.list_skills()

    assert {s["slug"] for s in skills} == {"a", "b"}
    assert skills[0]["step_count"] == 2
    assert skills[1]["step_count"] == 0


def test_list_skills_returns_empty_without_a_client():
    assert SkillBuilderService(client=None).list_skills() == []


def test_update_skill_status_rejects_an_unknown_value():
    service = SkillBuilderService(client=None)
    result = service.update_skill_status("1", "published", actor="x")
    assert result["success"] is False
    assert "published" in result["error"]


def test_update_skill_status_accepts_the_four_valid_values():
    captured = {}

    class _Table:
        def update(self, payload):
            captured.update(payload)
            return self

        def eq(self, *_a):
            return self

        def execute(self):
            class _R:
                data = [{"id": "1", "status": captured.get("status")}]
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    for status in ("draft", "active", "disabled", "unusable"):
        result = SkillBuilderService(client=_Client()).update_skill_status(
            "1", status, actor="ops@example.com"
        )
        assert result["success"] is True, status
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -k "list_skills or update_skill_status" -v`
Expected: FAIL with `AttributeError: 'SkillBuilderService' object has no attribute 'list_skills'`

- [ ] **Step 3: Implement**

Add to `SkillBuilderService`:

```python
    VALID_STATUSES = ("draft", "active", "disabled", "unusable")

    def list_skills(self) -> List[Dict[str, Any]]:
        """Every skill, whatever its status -- this is the admin list.

        Deliberately not SkillCatalogStore.all_skills(), which filters to
        active because it feeds model context. An operator must see drafts
        and disabled skills; a model must not.
        """
        if not self.client:
            return []
        try:
            response = (
                self.client.table("skills")
                .select(
                    "id, slug, title, summary, steps, staff_only, status, "
                    "created_by, created_at, updated_at"
                )
                .order("updated_at", desc=True)
                .execute()
            )
        except Exception as e:
            LOGGER.warning(f"Skill list fetch failed: {e}")
            return []

        skills = []
        for row in response.data or []:
            row = dict(row)
            row["step_count"] = len(row.get("steps") or [])
            skills.append(row)
        return skills

    def update_skill_status(self, skill_id: str, status: str, actor: str) -> Dict[str, Any]:
        """Move a skill between draft/active/disabled/unusable.

        Promotion to 'active' is gated on validation by the caller (the
        modal's Save), not here -- this method is also how a scheduled run
        marks a skill 'unusable', which must never be blocked.
        """
        if status not in self.VALID_STATUSES:
            return {
                "success": False,
                "error": f"'{status}' is not a valid status; expected one of "
                         f"{', '.join(self.VALID_STATUSES)}",
            }
        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}
        try:
            response = (
                self.client.table("skills")
                .update({"status": status})
                .eq("id", skill_id)
                .execute()
            )
        except Exception as e:
            return {"success": False, "error": str(e)}
        rows = response.data or []
        if not rows:
            return {"success": False, "error": f"No skill with id {skill_id}"}
        LOGGER.info(f"{actor} set skill {skill_id} status to {status}")
        return {"success": True, "skill": rows[0]}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "feat(skills): list and status-update service methods"
```

---

### Task 4: Schedule summary per skill

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py`
- Test: `anansi_app/tests/test_skill_builder_service.py`

- [ ] **Step 1: Write the failing test**

```python
def test_schedule_summary_reports_cron_and_anchor():
    rows = [
        {"skill_id": "1", "cron_expression": "0 8 * * 1", "schedule_type": "recurring",
         "anchor_entity_type": "grid", "is_active": True},
    ]

    class _Table:
        def select(self, _cols):
            return self

        def not_(self, *_a, **_k):
            return self

        def is_(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = rows
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    summaries = SkillBuilderService(client=_Client()).schedule_summaries()

    assert summaries["1"]["cron_expression"] == "0 8 * * 1"
    assert summaries["1"]["anchor_entity_type"] == "grid"


def test_schedule_summary_is_empty_when_nothing_is_scheduled():
    class _Table:
        def select(self, _cols):
            return self

        def not_(self, *_a, **_k):
            return self

        def is_(self, *_a, **_k):
            return self

        def execute(self):
            class _R:
                data = []
            return _R()

    class _Client:
        def table(self, _n):
            return _Table()

    assert SkillBuilderService(client=_Client()).schedule_summaries() == {}


def test_schedule_summary_survives_a_query_failure():
    class _Client:
        def table(self, _n):
            raise RuntimeError("db down")

    assert SkillBuilderService(client=_Client()).schedule_summaries() == {}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -k schedule_summary -v`
Expected: FAIL with `AttributeError: ... has no attribute 'schedule_summaries'`

- [ ] **Step 3: Implement**

```python
    def schedule_summaries(self) -> Dict[str, Dict[str, Any]]:
        """skill_id -> its schedule row, for the list page's Schedule column.

        Reads user_schedules rather than a skills column: 0013 deliberately
        reused the existing scheduler rather than adding a fifth one, so a
        skill's schedule lives there.
        """
        if not self.client:
            return {}
        try:
            response = (
                self.client.table("user_schedules")
                .select(
                    "skill_id, cron_expression, schedule_type, anchor_entity_type, "
                    "timezone, is_active"
                )
                .not_.is_("skill_id", "null")
                .execute()
            )
        except Exception as e:
            LOGGER.warning(f"Skill schedule fetch failed: {e}")
            return {}
        return {
            row["skill_id"]: row for row in (response.data or []) if row.get("skill_id")
        }
```

- [ ] **Step 4: Verify the `not_.is_` API against the installed supabase client**

Run: `python -c "from supabase import create_client; import postgrest; print(postgrest.__version__)"`
Run: `command grep -rn "not_\.is_\|\.is_(" --include="*.py" anansi_app/ chat_orchestrator/ | command grep -v test | head -3`

If the codebase uses a different null-filter idiom, match it rather than
introducing a second one.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "feat(skills): read per-skill schedule summaries"
```

---

### Task 5: The list page

**Files:**
- Create: `anansi_app/nicegui_app/pages/skills.py`
- Create: `anansi_app/tests/test_skills_page.py`
- Modify: `anansi_app/nicegui_app/layout.py:25`

- [ ] **Step 1: Write the failing test**

Create `anansi_app/tests/test_skills_page.py`:

```python
"""Skills list page: row building and display rules."""

from anansi_app.nicegui_app.pages.skills import (
    STATUS_COLORS,
    build_skill_rows,
    format_schedule,
)


def _skill(slug="a", status="active", step_count=3, staff_only=True):
    return {
        "id": slug, "slug": slug, "title": slug.upper(), "summary": "Does a thing.",
        "step_count": step_count, "staff_only": staff_only, "status": status,
        "created_by": "ops@example.com", "updated_at": "2026-08-22T10:00:00Z",
    }


def test_rows_carry_the_display_fields():
    row = build_skill_rows([_skill()], schedules={})[0]
    assert row["title"] == "A"
    assert row["step_count"] == 3
    assert row["status"] == "active"
    assert row["audience"] == "Staff only"


def test_customer_visible_skills_say_so():
    row = build_skill_rows([_skill(staff_only=False)], schedules={})[0]
    assert row["audience"] == "Everyone"


def test_schedule_column_is_empty_when_unscheduled():
    assert build_skill_rows([_skill()], schedules={})[0]["schedule"] == "—"


def test_schedule_column_describes_the_cron_and_anchor():
    schedules = {
        "a": {"cron_expression": "0 8 * * 1", "anchor_entity_type": "grid", "is_active": True}
    }
    row = build_skill_rows([_skill()], schedules=schedules)[0]
    assert "per grid" in row["schedule"]


def test_an_inactive_schedule_is_marked_paused():
    schedules = {
        "a": {"cron_expression": "0 8 * * 1", "anchor_entity_type": "grid", "is_active": False}
    }
    assert "paused" in build_skill_rows([_skill()], schedules=schedules)[0]["schedule"].lower()


def test_format_schedule_handles_a_missing_cron():
    assert format_schedule({"anchor_entity_type": "grid", "is_active": True}) == "—"


def test_every_status_has_a_colour():
    for status in ("draft", "active", "disabled", "unusable"):
        assert status in STATUS_COLORS


def test_drafts_are_visually_distinct_from_active():
    assert STATUS_COLORS["draft"] != STATUS_COLORS["active"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_skills_page.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'anansi_app.nicegui_app.pages.skills'`

- [ ] **Step 3: Write the page**

Create `anansi_app/nicegui_app/pages/skills.py`:

```python
"""Skills list: every skill, its status, and its schedule.

Replaces /skill-builder as the entry point. The builder itself becomes a
section inside this page's edit modal (Phase 3) so an in-progress build
survives navigation as a draft rather than being lost.
"""

from __future__ import annotations

from typing import Any, Dict, List

from nicegui import ui

STATUS_COLORS: Dict[str, str] = {
    "draft": "grey",
    "active": "green",
    "disabled": "orange",
    "unusable": "red",
}


def format_schedule(schedule: Dict[str, Any]) -> str:
    """One-line description of a skill's schedule. '—' when unscheduled."""
    cron = (schedule or {}).get("cron_expression")
    if not cron:
        return "—"
    anchor = schedule.get("anchor_entity_type")
    text = f"{cron} per {anchor}" if anchor else cron
    if not schedule.get("is_active", True):
        text = f"{text} (paused)"
    return text


def build_skill_rows(
    skills: List[Dict[str, Any]], schedules: Dict[str, Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Table rows for the list. Pure -- all formatting decisions live here."""
    rows = []
    for skill in skills:
        rows.append(
            {
                "id": skill["id"],
                "title": skill["title"],
                "summary": skill.get("summary") or "",
                "step_count": skill.get("step_count", 0),
                "status": skill.get("status", "draft"),
                "audience": "Staff only" if skill.get("staff_only") else "Everyone",
                "schedule": format_schedule(schedules.get(skill["id"], {})),
                "updated_at": skill.get("updated_at") or "",
                "created_by": skill.get("created_by") or "",
            }
        )
    return rows


async def render(user: dict[str, Any]) -> None:
    from nicegui import run

    from anansi_app.nicegui_app.services_access import get_skill_builder_service

    service = get_skill_builder_service()
    user_email = user.get("email", "")

    ui.label("🧩 Skills").classes("text-h5")
    ui.label(
        "Reusable step-by-step procedures. A draft is saved but never offered to "
        "the assistant; only active skills reach a conversation."
    ).classes("text-sm text-gray-600 mb-4")

    container = ui.column().classes("w-full gap-2")

    async def refresh() -> None:
        skills = await run.io_bound(service.list_skills)
        schedules = await run.io_bound(service.schedule_summaries)
        rows = build_skill_rows(skills, schedules)

        container.clear()
        with container:
            if not rows:
                ui.label("No skills yet. Create one to get started.").classes(
                    "text-gray-500 italic"
                )
                return
            for row in rows:
                _render_row(row, service, refresh, user_email)

    with ui.row().classes("w-full justify-end mb-2"):
        ui.button(
            "New skill",
            icon="add",
            on_click=lambda: _open_editor(None, service, refresh, user_email),
        ).props("color=primary")

    await refresh()


def _render_row(row, service, refresh, user_email) -> None:
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
                    on_click=lambda r=row: _open_editor(r, service, refresh, user_email),
                ).props("flat dense")


def _open_editor(row, service, refresh, user_email) -> None:
    """Phase 3 replaces this with the full builder modal."""
    ui.notify("The skill editor arrives in Phase 3.", type="info")
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_skills_page.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Register the route**

In `anansi_app/nicegui_app/layout.py`, replace the `/skill-builder` nav entry:

```python
    ("/skills", "🧩 Skills"),
```

In `anansi_app/nicegui_app/main.py`, add a `/skills` page that calls
`pages.skills.render(user)`, following the exact pattern of the existing
`/skill-builder` route. Keep `/skill-builder` registered for now — Phase 3 removes
it, and leaving it reachable means a half-migrated deploy still works.

- [ ] **Step 6: Verify the app starts and the page renders**

Run the admin app and open `/skills`. Confirm the list loads, statuses show as
badges, and "New skill" notifies.

- [ ] **Step 7: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py anansi_app/nicegui_app/layout.py \
        anansi_app/nicegui_app/main.py
git add -f anansi_app/tests/test_skills_page.py
git commit -m "feat(skills): add the skills list page"
```

---

# Phase 3 — The editor modal

### Task 6: Extract the builder into a reusable component

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skill_builder.py`

The builder's transcript/send/rewind behaviour works and is the risky part to
touch. Move it without redesigning it.

- [ ] **Step 1: Extract**

In `skill_builder.py`, change the signature of the body of `render` so the builder
UI is a function that renders into the current container and returns its state:

```python
async def render_builder(user_email: str, user_id: str, initial_steps=None) -> Dict[str, Any]:
    """Render the step builder into the current container.

    Returns the mutable `state` dict the caller reads on save. Extracted
    from `render` unchanged so the same widget serves both the standalone
    page and the skills modal -- the transcript, send and rewind behaviour
    is deliberately untouched.
    """
```

Move everything from `render`'s body except the page chrome (title, description,
the Save-as-skill button) into `render_builder`, and have `render` call it:

```python
async def render(user: dict[str, Any]) -> None:
    ui.label("🧩 Skill Builder").classes("text-h5")
    ui.label(
        "Chat normally to build a skill step by step. Each message you send "
        "becomes one step; rewind any step to redo it and everything after."
    ).classes("text-sm text-gray-600 mb-4")
    state = await render_builder(user.get("email", ""), _user_id())
    ...existing save button wiring, reading `state`...
```

- [ ] **Step 2: Verify nothing changed**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -v`
Expected: PASS

Open `/skill-builder` and build a two-step skill. Confirm send, rewind and the
per-step toggles all behave exactly as before.

- [ ] **Step 3: Commit**

```bash
git add anansi_app/nicegui_app/pages/skill_builder.py
git commit -m "refactor(skills): extract the builder into a reusable component"
```

---

### Task 7: Schedule writes

**Files:**
- Modify: `anansi_app/services/skill_builder_service.py`
- Test: `anansi_app/tests/test_skill_builder_service.py`

- [ ] **Step 1: Write the failing test**

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


def test_set_skill_schedule_rejects_an_unsupported_anchor():
    result = SkillBuilderService(client=None).set_skill_schedule(
        "1", anchor_entity_type="meter", first_run="2026-09-01 08:00",
        frequency="Weekly", actor="x",
    )
    assert result["success"] is False
    assert "meter" in result["error"]


def test_set_skill_schedule_rejects_an_unparseable_first_run():
    result = SkillBuilderService(client=None).set_skill_schedule(
        "1", anchor_entity_type="grid", first_run="next tuesday",
        frequency="Weekly", actor="x",
    )
    assert result["success"] is False
    assert "first run" in result["error"].lower()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -k set_skill_schedule -v`
Expected: FAIL with `AttributeError: ... has no attribute 'set_skill_schedule'`

- [ ] **Step 3: Implement, reusing the broadcast recurrence builder**

```python
    SUPPORTED_ANCHORS = ("grid", "organization")

    def set_skill_schedule(
        self,
        skill_id: str,
        anchor_entity_type: str,
        first_run: str,
        frequency: str,
        actor: str,
    ) -> Dict[str, Any]:
        """Schedule a skill to fan out across every eligible entity.

        Reuses broadcast.py's _build_recurrence rather than deriving cron a
        second way -- the two must agree on what "Weekly" means.

        `command` is explicitly None: user_schedules_command_xor_skill_chk
        requires exactly one of command / skill_id per row.
        """
        from datetime import datetime, timezone

        if anchor_entity_type not in self.SUPPORTED_ANCHORS:
            return {
                "success": False,
                "error": f"'{anchor_entity_type}' is not a supported anchor; expected "
                         f"{' or '.join(self.SUPPORTED_ANCHORS)}",
            }
        try:
            when = datetime.strptime(first_run.strip(), "%Y-%m-%d %H:%M").replace(
                tzinfo=timezone.utc
            )
        except (ValueError, AttributeError):
            return {
                "success": False,
                "error": "Could not read the first run time; expected YYYY-MM-DD HH:MM",
            }

        from anansi_app.nicegui_app.pages.broadcast import _build_recurrence

        recurrence = _build_recurrence(when, frequency) or {}
        if not recurrence.get("cron_expression"):
            return {"success": False, "error": f"Could not derive a schedule from '{frequency}'"}

        if not self.client:
            return {"success": False, "error": "Chat DB not configured"}

        payload = {
            "skill_id": skill_id,
            "command": None,
            "anchor_entity_type": anchor_entity_type,
            "cron_expression": recurrence["cron_expression"],
            "schedule_type": recurrence.get("schedule_type", "recurring"),
            "timezone": recurrence.get("timezone", "UTC"),
            "is_active": True,
            "created_by": actor,
        }
        try:
            response = self.client.table("user_schedules").upsert(
                payload, on_conflict="skill_id"
            ).execute()
        except Exception as e:
            return {"success": False, "error": str(e)}
        return {"success": True, "schedule": (response.data or [payload])[0]}
```

- [ ] **Step 4: Check `user_schedules` required columns**

Run: `command grep -n "CREATE TABLE IF NOT EXISTS user_schedules" -A 40 db/schema/chat_db.sql`

Add any NOT NULL column without a default to `payload`. A missing one fails at
runtime, not in these tests, which use a fake client.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_skill_builder_service.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add anansi_app/services/skill_builder_service.py anansi_app/tests/test_skill_builder_service.py
git commit -m "feat(skills): schedule a skill from the editor modal"
```

---

### Task 8: The editor modal

**Files:**
- Modify: `anansi_app/nicegui_app/pages/skills.py`
- Test: `anansi_app/tests/test_skills_page.py`

- [ ] **Step 1: Write the failing test**

```python
def test_promotion_to_active_requires_valid_steps():
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0, "name": "a", "instruction": "do a"}],
        validation_errors=[],
        title="A",
    )
    assert ok is True
    assert reason == ""


def test_promotion_blocked_by_a_validation_error():
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0}],
        validation_errors=[{"severity": "error", "message": "unresolved {{x}}"}],
        title="A",
    )
    assert ok is False
    assert "unresolved" in reason


def test_promotion_not_blocked_by_a_warning():
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, _ = can_promote_to_active(
        steps=[{"index": 0}],
        validation_errors=[{"severity": "warning", "message": "unused write"}],
        title="A",
    )
    assert ok is True


def test_promotion_blocked_with_no_steps():
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(steps=[], validation_errors=[], title="A")
    assert ok is False
    assert "step" in reason.lower()


def test_promotion_blocked_without_a_title():
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0}], validation_errors=[], title="  "
    )
    assert ok is False
    assert "title" in reason.lower()


def test_promotion_blocked_when_a_step_captured_a_tool_error():
    """A step whose tools errored saved an apology, not a result."""
    from anansi_app.nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0, "name": "a", "had_tool_error": True}],
        validation_errors=[],
        title="A",
    )
    assert ok is False
    assert "error" in reason.lower()


def test_a_draft_can_always_be_saved():
    from anansi_app.nicegui_app.pages.skills import can_save_as_draft

    assert can_save_as_draft(title="A") is True
    assert can_save_as_draft(title="  ") is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest anansi_app/tests/test_skills_page.py -k promot -v`
Expected: FAIL with `ImportError: cannot import name 'can_promote_to_active'`

- [ ] **Step 3: Implement the gates**

Add to `anansi_app/nicegui_app/pages/skills.py`:

```python
def can_save_as_draft(title: str) -> bool:
    """A draft needs only a name -- that is what makes the modal viable.

    Saving partial, invalid step lists is the point: losing an in-progress
    build on navigation is the current behaviour this replaces.
    """
    return bool((title or "").strip())


def can_promote_to_active(
    steps: List[Dict[str, Any]],
    validation_errors: List[Dict[str, Any]],
    title: str,
) -> "tuple[bool, str]":
    """Whether this skill may go live. Returns (ok, reason_if_not).

    Warnings never block -- validate_skill_steps emits one for a write no
    later step reads, which is often deliberate in a final response step.
    """
    if not (title or "").strip():
        return False, "A skill needs a title before it can be activated."
    if not steps:
        return False, "A skill needs at least one step before it can be activated."

    blocking = [e for e in validation_errors if e.get("severity") == "error"]
    if blocking:
        return False, "; ".join(e.get("message", "invalid step") for e in blocking)

    failed = [s for s in steps if s.get("had_tool_error")]
    if failed:
        names = ", ".join(s.get("name") or f"step {s.get('index', 0) + 1}" for s in failed)
        return False, (
            f"These steps captured a tool error rather than a result: {names}. "
            f"Rewind and re-run them before activating."
        )

    return True, ""
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest anansi_app/tests/test_skills_page.py -v`
Expected: PASS

- [ ] **Step 5: Set `had_tool_error` when a step's tools failed**

`can_promote_to_active` reads this flag, but nothing sets it yet. A step whose
tool calls errored saved an apology as its response text, and that text is
indistinguishable from a real result once stored — this is exactly what the
builder screenshots showed, where `customer_get_my_open_issues` failed and the
saved step read "I'm sorry, I'm unable to retrieve the list of open tickets".

In `anansi_app/nicegui_app/pages/skill_builder.py`, add the detector next to
`_step_tool_names`:

```python
# Response text an escalation produces when a tool failed. A step that
# captured one of these saved an apology, not a result -- saving that
# skill bakes the apology in permanently.
_FAILURE_MARKERS = (
    "#nxtaction",
    "unable to retrieve",
    "something went wrong on our end",
)


def _step_had_tool_error(step: Dict[str, Any]) -> bool:
    """Whether this step's tools failed rather than returning data.

    Two signals, either sufficient: an explicit error on a recorded tool
    call, or escalation text in the response. The text check exists because
    the orchestrator swallows a tool failure and escalates rather than
    surfacing the error to the builder.
    """
    for call in step.get("tool_calls") or []:
        if call.get("error") or call.get("is_error"):
            return True
    if "escalate_to_support" in _step_tool_names(step):
        return True
    text = _step_response_text(step).lower()
    return any(marker in text for marker in _FAILURE_MARKERS)
```

and set it in `_derive_steps_payload`, on each step dict it builds:

```python
        "had_tool_error": _step_had_tool_error(step),
```

- [ ] **Step 6: Write the test**

Append to `anansi_app/tests/test_skills_page.py`:

```python
def test_a_step_that_escalated_is_flagged():
    from anansi_app.nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {
        "messages": [{"role": "assistant", "content": "I'm sorry, I'm unable to "
                      "retrieve the list of open tickets. #NXTAction"}],
        "tool_calls": [{"name": "customer_get_my_open_issues"},
                       {"name": "escalate_to_support"}],
    }
    assert _step_had_tool_error(step) is True


def test_a_step_with_an_explicit_tool_error_is_flagged():
    from anansi_app.nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {"messages": [], "tool_calls": [{"name": "get_x", "error": "timeout"}]}
    assert _step_had_tool_error(step) is True


def test_a_clean_step_is_not_flagged():
    from anansi_app.nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {
        "messages": [{"role": "assistant", "content": "Found 4 open tickets: ..."}],
        "tool_calls": [{"name": "customer_get_my_open_issues"}],
    }
    assert _step_had_tool_error(step) is False


def test_a_step_with_no_tool_calls_is_not_flagged():
    from anansi_app.nicegui_app.pages.skill_builder import _step_had_tool_error

    assert _step_had_tool_error({"messages": [], "tool_calls": []}) is False
```

- [ ] **Step 7: Run the tests**

Run: `python -m pytest anansi_app/tests/test_skills_page.py -v`
Expected: PASS

Adapt `_step_had_tool_error` to the real step dict shape if `tool_calls` is keyed
differently — read `_step_tool_names` and `_derive_steps_payload` in
`skill_builder.py` before writing it, rather than assuming the shape above.

- [ ] **Step 8: Build the modal**

Replace `_open_editor` with a real implementation:

```python
def _open_editor(row, service, refresh, user_email) -> None:
    from nicegui import run

    from anansi_app.nicegui_app.pages.skill_builder import render_builder

    with ui.dialog().props("persistent maximized") as dialog, ui.card().classes("w-full"):
        ui.label("Edit skill" if row else "New skill").classes("text-h6")

        with ui.column().classes("w-full gap-4"):
            # 1. Identity
            with ui.card().classes("w-full"):
                ui.label("Identity").classes("text-subtitle2")
                title_input = ui.input("Title", value=(row or {}).get("title", "")).classes("w-full")
                summary_input = (
                    ui.textarea("Summary", value=(row or {}).get("summary", ""))
                    .classes("w-full").props("autogrow")
                )
                staff_switch = ui.switch(
                    "Staff only", value=(row or {}).get("audience") != "Everyone"
                )
                status_select = ui.select(
                    ["draft", "active", "disabled"],
                    value=(row or {}).get("status", "draft"),
                    label="Status",
                )

            # 2. Steps -- the existing builder, unchanged
            with ui.card().classes("w-full"):
                ui.label("Steps").classes("text-subtitle2")
                state_holder: Dict[str, Any] = {}

            # 3. Schedule
            with ui.card().classes("w-full"):
                ui.label("Schedule").classes("text-subtitle2")
                ui.label(
                    "A scheduled skill runs once per entity of the chosen type."
                ).classes("text-xs text-gray-500")
                anchor_select = ui.select(
                    {"": "Not scheduled", "grid": "Per grid", "organization": "Per organization"},
                    value="",
                    label="Fan out across",
                )
                first_run = ui.input("First run (YYYY-MM-DD HH:MM)").classes("w-full")
                repeat_select = ui.select(
                    ["Once", "Daily", "Weekly", "Biweekly", "Monthly"],
                    value="Once",
                    label="Repeat",
                )

        with ui.row().classes("w-full justify-end gap-2"):
            ui.button("Cancel", on_click=dialog.close).props("flat")

            async def _save() -> None:
                title = title_input.value or ""
                if not can_save_as_draft(title):
                    ui.notify("A title is required.", type="negative")
                    return

                steps = state_holder.get("steps", [])
                if status_select.value == "active":
                    ok, reason = can_promote_to_active(
                        steps, state_holder.get("validation_errors", []), title
                    )
                    if not ok:
                        ui.notify(reason, type="negative")
                        return

                result = await run.io_bound(
                    lambda: service.save_skill(
                        title, summary_input.value or "", steps,
                        staff_switch.value, user_email,
                    )
                )
                if not result.get("success"):
                    ui.notify(result.get("error") or "Save failed", type="negative")
                    return

                skill_id = result["skill"]["id"]
                await run.io_bound(
                    lambda: service.update_skill_status(
                        skill_id, status_select.value, actor=user_email
                    )
                )
                if anchor_select.value:
                    await run.io_bound(
                        lambda: service.set_skill_schedule(
                            skill_id,
                            anchor_entity_type=anchor_select.value,
                            first_run=first_run.value,
                            frequency=repeat_select.value,
                            actor=user_email,
                        )
                    )
                ui.notify(f"Saved '{title}'.", type="positive")
                dialog.close()
                await refresh()

            ui.button("Save", on_click=_save, color="primary")

    dialog.open()

    async def _mount_builder() -> None:
        state_holder.update(await render_builder(user_email, user_email))
```

Wire `_mount_builder` into the steps card so the builder renders inside it. Follow
the container pattern the existing `skill_builder.render` uses.

- [ ] **Step 9: Commit**

```bash
git add anansi_app/nicegui_app/pages/skills.py \
        anansi_app/nicegui_app/pages/skill_builder.py \
        anansi_app/tests/test_skills_page.py
git commit -m "feat(skills): editor modal with identity, steps and schedule"
```

---

### Task 9: Retire the standalone builder page

**Files:**
- Modify: `anansi_app/nicegui_app/main.py`

- [ ] **Step 1: Confirm the modal works end to end**

Create a skill through `/skills` → New skill: name it, build two steps, save as
draft. Reopen it, promote to active, confirm it appears in the model's catalog.

- [ ] **Step 2: Redirect the old route**

In `main.py`, change the `/skill-builder` handler to redirect to `/skills` rather
than deleting the route — bookmarks and any saved link keep working.

```python
@ui.page("/skill-builder")
async def skill_builder_page():
    ui.navigate.to("/skills")
```

- [ ] **Step 3: Commit**

```bash
git add anansi_app/nicegui_app/main.py
git commit -m "refactor(skills): redirect the old builder route to the list"
```

---

# Phase 4 — Function steps

### Task 10: Handlers opt in to the builder

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/step_registry.py`
- Test: `chat_orchestrator/tests/experts/test_step_registry_exposure.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/experts/test_step_registry_exposure.py`:

```python
"""Which registered step handlers a skill author may pick."""

from orchestrator.experts.step_registry import (
    get_step_registry,
    register_step,
)


def test_handlers_are_not_exposed_by_default():
    """The registry holds handlers that mutate spreadsheets and trigger BOM
    generation. None may appear in a picker without a deliberate opt-in."""

    @register_step("test_unexposed_handler")
    async def _handler(context):
        return None

    registry = get_step_registry()
    assert "test_unexposed_handler" not in registry.builder_exposed_handlers()


def test_a_handler_can_opt_in():
    @register_step("test_exposed_handler", exposed_to_builder=True)
    async def _handler(context):
        return None

    registry = get_step_registry()
    assert "test_exposed_handler" in registry.builder_exposed_handlers()


def test_exposed_handlers_are_sorted():
    names = get_step_registry().builder_exposed_handlers()
    assert names == sorted(names)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/experts/test_step_registry_exposure.py -v`
Expected: FAIL with `TypeError: register_step() got an unexpected keyword argument 'exposed_to_builder'`

- [ ] **Step 3: Implement**

In `step_registry.py`, add the flag to `register_step` and track it:

```python
def register_step(name: str, exposed_to_builder: bool = False):
    """Register a step handler.

    `exposed_to_builder` opts this handler into the skill builder's step
    picker. Defaults to False: the registry holds handlers that write to
    spreadsheets, trigger BOM generation and sleep on external systems, and
    none of those belong in a picker a non-engineer drives. Each opt-in is
    reviewed on its own.
    """
    def decorator(fn):
        registry = get_step_registry()
        registry.register(name, fn)
        if exposed_to_builder:
            registry.expose_to_builder(name)
        return fn

    return decorator
```

Add to the registry class:

```python
    def expose_to_builder(self, name: str) -> None:
        self._builder_exposed.add(name)

    def builder_exposed_handlers(self) -> List[str]:
        """Handler names a skill author may pick, sorted for stable display."""
        return sorted(self._builder_exposed)
```

and initialise `self._builder_exposed: Set[str] = set()` in its constructor.

Match the existing `register`/`get_handler` signatures — read the class before
editing rather than assuming the shape above.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/experts/test_step_registry_exposure.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/step_registry.py
git add -f chat_orchestrator/tests/experts/test_step_registry_exposure.py
git commit -m "feat(skills): handlers opt in to the builder step picker"
```

---

### Task 11: `build_parsed_steps` honours `kind`

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/skill_runner.py:90-124`
- Test: `chat_orchestrator/tests/experts/test_skill_step_bindings.py`

- [ ] **Step 1: Write the failing test**

Append to `chat_orchestrator/tests/experts/test_skill_step_bindings.py`:

```python
def test_a_step_without_a_kind_is_an_llm_step():
    """Every existing skills.steps row omits `kind`; none may change behaviour."""
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([{"index": 0, "name": "a", "instruction": "do a"}])

    assert parsed[0].step_type == "llm"
    assert parsed[0].is_skill_step is True


def test_a_function_step_becomes_a_function_parsed_step():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
    ])

    assert parsed[0].step_type == "function"
    assert parsed[0].name == "fetch_grafana_kpis"


def test_a_function_step_is_not_marked_as_a_skill_step():
    """is_skill_step unlocks {{var}} binding for [llm] steps and is
    meaningless for [function] steps, which have their own tool access."""
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis"},
    ])

    assert parsed[0].is_skill_step is False


def test_mixed_steps_keep_their_order():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 1, "name": "reason", "instruction": "analyse {{kpis}}"},
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
    ])

    assert [s.step_type for s in parsed] == ["function", "llm"]
    assert [s.index for s in parsed] == [0, 1]


def test_the_final_step_is_still_forced_to_be_a_response_step():
    from orchestrator.experts.skill_runner import build_parsed_steps

    parsed = build_parsed_steps([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise"},
    ])

    assert parsed[-1].is_response_step is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/experts/test_skill_step_bindings.py -k "function_step or without_a_kind" -v`
Expected: FAIL — `assert 'llm' == 'function'`

- [ ] **Step 3: Implement**

Replace the loop body in `build_parsed_steps`:

```python
    ordered = sorted(skill_steps, key=lambda s: s.get("index", 0))
    parsed: List[ParsedStep] = []
    for i, step in enumerate(ordered):
        is_last = i == len(ordered) - 1
        kind = step.get("kind") or "llm"

        if kind == "function":
            # A function step names a registered handler, which brings its
            # own tool access -- is_skill_step (which unlocks {{var}}
            # binding and read-only tool gating for [llm] steps) is
            # meaningless here and stays False.
            parsed.append(
                ParsedStep(
                    index=i,
                    step_type="function",
                    name=step.get("handler") or f"step_{i + 1}",
                    description=step.get("instruction") or "",
                    is_skill_step=False,
                    is_response_step=is_last or bool(step.get("is_response_step", False)),
                )
            )
            continue

        parsed.append(
            ParsedStep(
                index=i,
                step_type="llm",
                name=step.get("name") or f"step_{i + 1}",
                description=step.get("instruction") or "",
                is_skill_step=True,
                allow_write=bool(step.get("allow_write", False)),
                is_response_step=is_last or bool(step.get("is_response_step", False)),
            )
        )
    return parsed
```

Update the docstring's "Every skill step is an [llm] step" paragraph to describe
both kinds.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/experts/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/skill_runner.py \
        chat_orchestrator/tests/experts/test_skill_step_bindings.py
git commit -m "feat(skills): function steps dispatch to registered handlers"
```

---

### Task 12: Validate function steps at save time

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/skill_validation.py`
- Test: `chat_orchestrator/tests/experts/test_skill_validation_function_steps.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/experts/test_skill_validation_function_steps.py`:

```python
"""Function steps are validated before a skill can be saved."""

from orchestrator.experts.skill_validation import validate_skill_steps


def _errors(steps, exposed=("fetch_grafana_kpis",)):
    return validate_skill_steps(steps, exposed_handlers=list(exposed))


def test_a_known_exposed_handler_validates():
    assert _errors([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise {{kpis}}"},
    ]) == []


def test_an_unknown_handler_is_rejected():
    errors = _errors([{"index": 0, "kind": "function", "handler": "no_such_handler"}])
    assert len(errors) == 1
    assert "no_such_handler" in errors[0].message


def test_a_registered_but_unexposed_handler_is_rejected():
    errors = _errors(
        [{"index": 0, "kind": "function", "handler": "copy_lpp_template"}],
        exposed=("fetch_grafana_kpis",),
    )
    assert len(errors) == 1
    assert errors[0].severity == "error"


def test_a_function_step_without_a_handler_is_rejected():
    errors = _errors([{"index": 0, "kind": "function"}])
    assert len(errors) == 1
    assert "handler" in errors[0].message


def test_an_unknown_kind_is_rejected():
    errors = _errors([{"index": 0, "kind": "webhook", "handler": "x"}])
    assert len(errors) == 1
    assert "webhook" in errors[0].message


def test_a_function_step_output_var_is_readable_downstream():
    """The write comes from the handler, not a '-> {{var}}' clause."""
    assert _errors([
        {"index": 0, "kind": "function", "handler": "fetch_grafana_kpis",
         "output_var": "kpis"},
        {"index": 1, "name": "reply", "instruction": "summarise {{kpis}}"},
    ]) == []


def test_omitting_exposed_handlers_skips_handler_checks():
    """Back-compat: existing callers pass no handler list at all."""
    assert validate_skill_steps(
        [{"index": 0, "name": "a", "instruction": "do a"}]
    ) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/experts/test_skill_validation_function_steps.py -v`
Expected: FAIL with `TypeError: validate_skill_steps() got an unexpected keyword argument 'exposed_handlers'`

- [ ] **Step 3: Implement**

In `skill_validation.py`, extend the signature:

```python
VALID_STEP_KINDS = ("llm", "function")


def validate_skill_steps(
    steps: List[Dict[str, Any]],
    declared_inputs: Optional[List[str]] = None,
    exposed_handlers: Optional[List[str]] = None,
) -> List[ValidationError]:
```

Add a pass before the existing Pass 1, and make the existing `{{var}}` passes skip
function steps (a function step's write comes from its handler, not a `-> {{var}}`
clause):

```python
    # Pass 0: step kind and handler validity. Runs first -- a malformed
    # function step would otherwise be misdiagnosed as a bad llm step.
    for step in ordered_steps:
        index = step.get("index", 0)
        name = step.get("name") or step.get("handler") or f"step_{index}"
        kind = step.get("kind") or "llm"

        if kind not in VALID_STEP_KINDS:
            errors.append(
                ValidationError(
                    index, name,
                    f"unknown step kind {kind!r}; expected one of "
                    f"{', '.join(VALID_STEP_KINDS)}",
                )
            )
            continue

        if kind != "function":
            continue

        handler = step.get("handler")
        if not handler:
            errors.append(
                ValidationError(index, name, "a function step must name a handler")
            )
            continue

        if exposed_handlers is not None and handler not in exposed_handlers:
            errors.append(
                ValidationError(
                    index, name,
                    f"handler {handler!r} is not available to the skill builder; "
                    f"available: {', '.join(exposed_handlers) or '(none)'}",
                )
            )
```

Then, in the existing write-clause pass, skip function steps:

```python
    for step in ordered_steps:
        if (step.get("kind") or "llm") == "function":
            # The write comes from the handler's return value, so there is
            # no '-> {{var}}' clause to check. Its output_var still
            # registers below so later reads resolve.
            if step.get("output_var"):
                seen_output_vars[step["output_var"]] = step.get("index", 0)
            continue
        ...existing body...
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/experts/ -v`
Expected: PASS

- [ ] **Step 5: Pass exposed handlers from the validate endpoint**

Find the `/skills/validate` endpoint and pass
`get_step_registry().builder_exposed_handlers()` into `validate_skill_steps`:

Run: `command grep -rn "validate_skill_steps" --include="*.py" chat_orchestrator/ | command grep -v test`

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/skill_validation.py \
        chat_orchestrator/orchestrator/api/
git add -f chat_orchestrator/tests/experts/test_skill_validation_function_steps.py
git commit -m "feat(skills): validate function steps against exposed handlers"
```

---

### Task 13: Expose the first handlers

**Files:**
- Modify: handler modules under `chat_orchestrator/orchestrator/experts/handlers/`

- [ ] **Step 1: Find the read-only candidates**

Run: `command grep -rn "@register_step(\"fetch_\|@register_step(\"resolve_\|@register_step(\"check_" --include="*.py" chat_orchestrator/orchestrator/experts/handlers/`

- [ ] **Step 2: Opt in exactly three, all read-only**

Start with `fetch_grafana_kpis`, `fetch_chat_chronology` and
`fetch_pending_actions` from `grids_technical_reviewer` — all read-only, all
useful on their own:

```python
@register_step("fetch_grafana_kpis", exposed_to_builder=True)
```

**Do not opt in any handler that writes.** `create_site_folder`,
`copy_lpp_template`, `update_design_distances`, `generate_site_bom`,
`populate_lpp_cells`, `embed_and_store` and `store_module` all mutate external
systems and stay unexposed. Each future opt-in is its own review.

- [ ] **Step 3: Verify**

```python
python -c "
import orchestrator.experts.handlers
from orchestrator.experts.step_registry import get_step_registry
print(get_step_registry().builder_exposed_handlers())
"
```
Expected: exactly the three names

- [ ] **Step 4: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/
git commit -m "feat(skills): expose three read-only handlers to the builder"
```

---

# Phase 5 — Convert the prompt-only experts

### Task 14: Conversion script

**Files:**
- Create: `scripts/convert_expert_to_skill.py`
- Test: `shared/tests/test_convert_expert_to_skill.py`

- [ ] **Step 1: Write the failing test**

Create `shared/tests/test_convert_expert_to_skill.py`:

```python
"""Converting a prompt-only expert into a skill."""

import pytest

from scripts.convert_expert_to_skill import (
    CONVERTIBLE_EXPERTS,
    expert_to_skill,
    split_instructions_into_steps,
)


def test_only_the_five_prompt_only_experts_are_convertible():
    assert set(CONVERTIBLE_EXPERTS) == {
        "grid_analyst", "grid_monitor", "site_visit_tracker", "signing", "community_sizing",
    }


def test_pipeline_experts_are_refused():
    with pytest.raises(ValueError, match="function steps"):
        expert_to_skill("package_generator", "some instructions")


def test_a_single_block_becomes_a_one_step_skill():
    steps = split_instructions_into_steps("You are the Grid Analyst. Do the thing.")
    assert len(steps) == 1
    assert "Grid Analyst" in steps[0]["instruction"]


def test_numbered_instructions_become_separate_steps():
    text = (
        "You are the Grid Analyst.\n\n"
        "1. Analyze performance data\n"
        "2. Identify anomalies\n"
        "3. Generate recommendations\n"
    )
    steps = split_instructions_into_steps(text)
    assert len(steps) == 3
    assert "Analyze performance data" in steps[0]["instruction"]
    assert "Generate recommendations" in steps[2]["instruction"]


def test_the_preamble_is_prepended_to_the_first_step():
    """Dropping 'You are the Grid Analyst' would lose the whole persona."""
    text = "You are the Grid Analyst.\n\n1. Analyze data\n2. Report\n"
    steps = split_instructions_into_steps(text)
    assert "Grid Analyst" in steps[0]["instruction"]


def test_steps_are_indexed_from_zero():
    steps = split_instructions_into_steps("1. A\n2. B\n")
    assert [s["index"] for s in steps] == [0, 1]


def test_every_step_defaults_to_read_only():
    steps = split_instructions_into_steps("1. A\n2. B\n")
    assert all(s["allow_write"] is False for s in steps)


def test_converted_skill_starts_as_a_draft():
    skill = expert_to_skill("grid_analyst", "You are the Grid Analyst. Do the thing.")
    assert skill["status"] == "draft"
    assert skill["staff_only"] is True
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest shared/tests/test_convert_expert_to_skill.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write the script**

Create `scripts/convert_expert_to_skill.py`:

```python
"""Convert a prompt-only expert into a draft skill.

Only the five experts with no workflow at all convert. The four pipeline
experts (context_expert, grids_technical_reviewer, ingestion_expert,
package_generator) have 7-12 registered function handlers each whose order
encodes real external constraints -- package_generator sleeps 60s waiting on
AppSheet between two of them. They stay as code.

Everything lands as status='draft': a converted skill is reviewed and
promoted by a human, never activated by a script.

Usage:
    python scripts/convert_expert_to_skill.py grid_analyst
    python scripts/convert_expert_to_skill.py grid_analyst --apply
"""

from __future__ import annotations

import argparse
import re
import sys
from typing import Any, Dict, List

CONVERTIBLE_EXPERTS = (
    "grid_analyst",
    "grid_monitor",
    "site_visit_tracker",
    "signing",
    "community_sizing",
)

_NUMBERED = re.compile(r"^\s*(\d+)\.\s+(.+)$", re.MULTILINE)


def split_instructions_into_steps(instructions: str) -> List[Dict[str, Any]]:
    """Split an expert's instruction block into skill steps.

    A numbered list becomes one step per item. Anything else becomes a
    single step -- which is still an improvement: the result is nameable,
    schedulable and editable without touching a Google Doc.

    Text before the first numbered item is the persona and is prepended to
    step one; dropping it would lose the expert's identity entirely.
    """
    text = (instructions or "").strip()
    if not text:
        return []

    matches = list(_NUMBERED.finditer(text))
    if not matches:
        return [
            {"index": 0, "name": "step_1", "instruction": text,
             "allow_write": False, "is_response_step": True}
        ]

    preamble = text[: matches[0].start()].strip()
    steps: List[Dict[str, Any]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[match.start() : end].strip()
        body = _NUMBERED.sub(r"\2", body, count=1).strip()
        if i == 0 and preamble:
            body = f"{preamble}\n\n{body}"
        steps.append(
            {
                "index": i,
                "name": f"step_{i + 1}",
                "instruction": body,
                "allow_write": False,
                "is_response_step": i == len(matches) - 1,
            }
        )
    return steps


def expert_to_skill(expert_name: str, instructions: str) -> Dict[str, Any]:
    """Build a draft skills row from a prompt-only expert."""
    if expert_name not in CONVERTIBLE_EXPERTS:
        raise ValueError(
            f"'{expert_name}' has function steps and stays as code. "
            f"Convertible experts: {', '.join(CONVERTIBLE_EXPERTS)}"
        )
    steps = split_instructions_into_steps(instructions)
    if not steps:
        raise ValueError(f"'{expert_name}' has no instruction text to convert")
    title = expert_name.replace("_", " ").title()
    return {
        "slug": expert_name.replace("_", "-"),
        "title": title,
        "summary": f"Converted from the {title} expert. Review before activating.",
        "steps": steps,
        "inputs": [],
        "staff_only": True,
        "status": "draft",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("expert", choices=CONVERTIBLE_EXPERTS)
    parser.add_argument("--apply", action="store_true", help="write; default is a dry run")
    args = parser.parse_args()

    from shared.prompts import PROMPTS

    body, source, _version = PROMPTS.resolve("experts.definitions")
    print(f"experts.definitions resolved from {source.value}, {len(body)} chars\n")

    section = re.search(
        rf"^# Expert: {re.escape(args.expert)}\s*$(.*?)(?=^# Expert: |\Z)",
        PROMPTS.text("experts.definitions"),
        re.MULTILINE | re.DOTALL,
    )
    if not section:
        print(f"No '# Expert: {args.expert}' section found.", file=sys.stderr)
        return 1

    skill = expert_to_skill(args.expert, section.group(1))

    print(f"{skill['title']} -> {len(skill['steps'])} step(s), status={skill['status']}\n")
    for step in skill["steps"]:
        preview = step["instruction"][:200].replace("\n", " ")
        print(f"  {step['index'] + 1}. {preview}...")

    if not args.apply:
        print("\nDry run. Re-run with --apply to create the draft skill.")
        return 0

    from shared.config.db_credentials import chat_db_service_key, chat_db_url
    from supabase import create_client

    client = create_client(chat_db_url(), chat_db_service_key())
    skill["created_by"] = "convert_expert_to_skill.py"
    client.table("skills").insert(skill).execute()
    print(
        f"\nCreated draft skill '{skill['slug']}'. Review it in /skills, promote to "
        f"active, verify, then strike through '# Expert: {args.expert}' in the "
        f"experts.definitions source."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest shared/tests/test_convert_expert_to_skill.py -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add scripts/convert_expert_to_skill.py
git add -f shared/tests/test_convert_expert_to_skill.py
git commit -m "feat(skills): convert prompt-only experts to draft skills"
```

---

### Task 15: Convert the five, one at a time

- [ ] **Step 1: Confirm where the definitions actually resolve from**

```bash
python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('experts.definitions')[1:])"
```

If `DB` or `GDOC`, the strike-through in Step 4 must be applied to *that* source,
not the bundled file.

- [ ] **Step 2: Start with `grid_analyst`**

It is already struck through (disabled) in the live doc, so nothing regresses if
the conversion is wrong.

```bash
python scripts/convert_expert_to_skill.py grid_analyst
python scripts/convert_expert_to_skill.py grid_analyst --apply
```

- [ ] **Step 3: Review and activate**

Open `/skills`, find the `grid-analyst` draft, read every step, fix the summary to
describe when to use it, promote to `active`.

- [ ] **Step 4: Verify, then disable the expert**

Ask the bot to do what the expert did. Confirm the skill is selected and behaves.
Then strike through `# Expert: grid_analyst` in whichever source Step 1 named.

Do not delete the definition — strike-through is the existing disable convention
and keeps it recoverable.

- [ ] **Step 5: Repeat for the remaining four**

`grid_monitor`, `site_visit_tracker`, `signing`, `community_sizing` — one at a
time, verifying each before starting the next. Do not batch them.

- [ ] **Step 6: Record which converted cleanly in the PR**

---

### Task 16: Final verification and PR

- [ ] **Step 1: Run every suite**

Run: `python -m pytest shared/tests/ chat_orchestrator/tests/ anansi_app/tests/ -q`
Expected: PASS

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. `git add -f` any untracked test files, then re-run.

- [ ] **Step 3: Confirm every test file is tracked**

Run: `git log --stat --oneline -20 | command grep "tests/"`
Cross-check against the 6 test files this plan creates.

- [ ] **Step 4: Confirm the migration is applied**

```sql
SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = 'skills_status_chk';
```
Expected: includes `'draft'`

- [ ] **Step 5: Open the PR**

```bash
git push -u origin feat/skills-lifecycle
gh pr create --title "feat(skills): skills lifecycle, scheduling UI and function steps" --body "$(cat <<'EOF'
Turns the Skill Builder from a single ephemeral page into a managed list of named,
draftable, schedulable skills, and lets skill steps call the registered Python
handlers expert workflows already use.

Stage 3 of 4 in the context-architecture programme. Independent of P1 and P2 —
touches no files they touch.

Spec: `docs/superpowers/specs/2026-08-19-skills-lifecycle-and-function-steps-design.md`
Plan: `docs/superpowers/plans/2026-08-22-p3-skills-lifecycle-and-function-steps.md`

## Migration — apply by hand before merging

- `00NN_skill_draft_status.sql`

## What was already there

Most of the backend: `user_schedules.skill_id` fan-out (0013), `ParsedStep.step_type`
already accepting `"function"`, and `SkillCatalogStore` already filtering to
`status='active'` — which is why a draft is invisible to the model with no code
change. This PR is mostly UI plus one line in `build_parsed_steps`.

## Experts converted

Only the five with no workflow at all: __. The four pipeline experts
(context_expert, grids_technical_reviewer, ingestion_expert, package_generator)
stay as code — 7-12 function handlers each whose order encodes external
constraints.

## Handlers exposed to the builder

Three, all read-only: __. Every write-capable handler stays unexposed.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Handoff to P4

Record in the PR description:

- Which experts converted cleanly and which needed hand-editing
- Which handlers were exposed to the builder
- The final migration number used, so P4 takes the next one
