"""Tests for agent worker database connection patterns.

Verifies SSL is always used for both asyncpg (PG LISTEN) and
psycopg (LangGraph checkpointer) connections.
"""

import os
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_ctx_manager(mock_saver):
    """Wrap a mock saver in an async context manager like from_conn_string returns."""

    @asynccontextmanager
    async def _ctx(*args, **kwargs):
        yield mock_saver

    return _ctx


# Test URLs without user:pass to avoid detect-secrets false positives
PG_URL = "postgresql://host:5432/db"
PG_URL_WITH_SSL = "postgresql://host:5432/db?sslmode=verify-full"
PG_URL_WITH_PARAMS = "postgresql://host:5432/db?connect_timeout=10"


@pytest.fixture
def worker():
    from orchestrator.services.agent_worker import AgentWorker

    return AgentWorker(supabase_url="https://test.supabase.co", supabase_key="test-key")


class TestPgListenerSSL:
    """Verify _start_pg_listener passes ssl='require' to asyncpg."""

    @pytest.mark.asyncio
    async def test_asyncpg_connect_uses_ssl(self, worker):
        mock_conn = AsyncMock()
        mock_conn.add_listener = AsyncMock()

        with patch.dict(os.environ, {"CHAT_DB_POSTGRES_URL": PG_URL}):
            with patch(
                "asyncpg.connect", new_callable=AsyncMock, return_value=mock_conn
            ) as mock_connect:
                await worker._start_pg_listener()

                mock_connect.assert_called_once_with(
                    PG_URL,
                    ssl="require",
                    statement_cache_size=0,
                )
                mock_conn.add_listener.assert_called_once()

    @pytest.mark.asyncio
    async def test_pg_listener_falls_back_on_no_url(self, worker):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CHAT_DB_POSTGRES_URL", None)
            os.environ.pop("CHAT_DB_URL", None)
            await worker._start_pg_listener()
            assert worker._listener_conn is None

    @pytest.mark.asyncio
    async def test_pg_listener_graceful_on_connection_failure(self, worker):
        with patch.dict(os.environ, {"CHAT_DB_POSTGRES_URL": PG_URL}):
            with patch(
                "asyncpg.connect",
                new_callable=AsyncMock,
                side_effect=Exception("conn refused"),
            ):
                await worker._start_pg_listener()
                assert worker._listener_conn is None


class TestCheckpointerSSL:
    """Verify _init_checkpointer appends sslmode=require when missing."""

    @pytest.mark.asyncio
    async def test_appends_sslmode_when_missing(self, worker):
        mock_saver = MagicMock()
        mock_saver.setup = AsyncMock()
        captured_urls = []

        original_ctx = _mock_ctx_manager(mock_saver)

        def capturing_from_conn(url):
            captured_urls.append(url)
            return original_ctx(url)

        with patch.dict(os.environ, {"CHAT_DB_POSTGRES_URL": PG_URL}):
            with patch(
                "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
                side_effect=capturing_from_conn,
            ):
                await worker._init_checkpointer()

                assert len(captured_urls) == 1
                assert "sslmode=require" in captured_urls[0]
                assert captured_urls[0] == f"{PG_URL}?sslmode=require"
                mock_saver.setup.assert_called_once()

    @pytest.mark.asyncio
    async def test_preserves_existing_sslmode(self, worker):
        mock_saver = MagicMock()
        mock_saver.setup = AsyncMock()
        captured_urls = []

        original_ctx = _mock_ctx_manager(mock_saver)

        def capturing_from_conn(url):
            captured_urls.append(url)
            return original_ctx(url)

        with patch.dict(os.environ, {"CHAT_DB_POSTGRES_URL": PG_URL_WITH_SSL}):
            with patch(
                "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
                side_effect=capturing_from_conn,
            ):
                await worker._init_checkpointer()

                assert captured_urls[0] == PG_URL_WITH_SSL

    @pytest.mark.asyncio
    async def test_appends_sslmode_with_existing_query_params(self, worker):
        mock_saver = MagicMock()
        mock_saver.setup = AsyncMock()
        captured_urls = []

        original_ctx = _mock_ctx_manager(mock_saver)

        def capturing_from_conn(url):
            captured_urls.append(url)
            return original_ctx(url)

        with patch.dict(os.environ, {"CHAT_DB_POSTGRES_URL": PG_URL_WITH_PARAMS}):
            with patch(
                "langgraph.checkpoint.postgres.aio.AsyncPostgresSaver.from_conn_string",
                side_effect=capturing_from_conn,
            ):
                await worker._init_checkpointer()

                assert captured_urls[0] == f"{PG_URL_WITH_PARAMS}&sslmode=require"

    @pytest.mark.asyncio
    async def test_raises_on_no_url(self, worker):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("CHAT_DB_POSTGRES_URL", None)
            os.environ.pop("CHAT_DB_URL", None)
            with pytest.raises(ValueError, match="CHAT_DB_POSTGRES_URL"):
                await worker._init_checkpointer()
