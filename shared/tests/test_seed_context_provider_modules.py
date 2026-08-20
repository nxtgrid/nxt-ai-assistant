"""Seeding the provider-backed singleton modules.

sources_to_seed is a thin CLI-facing wrapper around
shared.prompts.knowledge.SINGLETON_SOURCES -- the actual row shape and the
ensure/idempotent behavior are covered in test_prompt_knowledge_store.py's
KnowledgeStore.ensure_singleton_modules tests, which this script now calls
under --apply rather than keeping its own copy.
"""

from scripts.seed_context_provider_modules import sources_to_seed
from shared.prompts.knowledge import SINGLETON_SOURCES


def test_seeds_every_singleton_source_when_none_exist():
    assert sources_to_seed(existing_sources=set()) == list(SINGLETON_SOURCES)


def test_sources_to_seed_skips_existing_sources():
    missing = sources_to_seed(existing_sources={"directory"})
    assert "directory" not in missing
    assert set(missing) == set(SINGLETON_SOURCES) - {"directory"}


def test_sources_to_seed_is_empty_when_all_exist():
    assert sources_to_seed(existing_sources=set(SINGLETON_SOURCES)) == []


def test_episodic_is_covered_alongside_directory_and_graph():
    """The pre-existing script only ever seeded directory/graph -- episodic
    stayed permanently uncreated. Sourcing from SINGLETON_SOURCES fixes that
    for both this script and the admin page in one place."""
    assert "episodic" in sources_to_seed(existing_sources=set())
