"""ProcedureProvider now sources content through the prompt library."""

from orchestrator.services import procedure_provider as pp


def test_no_longer_caches_procedures_locally():
    provider = pp.ProcedureProvider()
    assert not hasattr(provider, "_cached_procedures")


def test_get_procedures_returns_empty_list_for_generic_bundled_content():
    """The bundled customer.system default has no '## Procedure N: Title'
    headers, so parsing it naturally yields no procedures — same result the
    old code produced when CUSTOMER_SUPPORT_DOC_ID was unset, but reached
    through the prompt library's bundled floor instead of an early return."""
    provider = pp.ProcedureProvider()
    assert provider.get_procedures() == []


def test_clear_cache_invalidates_the_prompt_library_doc_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(pp.PROMPTS, "invalidate_doc_cache", lambda: calls.append(1))
    pp.ProcedureProvider().clear_cache()
    assert calls == [1]


def test_force_reload_invalidates_before_fetching(monkeypatch):
    calls = []
    monkeypatch.setattr(pp.PROMPTS, "invalidate_doc_cache", lambda: calls.append(1))
    pp.ProcedureProvider().get_procedures(force_reload=True)
    assert calls == [1]


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
