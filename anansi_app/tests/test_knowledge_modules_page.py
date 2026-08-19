"""Knowledge Modules page view-model and the Prompts page knowledge tab."""

import pytest
from nicegui_app.pages.knowledge_modules import (
    ModuleRow,
    body_is_editable,
    build_module_rows,
    group_module_rows,
    module_is_deletable,
    preview_module_body,
    prompt_option_label,
    validate_module,
)
from nicegui_app.pages.prompts import KnowledgeTabRow, build_knowledge_tab, filter_module_rows

from shared.prompts.knowledge import KnowledgeModule


def _module(slug, tags=("grid_ops",), mode="pinned"):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary="s",
        body="b" * 40,
        tags=list(tags),
        scope="sector",
        mode=mode,
    )


def test_build_module_rows_reports_size():
    rows = build_module_rows([_module("comms")])
    assert rows == [
        ModuleRow(
            slug="comms", title="Comms", tags=["grid_ops"], scope="sector", mode="pinned",
            chars=40, source="manual", size_label="40 chars",
        )
    ]


def test_knowledge_tab_lists_all_modules_with_pinned_state():
    rows = build_knowledge_tab([_module("beta"), _module("alpha", mode="on_demand")], {"alpha": True})
    assert rows == [
        KnowledgeTabRow(
            slug="alpha", title="Alpha", mode="on_demand", chars=40, checked=True, summary="s"
        ),
        KnowledgeTabRow(
            slug="beta", title="Beta", mode="pinned", chars=40, checked=False, summary="s"
        ),
    ]


def test_knowledge_tab_with_no_pins_checks_nothing():
    rows = build_knowledge_tab([_module("alpha")], {})
    assert [r.checked for r in rows] == [False]


def test_filter_modules_matches_slug_title_and_summary():
    rows = _knowledge_tab_rows_fixture()
    assert [r.slug for r in filter_module_rows(rows, "azimuth")] == ["azimuth-calculation"]
    assert [r.slug for r in filter_module_rows(rows, "LED")] == ["victron-led"]
    assert len(filter_module_rows(rows, "")) == 2


def _knowledge_tab_rows_fixture():
    return [
        KnowledgeTabRow(
            slug="azimuth-calculation", title="Azimuth Calculation",
            mode="on_demand", chars=318, checked=False,
            summary="How PV azimuth is measured.",
        ),
        KnowledgeTabRow(
            slug="victron-led", title="Victron Quattro Codes",
            mode="on_demand", chars=2438, checked=True,
            summary="Decoding inverter LED error states.",
        ),
    ]


def test_validate_module_requires_a_summary_for_on_demand():
    with pytest.raises(ValueError, match="summary"):
        validate_module(slug="a", title="A", summary="", body="b", mode="on_demand")


def test_validate_module_rejects_a_bad_scope():
    with pytest.raises(ValueError, match="scope"):
        validate_module(slug="a", title="A", summary="s", body="b", scope="nonsense")


def test_validate_module_accepts_a_site_scope():
    validate_module(slug="a", title="A", summary="s", body="b", scope="site:ABC")


def test_validate_module_accepts_an_org_scope():
    validate_module(slug="a", title="A", summary="s", body="b", scope="org:2")


def test_validate_module_requires_slug_title_and_body():
    with pytest.raises(ValueError, match="required"):
        validate_module(slug="", title="A", summary="s", body="b")


def test_validate_module_rejects_a_bad_mode():
    with pytest.raises(ValueError, match="mode"):
        validate_module(slug="a", title="A", summary="s", body="b", mode="sometimes")


def test_group_module_rows_orders_pinned_before_on_demand():
    rows = [
        _row_for_grouping("catalog", mode="on_demand"),
        _row_for_grouping("comms", mode="pinned"),
    ]
    groups = group_module_rows(rows)
    assert [label for label, _ in groups] == ["Pinned", "On-demand"]


def test_group_module_rows_keeps_slug_order_within_a_group():
    rows = [_row_for_grouping("a"), _row_for_grouping("b")]
    groups = group_module_rows(rows)
    assert [r.slug for r in groups[0][1]] == ["a", "b"]


def test_group_module_rows_omits_empty_buckets():
    groups = group_module_rows([_row_for_grouping("a", mode="pinned")])
    assert [label for label, _ in groups] == ["Pinned"]


