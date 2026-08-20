"""Knowledge module selection, tiering and budgeting."""

from shared.prompts.knowledge import (
    KnowledgeModule,
    budget_pinned,
    diff_prompt_pins,
    render_catalog,
    render_pinned,
    select_for_prompt,
)
from shared.prompts.types import RequestScope


def _module(slug, tags=("grid_ops",), scope="sector", mode="pinned", body="B"):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary=f"About {slug}.",
        body=body,
        tags=list(tags),
        scope=scope,
        mode=mode,
    )


def test_selects_only_pinned_modules():
    modules = [_module("comms"), _module("billing")]
    picked = select_for_prompt(modules, {"comms": True})
    assert [m.slug for m in picked] == ["comms"]


def test_unpinned_override_excludes():
    modules = [_module("comms")]
    assert select_for_prompt(modules, {"comms": False}) == []


def test_no_pins_selects_nothing():
    assert select_for_prompt([_module("a")], {}) == []


def test_scope_still_gates_a_pinned_module():
    modules = [_module("abc", scope="site:ABC")]
    assert select_for_prompt(modules, {"abc": True}, RequestScope(grid="ABC"))
    assert select_for_prompt(modules, {"abc": True}, RequestScope(grid="XYZ")) == []


def test_sector_scope_applies_everywhere():
    modules = [_module("a", scope="sector")]
    assert len(select_for_prompt(modules, {"a": True}, RequestScope(grid="XYZ"))) == 1


def test_selection_is_slug_sorted():
    modules = [_module("zulu"), _module("alpha")]
    picked = select_for_prompt(modules, {"zulu": True, "alpha": True})
    assert [m.slug for m in picked] == ["alpha", "zulu"]


def test_budget_keeps_site_scoped_modules_first():
    site = _module("site", scope="site:ABC", body="x" * 60)
    sector = _module("sector", body="y" * 60)
    kept, dropped = budget_pinned([sector, site], limit=100)
    assert [m.slug for m in kept] == ["site"]
    assert [m.slug for m in dropped] == ["sector"]


def test_budget_drops_whole_modules_never_partial():
    modules = [_module("a", body="x" * 200)]
    kept, dropped = budget_pinned(modules, limit=100)
    assert kept == []
    assert [m.slug for m in dropped] == ["a"]


def test_budget_within_limit_keeps_everything():
    modules = [_module("a", body="x"), _module("b", body="y")]
    kept, dropped = budget_pinned(modules, limit=1000)
    assert len(kept) == 2 and dropped == []


def test_render_pinned_has_a_stable_heading():
    out = render_pinned([_module("a", body="Body text.")])
    assert out.startswith("# Technical Knowledge")
    assert "Body text." in out


def test_render_pinned_of_nothing_is_none():
    assert render_pinned([]) is None


def test_catalog_lists_slug_and_summary_only():
    out = render_catalog([_module("a", mode="on_demand", body="SECRET BODY")])
    assert "SECRET BODY" not in out
    assert "About a." in out


def test_catalog_of_nothing_is_none():
    assert render_catalog([]) is None


def test_diff_prompt_pins_adds_newly_selected():
    to_add, to_remove = diff_prompt_pins(set(), {"customer.system"})
    assert to_add == {"customer.system"}
    assert to_remove == set()


def test_diff_prompt_pins_removes_deselected():
    to_add, to_remove = diff_prompt_pins({"customer.system"}, set())
    assert to_add == set()
    assert to_remove == {"customer.system"}


def test_diff_prompt_pins_is_a_noop_when_unchanged():
    to_add, to_remove = diff_prompt_pins({"a.b"}, {"a.b"})
    assert to_add == set() and to_remove == set()


def test_module_defaults_to_manual_source():
    assert _module("comms").source == "manual"


def test_manual_module_is_not_jit():
    assert _module("comms").is_jit is False


def test_provider_sources_are_jit():
    for source in ("gdoc", "graph", "directory", "episodic"):
        module = KnowledgeModule(
            id=source, slug=source, title=source, summary="s", body=None, source=source
        )
        assert module.is_jit is True, source


def test_a_gdoc_module_carries_its_source_ref():
    """gdoc resolves via the async JIT resolver, not synchronously inside
    PromptLibrary -- it needs the caller's identity for the Drive ACL check,
    which render() does not carry."""
    module = KnowledgeModule(
        id="d", slug="d", title="D", summary="s", body=None, source="gdoc", source_ref="abc123"
    )
    assert module.is_jit is True
    assert module.source_ref == "abc123"


def test_budget_treats_unresolved_body_as_zero_cost():
    jit = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )
    kept, dropped = budget_pinned([jit])
    assert kept == [jit]
    assert dropped == []


def test_render_pinned_skips_unresolved_bodies():
    jit = KnowledgeModule(
        id="g", slug="graph", title="Graph", summary="s", body=None, source="graph"
    )
    assert render_pinned([jit]) is None


def test_a_module_carries_its_audience_and_tab():
    module = KnowledgeModule(
        id="d", slug="specs", title="Specs", summary="s", body=None,
        source="gdoc", source_ref="abc123", source_tab="Thresholds",
        doc_audience="acl_mirror", doc_audience_set_by=None,
    )
    assert module.source_tab == "Thresholds"
    assert module.doc_audience == "acl_mirror"


def test_audience_defaults_to_none_for_a_typed_module():
    module = KnowledgeModule(id="m", slug="m", title="M", summary="s", body="text")
    assert module.doc_audience is None
    assert module.source_tab is None


def test_global_scope_matches_every_request():
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("global") is True
    assert RequestScope(organization_id="7").matches("global") is True


def test_sector_is_still_accepted_as_a_synonym():
    """matches() fails closed on an unknown scope, so a row the migration
    missed would go silently dark. Both spellings work, permanently."""
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("sector") is True


def test_an_unknown_scope_still_matches_nothing():
    from shared.prompts.types import RequestScope

    assert RequestScope().matches("universe") is False


def test_a_new_module_defaults_to_global_scope():
    assert KnowledgeModule(id="m", slug="m", title="M", summary="s", body="b").scope == "global"
