"""Skills list page: row building and display rules."""

from types import SimpleNamespace

import pytest
from nicegui_app.pages.skills import (
    STATUS_COLORS,
    build_skill_rows,
    format_schedule,
)


class _FakeElement:
    def classes(self, _classes):
        return self

    def props(self, _props):
        return self

    def clear(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@pytest.mark.asyncio
async def test_workflows_page_renders_requested_icon(monkeypatch):
    from nicegui import run
    from nicegui_app import services_access
    from nicegui_app.pages import skills

    labels = []
    element = _FakeElement()
    monkeypatch.setattr(skills.ui, "label", lambda text: labels.append(text) or element, raising=False)
    monkeypatch.setattr(skills.ui, "column", lambda: element, raising=False)
    monkeypatch.setattr(skills.ui, "row", lambda: element, raising=False)
    monkeypatch.setattr(skills.ui, "button", lambda *_args, **_kwargs: element, raising=False)
    monkeypatch.setattr(
        services_access,
        "get_skill_builder_service",
        lambda: SimpleNamespace(list_skills=lambda: [], schedule_summaries=lambda: {}),
    )

    async def io_bound(function):
        return function()

    monkeypatch.setattr(run, "io_bound", io_bound, raising=False)

    await skills.render({"email": "ops@example.com"})

    assert labels[0] == "🎬 Workflows"


_STORED_STEPS = [
    {"index": 0, "name": "find", "instruction": "List all open tickets.",
     "output_var": "find", "allow_write": False, "is_response_step": False},
    {"index": 1, "name": "summarize", "instruction": "Summarize {{find}}.",
     "output_var": None, "allow_write": False, "is_response_step": True},
]


def _skill(slug="a", status="active", step_count=3, staff_only=True, steps=None):
    # list_skills returns both: `steps` straight from the DB column and
    # `step_count` computed from it. A fixture carrying only step_count
    # would hide exactly the bug the steps assertions below pin.
    return {
        "id": slug, "slug": slug, "title": slug.upper(), "summary": "Does a thing.",
        "step_count": step_count, "staff_only": staff_only, "status": status,
        "steps": _STORED_STEPS if steps is None else steps,
        "created_by": "ops@example.com", "updated_at": "2026-08-22T10:00:00Z",
    }


def test_rows_carry_the_display_fields():
    row = build_skill_rows([_skill()], schedules={})[0]
    assert row["title"] == "A"
    assert row["step_count"] == 3
    assert row["status"] == "active"
    assert row["audience"] == "Staff only"


def test_rows_carry_the_stored_steps_for_the_editor():
    """The row build_skill_rows returns is the same object _render_row hands
    to _open_editor, which seeds the step builder from row["steps"]. Leaving
    steps out of the projection opened every workflow with zero steps while
    the list still showed the right count (step_count is computed upstream),
    which is what made the regression so quiet.
    """
    row = build_skill_rows([_skill()], schedules={})[0]
    assert row["steps"] == _STORED_STEPS


def test_rows_fall_back_to_no_steps_when_the_column_is_absent():
    skill = _skill()
    del skill["steps"]

    row = build_skill_rows([skill], schedules={})[0]

    assert row["steps"] == []


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


def test_every_status_has_a_colour():
    for status in ("draft", "active", "disabled", "unusable"):
        assert status in STATUS_COLORS


def test_drafts_are_visually_distinct_from_active():
    assert STATUS_COLORS["draft"] != STATUS_COLORS["active"]


def test_promotion_to_active_requires_valid_steps():
    from nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0, "name": "a", "instruction": "do a"}],
        validation_errors=[],
        title="A",
    )
    assert ok is True
    assert reason == ""


def test_promotion_blocked_by_a_validation_error():
    from nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0}],
        validation_errors=[{"severity": "error", "message": "unresolved {{x}}"}],
        title="A",
    )
    assert ok is False
    assert "unresolved" in reason


def test_promotion_not_blocked_by_a_warning():
    from nicegui_app.pages.skills import can_promote_to_active

    ok, _ = can_promote_to_active(
        steps=[{"index": 0}],
        validation_errors=[{"severity": "warning", "message": "unused write"}],
        title="A",
    )
    assert ok is True


def test_promotion_blocked_with_no_steps():
    from nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(steps=[], validation_errors=[], title="A")
    assert ok is False
    assert "step" in reason.lower()


def test_promotion_blocked_without_a_title():
    from nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0}], validation_errors=[], title="  "
    )
    assert ok is False
    assert "title" in reason.lower()


