"""ExpertInstructionsProvider now sources its document through the prompt library."""

import pytest

from orchestrator.services import expert_instructions_provider as eip


@pytest.fixture(autouse=True)
def _force_bundled_prompts(monkeypatch):
    """Force PROMPTS to resolve "experts.definitions" from the bundled file,
    regardless of what's configured in the environment.

    This file's tests are about ExpertInstructionsProvider's *parsing* of the
    bundled default document, not about live DB/Google-Doc override
    resolution -- but PROMPTS is a process-wide singleton
    (shared/prompts/core.py's _build_default_library) whose DB/GDoc lookups
    activate automatically whenever CHAT_DB_URL / GOOGLE_SERVICE_ACCOUNT_JSON
    happen to be configured, e.g. a developer's local .env copied in for
    unrelated reasons. Without this, these tests silently stop testing the
    committed bundled document and start testing whatever's live in the real
    chat_db prompts table or Google Doc at the moment they happen to run --
    which is exactly what produced a false "grid_analyst is missing"
    failure during Phase 4 of
    docs/superpowers/plans/2026-08-06-user-designed-skills.md (the live doc
    had grid_analyst struck through at that moment; the bundled file never
    did). Monkeypatch auto-reverts after each test.
    """
    monkeypatch.setattr(eip.PROMPTS, "_db_body_for", None)
    monkeypatch.setattr(eip.PROMPTS, "_gdoc_body_for", None)


def test_fallback_loader_is_gone():
    assert not hasattr(eip, "_load_fallback_expert_instructions")


def test_instructions_dir_constant_is_gone():
    assert not hasattr(eip, "_INSTRUCTIONS_DIR")


def test_provider_no_longer_takes_a_doc_id():
    provider = eip.ExpertInstructionsProvider()
    assert not hasattr(provider, "doc_id")


@pytest.mark.asyncio
async def test_get_all_expert_ids_returns_the_bundled_experts():
    eip._expert_cache = {}
    eip._cache_timestamp = 0
    provider = eip.ExpertInstructionsProvider()
    ids = await provider.get_all_expert_ids()
    assert "grid_analyst" in ids
    assert len(ids) >= 5


@pytest.mark.asyncio
async def test_bundled_experts_still_parse_despite_pre_existing_format_quirks():
    """The bundled default's 'Shared Components' text has no '# ' heading, and
    each expert's 'System Instructions' label has no '## ' prefix either
    (unlike a well-formed live doc) — both pre-date this refactor (verified
    against the file's content before it moved) and are preserved exactly:
    system_instructions comes back empty for bundled experts, same as before.
    Parsing must still succeed and return the expert with its other fields."""
    eip._expert_cache = {}
    eip._cache_timestamp = 0
    provider = eip.ExpertInstructionsProvider()
    config = await provider.get_expert_config("grid_analyst")
    assert config is not None
    assert config.display_name == "Grid Analyst"
    assert config.system_instructions == ""


@pytest.mark.asyncio
async def test_cache_is_used_across_calls():
    eip._expert_cache = {}
    eip._cache_timestamp = 0
    provider = eip.ExpertInstructionsProvider()
    first = await provider.get_all_experts()
    second = await provider.get_all_experts()
    assert first is second
