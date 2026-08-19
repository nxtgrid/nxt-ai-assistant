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
