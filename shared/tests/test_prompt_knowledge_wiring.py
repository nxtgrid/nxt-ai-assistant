"""Rendering a prompt composes its knowledge into the context channel."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.core import PromptLibrary
from shared.prompts.knowledge import KnowledgeModule
from shared.prompts.types import RequestScope


class FakeKnowledge:
    def __init__(self, modules, overrides=None):
        self._modules = modules
        self._overrides = overrides or {}

    def all_modules(self):
        return self._modules

    def overrides_for(self, prompt_id):
        return self._overrides.get(prompt_id, {})


@pytest.fixture
def bundled(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\nknowledge_tags: [grid_ops]\n---\nBody.\n"
    )
    (tmp_path / "none.prompt").write_text("---\nid: none\ndescription: d\n---\nBody.\n")
    return BundledStore(directory=tmp_path)


def _module(slug, mode="pinned"):
    return KnowledgeModule(
        id=slug,
        slug=slug,
        title=slug.title(),
        summary=f"About {slug}.",
        body=f"{slug} body",
        tags=["grid_ops"],
        scope="sector",
        mode=mode,
    )


def test_pinned_module_lands_in_the_context_channel(bundled):
    library = PromptLibrary(
        bundled=bundled,
        knowledge=FakeKnowledge([_module("comms")], {"a.b": {"comms": True}}),
    )
    out = library.render("a.b", scope=RequestScope())
    assert "# Technical Knowledge" in (out.context_text or "")
    assert "comms body" in out.context_text
    assert out.knowledge_used == ["comms"]


def test_on_demand_module_contributes_only_a_catalog_line(bundled):
    library = PromptLibrary(
        bundled=bundled,
        knowledge=FakeKnowledge([_module("sites", mode="on_demand")], {"a.b": {"sites": True}}),
    )
    out = library.render("a.b", scope=RequestScope())
    assert "sites body" not in (out.context_text or "")
    assert "About sites." in out.context_text


def test_prompt_without_tags_gets_no_knowledge(bundled):
    library = PromptLibrary(bundled=bundled, knowledge=FakeKnowledge([_module("comms")]))
    out = library.render("none", scope=RequestScope())
    assert out.context_text is None
    assert out.knowledge_used == []


def test_per_prompt_override_forces_a_module_off(bundled):
    library = PromptLibrary(
        bundled=bundled,
        knowledge=FakeKnowledge([_module("comms")], {"a.b": {"comms": False}}),
    )
    assert library.render("a.b", scope=RequestScope()).knowledge_used == []


def test_knowledge_failure_does_not_break_rendering(bundled):
    class Broken:
        def all_modules(self):
            raise RuntimeError("db down")

        def overrides_for(self, prompt_id):
            return {}

    out = PromptLibrary(bundled=bundled, knowledge=Broken()).render("a.b")
    assert out.system_text == "Body."
    assert out.knowledge_used == []


def test_no_knowledge_store_configured_is_a_noop(bundled):
    library = PromptLibrary(bundled=bundled)
    out = library.render("a.b", scope=RequestScope())
    assert out.context_text is None
    assert out.knowledge_used == []


def test_knowledge_appends_after_existing_context_text(tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\nknowledge_tags: [grid_ops]\n"
        "sections: [system_instructions]\n---\n"
        "# System Instructions\n\nSys.\n\n# Examples\n\nEx.\n"
    )
    bundled = BundledStore(directory=tmp_path)
    library = PromptLibrary(
        bundled=bundled,
        knowledge=FakeKnowledge([_module("comms")], {"c.d": {"comms": True}}),
    )
    out = library.render("c.d", scope=RequestScope())
    assert "# Examples" in out.context_text
    assert "# Technical Knowledge" in out.context_text
    assert out.context_text.index("# Examples") < out.context_text.index("# Technical Knowledge")


def test_compose_uses_pins_not_tags(monkeypatch):
    """A pinned module renders even though the prompt declares no tags."""
    from shared.prompts.core import PromptLibrary
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="m1", slug="comms", title="Comms", summary="About comms.",
        body="Radio checks hourly.", tags=[], scope="sector", mode="pinned",
    )

    class _Store:
        def all_modules(self):
            return [module]

        def overrides_for(self, prompt_id):
            return {"comms": True}

    library = PromptLibrary(knowledge=_Store())
    spec = library.spec("staff.system")
    text, used = library._compose_knowledge(spec, RequestScope())
    assert "Radio checks hourly." in text
    assert used == ["comms"]


def test_a_gdoc_module_is_left_to_the_jit_resolver():
    """gdoc is JIT now: PromptLibrary must not try to resolve it inline.

    It has no caller identity (RequestScope carries grid and organization
    only), so resolving here would mean serving document content with no
    access check at all.
    """
    from shared.prompts.knowledge import KnowledgeModule

    module = KnowledgeModule(
        id="d", slug="doc-module", title="Doc", summary="From a doc.",
        body=None, mode="pinned", source="gdoc", source_ref="doc-1",
        doc_audience="acl_mirror",
    )
    assert module.is_jit is True
