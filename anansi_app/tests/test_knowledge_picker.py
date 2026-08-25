"""Tests for the shared prompt/skill <-> knowledge-module picker widget
(moved and generalized from the Prompts page's original Context tab --
see test_knowledge_modules_page.py's git history for the pre-move tests
this file replaces)."""

import ast
from pathlib import Path

from nicegui_app.pages.knowledge_picker import (
    PickerRow,
    build_picker_rows,
    entity_select_options,
    filter_picker_rows,
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


# ── Why this direction is a native dropdown, not an inline tick-list ────────
# knowledge_modules.py's "Used by these prompts"/"Used by these skills" sit
# inside _open_edit_dialog's ui.card() -- a flex column capped at
# `max-height: calc(100dvh - 32px)`. An inline list styled
# `max-height: …px; overflow-y: auto` is a flex item whose *automatic minimum
# size is 0*: CSS only resolves min-height:auto to the content size while
# overflow is `visible`. So the card's default flex-shrink:1 crushed that
# list to zero height whenever the dialog's own fields outgrew the viewport
# -- every row still in the DOM (scrollHeight 188px) at a rendered height of
# 0, which is precisely what an operator reported as "nothing pops up".
#
# Measured directly in a browser against the shipped build: it reproduces
# only when the card is over-constrained, which is why every unit test and
# every standalone repro of the widget passed while the real dialog showed an
# empty box -- and why two prior fixes that only changed *which rows* got
# rendered could never have helped.
#
# A ui.select's popup is portalled to <body> by Quasar, so no ancestor's
# height cap can clip or shrink it. That is what makes the dropdown the
# structural fix rather than another style tweak, and it restores the
# click-to-open behaviour operators expect from every other field on the
# same form.


def _rows(*slugs_and_checked):
    """N distinct rows from (slug, checked) pairs. knowledge_modules.py hands
    the picker its rows pre-sorted (alphabetically by prompt id, or by skill
    title), so these fixtures do too."""
    return [
        PickerRow(slug=slug, title=slug, chars=0, checked=checked, summary="")
        for slug, checked in slugs_and_checked
    ]


def test_entity_select_options_are_keyed_by_the_id_that_gets_saved():
    """The dict key is what resolve_pins_to_save writes into
    prompt_knowledge_overrides; the label is only what the operator reads.
    Conflating the two would silently save a display string as a pin."""
    options = entity_select_options(_picker_rows_fixture())
    assert set(options) == {"azimuth-calculation", "victron-led"}


def test_entity_select_options_lead_with_the_title_then_the_summary():
    """The original chip-select showed bare ids ("customer.system") and was
    reported as unreadable -- which is what started this whole detour. A
    label has to carry the human title first."""
    options = entity_select_options(_picker_rows_fixture())
    assert options["victron-led"] == (
        "Victron Quattro Codes — Decoding inverter LED error states."
    )


def test_entity_select_options_without_a_summary_is_just_the_title():
    assert entity_select_options(_rows(("solo", False)))["solo"] == "solo"


def test_entity_select_options_falls_back_to_the_id_when_there_is_no_title():
    """A skill saved with an empty title would otherwise render as a blank,
    unpickable row -- and the summary has to survive the fallback, because
    "/gtr · active" is then the only readable part of that row."""
    options = entity_select_options(
        [PickerRow(slug="skill-uuid", title="", chars=0, checked=False, summary="/gtr · active")]
    )
    assert options["skill-uuid"] == "skill-uuid — /gtr · active"


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
        # Recorded, not discarded: the picker's one shipped rendering bug
        # lived entirely in a .style() string (see
        # test_module_picker_scroll_box_cannot_be_flex_shrunk), and a fake
        # that swallowed styles is why no test ever saw it.
        self.styles: list = []

    def classes(self, *_a, **_k):
        return self

    def props(self, *_a, **_k):
        return self

    def style(self, *a, **_k):
        if a:
            self.styles.append(a[0])
        return self

    def clear(self):
        self.children = []

    def on_value_change(self, _callback):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeSelect(_FakeElement):
    def __init__(self, options=None, value=None, **kwargs):
        super().__init__()
        self.options = options
        self.value = value
        self.kwargs = kwargs
        self.change_handlers: list = []

    def on_value_change(self, callback):
        self.change_handlers.append(callback)
        return self


def _fake_select_ui(monkeypatch, sink):
    """Patch knowledge_picker.ui with a stub whose ui.select records every
    select built, so a test can read back its options/kwargs and drive its
    on_value_change the way a real pick would."""
    import nicegui_app.pages.knowledge_picker as knowledge_picker

    def _select(options=None, value=None, **kwargs):
        element = _FakeSelect(options, value, **kwargs)
        sink.append(element)
        return element

    fake_ui = type(
        "FakeUi",
        (),
        {
            "label": staticmethod(lambda *a, **k: _FakeElement()),
            "select": staticmethod(_select),
        },
    )()
    monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)
    return knowledge_picker


