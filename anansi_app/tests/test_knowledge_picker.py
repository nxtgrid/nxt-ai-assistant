"""Tests for the shared prompt/skill <-> knowledge-module picker widget
(moved and generalized from the Prompts page's original Context tab --
see test_knowledge_modules_page.py's git history for the pre-move tests
this file replaces)."""

import ast
from pathlib import Path

from nicegui_app.pages.knowledge_picker import (
    PickerRow,
    build_picker_rows,
    filter_picker_rows,
    rows_to_display,
)

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


# ── The reverse-direction picker's own scaling hazard ───────────────────────
# render_entity_picker (knowledge_modules.py's "Used by these prompts"/"these
# skills") can face dozens of candidates -- every registered prompt id today,
# every skill ever created tomorrow. Rendering all of them, unfiltered, the
# moment the Edit-module dialog opens produced a single websocket update big
# enough to trip NiceGUI's own message-size limit -- the dialog would open
# with the connection dropping mid-render, leaving both "Used by" sections
# (and everything below them) blank with no error the operator could see.
# rows_to_display is the fix: an empty search shows only what's already
# pinned (always small in practice), and typing a query searches the full
# candidate set regardless of pinned state, so a new pin stays discoverable.


def test_rows_to_display_with_empty_query_shows_only_selected_rows():
    rows = _picker_rows_fixture()  # azimuth-calculation, victron-led
    assert [r.slug for r in rows_to_display(rows, {"victron-led"}, "")] == ["victron-led"]


def test_rows_to_display_with_a_query_searches_every_row_regardless_of_selection():
    rows = _picker_rows_fixture()
    assert [r.slug for r in rows_to_display(rows, set(), "azimuth")] == ["azimuth-calculation"]


def test_rows_to_display_with_nothing_selected_and_empty_query_is_empty():
    rows = _picker_rows_fixture()
    assert rows_to_display(rows, set(), "") == []


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


class _FakeElement:
    def __init__(self):
        self.children = []
        self.value = ""

    def classes(self, *_a, **_k):
        return self

    def props(self, *_a, **_k):
        return self

    def style(self, *_a, **_k):
        return self

    def clear(self):
        self.children = []

    def on_value_change(self, _callback):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_render_entity_picker_returns_a_getter_seeded_from_checked_rows(monkeypatch):
    import nicegui_app.pages.knowledge_picker as knowledge_picker

    fake_ui = type(
        "FakeUi",
        (),
        {
            "label": staticmethod(lambda *a, **k: _FakeElement()),
            "input": staticmethod(lambda *a, **k: _FakeElement()),
            "column": staticmethod(lambda *a, **k: _FakeElement()),
            "row": staticmethod(lambda *a, **k: _FakeElement()),
            "checkbox": staticmethod(lambda *a, **k: _FakeElement()),
        },
    )()
    monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)

    rows = [
        PickerRow(slug="a", title="A", chars=0, checked=True, summary=""),
        PickerRow(slug="b", title="B", chars=0, checked=False, summary=""),
    ]
    get_selected = knowledge_picker.render_entity_picker(rows, label="Used by these prompts")

    assert get_selected() == ["a"]
