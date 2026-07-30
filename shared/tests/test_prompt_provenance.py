"""Prompt provenance reaches logs and the Langfuse trace."""

from shared.prompts.types import PromptSource, RenderedPrompt
from shared.utils.langfuse_utils import prompt_metadata


def test_prompt_metadata_shape():
    rendered = RenderedPrompt(
        prompt_id="customer.system",
        system_text="x",
        context_text=None,
        source=PromptSource.DB,
        version=3,
        checksum="abcdef0123456789",
    )
    assert prompt_metadata(rendered) == {
        "prompt_id": "customer.system",
        "prompt_source": "db",
        "prompt_version": 3,
        "prompt_checksum": "abcdef01",
    }


def test_prompt_metadata_for_bundled_has_null_version():
    rendered = RenderedPrompt(
        prompt_id="a.b",
        system_text="x",
        context_text=None,
        source=PromptSource.BUNDLED,
        version=None,
        checksum="0011223344556677",
    )
    assert prompt_metadata(rendered)["prompt_version"] is None


def test_prompt_metadata_checksum_is_truncated_to_eight_chars():
    rendered = RenderedPrompt(
        prompt_id="a.b",
        system_text="x",
        context_text=None,
        source=PromptSource.GDOC,
        version=None,
        checksum="a" * 64,
    )
    assert prompt_metadata(rendered)["prompt_checksum"] == "aaaaaaaa"
