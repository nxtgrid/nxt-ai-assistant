"""The write API enforces access and the propose-but-never-publish rule."""

import pytest

from shared.prompts.bundled import BundledStore
from shared.prompts.core import PromptLibrary


class RecordingStore:
    def __init__(self):
        self.proposed = []
        self.published = []

    def is_configured(self):
        return True

    def propose(self, prompt_id, body, note, actor, via="ui"):
        self.proposed.append((prompt_id, body, actor, via))
        return len(self.proposed)

    def publish(self, prompt_id, version, actor):
        self.published.append((prompt_id, version, actor))

    def body_for(self, prompt_id):
        return None

    def doc_id_for(self, prompt_id):
        return None


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\noverridable: true\n"
        "access:\n  edit: [ops]\n  publish: [eng]\n---\nbody\n"
    )
    (tmp_path / "locked.prompt").write_text(
        "---\nid: locked\ndescription: d\noverridable: false\n---\nbody\n"
    )
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    monkeypatch.setenv("PROMPT_EDITORS_ENG", "eve@x.com")
    monkeypatch.delenv("GRID_DESIGN_DEV_NO_AUTH", raising=False)
    monkeypatch.delenv("PROMPT_ADMINS", raising=False)
    store = RecordingStore()
    library = PromptLibrary(bundled=BundledStore(directory=tmp_path), overrides=store)
    return library, store


def test_editor_can_propose(lib):
    library, store = lib
    library.propose("a.b", "new body", note="n", actor="ada@x.com")
    assert store.proposed[0][0] == "a.b"


def test_non_editor_cannot_propose(lib):
    library, _ = lib
    with pytest.raises(PermissionError, match="edit"):
        library.propose("a.b", "new", note="n", actor="bob@x.com")


def test_editor_without_publish_cannot_publish(lib):
    library, _ = lib
    with pytest.raises(PermissionError, match="publish"):
        library.publish("a.b", 1, actor="ada@x.com")


def test_publisher_can_publish(lib):
    library, store = lib
    library.publish("a.b", 1, actor="eve@x.com")
    assert store.published == [("a.b", 1, "eve@x.com")]


def test_non_overridable_prompt_rejects_propose(lib):
    library, _ = lib
    with pytest.raises(PermissionError):
        library.propose("locked", "new", note="n", actor="ada@x.com")


def test_api_actor_may_propose_but_never_publish(lib):
    library, store = lib
    library.propose("a.b", "new", note="n", actor="agent@system", via="api", enforce_access=False)
    assert store.proposed[-1][3] == "api"
    with pytest.raises(PermissionError, match="Automated"):
        library.publish("a.b", 1, actor="agent@system", via="api")


def test_propose_without_overrides_configured_raises(tmp_path, monkeypatch):
    (tmp_path / "a.b.prompt").write_text(
        "---\nid: a.b\ndescription: d\noverridable: true\naccess:\n  edit: [ops]\n---\nbody\n"
    )
    monkeypatch.setenv("PROMPT_EDITORS_OPS", "ada@x.com")
    library = PromptLibrary(bundled=BundledStore(directory=tmp_path))
    with pytest.raises(RuntimeError, match="not configured"):
        library.propose("a.b", "new", note="n", actor="ada@x.com")
