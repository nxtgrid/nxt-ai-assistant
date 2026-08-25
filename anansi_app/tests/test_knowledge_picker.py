"""Tests for the shared prompt/skill <-> knowledge-module picker widget
(moved and generalized from the Prompts page's original Context tab --
see test_knowledge_modules_page.py's git history for the pre-move tests
this file replaces)."""

import ast
from pathlib import Path

from nicegui_app.pages.knowledge_picker import (
    UNPINNED_PREVIEW_LIMIT,
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


# ── The reverse-direction picker's own scaling hazard, and its discoverability
# gap ────────────────────────────────────────────────────────────────────────
# render_entity_picker (knowledge_modules.py's "Used by these prompts"/"these
# skills") can face dozens of candidates -- every registered prompt id today,
# every skill ever created tomorrow -- so its default (no-query) view must
# never grow proportionally to the candidate pool: rows_to_display keeps it
# capped at UNPINNED_PREVIEW_LIMIT no matter how large that pool gets.
#
# An earlier version of that cap showed *only* whatever was already pinned
# and nothing else until you typed -- for a module with nothing pinned yet
# (every module, the day this shipped) the picker opened looking completely
# inert: a plain text box with no rows and no indication anything was there
# to find, unlike the native dropdowns elsewhere in the same dialog that
# visibly pop open on click. rows_to_display now always tops up to
# UNPINNED_PREVIEW_LIMIT with a small, deterministic (alphabetical) sample of
# not-yet-pinned rows too, so there's always something to see and click the
# moment the dialog opens -- capped, not proportional, so it can't
# reintroduce the original scaling problem.


def _rows(*slugs_and_checked):
    """N distinct rows from (slug, checked) pairs, for the preview-limit
    tests below -- knowledge_modules.py always hands render_entity_picker
    its rows pre-sorted (alphabetically by prompt id, or by skill title), so
    these fixtures do too."""
    return [
        PickerRow(slug=slug, title=slug, chars=0, checked=checked, summary="")
        for slug, checked in slugs_and_checked
    ]


def test_rows_to_display_with_empty_query_and_room_shows_pinned_plus_a_sample():
    rows = _picker_rows_fixture()  # 2 rows total, well under the preview limit
    # victron-led is pinned; azimuth-calculation isn't -- both now fit under
    # the cap, so an empty query shows both, not just the pinned one.
    assert {r.slug for r in rows_to_display(rows, {"victron-led"}, "")} == {
        "victron-led",
        "azimuth-calculation",
    }


def test_rows_to_display_with_more_unpinned_rows_than_the_limit_caps_the_sample():
    rows = _rows(*[(f"prompt-{i}", False) for i in range(20)])
    visible = rows_to_display(rows, set(), "")
    assert len(visible) == UNPINNED_PREVIEW_LIMIT
    # Deterministic and stable -- the same alphabetical prefix every time,
    # not a random/rotating sample, so an unrelated redraw doesn't reshuffle
    # what's already on screen.
    assert [r.slug for r in visible] == [f"prompt-{i}" for i in range(UNPINNED_PREVIEW_LIMIT)]


def test_rows_to_display_always_shows_every_pinned_row_even_past_the_limit():
    # 12 pinned rows, over the 8-row cap -- pinned rows are never hidden;
    # the cap only ever applies to the sample of *unpinned* rows.
    rows = _rows(*[(f"prompt-{i}", True) for i in range(12)])
    visible = rows_to_display(rows, {f"prompt-{i}" for i in range(12)}, "")
    assert len(visible) == 12


def test_rows_to_display_tops_up_pinned_rows_with_unpinned_ones_up_to_the_limit():
    rows = _rows(("pinned-a", True), *[(f"unpinned-{i}", False) for i in range(20)])
    visible = rows_to_display(rows, {"pinned-a"}, "")
    assert len(visible) == UNPINNED_PREVIEW_LIMIT
    assert visible[0].slug == "pinned-a"
    assert [r.slug for r in visible[1:]] == [
        f"unpinned-{i}" for i in range(UNPINNED_PREVIEW_LIMIT - 1)
    ]


def test_rows_to_display_with_no_candidates_at_all_is_empty():
    assert rows_to_display([], set(), "") == []


def test_rows_to_display_with_a_query_searches_every_row_regardless_of_selection():
    rows = _picker_rows_fixture()
    assert [r.slug for r in rows_to_display(rows, set(), "azimuth")] == ["azimuth-calculation"]


def test_rows_to_display_with_a_query_is_not_capped_by_the_preview_limit():
    # Typing a query bypasses the preview cap entirely -- every match must
    # stay reachable however many there are; only the empty-query default
    # view is capped.
    rows = _rows(*[(f"prompt-{i}", False) for i in range(20)])
    visible = rows_to_display(rows, set(), "prompt")
    assert len(visible) == 20


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


def _record_label(sink):
    element = _FakeElement()
    sink.append(element)
    return element


def _record_checkbox(sink, kwargs):
    element = _FakeElement()
    sink.append((element, kwargs.get("on_change")))
    return element


def test_render_entity_picker_toggling_a_row_updates_the_live_selected_count(monkeypatch):
    """A tick/untick here has no Save button of its own and no other
    feedback (see render_entity_picker's toggle) -- the running "N selected"
    label is the only visible confirmation a click registered at all, so it
    must actually move when a row is ticked, not just exist."""
    from types import SimpleNamespace

    import nicegui_app.pages.knowledge_picker as knowledge_picker

    created_labels: list = []
    created_checkboxes: list = []

    fake_ui = type(
        "FakeUi",
        (),
        {
            "label": staticmethod(lambda *a, **k: _record_label(created_labels)),
            "input": staticmethod(lambda *a, **k: _FakeElement()),
            "column": staticmethod(lambda *a, **k: _FakeElement()),
            "row": staticmethod(lambda *a, **k: _FakeElement()),
            "checkbox": staticmethod(lambda *a, **k: _record_checkbox(created_checkboxes, k)),
        },
    )()
    monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)

    rows = [
        PickerRow(slug="a", title="A", chars=0, checked=True, summary=""),
        PickerRow(slug="b", title="B", chars=0, checked=False, summary=""),
    ]
    knowledge_picker.render_entity_picker(rows, label="Used by these prompts")

    # created_labels[0] is the "Used by these prompts" heading; [1] is the
    # running count -- both created once, up front, before redraw() ever
    # touches the row list itself.
    picked_label = created_labels[1]
    assert picked_label.text == "1 selected"

    # created_checkboxes[0] is row "a" (already checked); [1] is row "b".
    # Simulate ticking it the same way a real click would: invoke the
    # on_change NiceGUI itself hands to toggle().
    _, toggle_b = created_checkboxes[1]
    toggle_b(SimpleNamespace(value=True))

    assert picked_label.text == "2 selected"
