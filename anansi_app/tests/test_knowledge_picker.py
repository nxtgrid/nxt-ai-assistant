"""Tests for the shared prompt/skill <-> knowledge-module picker widget
(moved and generalized from the Prompts page's original Context tab --
see test_knowledge_modules_page.py's git history for the pre-move tests
this file replaces)."""

import ast
from pathlib import Path

from nicegui_app.pages.knowledge_picker import PickerRow, build_picker_rows, filter_picker_rows
from shared.prompts.knowledge import KnowledgeModule

KNOWLEDGE_PICKER_PATH = (
    Path(__file__).resolve().parents[1] / "nicegui_app" / "pages" / "knowledge_picker.py"
)


def _module(slug, chars=40, summary="s", is_jit=False):
    from types import SimpleNamespace

    return SimpleNamespace(
        slug=slug, title=slug.title(), body="b" * chars, summary=summary, is_jit=is_jit,
    )


def test_picker_rows_list_all_modules_with_attached_state():
    rows = build_picker_rows([_module("beta"), _module("alpha")], {"alpha": True})
    assert rows == [
        PickerRow(slug="alpha", title="Alpha", chars=40, checked=True, summary="s"),
        PickerRow(slug="beta", title="Beta", chars=40, checked=False, summary="s"),
    ]


def test_picker_rows_with_no_pins_checks_nothing():
    rows = build_picker_rows([_module("alpha")], {})
    assert [r.checked for r in rows] == [False]


def test_picker_rows_carries_is_jit_through():
    rows = build_picker_rows([_module("live-one", is_jit=True)], {})
    assert rows[0].is_jit is True


def _picker_rows_fixture():
    return [
        PickerRow(
            slug="azimuth-calculation", title="Azimuth Calculation",
            chars=318, checked=False,
            summary="How PV azimuth is measured.",
        ),
        PickerRow(
            slug="victron-led", title="Victron Quattro Codes",
            chars=2438, checked=True,
            summary="Decoding inverter LED error states.",
        ),
    ]


def test_filter_picker_rows_matches_slug_title_and_summary():
    rows = _picker_rows_fixture()
    assert [r.slug for r in filter_picker_rows(rows, "azimuth")] == ["azimuth-calculation"]
    assert [r.slug for r in filter_picker_rows(rows, "LED")] == ["victron-led"]
    assert len(filter_picker_rows(rows, "")) == 2


# ── The same null-body / JIT hazard build_module_rows has ──────────────────
# build_picker_rows lists every module (including provider-backed ones) so
# an operator can pin/unpin it to a prompt or skill. It has the exact same
# len(module.body) crash build_module_rows had -- fixed alongside it,
# since a JIT module now genuinely exists once the seed script runs.


def test_picker_rows_handles_a_jit_module_without_crashing():
    rows = build_picker_rows(
        [KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                         body=None, source="graph")],
        {},
    )
    assert rows[0].chars == 0
    assert rows[0].is_jit is True


def test_picker_rows_marks_manual_modules_as_not_jit():
    rows = build_picker_rows([_module("comms")], {})
    assert rows[0].is_jit is False


def _caught_exception_names(src: str, func_name: str) -> "set[str]":
    """Exception type names a top-level ``except`` clause in async def
    ``func_name`` catches, e.g. {"PermissionError", "RuntimeError"}. Mirrors
    test_prompts_dialog.py's own copy of this check -- both dialogs need the
    same "does this save handler catch broad Exception" guarantee, and this
    module's save_pins is where the Prompts dialog's version moved from."""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == func_name:
            names: "set[str]" = set()
            for handler in ast.walk(node):
                if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
                    continue
                candidates = (
                    handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
                )
                names.update(c.id for c in candidates if isinstance(c, ast.Name))
            return names
    raise AssertionError(f"no `async def {func_name}` found in {KNOWLEDGE_PICKER_PATH}")


def test_save_pins_surfaces_unexpected_errors():
    """NiceGUI never surfaces an exception an event handler doesn't catch
    itself, so save_pins must catch a broad Exception, not just
    PermissionError/RuntimeError, or a real write failure leaves the
    operator with a dialog that silently did nothing."""
    src = KNOWLEDGE_PICKER_PATH.read_text()
    assert "Exception" in _caught_exception_names(src, "save_pins")
