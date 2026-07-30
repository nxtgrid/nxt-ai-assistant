"""Knowledge Modules page view-model and the Prompts page knowledge tab."""

import pytest

from nicegui_app.pages.knowledge_modules import ModuleRow, build_module_rows, validate_module
from nicegui_app.pages.prompts import build_knowledge_tab
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


def test_knowledge_tab_marks_tag_derived_versus_overridden():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"],
        modules=[_module("comms"), _module("extra", tags=["other"])],
        overrides={"extra": True},
    )
    by_slug = {row.slug: row for row in tab}
    assert by_slug["comms"].checked is True and by_slug["comms"].origin == "tag"
    assert by_slug["extra"].checked is True and by_slug["extra"].origin == "override"


def test_knowledge_tab_shows_a_forced_off_module_as_unchecked():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"], modules=[_module("comms")], overrides={"comms": False}
    )
    assert tab[0].checked is False and tab[0].origin == "override"


def test_knowledge_tab_totals_only_pinned_checked_modules():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"],
        modules=[_module("a"), _module("b", mode="on_demand")],
        overrides={},
    )
    assert sum(row.chars for row in tab if row.checked and row.mode == "pinned") == 40


def test_knowledge_tab_excludes_unrelated_unoverridden_modules():
    tab = build_knowledge_tab(
        prompt_tags=["grid_ops"], modules=[_module("unrelated", tags=["billing"])], overrides={}
    )
    assert tab == []


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
