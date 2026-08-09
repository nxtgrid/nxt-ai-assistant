"""Tests for ConversationSummarizer."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch
from uuid import uuid4

import pytest

from orchestrator.services.conversation_summarizer import (
    SUMMARY_BATCH_SIZE,
    SUMMARY_THRESHOLD,
    ConversationSummarizer,
)
from shared.llm.model_tiers import TIER_ENV_VARS
from shared.prompts import PROMPTS

SUPABASE_CLIENT_TARGET = "orchestrator.services.supabase_client.get_supabase_client"

# ---------------------------------------------------------------------------
# Fakes
#
# ConversationSummarizer has no constructor seam for supabase -- it does a
# lazy `from orchestrator.services.supabase_client import get_supabase_client`
# inside maybe_summarize/get_cached_summary, so tests patch that import
# target directly. _FakeQuery/_FakeTable/_FakeRawClient are a trimmed-down
# version of the _FakeQuery/_FakeTable/_FakeRaw fakes in
# test_escalation_service_ticketing.py, cut down to only what this service
# calls: select/insert, eq/gte/lt, order, limit, execute.
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data


class _FakeQuery:
    """Stands in for the supabase-py postgrest query builder."""

    def __init__(self, table: "_FakeTable", op: str, payload: Optional[Dict] = None) -> None:
        self._table = table
        self._op = op
        self._payload = payload
        self._filters: Dict[str, Any] = {}
        self._range_filters: List[tuple] = []
        self._order: Optional[tuple] = None
        self._limit: Optional[int] = None

    def select(self, *_a: Any, **_k: Any) -> "_FakeQuery":
        return self

    def insert(self, payload: Dict[str, Any]) -> "_FakeQuery":
        self._op = "insert"
        self._payload = payload
        return self

    def eq(self, col: str, value: Any) -> "_FakeQuery":
        self._filters[col] = value
        return self

    def gte(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("gte", col, value))
        return self

    def lt(self, col: str, value: Any) -> "_FakeQuery":
        self._range_filters.append(("lt", col, value))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeQuery":
        self._order = (col, desc)
        return self

    def limit(self, n: int) -> "_FakeQuery":
        self._limit = n
        return self

    def execute(self) -> _FakeResponse:
        if self._op == "insert":
            row = dict(self._payload or {})
            self._table.rows.append(row)
            return _FakeResponse([row])

        rows = [r for r in self._table.rows if all(r.get(c) == v for c, v in self._filters.items())]
        for op, col, value in self._range_filters:
            if op == "gte":
                rows = [r for r in rows if r.get(col) is not None and r[col] >= value]
            elif op == "lt":
                rows = [r for r in rows if r.get(col) is not None and r[col] < value]
        if self._order is not None:
            col, desc = self._order
            rows = sorted(rows, key=lambda r: r.get(col), reverse=desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResponse(rows)


class _FakeTable:
    def __init__(self, rows: Optional[List[Dict[str, Any]]] = None) -> None:
        self.rows: List[Dict[str, Any]] = rows or []

    def select(self, *_a: Any, **_k: Any) -> _FakeQuery:
        return _FakeQuery(self, "select")

    def insert(self, payload: Dict[str, Any]) -> _FakeQuery:
        return _FakeQuery(self, "insert", payload)


class _FakeRawClient:
    def __init__(self) -> None:
        self.tables: Dict[str, _FakeTable] = {}

    def table(self, name: str) -> _FakeTable:
        return self.tables.setdefault(name, _FakeTable())


class _FakeSupabase:
    """Stands in for EnhancedSupabaseClient -- exposes only _get_client()."""

    def __init__(self, raw: _FakeRawClient) -> None:
        self._raw = raw

    def _get_client(self) -> _FakeRawClient:
        return self._raw


class _FakeGateway:
    """Records calls and returns canned text; mirrors GenerationGateway's shape."""

    def __init__(self, text: str = "", *, error: Optional[Exception] = None) -> None:
        self.text = text
        self.error = error
        self.calls: List[Any] = []

    async def generate(self, messages: Any, options: Any) -> SimpleNamespace:
        self.calls.append((messages, options))
        if self.error is not None:
            raise self.error
        return SimpleNamespace(text=self.text)


def _seed_chat_messages(
    raw: _FakeRawClient, session_uuid: Any, count: int = SUMMARY_BATCH_SIZE
) -> None:
    """Seed chat_messages rows for session_uuid -- the real query filters by it."""
    raw.tables["chat_messages"] = _FakeTable(
        rows=[
            {
                "session_id": str(session_uuid),
                "role": "user" if i % 2 == 0 else "model",
                "content": f"message {i}",
                "metadata": {},
                "message_index": i,
            }
            for i in range(count)
        ]
    )


# ---------------------------------------------------------------------------
# maybe_summarize -- threshold gate
# ---------------------------------------------------------------------------


