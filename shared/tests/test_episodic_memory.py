"""Episodic distillation batch: selection, write rules and failure handling."""

from typing import Any, Dict, List

import pytest

from shared.episodic_memory import (
    anchors_to_refresh,
    build_distillation_prompt,
    distill_anchor,
    distill_anchor_type,
    select_targets,
)


def _row(anchor_id, edited_by=None, message_count=50):
    return {
        "anchor_type": "grid",
        "anchor_id": anchor_id,
        "edited_by": edited_by,
        "message_count": message_count,
    }


def test_refreshes_an_anchor_with_no_existing_row():
    assert anchors_to_refresh(["Alpha"], existing=[]) == ["Alpha"]


def test_refreshes_an_existing_generated_row():
    assert anchors_to_refresh(["Alpha"], existing=[_row("Alpha")]) == ["Alpha"]


def test_never_overwrites_a_hand_edited_row():
    existing = [_row("Alpha", edited_by="ops@example.com")]
    assert anchors_to_refresh(["Alpha"], existing=existing) == []


def test_refreshes_only_the_anchors_asked_for():
    existing = [_row("Alpha"), _row("Beta")]
    assert anchors_to_refresh(["Beta"], existing=existing) == ["Beta"]


def test_prompt_includes_the_anchor_and_the_messages():
    prompt = build_distillation_prompt("Alpha", ["inverter tripped", "replaced fuse"])
    assert "Alpha" in prompt
    assert "inverter tripped" in prompt
    assert "replaced fuse" in prompt


def test_prompt_asks_for_durable_lessons_not_a_transcript():
    prompt = build_distillation_prompt("Alpha", ["x"])
    assert "transcript" in prompt.lower()


# ── Fakes ────────────────────────────────────────────────────────────────────


class _Query:
    def __init__(self, table: "_Table", rows: List[Dict[str, Any]]):
        self._table = table
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def ilike(self, *_a, **_k):
        return self

    def gte(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def upsert(self, row, **_k):
        self._table.upserts.append(row)
        return self

    def execute(self):
        return type("R", (), {"data": self._rows})()


class _Table:
    def __init__(self, rows_by_table: Dict[str, List[Dict[str, Any]]]):
        self._rows_by_table = rows_by_table
        self.upserts: List[Dict[str, Any]] = []

    def table(self, name):
        return _Query(self, self._rows_by_table.get(name, []))


class _Gateway:
    def __init__(self, text="Durable lesson.", raises=None):
        self._text = text
        self._raises = raises
        self.calls = 0

    async def generate(self, _messages, _options):
        self.calls += 1
        if self._raises:
            raise self._raises
        return type("Resp", (), {"text": self._text})()


def _client(messages=("Alpha inverter tripped",), existing=()):
    return _Table(
        {
            "chat_messages": [{"content": m} for m in messages],
            "episodic_distillations": list(existing),
        }
    )


# ── select_targets ───────────────────────────────────────────────────────────


def test_select_targets_drops_hand_edited_anchors():
    client = _client(existing=[_row("Alpha", edited_by="ops@example.com"), _row("Beta")])
    assert select_targets(client, "grid", ["Alpha", "Beta"]) == ["Beta"]


# ── distill_anchor ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_distill_anchor_writes_the_summary():
    client = _client()
    written = await distill_anchor(client, "grid", "Alpha", _Gateway(), "m")
    assert written == len("Durable lesson.")
    assert len(client.upserts) == 1
    assert client.upserts[0]["anchor_id"] == "Alpha"
    assert client.upserts[0]["summary"] == "Durable lesson."
    assert client.upserts[0]["message_count"] == 1


@pytest.mark.asyncio
async def test_a_quiet_anchor_writes_nothing():
    """No messages is an ordinary outcome, not a reason to write an empty row."""
    client = _client(messages=())
    gateway = _Gateway()
    assert await distill_anchor(client, "grid", "Alpha", gateway, "m") is None
    assert gateway.calls == 0  # never pay for an LLM call with nothing to distil
    assert client.upserts == []


@pytest.mark.asyncio
async def test_an_empty_model_response_does_not_overwrite_a_good_summary():
    client = _client()
    assert await distill_anchor(client, "grid", "Alpha", _Gateway(text="  "), "m") is None
    assert client.upserts == []


# ── distill_anchor_type ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_zero_enumerated_anchors_is_reported_separately_from_zero_targets(monkeypatch):
    """The scheduler needs to tell an outage from an empty deployment.

    get_eligible_entities returns [] both when the Auth DB is unreachable and
    when there genuinely are none, so `enumerated` is surfaced rather than
    collapsed into "nothing to do".
    """
    monkeypatch.setattr(
        "shared.episodic_memory.eligible_anchor_names", _async_returning([])
    )
    result = await distill_anchor_type("grid", apply=True, client=_client())
    assert result["enumerated"] == 0
    assert result["targets"] == []
    assert result["written"] == 0


@pytest.mark.asyncio
async def test_a_dry_run_enumerates_targets_but_writes_nothing(monkeypatch):
    monkeypatch.setattr(
        "shared.episodic_memory.eligible_anchor_names", _async_returning(["Alpha"])
    )
    client = _client()
    result = await distill_anchor_type("grid", apply=False, client=client)
    assert result["targets"] == ["Alpha"]
    assert result["written"] == 0
    assert client.upserts == []


@pytest.mark.asyncio
async def test_one_failing_anchor_does_not_sink_the_rest(monkeypatch):
    """A single oversized history or transient LLM error must not cost the run."""
    monkeypatch.setattr(
        "shared.episodic_memory.eligible_anchor_names", _async_returning(["Alpha", "Beta"])
    )

    calls = []

    async def _flaky(client, anchor_type, name, gateway, model):
        calls.append(name)
        if name == "Alpha":
            raise RuntimeError("context window exceeded")
        return 42

    monkeypatch.setattr("shared.episodic_memory.distill_anchor", _flaky)
    monkeypatch.setattr(
        "shared.llm.get_default_generation_gateway", lambda: _Gateway(), raising=False
    )
    monkeypatch.setattr("shared.llm.model_tiers.resolve_model", lambda _t: "m", raising=False)

    result = await distill_anchor_type("grid", apply=True, client=_client())

    assert calls == ["Alpha", "Beta"]
    assert result["written"] == 1
    assert result["skipped"] == ["Alpha"]


@pytest.mark.asyncio
async def test_missing_credentials_report_an_error_rather_than_crashing(monkeypatch):
    """No chat_db is a reported condition, not a traceback in a daemon loop.

    build_client is patched rather than the environment: a local
    chat_orchestrator/.env with real credentials would otherwise make this
    pass for the wrong reason, or hit the real database.
    """
    monkeypatch.setattr("shared.episodic_memory.build_client", lambda: None)
    result = await distill_anchor_type("grid", apply=True, client=None)
    assert result["error"] == "CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set"


def _async_returning(value):
    async def _fn(_anchor_type):
        return value

    return _fn
