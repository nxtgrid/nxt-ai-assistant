"""AuthService's cached asyncpg pool must follow the running event loop.

``get_auth_service()`` is a process-wide singleton and it caches its asyncpg
pool on ``self._db_pool``. asyncpg pools are bound to the event loop that
created them. The anansi_app poller daemons (episodic_scheduler.py,
grafana_scheduler.py, broadcast_scheduler.py) drive each unit of work with a
fresh ``asyncio.run(...)`` and that call closes its loop on return. The next
call then finds a pool bound to a dead loop -> ``RuntimeError: Event loop is
closed`` on acquire, forever, and the dead pool's connections are never
released (a socket/fd leak that accumulates every nightly run).

The fix: when the cached pool belongs to a different loop, discard it (best
effort ``terminate()``) and build a fresh one for the current loop.
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("asyncpg")


@pytest.fixture(autouse=True)
def _auth_db_env(monkeypatch):
    monkeypatch.setenv("AUTH_DB_HOST", "localhost")
    monkeypatch.setenv("AUTH_DB_USER", "readonly")
    monkeypatch.setenv("AUTH_DB_PASSWORD", "secret")


class _FakePool:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True


@pytest.fixture
def fake_create_pool(monkeypatch):
    created: list[_FakePool] = []

    async def _create_pool(**_kwargs):
        pool = _FakePool()
        created.append(pool)
        return pool

    monkeypatch.setattr("asyncpg.create_pool", _create_pool)
    return created


def _new_auth_service():
    from shared.auth.auth_service import AuthService

    return AuthService()


@pytest.mark.asyncio
async def test_pool_is_reused_within_one_loop(fake_create_pool):
    auth = _new_auth_service()

    first = await auth._get_db_pool()
    second = await auth._get_db_pool()

    assert first is second
    assert len(fake_create_pool) == 1
    assert first.terminated is False


def test_pool_is_rebuilt_after_its_loop_closes(fake_create_pool):
    """Simulates a poller daemon: one AuthService, two asyncio.run() calls."""
    auth = _new_auth_service()

    pool1 = asyncio.run(auth._get_db_pool())
    pool2 = asyncio.run(auth._get_db_pool())

    assert pool1 is not pool2
    assert len(fake_create_pool) == 2
    assert pool1.terminated is True, "the stale pool must be torn down, not leaked"


def test_stale_pool_discarded_even_when_terminate_raises(fake_create_pool, monkeypatch):
    auth = _new_auth_service()

    pool1 = asyncio.run(auth._get_db_pool())

    def _raise() -> None:
        raise RuntimeError("Event loop is closed")

    monkeypatch.setattr(pool1, "terminate", _raise)

    pool2 = asyncio.run(auth._get_db_pool())  # must not propagate

    assert pool2 is not pool1
    assert len(fake_create_pool) == 2


@pytest.mark.asyncio
async def test_pool_records_the_loop_it_was_built_on(fake_create_pool):
    auth = _new_auth_service()

    await auth._get_db_pool()

    assert auth._db_pool_loop is asyncio.get_running_loop()
