"""perms.py delegates prompt access checks to shared.prompts.access."""

from grid_app.lib import perms


def test_can_view_prompt_delegates_to_shared_access(monkeypatch):
    from shared.prompts.spec import PromptSpec

    spec = PromptSpec(id="a.b", description="d", body="x", checksum="c")
    monkeypatch.setattr("shared.prompts.PROMPTS.spec", lambda pid: spec)
    monkeypatch.setattr("shared.prompts.access.can_view_prompt", lambda s, email: s is spec)
    assert perms.can_view_prompt("a.b", "ada@x.com") is True


def test_can_edit_prompt_uses_current_email_when_none_given(monkeypatch):
    from shared.prompts.spec import PromptSpec

    spec = PromptSpec(id="a.b", description="d", body="x", checksum="c")
    monkeypatch.setattr("shared.prompts.PROMPTS.spec", lambda pid: spec)
    seen = {}

    def fake_check(s, email):
        seen["email"] = email
        return True

    monkeypatch.setattr("shared.prompts.access.can_edit_prompt", fake_check)
    monkeypatch.setattr(perms, "current_email", lambda: "current@x.com")
    assert perms.can_edit_prompt("a.b") is True
    assert seen["email"] == "current@x.com"


def test_can_publish_prompt_denies_by_default(monkeypatch):
    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    assert perms.can_publish_prompt("customer.system", "nobody@x.com") is False
