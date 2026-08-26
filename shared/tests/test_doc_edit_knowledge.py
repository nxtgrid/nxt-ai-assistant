"""Knowledge modules attached to the doc editor must actually reach it."""

import pytest

from shared.prompts.types import RequestScope
from shared.utils import doc_editing


@pytest.mark.asyncio
async def test_scope_is_passed_to_the_prompt_render(monkeypatch):
    """A site-scoped module is invisible unless render() gets a scope."""
    captured = {}

    class _Rendered:
        system_text = "prompt"
        context_text = None

    def _render(prompt_id, vars=None, scope=None):
        captured["scope"] = scope
        return _Rendered()

    monkeypatch.setattr(doc_editing.PROMPTS, "render", _render)

    class _Gateway:
        async def generate(self, *a, **k):
            from shared.llm.types import GenerateResult

            return GenerateResult(text="ok", tool_calls=[])

    monkeypatch.setattr(doc_editing, "_generation_gateway", lambda: _Gateway(), raising=False)

    await doc_editing.generate_replacement_markdown(
        "rewrite", "old", scope=RequestScope(grid="ExampleGrid")
    )

    assert captured["scope"] is not None
    assert captured["scope"].grid == "ExampleGrid"


@pytest.mark.asyncio
async def test_gdoc_backed_modules_are_resolved(monkeypatch):
    """JIT sources are dropped by render(); the caller must resolve them."""
    resolved = {}

    class _Resolver:
        async def resolve_for_prompt(self, prompt_id, ctx):
            resolved["prompt_id"] = prompt_id
            resolved["email"] = ctx.user_email
            return "# Live Context\n\n## House style\n\nUse short sentences.", ["house-style"]

    monkeypatch.setattr(doc_editing, "_jit_resolver", lambda: _Resolver(), raising=False)

    sent = {}

    class _Gateway:
        async def generate(self, messages, options, **k):
            sent["text"] = messages[0].text
            from shared.llm.types import GenerateResult

            return GenerateResult(text="ok", tool_calls=[])

    monkeypatch.setattr(doc_editing, "_generation_gateway", lambda: _Gateway(), raising=False)

    await doc_editing.generate_replacement_markdown(
        "rewrite", "old", user_email="a@example.com"
    )

    assert resolved["prompt_id"] == "doc_editing.edit_highlighted"
    assert "Use short sentences." in sent["text"]


@pytest.mark.asyncio
async def test_a_failing_resolver_degrades_to_no_live_context(monkeypatch):
    """A broken JIT lookup must not fail the whole edit."""

    class _Resolver:
        async def resolve_for_prompt(self, prompt_id, ctx):
            raise RuntimeError("Drive is down")

    monkeypatch.setattr(doc_editing, "_jit_resolver", lambda: _Resolver(), raising=False)

    class _Gateway:
        async def generate(self, *a, **k):
            from shared.llm.types import GenerateResult

            return GenerateResult(text="ok", tool_calls=[])

    monkeypatch.setattr(doc_editing, "_generation_gateway", lambda: _Gateway(), raising=False)

    out = await doc_editing.generate_replacement_markdown("rewrite", "old")
    assert out == "ok"