def test_render_entity_select_returns_a_getter_seeded_from_checked_rows(monkeypatch):
    created: list = []
    knowledge_picker = _fake_select_ui(monkeypatch, created)

    get_selected = knowledge_picker.render_entity_select(
        _rows(("a", True), ("b", False)), label="Used by these prompts"
    )

    assert get_selected() == ["a"]


def test_render_entity_select_getter_follows_a_later_pick(monkeypatch):
    """knowledge_modules.py reads this getter once, at Save time (see
    resolve_pins_to_save), so it has to report live state rather than the
    snapshot taken when the dialog opened."""
    created: list = []
    knowledge_picker = _fake_select_ui(monkeypatch, created)

    get_selected = knowledge_picker.render_entity_select(
        _rows(("a", True), ("b", False)), label="Used by these prompts"
    )
    created[0].value = ["b", "a"]

    assert get_selected() == ["a", "b"]


def test_render_entity_select_is_a_searchable_multi_select(monkeypatch):
    """multiple: a module is used by more than one prompt. with_input: the
    ~30 registered prompt ids have to stay findable by typing, which is the
    one thing the inline tick-list did better than the original chip-select
    and must not be lost going back to a dropdown."""
    created: list = []
    knowledge_picker = _fake_select_ui(monkeypatch, created)

    knowledge_picker.render_entity_select(_rows(("a", False)), label="Used by these prompts")

    assert created[0].kwargs.get("multiple") is True
    assert created[0].kwargs.get("with_input") is True


def test_render_entity_select_notifies_on_change(monkeypatch):
    """knowledge_modules.py recomputes its audience warning from the current
    prompt selection, so every pick has to call back."""
    created: list = []
    knowledge_picker = _fake_select_ui(monkeypatch, created)
    fired: list = []

    knowledge_picker.render_entity_select(
        _rows(("a", False)), label="Used by these prompts", on_change=lambda: fired.append(1)
    )
    for callback in created[0].change_handlers:
        callback(None)

    assert fired == [1]


def test_render_entity_select_with_no_candidates_still_renders(monkeypatch):
    """A fresh install has no skills at all -- the field must still build
    (empty) rather than break the whole dialog."""
    created: list = []
    knowledge_picker = _fake_select_ui(monkeypatch, created)

    get_selected = knowledge_picker.render_entity_select([], label="Used by these skills")

    assert get_selected() == []
    assert created[0].options == {}


def test_module_picker_scroll_box_cannot_be_flex_shrunk(monkeypatch):
    """render_module_picker's options box is `overflow-y: auto`, which drops
    its flex automatic minimum size to 0. Inside any height-capped flex
    column -- skills.py's Context card, the Prompts page's Context tab -- the
    default flex-shrink:1 is then free to crush it to zero height with every
    row still present in the DOM.

    That is exactly the failure that made the modules dialog's own picker
    render as an empty box across three releases (see the comment block above
    entity_select_options). Nothing about it is visible to a test that only
    checks which rows were computed, so pin the style that prevents it."""
    import nicegui_app.pages.knowledge_picker as knowledge_picker

    built_columns: list = []

    def _column(*_a, **_k):
        element = _FakeElement()
        built_columns.append(element)
        return element

    fake_ui = type(
        "FakeUi",
        (),
        {
            "label": staticmethod(lambda *a, **k: _FakeElement()),
            "input": staticmethod(lambda *a, **k: _FakeElement()),
            "column": staticmethod(_column),
            "row": staticmethod(lambda *a, **k: _FakeElement()),
            "checkbox": staticmethod(lambda *a, **k: _FakeElement()),
            "button": staticmethod(lambda *a, **k: _FakeElement()),
        },
    )()
    monkeypatch.setattr(knowledge_picker, "ui", fake_ui, raising=False)

    class _Store:
        _client = object()

        def all_modules(self):
            return [_module("alpha")]

        def overrides_for(self, _pinning_id):
            return {}

    knowledge_picker.render_module_picker(
        "customer.system", _Store(), "operator@example.com", show_budget=False
    )

    scroll_boxes = [
        column
        for column in built_columns
        if any("overflow-y: auto" in style for style in column.styles)
    ]
    assert scroll_boxes, "expected render_module_picker to build a scrollable options box"
    for box in scroll_boxes:
        assert "flex-shrink: 0" in " ".join(box.styles)
