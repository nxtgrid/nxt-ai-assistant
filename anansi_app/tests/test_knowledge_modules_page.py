"""Knowledge Modules page view-model and the Prompts page knowledge tab."""

import pytest
from nicegui_app.pages.knowledge_modules import (
    ModuleRow,
    build_module_rows,
    group_module_rows,
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
        ModuleRow(slug="comms", title="Comms", tags=["grid_ops"], scope="sector", mode="pinned", chars=40)
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
