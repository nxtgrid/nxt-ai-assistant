"""InstructionsProvider now sources everything through the prompt library."""

import pytest

from orchestrator.services import instructions_provider as ip


def test_fallback_loader_is_gone():
    assert not hasattr(ip, "_load_fallback_instructions")


def test_default_instructions_dict_is_gone():
    """The generic-assistant fallback dict is dead now that both paths raise
    on a genuinely malformed prompt instead of silently substituting it."""
    provider = ip.InstructionsProvider()
    assert not hasattr(provider, "_default_instructions")


def test_examples_word_cap_lives_in_one_place():
    """The original file defined MAX_EXAMPLES_WORDS = 5000 twice (once per
    customer/staff method). There must be exactly one definition now."""
    import inspect

    source = inspect.getsource(ip)
    assert source.count("MAX_EXAMPLES_WORDS = 5000") == 1


def test_get_last_provenance_is_none_before_any_fetch():
    assert ip.InstructionsProvider().get_last_provenance() is None


@pytest.mark.asyncio
async def test_get_last_provenance_populated_after_customer_fetch():
    provider = ip.InstructionsProvider()
    await provider.get_customer_instructions()
    provenance = provider.get_last_provenance()
    assert provenance["prompt_id"] == "customer.system"
    assert provenance["prompt_source"] in ("bundled", "gdoc", "db")


@pytest.mark.asyncio
async def test_get_last_provenance_populated_after_staff_fetch():
    provider = ip.InstructionsProvider()
    await provider._get_staff_instructions_from_doc()
    provenance = provider.get_last_provenance()
    assert provenance["prompt_id"] == "staff.system"


@pytest.mark.asyncio
async def test_customer_instructions_come_from_the_library():
    system, _context = await ip.InstructionsProvider().get_customer_instructions()
    assert system.strip()


@pytest.mark.asyncio
async def test_staff_instructions_come_from_the_library():
    system, _context = await ip.InstructionsProvider()._get_staff_instructions_from_doc()
    assert system.strip()


@pytest.mark.asyncio
async def test_staff_missing_section_no_longer_silently_degrades(monkeypatch):
    """The staff path used to substitute a generic assistant prompt on error.

    It must now raise, exactly as the customer path already did.
    """
    from shared.prompts.types import PromptRenderError

    def boom(*args, **kwargs):
        raise PromptRenderError("missing section")

    monkeypatch.setattr(ip.PROMPTS, "render", boom)
    with pytest.raises(PromptRenderError):
        await ip.InstructionsProvider()._get_staff_instructions_from_doc()


@pytest.mark.asyncio
async def test_customer_missing_section_still_raises(monkeypatch):
    from shared.prompts.types import PromptRenderError

    def boom(*args, **kwargs):
        raise PromptRenderError("missing section")

    monkeypatch.setattr(ip.PROMPTS, "render", boom)
    with pytest.raises(PromptRenderError):
        await ip.InstructionsProvider().get_customer_instructions()


@pytest.mark.asyncio
async def test_verification_instructions_always_return_something():
    text = await ip.InstructionsProvider().get_verification_instructions()
    assert text is not None
    assert text.strip()


@pytest.mark.asyncio
async def test_troubleshooting_procedures_always_return_something():
    text = await ip.InstructionsProvider().get_troubleshooting_procedures()
    assert text is not None
    assert text.strip()


# ── block splitting / extraction helpers ────────────────────────────────────


def test_split_context_blocks_separates_on_heading_boundaries():
    text = "# Alpha\n\nBody one.\n\n# Beta\n\nBody two."
    assert ip._split_context_blocks(text) == ["# Alpha\n\nBody one.", "# Beta\n\nBody two."]


def test_block_key_lowercases_and_underscores():
    assert ip._block_key("# Staff Groups\n\nbody") == "staff_groups"


def test_extract_block_pulls_matching_block_only():
    blocks = ["# Alpha\n\na", "# Staff Groups\n\ng", "# Beta\n\nb"]
    found, remaining = ip._extract_block(blocks, "staff_groups")
    assert found == "# Staff Groups\n\ng"
    assert remaining == ["# Alpha\n\na", "# Beta\n\nb"]


def test_extract_block_returns_none_when_absent():
    blocks = ["# Alpha\n\na"]
    found, remaining = ip._extract_block(blocks, "staff_groups")
    assert found is None
    assert remaining == blocks


def test_truncate_examples_block_leaves_short_blocks_untouched():
    block = "# Examples\n\nshort body"
    assert ip._truncate_examples_block(block) == block


def test_truncate_examples_block_caps_long_bodies():
    long_body = " ".join(f"word{i}" for i in range(ip.MAX_EXAMPLES_WORDS + 10))
    block = f"# Examples\n\n{long_body}"
    result = ip._truncate_examples_block(block)
    assert "[Truncated: showing 5000/5010 words]" in result
    assert "word4999" in result
    assert "word5009" not in result


