"""Prompts page view-model."""

import sys
from types import SimpleNamespace

sys.modules.setdefault("nicegui", SimpleNamespace(run=SimpleNamespace(), ui=SimpleNamespace()))

from nicegui_app.pages.prompts import PromptRow, build_rows, diff_lines


class FakeLibrary:
    def __init__(self, specs, sources):
        self._specs = specs
        self._sources = sources

    def ids(self):
        return list(self._specs)

    def spec(self, prompt_id):
        return self._specs[prompt_id]

    def resolve(self, prompt_id):
        return self._sources[prompt_id]


def test_build_rows_marks_overridden(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(
        id="a.b",
        description="d",
        body="x",
        checksum="c",
        overridable=True,
        access=AccessSpec(view=["ops"]),
    )
    resolved = ("x", PromptSource.DB, 4)
    rows = build_rows(FakeLibrary({"a.b": spec}, {"a.b": resolved}), "root@x.com")
    assert rows == [
        PromptRow(
            prompt_id="a.b",
            description="d",
            owner="eng",
            source="Overridden",
            version=4,
            overridable=True,
            can_edit=True,
            can_publish=True,
        )
    ]


def test_build_rows_hides_prompts_the_user_cannot_view(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    spec = PromptSpec(
        id="a.b", description="d", body="x", checksum="c", access=AccessSpec(view=["ops"])
    )
    resolved = ("x", PromptSource.BUNDLED, None)
    assert build_rows(FakeLibrary({"a.b": spec}, {"a.b": resolved}), "nobody@x.com") == []


def test_locked_prompt_is_never_editable(monkeypatch):
    from shared.prompts.spec import PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(id="locked", description="d", body="x", checksum="c", overridable=False)
    resolved = ("x", PromptSource.BUNDLED, None)
    rows = build_rows(FakeLibrary({"locked": spec}, {"locked": resolved}), "root@x.com")
    assert rows[0].can_edit is False


def test_rows_are_sorted_by_prompt_id(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    specs = {
        pid: PromptSpec(id=pid, description="d", body="x", checksum="c", access=AccessSpec(view=["ops"]))
        for pid in ["z.z", "a.a"]
    }
    sources = {pid: ("x", PromptSource.BUNDLED, None) for pid in specs}
    rows = build_rows(FakeLibrary(specs, sources), "root@x.com")
    assert [r.prompt_id for r in rows] == ["a.a", "z.z"]


def test_diff_lines_marks_additions_and_removals():
    assert diff_lines("a\nb\n", "a\nc\n") == [
        ("  ", "a"),
        ("- ", "b"),
        ("+ ", "c"),
    ]


def test_diff_lines_of_identical_text_is_all_context():
    assert diff_lines("a\n", "a\n") == [("  ", "a")]


def test_build_rows_does_not_render_a_prompt_that_declares_variables(monkeypatch, tmp_path):
    """Regression test: build_rows must never substitute {{vars}} to build the
    list -- it has no runtime values (things like the user's message), and a
    prompt requiring one used to 500 the whole admin page the moment it was
    reached (shared.prompts.core.PromptLibrary.resolve exists for exactly
    this: it returns the raw stored body, unrendered).
    """
    from shared.prompts.bundled import BundledStore
    from shared.prompts.core import PromptLibrary

    (tmp_path / "needs.vars.prompt").write_text(
        "---\nid: needs.vars\ndescription: d\nvariables: [incoming_message]\n---\n"
        "Given {{incoming_message}}, respond.\n"
    )
    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    library = PromptLibrary(bundled=BundledStore(directory=tmp_path))

    rows = build_rows(library, "root@x.com")  # must not raise PromptRenderError

    assert [r.prompt_id for r in rows] == ["needs.vars"]
