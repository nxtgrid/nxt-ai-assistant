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