def test_promotion_blocked_when_a_step_captured_a_tool_error():
    """A step whose tools errored saved an apology, not a result."""
    from nicegui_app.pages.skills import can_promote_to_active

    ok, reason = can_promote_to_active(
        steps=[{"index": 0, "name": "a", "had_tool_error": True}],
        validation_errors=[],
        title="A",
    )
    assert ok is False
    assert "error" in reason.lower()


def test_a_draft_can_be_saved_with_just_a_skill_name():
    from nicegui_app.pages.skills import can_save_as_draft

    assert can_save_as_draft(skill_name="grid-health", summary="") is True


def test_a_draft_can_be_saved_with_just_a_summary():
    """The /skill name box is optional (item d) -- an auto-generated
    summary (item b) is enough to derive a fallback title from."""
    from nicegui_app.pages.skills import can_save_as_draft

    assert can_save_as_draft(skill_name="", summary="Checks grid health weekly.") is True


def test_a_draft_needs_a_name_or_a_summary():
    from nicegui_app.pages.skills import can_save_as_draft

    assert can_save_as_draft(skill_name="  ", summary="  ") is False
    assert can_save_as_draft(skill_name="", summary="") is False


def test_fallback_title_reuses_a_short_summary_verbatim():
    from nicegui_app.pages.skills import derive_fallback_title

    assert derive_fallback_title("Checks grid health weekly.") == "Checks grid health weekly."


def test_fallback_title_trims_a_long_summary_at_a_word_boundary():
    from nicegui_app.pages.skills import derive_fallback_title

    summary = (
        "Retrieves every open ticket across all grids and summarizes them by "
        "severity and grid for the weekly ops review."
    )
    title = derive_fallback_title(summary)

    assert len(title) <= 61  # 60 chars + the ellipsis character
    assert title.endswith("…")
    assert not title[:-1].endswith(" ")  # trimmed at a word boundary, not mid-word


def test_fallback_title_is_blank_for_a_blank_summary():
    from nicegui_app.pages.skills import derive_fallback_title

    assert derive_fallback_title("") == ""
    assert derive_fallback_title("   ") == ""


def test_a_step_that_escalated_is_flagged():
    from nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {
        "response_messages": [
            {
                "role": "model",
                "content": "I'm sorry, I'm unable to retrieve the list of open tickets. #NXTAction",
                "function_call": {"name": "escalate_to_support"},
            }
        ],
    }
    assert _step_had_tool_error(step) is True


def test_a_step_with_an_explicit_tool_error_is_flagged():
    from nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {
        "response_messages": [
            {
                "role": "tool",
                "content": None,
                "function_call": {"name": "get_x"},
                "tool_result": {"error": "timeout"},
            }
        ],
    }
    assert _step_had_tool_error(step) is True


def test_a_clean_step_is_not_flagged():
    from nicegui_app.pages.skill_builder import _step_had_tool_error

    step = {
        "response_messages": [
            {
                "role": "tool",
                "content": None,
                "function_call": {"name": "customer_get_my_open_issues"},
                "tool_result": {"count": 4},
            },
            {"role": "model", "content": "Found 4 open tickets: ...", "function_call": None},
        ],
    }
    assert _step_had_tool_error(step) is False


def test_a_step_with_no_tool_calls_is_not_flagged():
    from nicegui_app.pages.skill_builder import _step_had_tool_error

    assert _step_had_tool_error({"response_messages": []}) is False


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


def test_derive_steps_payload_carries_a_mock_toggle_edit_on_a_pending_step():
    """Task 5.3 (docs/superpowers/plans/2026-08-20-expert-steps-as-skill-
    tools.md): _render_pending_step's mock switch mutates the SAME dict
    object stored in state["initial_steps"] in place (s.__setitem__("mock",
    ...), not a copy) -- confirms that mutation survives into the payload
    _derive_steps_payload builds for Save, the same way it already proves
    for a plain pass-through field above."""
    from nicegui_app.pages.skill_builder import _derive_steps_payload

    stored_step = {
        "index": 0, "kind": "function", "handler": "write_review_section",
        "mutates": True, "is_response_step": True,
    }
    state = {"steps": [], "flags": {}, "initial_steps": [stored_step]}

    # Simulate the switch's on_value_change callback -- mutates the ORIGINAL
    # dict object in place, exactly as the real callback does.
    stored_step["mock"] = False

    steps = _derive_steps_payload(state)

    assert steps[0]["mock"] is False
    assert steps[0]["handler"] == "write_review_section"  # untouched fields still ride through
