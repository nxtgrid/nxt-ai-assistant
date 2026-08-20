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
