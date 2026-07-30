"""Tests for prompt library value types."""

import pytest

from shared.prompts.types import (
    PromptNotFound,
    PromptSource,
    RenderedPrompt,
    RequestScope,
)


def test_rendered_prompt_carries_provenance():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="You are Anansi.",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="abc123",
    )
    assert rendered.prompt_id == "customer.system"
    assert rendered.source is PromptSource.BUNDLED
    assert rendered.knowledge_used == []


def test_rendered_prompt_provenance_string_is_log_friendly():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="x",
        context_text=None,
        source=PromptSource.DB,
        version=7,
        checksum="deadbeefcafe",
    )
    assert rendered.provenance() == "customer.system@db:v7:deadbeef"


def test_bundled_provenance_has_no_version():
    rendered = RenderedPrompt(
        prompt_id="a.b",
        system_text="x",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="0123456789ab",
    )
    assert rendered.provenance() == "a.b@bundled:default:01234567"


def test_request_scope_matches_sector_always():
    scope = RequestScope(grid="ABC")
    assert scope.matches("sector") is True
    assert scope.matches("site:ABC") is True
    assert scope.matches("site:XYZ") is False


def test_request_scope_without_grid_matches_only_sector():
    scope = RequestScope()
    assert scope.matches("sector") is True
    assert scope.matches("site:ABC") is False


def test_request_scope_site_match_is_case_insensitive():
    assert RequestScope(grid="abc").matches("site:ABC") is True


def test_request_scope_matches_org():
    assert RequestScope(organization_id="2").matches("org:2") is True
    assert RequestScope(organization_id="3").matches("org:2") is False


def test_prompt_not_found_is_an_exception():
    with pytest.raises(PromptNotFound):
        raise PromptNotFound("nope")
