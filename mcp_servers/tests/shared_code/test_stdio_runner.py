"""Behavior of the shared stdio entrypoint every migrated server's main() calls.

Pins the contract: stdio connects, the server loop runs with the given
name/version/capabilities, on_startup runs before the connection, on_cleanup
always runs (success or failure), and exceptions still propagate to the
caller's own `if __name__ == "__main__":` handler after being logged.
"""

import contextlib
import os
import sys

import pytest

_MCP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../"))
_REPO_ROOT = os.path.abspath(os.path.join(_MCP_ROOT, ".."))
for _p in (_MCP_ROOT, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from mcp.server.models import InitializationOptions  # noqa: E402
from mcp.types import ServerCapabilities  # noqa: E402
from shared_code import stdio_runner  # noqa: E402
from shared_code.stdio_runner import run_stdio_server  # noqa: E402


class _FakeServer:
    def __init__(self, exc=None):
        self.exc = exc
        self.run_calls = []

    async def run(self, read_stream, write_stream, options):
        self.run_calls.append((read_stream, write_stream, options))
        if self.exc:
            raise self.exc


@contextlib.asynccontextmanager
async def _fake_stdio_streams():
    yield ("read", "write")


@pytest.fixture(autouse=True)
def _patch_stdio(monkeypatch):
    monkeypatch.setattr(stdio_runner.mcp.server.stdio, "stdio_server", _fake_stdio_streams)


class TestRunStdioServer:
    @pytest.mark.asyncio
    async def test_runs_server_with_expected_options(self):
        server = _FakeServer()
        await run_stdio_server(server, "x-server", "2.0.0")
        assert len(server.run_calls) == 1
        _, _, options = server.run_calls[0]
        assert isinstance(options, InitializationOptions)
        assert options.server_name == "x-server"
        assert options.server_version == "2.0.0"
        assert isinstance(options.capabilities, ServerCapabilities)

    @pytest.mark.asyncio
    async def test_defaults_version_to_1_0_0(self):
        server = _FakeServer()
        await run_stdio_server(server, "x-server")
        assert server.run_calls[0][2].server_version == "1.0.0"

    @pytest.mark.asyncio
    async def test_passes_through_custom_capabilities(self):
        server = _FakeServer()
        custom_caps = ServerCapabilities(tools={})
        await run_stdio_server(server, "x-server", capabilities=custom_caps)
        assert server.run_calls[0][2].capabilities is custom_caps

    @pytest.mark.asyncio
    async def test_on_startup_runs_before_server_run(self):
        calls = []

        async def _startup():
            calls.append("startup")

        class _TrackedServer(_FakeServer):
            async def run(self, *a, **k):
                calls.append("run")
                return await super().run(*a, **k)

        await run_stdio_server(_TrackedServer(), "x-server", on_startup=_startup)
        assert calls == ["startup", "run"]

    @pytest.mark.asyncio
    async def test_on_cleanup_runs_on_success(self):
        calls = []

        async def _cleanup():
            calls.append("cleanup")

        await run_stdio_server(_FakeServer(), "x-server", on_cleanup=_cleanup)
        assert calls == ["cleanup"]

    @pytest.mark.asyncio
    async def test_on_cleanup_runs_and_exception_still_propagates(self):
        calls = []

        async def _cleanup():
            calls.append("cleanup")

        server = _FakeServer(exc=ValueError("kaboom"))
        with pytest.raises(ValueError, match="kaboom"):
            await run_stdio_server(server, "x-server", on_cleanup=_cleanup)
        assert calls == ["cleanup"]

    @pytest.mark.asyncio
    async def test_no_cleanup_is_optional(self):
        # Must not raise even though on_cleanup/on_startup are unset.
        await run_stdio_server(_FakeServer(), "x-server")
