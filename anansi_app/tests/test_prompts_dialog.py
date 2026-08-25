"""Regression tests for the Prompts detail dialog's viewport behavior."""

import ast
from pathlib import Path

import pytest

PROMPTS_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "prompts.py"
)


def _caught_exception_names(src: str, func_name: str) -> set[str]:
    """Exception type names a top-level ``except`` clause in async def
    ``func_name`` catches, e.g. {"PermissionError", "RuntimeError"}."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            names: set[str] = set()
            for handler in ast.walk(node):
                if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                    continue
                candidates = (
                    handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                )
                names.update(c.id for c in candidates if isinstance(c, ast.Name))
            return names
    raise AssertionError(f"no `async def {func_name}` found in {PROMPTS_PATH}")


@pytest.mark.parametrize(
    "handler_name",
    [
        "save_doc_binding",
        "save_tier",
        "revert_tier",
        "save_draft",
        "publish_latest",
        "revert",
    ],
)
def test_dialog_save_handlers_surface_unexpected_errors(handler_name):
    """Every save-style button handler in the prompt detail dialog must catch
    a broad Exception, not just PermissionError/RuntimeError.

    NiceGUI never surfaces an exception an event handler doesn't catch itself
    -- by default it's only logged server-side (see nicegui/app/app.py's
    ``App._exception_handlers = [log.exception]``), so the operator sees no
    toast, the dialog stays open, and the button looks like it did nothing.
    That's exactly what "Save tier" does today: it (and its siblings
    save_doc_binding/save_draft/publish_latest/revert) writes through
    OverrideStore to a Supabase/postgrest table, and any real write failure
    (``postgrest.exceptions.APIError`` -- e.g. because
    ``prompt_model_overrides`` from db/migrations/0015_prompt_model_overrides.sql
    hasn't been applied yet -- is a plain ``Exception`` subclass, not a
    ``RuntimeError``) hits exactly this gap.

    ``save_pins`` used to be this dialog's proof that the pattern works and
    was covered here too -- it now lives in knowledge_picker.py's
    render_module_picker (see test_knowledge_picker.py's own copy of this
    check) since the Context tab moved there.
    """
    src = PROMPTS_PATH.read_text()
    assert "Exception" in _caught_exception_names(src, handler_name)


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


def test_context_tab_delegates_to_the_shared_picker():
    src = PROMPTS_PATH.read_text()

    assert "render_module_picker(row.prompt_id, k_store, user_email, show_budget=True)" in src
