"""Episodic distillation batch: selection and write rules."""

from scripts.distill_episodic_memory import (
    anchors_to_refresh,
    build_distillation_prompt,
)


def _row(anchor_id, edited_by=None, message_count=50):
    return {
        "anchor_type": "grid",
        "anchor_id": anchor_id,
        "edited_by": edited_by,
        "message_count": message_count,
    }


def test_refreshes_an_anchor_with_no_existing_row():
    assert anchors_to_refresh(["Alpha"], existing=[]) == ["Alpha"]


def test_refreshes_an_existing_generated_row():
    assert anchors_to_refresh(["Alpha"], existing=[_row("Alpha")]) == ["Alpha"]


def test_never_overwrites_a_hand_edited_row():
    existing = [_row("Alpha", edited_by="ops@example.com")]
    assert anchors_to_refresh(["Alpha"], existing=existing) == []


def test_refreshes_only_the_anchors_asked_for():
    existing = [_row("Alpha"), _row("Beta")]
    assert anchors_to_refresh(["Beta"], existing=existing) == ["Beta"]


def test_prompt_includes_the_anchor_and_the_messages():
    prompt = build_distillation_prompt("Alpha", ["inverter tripped", "replaced fuse"])
    assert "Alpha" in prompt
    assert "inverter tripped" in prompt
    assert "replaced fuse" in prompt


def test_prompt_asks_for_durable_lessons_not_a_transcript():
    prompt = build_distillation_prompt("Alpha", ["x"])
    assert "transcript" in prompt.lower()
