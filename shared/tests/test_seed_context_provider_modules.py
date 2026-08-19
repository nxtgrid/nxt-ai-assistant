"""Seeding the provider-backed singleton modules."""

from scripts.seed_context_provider_modules import SEED_MODULES, rows_to_insert


def test_seeds_directory_and_graph_only():
    assert {m["slug"] for m in SEED_MODULES} == {"directory", "entity-graph"}


def test_seeded_modules_have_no_body():
    assert all(m["body"] is None for m in SEED_MODULES)


def test_seeded_modules_have_a_summary():
    """on_demand selection is summary-only; a blank one is invisible."""
    assert all(m["summary"].strip() for m in SEED_MODULES)


def test_rows_to_insert_skips_existing_slugs():
    rows = rows_to_insert(existing_slugs={"directory"})
    assert [r["slug"] for r in rows] == ["entity-graph"]


def test_rows_to_insert_is_empty_when_all_exist():
    assert rows_to_insert(existing_slugs={"directory", "entity-graph"}) == []