# ── postprocess_context: staff groups + examples reordering ────────────────


def test_postprocess_extracts_staff_groups_and_populates_registry():
    context = (
        "# Staff Groups\n\n"
        "## Ops Team\n- chat_id: -1001\n- purpose: alerts, escalations\n\n"
        "# Other Info\n\nsome other context"
    )
    result = ip._postprocess_context(context, extract_staff_groups=True)
    assert "Staff Groups" not in result
    assert "Other Info" in result
    assert ip.get_staff_group("-1001") == {"name": "Ops Team", "purposes": ["alerts", "escalations"]}


def test_postprocess_does_not_extract_staff_groups_for_customer_mode():
    context = "# Staff Groups\n\n## Ops Team\n- chat_id: -1002\n- purpose: x"
    result = ip._postprocess_context(context, extract_staff_groups=False)
    assert "Staff Groups" in result


def test_postprocess_moves_examples_section_to_the_end():
    context = "# Examples\n\nsome examples\n\n# FAQ\n\nsome faq content"
    result = ip._postprocess_context(context, extract_staff_groups=False)
    assert result.index("# FAQ") < result.index("# Examples")


def test_postprocess_handles_no_examples_section_gracefully():
    context = "# FAQ\n\nsome faq content"
    result = ip._postprocess_context(context, extract_staff_groups=False)
    assert result == context


def test_postprocess_returns_none_for_none_input():
    assert ip._postprocess_context(None, extract_staff_groups=False) is None


# ── overall context cap ─────────────────────────────────────────────────────


def test_cap_context_leaves_short_text_untouched():
    assert ip._cap_context("short") == "short"


def test_cap_context_truncates_long_text():
    long_text = "x" * (ip.MAX_CONTEXT_CHARS + 500)
    result = ip._cap_context(long_text)
    assert len(result) <= ip.MAX_CONTEXT_CHARS + len("\n\n[Context truncated due to size limits]")
    assert result.endswith("[Context truncated due to size limits]")


def test_cap_context_of_none_is_none():
    assert ip._cap_context(None) is None


# ── RequestScope threading ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_customer_instructions_pass_organization_id_as_scope(monkeypatch):
    from shared.prompts.types import PromptSource, RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["prompt_id"] = prompt_id
        captured["scope"] = scope

        class _Rendered:
            prompt_id = "customer.system"
            system_text = "system"
            context_text = None
            source = PromptSource.BUNDLED
            version = None
            checksum = "abc12345"

            def provenance(self):
                return "customer.system@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    await provider.get_customer_instructions(organization_id="42")

    assert captured["prompt_id"] == "customer.system"
    assert captured["scope"] == RequestScope(organization_id="42")


@pytest.mark.asyncio
async def test_staff_instructions_pass_organization_id_as_scope(monkeypatch):
    from shared.prompts.types import PromptSource, RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            prompt_id = "staff.system"
            system_text = "system"
            context_text = None
            source = PromptSource.BUNDLED
            version = None
            checksum = "abc12345"

            def provenance(self):
                return "staff.system@bundled:default:abc12345"

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    await provider._get_staff_instructions_from_doc(organization_id="7")

    assert captured["scope"] == RequestScope(organization_id="7")


@pytest.mark.asyncio
async def test_get_instructions_derives_scope_from_user_context(monkeypatch):
    from orchestrator.models.schemas import UserContext
    from shared.prompts.types import PromptSource, RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None
            source = PromptSource.BUNDLED
            version = None
            checksum = "abc12345"

            def provenance(self):
                return f"{prompt_id}@bundled:default:abc12345"

        _Rendered.prompt_id = prompt_id

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    user_context = UserContext(
        user_id="u1", user_email="staff@example.com", is_staff=True,
        organization_ids=["3", "9"],
    )
    await provider.get_instructions(user_context)

    assert captured["scope"] == RequestScope(organization_id="3")


@pytest.mark.asyncio
async def test_get_instructions_with_no_organizations_scopes_to_none(monkeypatch):
    from orchestrator.models.schemas import UserContext
    from shared.prompts.types import PromptSource, RequestScope

    captured = {}

    def capture_render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope

        class _Rendered:
            system_text = "system"
            context_text = None
            source = PromptSource.BUNDLED
            version = None
            checksum = "abc12345"

            def provenance(self):
                return f"{prompt_id}@bundled:default:abc12345"

        _Rendered.prompt_id = prompt_id

        return _Rendered()

    monkeypatch.setattr(ip.PROMPTS, "render", capture_render)

    provider = ip.InstructionsProvider()
    user_context = UserContext(
        user_id="u1", user_email="customer@example.com", is_staff=False,
        organization_ids=[],
    )
    await provider.get_instructions(user_context)

    assert captured["scope"] == RequestScope(organization_id=None)
