"""Prompts page view-model."""

from nicegui_app.pages.prompts import PromptRow, build_rows, diff_lines


class FakeLibrary:
    def __init__(self, specs, sources):
        self._specs = specs
        self._sources = sources

    def ids(self):
        return list(self._specs)

    def spec(self, prompt_id):
        return self._specs[prompt_id]

    def render(self, prompt_id, **kwargs):
        return self._sources[prompt_id]


def test_build_rows_marks_overridden(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(
        id="a.b",
        description="d",
        body="x",
        checksum="c",
        overridable=True,
        access=AccessSpec(view=["ops"]),
    )
    rendered = RenderedPrompt("a.b", "x", None, PromptSource.DB, 4, "c")
    rows = build_rows(FakeLibrary({"a.b": spec}, {"a.b": rendered}), "root@x.com")
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
    from shared.prompts.types import PromptSource, RenderedPrompt

    for var in ("PROMPT_EDITORS_OPS", "PROMPT_EDITORS_ENG", "PROMPT_ADMINS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    spec = PromptSpec(
        id="a.b", description="d", body="x", checksum="c", access=AccessSpec(view=["ops"])
    )
    rendered = RenderedPrompt("a.b", "x", None, PromptSource.BUNDLED, None, "c")
    assert build_rows(FakeLibrary({"a.b": spec}, {"a.b": rendered}), "nobody@x.com") == []


def test_locked_prompt_is_never_editable(monkeypatch):
    from shared.prompts.spec import PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(id="locked", description="d", body="x", checksum="c", overridable=False)
    rendered = RenderedPrompt("locked", "x", None, PromptSource.BUNDLED, None, "c")
    rows = build_rows(FakeLibrary({"locked": spec}, {"locked": rendered}), "root@x.com")
    assert rows[0].can_edit is False


def test_rows_are_sorted_by_prompt_id(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource, RenderedPrompt

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    specs = {
        pid: PromptSpec(id=pid, description="d", body="x", checksum="c", access=AccessSpec(view=["ops"]))
        for pid in ["z.z", "a.a"]
    }
    sources = {
        pid: RenderedPrompt(pid, "x", None, PromptSource.BUNDLED, None, "c") for pid in specs
    }
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
