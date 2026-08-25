"""Regression tests for the Context (knowledge module) edit dialog."""

from pathlib import Path

KNOWLEDGE_MODULES_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "knowledge_modules.py"
)


def test_knowledge_modules_dialog_body_defaults_to_preview():
    """Opening a module is almost always to read it, not edit it -- same
    default as the Prompts detail dialog (see test_prompts_dialog.py)."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert 'ui.toggle(["Edit", "Preview"], value="Preview")' in src


def test_knowledge_modules_dialog_has_viewport_scroll_container():
    """A rendered Preview can be arbitrarily tall (long modules commonly have
    tables and multiple headings). Without an explicit scroll container the
    body overflows past the dialog and overlaps the fields below it --
    same failure mode the Broadcast and Prompts dialogs had, fixed there
    with this exact style pair (see test_broadcast_dialog.py,
    test_prompts_dialog.py)."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "max-height: calc(100dvh - 32px); overflow-y: auto" in src


def test_the_dialog_offers_a_document_source():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "Google Doc or Sheet" in src


def test_the_preview_resolves_as_the_viewing_operator():
    """Preview must be a dry run of the real gate, not a second gate that
    could disagree with it. It previously passed no caller identity at all,
    so a document module would resolve under whatever the provider defaulted
    to rather than under the operator's own Drive access."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "user_email=user_email" in src


# ── Regression tests below pin fixes for four issues found reviewing this
# dialog (2026-08-25): the Slug field never suggested anything, an
# access-denied save gave no reason, Preview couldn't resolve an unsaved
# document, and switching Source to a Google Doc left the body pane looking
# editable. None of these are exercisable directly -- conftest.py stubs
# nicegui at import time (see its own docstring), so nothing that touches
# ``ui.*`` inside this dialog actually runs in tests, only pure functions
# do (covered with real behavior in test_knowledge_modules_page.py). These
# assertions pin the wiring itself, the same way the tests above do.


def test_slug_autofills_from_the_title_for_a_new_module_only():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "title_input.on_value_change(_on_title_change)" in src
    # Guarded by `if existing is None`, directly above the wiring -- an
    # existing module's slug must never be rewritten by a later title edit.
    assert "if existing is None:\n            # Live" in src


def test_source_select_reactively_updates_the_body_pane():
    """The bug this fixes: `source` was frozen from `existing` at dialog-
    open time, so choosing "Google Doc or Sheet" for a brand-new module
    never made the body pane read-only or resolved a preview -- both stayed
    keyed off the stale variable. The fix reads source_select.value live
    and re-applies on every change, not just at open."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "source_select.on_value_change(_on_source_change)" in src
    assert "def _apply_source_view() -> None:" in src
    assert "body_is_editable(source_select.value)" in src


def test_pasting_a_document_link_refreshes_the_preview_on_blur():
    """Not on_value_change: that fires per keystroke (see ui.input's own
    docstring), which would hit the Drive API on every character typed or
    pasted. blur fires once, when the operator is done."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert 'doc_ref_input.on("blur", _refresh_preview, [])' in src
    assert 'doc_tab_input.on("blur", _refresh_preview, [])' in src


def test_preview_resolves_a_document_module_before_it_is_saved():
    """The bug: _resolved_body() returned "save it first" for any unsaved
    module, so a document module's Preview could never show real content
    (or a real access denial) until after Save -- exactly backwards, since
    Preview exists to check before committing."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "draft_gdoc_module(" in src
    assert '"_Paste a Google Doc or Sheet link above to preview it._"' in src


def test_save_reports_why_access_was_denied_not_a_canned_message():
    """The bug report this fixes: "you don't have access" when the operator
    was certain they did -- because the check requires their own email on
    the file's sharing list specifically; sharing it with the bot alone, or
    access via a Google Group, both look like "access" to a human but don't
    satisfy it. check_access's .reason says which, and names the checked
    email -- see shared/utils/drive_permissions.py."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "from shared.utils.drive_permissions import check_access" in src
    assert "ui.notify(access.reason, type=\"negative\")" in src
    # The old canned string must be gone, not just supplemented.
    assert "You don't have access to that document, so you can't attach it." not in src


def test_save_rejects_a_colliding_slug_by_name():
    """Slug is a visible, editable field here (unlike the Skills editor's
    hidden autofill) -- a clash should read as "choose another," not
    disappear into a raw database UNIQUE-constraint error."""
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "taken_slugs=taken_slugs" in src


def test_prompts_picker_uses_the_shared_searchable_widget_not_a_chip_select():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert "use-chips" not in src
    # Not a single exact-formatting substring: render_entity_picker's real
    # call wraps across lines (ruff/black would reformat a one-liner here
    # anyway), so check the call and its argument are both present instead.
    assert "render_entity_picker(" in src
    assert "prompt_rows," in src


def test_skills_picker_is_present_and_distinctly_labeled():
    src = KNOWLEDGE_MODULES_PATH.read_text()

    assert 'label="Used by these skills"' in src
    assert "resolve_pins_to_save(" in src
