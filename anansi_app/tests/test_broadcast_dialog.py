"""Regression tests for the Broadcast dialog's viewport behavior."""

from pathlib import Path

BROADCAST_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "broadcast.py"
)


def test_broadcast_dialog_has_viewport_scroll_container():
    src = BROADCAST_PATH.read_text()

    assert "max-height: calc(100dvh - 32px)" in src
    assert "min-height: 0; overflow-y: auto" in src


def test_image_upload_handler_uses_current_nicegui_upload_event_shape():
    """Regression test for a silent image-attachment failure (2026-09-10):
    NiceGUI's ``UploadEventArguments`` moved from top-level ``content``/
    ``name``/``type`` attributes to a nested ``file: FileUpload`` object
    (``name``/``content_type``/async ``read()``) — the installed
    ``nicegui>=2.0.0`` here resolves to 3.15.0, which only has the new
    shape (it's a slotted dataclass, so the old attributes raise
    ``AttributeError`` rather than being merely absent-but-tolerated).

    ``anansi_app/tests/conftest.py`` stubs ``nicegui`` at the module level
    so this file can't construct a real ``ui.upload()`` and fire an actual
    event through it (CI's Validate job never installs
    ``anansi_app/requirements.txt`` -- see that stub's docstring) -- hence
    a source-pattern check rather than a behavioral one, same technique as
    ``test_broadcast_dialog_has_viewport_scroll_container`` above.
    """
    src = BROADCAST_PATH.read_text()

    assert "await e.file.read()" in src
    assert "e.file.name" in src
    assert "e.file.content_type" in src
    # The old, broken attribute paths (top-level content/name/type on the
    # event itself, not nested under `.file`) must never come back.
    assert "e.content.read()" not in src
    assert "filename=e.name" not in src
    assert "e.type or" not in src
