"""Procedure -> context module migration: pure functions only."""

import pytest

from scripts.migrate_procedures_to_modules import (
    detect_slug_collisions,
    procedure_to_module,
    slug_for_title,
    truncate_body_for_prompt,
)


def test_slug_is_derived_from_the_title_not_the_number():
    """Numbering is editorial and changes; a slug is a stable address."""
    assert slug_for_title("Commissioning Failed Troubleshooting") == (
        "procedure-commissioning-failed-troubleshooting"
    )


def test_slug_lowercases_and_hyphenates():
    assert slug_for_title("Meter Comms Loss") == "procedure-meter-comms-loss"


def test_slug_strips_punctuation():
    assert slug_for_title("DCU won't connect (E-402)") == "procedure-dcu-wont-connect-e-402"


def test_slug_collapses_repeated_separators():
    assert slug_for_title("Battery  --  Low  SoC") == "procedure-battery-low-soc"


def test_slug_rejects_an_empty_title():
    with pytest.raises(ValueError, match="empty"):
        slug_for_title("   ")


class _Procedure:
    def __init__(self, number, title, purpose, full_text):
        self.id = f"procedure_{number}"
        self.number = number
        self.title = title
        self.purpose = purpose
        self.full_text = full_text


def _proc(title="Commissioning Failed", purpose="Covers failed commissioning.",
          full_text="## Procedure 1: Commissioning Failed\n\nSteps..."):
    return _Procedure(1, title, purpose, full_text)


def test_module_carries_title_body_and_slug():
    module = procedure_to_module(_proc())
    assert module["slug"] == "procedure-commissioning-failed"
    assert module["title"] == "Commissioning Failed"
    assert module["body"] == "## Procedure 1: Commissioning Failed\n\nSteps..."


def test_module_is_manual_and_writes_no_mode():
    module = procedure_to_module(_proc())
    assert module["source"] == "manual"
    assert module["scope"] == "sector"
    # `mode` is retired (migration 0029): nothing reads it, and the column
    # has a NOT NULL DEFAULT, so the row must not carry one.
    assert "mode" not in module


def test_purpose_becomes_the_summary_when_no_override_is_given():
    assert procedure_to_module(_proc())["summary"] == "Covers failed commissioning."


def test_a_generated_summary_overrides_the_purpose():
    module = procedure_to_module(_proc(), summary="Meter stays pending, no callback.")
    assert module["summary"] == "Meter stays pending, no callback."


def test_a_procedure_with_no_purpose_and_no_summary_is_rejected():
    """An on_demand module with a blank summary is invisible to the model."""
    with pytest.raises(ValueError, match="summary"):
        procedure_to_module(_proc(purpose=""))


def test_module_is_tagged_for_filtering():
    assert "procedure" in procedure_to_module(_proc())["tags"]


def test_no_collisions_for_distinct_titles():
    modules = [
        {"slug": "procedure-a", "title": "A"},
        {"slug": "procedure-b", "title": "B"},
    ]
    assert detect_slug_collisions(modules, existing_slugs=set()) == []


def test_reports_two_procedures_sharing_a_slug():
    modules = [
        {"slug": "procedure-meter-comms", "title": "Meter Comms"},
        {"slug": "procedure-meter-comms", "title": "Meter  comms!"},
    ]
    collisions = detect_slug_collisions(modules, existing_slugs=set())
    assert len(collisions) == 1
    assert "procedure-meter-comms" in collisions[0]


def test_reports_a_slug_that_already_exists_in_the_database():
    modules = [{"slug": "procedure-meter-comms", "title": "Meter Comms"}]
    collisions = detect_slug_collisions(
        modules, existing_slugs={"procedure-meter-comms"}
    )
    assert len(collisions) == 1
    assert "already exists" in collisions[0]


def test_summary_generation_prompt_renders():
    from shared.prompts import PromptLibrary

    library = PromptLibrary()
    text = library.text(
        "procedure.module_summary",
        title="Commissioning Failed",
        purpose="Covers failed commissioning.",
        body="1. Check the DCU link...",
    )
    assert "Commissioning Failed" in text
    assert "symptom" in text.lower()


def test_generated_summaries_are_capped_for_review():
    assert len(truncate_body_for_prompt("x" * 10_000)) <= 4000
    assert truncate_body_for_prompt("short") == "short"