class TestMaybeSummarizeThreshold:
    @pytest.mark.asyncio
    async def test_below_threshold_is_noop(self) -> None:
        """Below SUMMARY_THRESHOLD, returns None without touching supabase."""
        summarizer = ConversationSummarizer(api_key="fake", model="fake-model")
        with patch(
            SUPABASE_CLIENT_TARGET,
            side_effect=AssertionError("should not query supabase below threshold"),
        ):
            result = await summarizer.maybe_summarize(
                session_uuid=uuid4(), total_message_count=SUMMARY_THRESHOLD - 1
            )
        assert result is None


# ---------------------------------------------------------------------------
# maybe_summarize -- generates and caches a summary once over threshold
# ---------------------------------------------------------------------------


class TestMaybeSummarizeGeneratesSummary:
    @pytest.mark.asyncio
    async def test_calls_gateway_and_caches_summary(self) -> None:
        session_uuid = uuid4()
        raw = _FakeRawClient()
        _seed_chat_messages(raw, session_uuid)
        gateway = _FakeGateway(text="Summary of the batch.")
        summarizer = ConversationSummarizer(api_key="fake", model="fake-model", gateway=gateway)

        with patch(SUPABASE_CLIENT_TARGET, return_value=_FakeSupabase(raw)):
            result = await summarizer.maybe_summarize(
                session_uuid=session_uuid, total_message_count=SUMMARY_THRESHOLD + 5
            )

        assert result == "Summary of the batch."
        assert len(gateway.calls) == 1  # gateway was actually invoked

        cached_rows = raw.tables["conversation_summaries"].rows
        assert len(cached_rows) == 1
        assert cached_rows[0]["session_id"] == str(session_uuid)
        assert cached_rows[0]["summary_text"] == "Summary of the batch."
        assert cached_rows[0]["message_range_start"] == 0
        assert cached_rows[0]["message_range_end"] == SUMMARY_BATCH_SIZE

    @pytest.mark.asyncio
    async def test_gateway_failure_fails_open(self) -> None:
        """A gateway error during summarization is swallowed; caller gets None.

        maybe_summarize wraps its whole body in one try/except that logs and
        returns None -- confirmed by reading the source, not assumed.
        """
        session_uuid = uuid4()
        raw = _FakeRawClient()
        _seed_chat_messages(raw, session_uuid)
        gateway = _FakeGateway(error=RuntimeError("boom"))
        summarizer = ConversationSummarizer(api_key="fake", model="fake-model", gateway=gateway)

        with patch(SUPABASE_CLIENT_TARGET, return_value=_FakeSupabase(raw)):
            result = await summarizer.maybe_summarize(
                session_uuid=session_uuid, total_message_count=SUMMARY_THRESHOLD + 5
            )

        assert result is None
        assert len(gateway.calls) == 1  # confirms the failure came from the gateway call
        assert raw.tables["conversation_summaries"].rows == []  # nothing cached on failure


# ---------------------------------------------------------------------------
# get_cached_summary
# ---------------------------------------------------------------------------


class TestGetCachedSummary:
    @pytest.mark.asyncio
    async def test_returns_cached_value(self) -> None:
        session_uuid = uuid4()
        raw = _FakeRawClient()
        raw.tables["conversation_summaries"] = _FakeTable(
            rows=[
                {
                    "session_id": str(session_uuid),
                    "summary_text": "cached blob",
                    "message_range_end": 20,
                }
            ]
        )
        summarizer = ConversationSummarizer(api_key="fake", model="fake-model")

        with patch(SUPABASE_CLIENT_TARGET, return_value=_FakeSupabase(raw)):
            result = await summarizer.get_cached_summary(session_uuid)

        assert result == "cached blob"

    @pytest.mark.asyncio
    async def test_returns_none_when_no_summary_exists(self) -> None:
        summarizer = ConversationSummarizer(api_key="fake", model="fake-model")

        with patch(SUPABASE_CLIENT_TARGET, return_value=_FakeSupabase(_FakeRawClient())):
            result = await summarizer.get_cached_summary(uuid4())

        assert result is None


# ---------------------------------------------------------------------------
# Model tier resolution (constructor)
# ---------------------------------------------------------------------------


class TestModelTierResolution:
    def test_default_construction_resolves_conversation_summarize_tier(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No model= override -> resolves via resolve_model(PROMPTS.spec(...).model).

        Looks up whichever tier conversation.summarize is actually registered
        under (see shared/prompts/library/conversation.summarize.prompt) so
        this doesn't hardcode "fast" and drift if that frontmatter changes.
        """
        monkeypatch.setenv("LLM_PROVIDER", "gemini")
        tier = PROMPTS.spec("conversation.summarize").model
        env_var = TIER_ENV_VARS[tier]
        monkeypatch.setenv(env_var, "gemini-test-model")

        summarizer = ConversationSummarizer(api_key="fake")

        assert summarizer._model == "gemini-test-model"
