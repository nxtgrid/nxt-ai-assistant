"""ProcedureProvider now sources content through knowledge_modules.

P2 of the context-architecture programme (docs/superpowers/plans/
2026-08-21-p2-procedures-to-context-modules.md) moved procedures off the
prompt library's Google Doc override entirely, onto context modules tagged
'procedure' -- see test_procedure_provider.py for the module-reading
behavior itself. This file previously asserted the *prior* migration (doc
override, not yet knowledge_modules); the two invalidation tests below are
rewritten for the new mechanism, not just patched to keep passing.
"""

from orchestrator.services import procedure_provider as pp


def test_no_longer_caches_procedures_locally():
    provider = pp.ProcedureProvider()
    assert not hasattr(provider, "_cached_procedures")


def test_get_procedures_returns_empty_list_when_no_store_is_configured():
    """No CHAT_DB_URL/CHAT_DB_SERVICE_KEY in this environment -- the same
    empty result get_procedures() has always produced when nothing is
    configured, now for a different reason (no knowledge store client
    rather than a bundled body with no procedure headers)."""
    provider = pp.ProcedureProvider()
    assert provider.get_procedures() == []


class _FakeStore:
    def __init__(self):
        self.invalidate_calls = 0

    def all_modules(self):
        return []

    def invalidate(self):
        self.invalidate_calls += 1


def test_clear_cache_invalidates_the_knowledge_store():
    store = _FakeStore()
    pp.ProcedureProvider(store=store).clear_cache()
    assert store.invalidate_calls == 1


def test_force_reload_invalidates_before_fetching():
    store = _FakeStore()
    pp.ProcedureProvider(store=store).get_procedures(force_reload=True)
    assert store.invalidate_calls == 1


def test_parse_procedures_still_finds_procedures_in_well_formed_content():
    provider = pp.ProcedureProvider()
    content = (
        "## Procedure 1: Meter Commissioning Failed\n\n"
        "### Purpose\n\nHelp when commissioning fails.\n\n"
        "### Procedure Steps\n\n1. Check signal\n2. Retry\n"
    )
    procedures = provider._parse_procedures(content)
    assert len(procedures) == 1
    assert procedures[0].title == "Meter Commissioning Failed"
