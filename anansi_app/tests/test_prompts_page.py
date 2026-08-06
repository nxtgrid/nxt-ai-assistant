"""Prompts page view-model."""

from nicegui_app.pages.prompts import PromptRow, build_rows, diff_lines, group_rows


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


def test_build_rows_uses_the_batched_doc_binding_when_present(monkeypatch):
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    spec = PromptSpec(
        id="a.b", description="d", body="x", checksum="c", access=AccessSpec(view=["ops"])
    )
    resolved = ("x", PromptSource.BUNDLED, None)
    rows = build_rows(
        FakeLibrary({"a.b": spec}, {"a.b": resolved}),
        "root@x.com",
        doc_bindings={"a.b": ("DOC1", True)},
    )
    assert rows[0].doc_id == "DOC1"
    assert rows[0].doc_override is True


def test_build_rows_falls_back_to_legacy_env_var_with_no_binding(monkeypatch):
    """customer.system is one of the 5 ids shared/prompts/gdoc.py's
    LEGACY_DOC_ENV_VARS maps to CUSTOMER_SUPPORT_DOC_ID -- with no batch
    binding for it, the row must still surface that legacy doc id, since
    that's what doc_id_for() would actually resolve to. doc_override is
    always False for a legacy-only doc: there's no override row to read a
    flag from."""
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "LEGACY_DOC")
    spec = PromptSpec(
        id="customer.system",
        description="d",
        body="x",
        checksum="c",
        access=AccessSpec(view=["ops"]),
    )
    resolved = ("x", PromptSource.BUNDLED, None)
    rows = build_rows(
        FakeLibrary({"customer.system": spec}, {"customer.system": resolved}), "root@x.com"
    )
    assert rows[0].doc_id == "LEGACY_DOC"
    assert rows[0].doc_override is False


def test_build_rows_batched_binding_wins_over_legacy_env_var(monkeypatch):
    """Matches OverrideStore.doc_id_for's real precedence: a binding row, if
    one exists, always wins over the legacy env var -- never merged, never
    "whichever is set"."""
    from shared.prompts.spec import AccessSpec, PromptSpec
    from shared.prompts.types import PromptSource

    monkeypatch.setenv("PROMPT_ADMINS", "root@x.com")
    monkeypatch.setenv("CUSTOMER_SUPPORT_DOC_ID", "LEGACY_DOC")
    spec = PromptSpec(
        id="customer.system",
        description="d",
        body="x",
        checksum="c",
        access=AccessSpec(view=["ops"]),
    )
    resolved = ("x", PromptSource.BUNDLED, None)
    rows = build_rows(
        FakeLibrary({"customer.system": spec}, {"customer.system": resolved}),
        "root@x.com",
        doc_bindings={"customer.system": ("BOUND_DOC", False)},
    )
    assert rows[0].doc_id == "BOUND_DOC"


def _row(prompt_id: str, component: str = "orchestrator_services") -> PromptRow:
    return PromptRow(
        prompt_id=prompt_id,
        description="d",
        owner="eng",
        source="Default",
        version=None,
        overridable=False,
        can_edit=False,
        can_publish=False,
        component=component,
    )


def test_group_rows_orders_groups_by_component_order():
    groups = group_rows([_row("z.z", component="mcp_servers"), _row("a.a")])
    assert [label for label, _ in groups] == ["Orchestrator — Core services", "MCP Servers"]


def test_group_rows_keeps_rows_within_a_group_in_input_order():
    groups = group_rows([_row("a.a"), _row("b.b")])
    assert [r.prompt_id for r in groups[0][1]] == ["a.a", "b.b"]


def test_group_rows_omits_components_with_no_prompts():
    groups = group_rows([_row("a.a", component="mcp_servers")])
    assert [label for label, _ in groups] == ["MCP Servers"]


def test_group_rows_puts_unrecognised_components_in_a_trailing_uncategorized_bucket():
    typo_row = _row("z.z", component="orchestraotr_services")
    groups = group_rows([_row("a.a"), typo_row])
    assert groups[-1] == ("Uncategorized", [typo_row])
