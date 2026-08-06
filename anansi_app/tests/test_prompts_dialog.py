"""Regression tests for the Prompts detail dialog's viewport behavior."""

from pathlib import Path

PROMPTS_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "prompts.py"
)


def test_prompts_dialog_has_viewport_scroll_container():
    """The Google Doc section can grow a wrapped warning banner (toggle on),
    pushing the Reload cache / Revert / Save draft / Save & Publish row below
    the fold. Without an explicit scroll container that row becomes
    unreachable -- same failure mode the Broadcast dialog had, fixed there
    with this exact style pair (see test_broadcast_dialog.py)."""
    src = PROMPTS_PATH.read_text()

    assert "max-height: calc(100dvh - 32px)" in src
    assert "min-height: 0; overflow-y: auto" in src


def test_prompts_dialog_body_defaults_to_preview():
    """Opening a prompt is almost always to read it, not edit it."""
    src = PROMPTS_PATH.read_text()

    assert 'ui.toggle(["Edit", "Preview"], value="Preview")' in src
