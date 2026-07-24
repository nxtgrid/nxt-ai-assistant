"""Regression tests for the Broadcast dialog's viewport behavior."""

from pathlib import Path


BROADCAST_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "broadcast.py"
)


def test_broadcast_dialog_card_is_viewport_safe():
    src = BROADCAST_PATH.read_text()

    assert "max-height: calc(100dvh - 32px)" in src
    assert "overflow-y: auto" in src
