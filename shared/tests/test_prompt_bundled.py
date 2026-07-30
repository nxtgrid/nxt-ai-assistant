"""Tests for the bundled .prompt file store."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.types import PromptNotFound


@pytest.fixture
def store(tmp_path):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: First.\n---\nAlpha body\n"
    )
    (tmp_path / "partials.tone.prompt").write_text(
        "---\nid: partials.tone\ndescription: Tone.\n---\nBe brief.\n"
    )
    return BundledStore(directory=tmp_path)


def test_get_returns_spec(store):
    spec = store.get("a.b")
    assert spec.description == "First."
    assert spec.body.strip() == "Alpha body"


def test_ids_lists_every_prompt(store):
    assert sorted(store.ids()) == ["a.b", "partials.tone"]


def test_unknown_id_raises(store):
    with pytest.raises(PromptNotFound, match="a.missing"):
        store.get("a.missing")


def test_filename_must_match_declared_id(tmp_path):
    (tmp_path / "wrong.prompt").write_text("---\nid: a.b\ndescription: d\n---\nx")
    with pytest.raises(ValueError, match="does not match"):
        BundledStore(directory=tmp_path).ids()


def test_specs_are_parsed_once(store):
    assert store.get("a.b") is store.get("a.b")


def test_reload_picks_up_new_files(store, tmp_path):
    (tmp_path / "c.d.prompt").write_text("---\nid: c.d\ndescription: d\n---\nx")
    store.reload()
    assert "c.d" in store.ids()
