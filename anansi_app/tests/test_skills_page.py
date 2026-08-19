"""Skills list page: row building and display rules."""

from nicegui_app.pages.skills import (
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


def test_a_draft_can_always_be_saved():
    from nicegui_app.pages.skills import can_save_as_draft

    assert can_save_as_draft(title="A") is True
    assert can_save_as_draft(title="  ") is False


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
