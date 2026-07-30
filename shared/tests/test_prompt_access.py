"""Per-prompt group access control."""

import pytest

from shared.prompts.access import can_edit_prompt, can_publish_prompt, can_view_prompt
from shared.prompts.spec import AccessSpec, PromptSpec


def _spec(prompt_id="a.b", overridable=True, **access):
    return PromptSpec(
        id=prompt_id,
        description="d",
        body="x",
        checksum="c",
        overridable=overridable,
        access=AccessSpec(**access),
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)


def test_group_member_gets_the_verb(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is True


def test_non_member_is_denied(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "bob@x.com") is False


def test_edit_does_not_imply_publish(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    spec = _spec(edit=["ops"], publish=["eng"])
    assert can_edit_prompt(spec, "ada@x.com") is True
    assert can_publish_prompt(spec, "ada@x.com") is False


def test_admin_passes_every_verb_without_being_listed(monkeypatch):
    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = _spec(view=[], edit=[], publish=[])
    assert can_view_prompt(spec, "root@x.com") is True
    assert can_edit_prompt(spec, "root@x.com") is True
    assert can_publish_prompt(spec, "root@x.com") is True


def test_non_overridable_beats_admin(monkeypatch):
    """A PR-only prompt is PR-only. Admin gets view, never edit."""
    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = _spec(overridable=False)
    assert can_view_prompt(spec, "root@x.com") is True
    assert can_edit_prompt(spec, "root@x.com") is False
    assert can_publish_prompt(spec, "root@x.com") is False


def test_non_overridable_beats_an_explicit_grant(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(overridable=False, edit=["ops"]), "ada@x.com") is False


def test_empty_whitelists_grant_nothing(monkeypatch):
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is False


def test_blank_email_is_denied(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "") is False


def test_membership_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "Ada@X.com")
    assert can_edit_prompt(_spec(edit=["ops"]), "ada@x.com") is True


def test_dev_bypass_grants_everything_except_non_overridable(monkeypatch):
    monkeypatch.setenv("GRID_DESIGN_DEV_NO_AUTH", "1")
    assert can_edit_prompt(_spec(), "anyone@x.com") is True
    assert can_edit_prompt(_spec(overridable=False), "anyone@x.com") is False


def test_view_respects_bound_groups_when_not_admin(monkeypatch):
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    spec = _spec(view=["ops"])
    assert can_view_prompt(spec, "ada@x.com") is True
    assert can_view_prompt(spec, "stranger@x.com") is False
