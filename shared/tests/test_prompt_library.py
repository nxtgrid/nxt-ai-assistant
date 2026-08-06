"""Tests for the PromptLibrary resolution order."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.core import PromptLibrary
from shared.prompts.types import PromptSource


@pytest.fixture
def bundled(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\noverridable: true\n"
        "sections: [system_instructions]\n---\n"
        "# System Instructions\n\nBundled text.\n"
    )
    (tmp_path / "locked.prompt").write_text(
        "---\nid: locked\ndescription: d\noverridable: false\n---\nLocked text.\n"
    )
    (tmp_path / "partials.tone.prompt").write_text(
        "---\nid: partials.tone\ndescription: d\n---\nBe brief.\n"
    )
    return BundledStore(directory=tmp_path)


def test_falls_back_to_bundled(bundled):
    lib = PromptLibrary(bundled=bundled)
    out = lib.render("a.b")
    assert out.system_text == "Bundled text."
    assert out.source is PromptSource.BUNDLED
    assert out.version is None


def test_gdoc_beats_bundled(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
    )
    out = lib.render("a.b")
    assert out.system_text == "Doc text."
    assert out.source is PromptSource.GDOC


def test_db_beats_gdoc(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
    )
    out = lib.render("a.b")
    assert out.system_text == "Db text."
    assert out.source is PromptSource.DB
    assert out.version == 4


def test_non_overridable_ignores_db_and_gdoc(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "Doc text.",
        db_body_for=lambda pid: ("Db text.", 4),
    )
    out = lib.render("locked")
    assert out.system_text == "Locked text."
    assert out.source is PromptSource.BUNDLED


def test_db_failure_falls_through_to_bundled(bundled):
    def boom(prompt_id):
        raise RuntimeError("db down")

    lib = PromptLibrary(bundled=bundled, db_body_for=boom)
    out = lib.render("a.b")
    assert out.system_text == "Bundled text."
    assert out.source is PromptSource.BUNDLED


def test_partials_resolve_from_the_bundled_store(bundled, tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\n---\n{{> partials.tone}}\n"
    )
    bundled.reload()
    lib = PromptLibrary(bundled=bundled)
    assert lib.render("c.d").system_text == "Be brief."


def test_partials_always_come_from_bundled_even_when_host_is_overridden(bundled, tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\noverridable: true\n---\nx\n"
    )
    bundled.reload()
    lib = PromptLibrary(
        bundled=bundled,
        db_body_for=lambda pid: ("{{> partials.tone}}", 1) if pid == "c.d" else None,
    )
    assert lib.render("c.d").system_text == "Be brief."


def test_checksum_reflects_the_body_actually_used(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 2),
    )
    a = lib.render("a.b")
    b = PromptLibrary(bundled=bundled).render("a.b")
    assert a.checksum != b.checksum


def test_ids_come_from_the_bundled_store(bundled):
    lib = PromptLibrary(bundled=bundled)
    assert "a.b" in lib.ids()


def test_spec_exposes_frontmatter(bundled):
    lib = PromptLibrary(bundled=bundled)
    assert lib.spec("locked").overridable is False


def test_invalidate_doc_cache_calls_the_hook(bundled):
    calls = []
    lib = PromptLibrary(bundled=bundled, invalidate_gdoc=lambda: calls.append(1))
    lib.invalidate_doc_cache()
    assert calls == [1]


def test_invalidate_doc_cache_is_a_noop_without_a_hook(bundled):
    lib = PromptLibrary(bundled=bundled)
    lib.invalidate_doc_cache()  # must not raise


def test_resolve_returns_the_raw_body_unsubstituted(bundled, tmp_path):
    (tmp_path / "v.w.prompt").write_text(
        "---\nid: v.w\ndescription: d\nvariables: [name]\n---\nHello {{name}}.\n"
    )
    bundled.reload()
    lib = PromptLibrary(bundled=bundled)
    body, source, version = lib.resolve("v.w")
    assert body == "Hello {{name}}.\n"
    assert source is PromptSource.BUNDLED
    assert version is None


def test_resolve_reflects_db_override_unsubstituted(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        db_body_for=lambda pid: ("Hi {{x}}.", 3) if pid == "a.b" else None,
    )
    body, source, version = lib.resolve("a.b")
    assert body == "Hi {{x}}."
    assert source is PromptSource.DB
    assert version == 3


def test_resolve_never_inlines_partials(bundled, tmp_path):
    (tmp_path / "c.d.prompt").write_text(
        "---\nid: c.d\ndescription: d\n---\n{{> partials.tone}}\n"
    )
    bundled.reload()
    lib = PromptLibrary(bundled=bundled)
    body, _source, _version = lib.resolve("c.d")
    assert body == "{{> partials.tone}}\n"


# ── doc-override precedence toggle ──────────────────────────────────────────
#
# is_override governs DB-vs-doc order only. False (including the default of
# no doc_override_for at all, covered by test_db_beats_gdoc above) means
# today's order: DB, then doc, then bundled. True flips DB and doc; bundled
# stays last either way.


def test_doc_override_flips_doc_ahead_of_db(bundled):
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
        doc_override_for=lambda pid: True,
    )
    out = lib.render("a.b")
    assert out.system_text == "Doc text."
    assert out.source is PromptSource.GDOC


def test_doc_override_false_keeps_db_first(bundled):
    """Explicit False must behave identically to no toggle wired up at all --
    this is the byte-identical-to-before guarantee, exercised explicitly
    rather than only via the implicit-None case in test_db_beats_gdoc."""
    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
        doc_override_for=lambda pid: False,
    )
    out = lib.render("a.b")
    assert out.system_text == "Db text."
    assert out.source is PromptSource.DB


def test_doc_override_with_doc_failure_falls_through_to_db(bundled):
    """Under override order (doc, then DB, then bundled), a doc-fetch
    failure must fall through to DB next -- not straight to bundled, which
    is what the old fixed-order code's "using bundled" log message
    (inaccurate once a toggle could reorder these) would have implied."""

    def boom(pid):
        raise RuntimeError("doc fetch failed")

    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=boom,
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
        doc_override_for=lambda pid: True,
    )
    out = lib.render("a.b")
    assert out.system_text == "Db text."
    assert out.source is PromptSource.DB


def test_doc_override_with_both_sources_failing_falls_through_to_bundled(bundled):
    def boom(pid):
        raise RuntimeError("down")

    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=boom,
        db_body_for=boom,
        doc_override_for=lambda pid: True,
    )
    out = lib.render("a.b")
    assert out.system_text == "Bundled text."
    assert out.source is PromptSource.BUNDLED


def test_doc_override_for_receiving_the_prompt_id(bundled):
    """The toggle is per-prompt, not global -- confirm the id passed to
    render() is what reaches doc_override_for."""
    seen = []

    def override_for(pid):
        seen.append(pid)
        return True

    lib = PromptLibrary(
        bundled=bundled,
        gdoc_body_for=lambda pid: "# System Instructions\n\nDoc text.",
        db_body_for=lambda pid: ("# System Instructions\n\nDb text.", 4),
        doc_override_for=override_for,
    )
    lib.render("a.b")
    assert seen == ["a.b"]