def _row_for_grouping(slug: str, mode: str = "pinned") -> ModuleRow:
    return ModuleRow(slug=slug, title=slug.title(), tags=[], scope="sector", mode=mode, chars=0)


def test_prompt_option_label_combines_id_and_description():
    assert prompt_option_label("customer.system", "Customer-mode instructions.") == (
        "customer.system — Customer-mode instructions."
    )


def test_prompt_option_label_truncates_a_long_description():
    label = prompt_option_label("a.b", "x" * 100, max_len=10)
    assert label == "a.b — " + "x" * 9 + "…"


def test_prompt_option_label_falls_back_to_bare_id_when_no_description():
    assert prompt_option_label("a.b", "") == "a.b"


# ── Provider-backed modules (source badges, read-only bodies, preview) ─────


def test_row_reports_live_instead_of_a_char_count_for_jit_modules():
    modules = [
        KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                        body=None, source="graph"),
        KnowledgeModule(id="m", slug="comms", title="Comms", summary="s",
                        body="12345"),
    ]
    rows = {r.slug: r for r in build_module_rows(modules)}

    assert rows["entity-graph"].size_label == "live"
    assert rows["comms"].size_label == "5 chars"


def test_row_carries_the_source():
    rows = build_module_rows([
        KnowledgeModule(id="g", slug="entity-graph", title="G", summary="s",
                        body=None, source="graph")
    ])
    assert rows[0].source == "graph"


def test_body_is_editable_only_for_manual_modules():
    assert body_is_editable("manual") is True
    assert body_is_editable("ingested") is True
    for source in ("gdoc", "graph", "directory", "episodic"):
        assert body_is_editable(source) is False, source


def test_singleton_providers_cannot_be_deleted():
    assert module_is_deletable("graph") is False
    assert module_is_deletable("directory") is False
    assert module_is_deletable("gdoc") is True
    assert module_is_deletable("episodic") is True
    assert module_is_deletable("manual") is True


def test_validate_module_does_not_require_a_body_when_not_required():
    """A provider-backed module's body isn't stored -- the save path must not
    demand one just because the caller happened to leave the field empty."""
    validate_module(slug="a", title="A", summary="s", body="", require_body=False)


def test_validate_module_still_requires_a_body_by_default():
    with pytest.raises(ValueError, match="required"):
        validate_module(slug="a", title="A", summary="s", body="")


# ── Preview ──────────────────────────────────────────────────────────────


def test_preview_resolves_against_the_viewing_operator():
    import asyncio

    module = KnowledgeModule(
        id="d", slug="directory", title="Directory", summary="s", body=None, source="directory"
    )

    class _Provider:
        source = "directory"

        async def resolve(self, m, ctx):
            return f"resolved for staff={ctx.is_staff}"

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert text == "resolved for staff=True"


def test_preview_reports_an_empty_resolution_clearly():
    import asyncio

    module = KnowledgeModule(
        id="e", slug="episodic", title="Episodic", summary="s", body=None, source="episodic"
    )

    class _Provider:
        source = "episodic"

        async def resolve(self, m, ctx):
            return None

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert "nothing" in text.lower()


def test_preview_reports_a_provider_failure_rather_than_raising():
    import asyncio

    module = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )

    class _Provider:
        source = "graph"

        async def resolve(self, m, ctx):
            raise RuntimeError("RPC missing")

    text = asyncio.run(preview_module_body(module, _Provider(), is_staff=True))
    assert "RPC missing" in text


# ── The Prompts page's Context tab: the same null-body / JIT hazard ────────
# build_knowledge_tab lists every module (including provider-backed ones) so
# an operator can pin/unpin it to a prompt. It has the exact same
# len(module.body) crash build_module_rows had -- fixed alongside it here,
# since a JIT module now genuinely exists once the seed script runs.


def test_knowledge_tab_handles_a_jit_module_without_crashing():
    rows = build_knowledge_tab(
        [KnowledgeModule(id="g", slug="entity-graph", title="Graph", summary="s",
                         body=None, source="graph")],
        {},
    )
    assert rows[0].chars == 0
    assert rows[0].is_jit is True


def test_knowledge_tab_marks_manual_modules_as_not_jit():
    rows = build_knowledge_tab([_module("comms")], {})
    assert rows[0].is_jit is False
