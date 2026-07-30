"""Knowledge module selection, tiering and budgeting."""

from shared.prompts.knowledge import (
    KnowledgeModule,
    apply_overrides,
    budget_pinned,
    diff_prompt_pins,
    render_catalog,
    render_pinned,
    select_modules,
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


def test_selects_modules_sharing_a_tag():
    modules = [_module("comms", tags=["comms"]), _module("billing", tags=["billing"])]
    picked = select_modules(modules, ["comms"], RequestScope())
    assert [m.slug for m in picked] == ["comms"]


def test_sector_scope_always_applies():
    picked = select_modules([_module("a")], ["grid_ops"], RequestScope(grid="XYZ"))
    assert len(picked) == 1


def test_site_scope_applies_only_to_that_site():
    modules = [_module("abc", scope="site:ABC")]
    assert select_modules(modules, ["grid_ops"], RequestScope(grid="ABC"))
    assert not select_modules(modules, ["grid_ops"], RequestScope(grid="XYZ"))


def test_no_tags_selects_nothing():
    assert select_modules([_module("a")], [], RequestScope()) == []


def test_override_can_force_a_module_on():
    modules = [_module("extra", tags=["unrelated"])]
    picked = apply_overrides([], modules, {"extra": True})
    assert [m.slug for m in picked] == ["extra"]


def test_override_can_force_a_module_off():
    selected = [_module("comms", tags=["comms"])]
    assert apply_overrides(selected, selected, {"comms": False}) == []


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
