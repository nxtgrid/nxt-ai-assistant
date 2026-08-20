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
