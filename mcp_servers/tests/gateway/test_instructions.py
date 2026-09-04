"""The per-caller `instructions` delivered at initialize.

Two distinct things are covered here:

1.  Composition (build_instructions) — that the right prompt is chosen for the
    caller, that the knowledge modules composed into context_text come along,
    that the preamble and tail are always present, and that a failure to
    render degrades to None instead of breaking the handshake.
2.  Delivery — that the composed string actually reaches InitializeResult over
    real ASGI, and is scoped to the calling user rather than leaking between
    concurrent callers.

(2) is the one that cannot be reasoned about safely. `instructions` is read
from Server.instructions inside create_initialization_options(), which the
session manager calls in a task it spawns from a task group created back in
the LIFESPAN context — so whether a ContextVar set in the ASGI adapter is
visible there is a question about anyio task-spawn semantics, not about this
code. It is pinned here rather than assumed.
"""

import asyncio
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import FrozenSet, Optional

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import httpx
import pytest
from gateway import instructions as instructions_module
from gateway.app import build_asgi_app
from gateway.instructions import PREAMBLE, TAIL, build_instructions, clear_cache
from gateway.tokens import issue_token

SECRET = "test-secret-not-a-real-key"  # pragma: allowlist secret


@dataclass
class _Session:
    email: str = "user@example.com"
    user_id: str = "u1"
    organization_id: str = "7"
    organization_short_name: Optional[str] = "acme"
    grid_names: FrozenSet[str] = frozenset()
    is_staff: bool = False


class _Rendered:
    def __init__(self, system_text, context_text=None, knowledge_used=None):
        self.system_text = system_text
        self.context_text = context_text
        self.knowledge_used = knowledge_used or []

    def provenance(self):
        return "test@bundled:default:0000"


def _renderer(system_text="SYSTEM BODY", context_text=None, knowledge_used=None, record=None):
    def render(prompt_id, scope=None):
        if record is not None:
            record.append((prompt_id, scope))
        return _Rendered(system_text, context_text, knowledge_used)

    return render


@pytest.fixture(autouse=True)
def _no_cache_between_tests():
    clear_cache()
    yield
    clear_cache()


# --------------------------------------------------------------------------
# Composition
# --------------------------------------------------------------------------


def test_staff_and_customer_get_different_prompts():
    """The staff/customer split is settled at sign-in, so it is a straight
    branch here — no runtime tiering, no capability negotiation."""
    record = []
    build_instructions(_Session(is_staff=True), render=_renderer(record=record))
    build_instructions(_Session(email="c@example.com", is_staff=False), render=_renderer(record=record))

    assert [r[0] for r in record] == ["staff.system", "customer.system"]


def test_the_caller_organization_scopes_the_render():
    """Knowledge modules are org- and site-scoped via RequestScope. Passing the
    wrong org here would inline another organisation's modules, so this pins
    that the caller's own org reaches render()."""
    record = []
    build_instructions(_Session(organization_id="42"), render=_renderer(record=record))

    _prompt_id, scope = record[0]
    assert scope.organization_id == "42"


def test_knowledge_modules_are_included():
    """The whole point: render() already composes attached modules into
    context_text (core.py's _compose_knowledge), so the ontology reaches the
    client under the same inclusion rules the chat path uses. If this stops
    being appended, an MCP client silently loses all curated domain context
    while still looking healthy."""
    text = build_instructions(
        _Session(),
        render=_renderer(
            system_text="SYSTEM BODY",
            context_text="# Technical Knowledge\n\nA grid is ...",
            knowledge_used=["grid-basics"],
        ),
    )

    assert "SYSTEM BODY" in text
    assert "# Technical Knowledge" in text
    assert "A grid is ..." in text


def test_preamble_leads_and_tail_closes():
    text = build_instructions(_Session(), render=_renderer())

    assert text.startswith(PREAMBLE)
    assert text.endswith(TAIL)
    assert text.index("SYSTEM BODY") > text.index(PREAMBLE.strip()[:40])


def test_the_preamble_names_every_exclusion_category():
    """These categories are the entire mechanism: the prompts are co-edited by
    operators, so nothing marks the Telegram-specific passages in the body.
    Losing a category here means a client silently starts acting on
    instructions written for a different harness — offering buttons it cannot
    render, or claiming to have escalated when it has no messaging tools."""
    for category in (
        "Channel and formatting",
        "Interactive UI",
        "Escalation and notification",
        "Tool names and availability",
        "Conversational turn-taking",
        "Media protocol",
        "Session machinery",
        "Who is being addressed",
    ):
        assert category in PREAMBLE, category


def test_the_preamble_warns_that_a_name_may_collide_with_a_public_place():
    """A real production failure, not a hypothetical: a client resolved a
    genuine Anansi site name (a private mini-grid) against an UNRELATED
    public record for a same-named place in the same region and answered
    confidently from that instead — no hedge, no check against Anansi's own
    tools, which had the correct answer one call away. General-knowledge and
    web-search confidence are not evidence a name isn't one of ours; this
    pins the standing instruction that a name-shaped answer must still be
    verified against this system before being treated as authoritative."""
    assert "public" in PREAMBLE
    assert "not sufficient confirmation" in PREAMBLE
    assert "this system's own tools" in PREAMBLE


def test_a_render_failure_degrades_to_no_instructions():
    """Instructions are advisory; the tool surface is not. A broken prompt must
    never take down initialize."""

    def exploding_render(prompt_id, scope=None):
        raise RuntimeError("prompt store unavailable")

    assert build_instructions(_Session(), render=exploding_render) is None


def test_an_empty_prompt_yields_no_instructions():
    assert build_instructions(_Session(), render=_renderer(system_text="  ")) is None


def test_oversized_content_is_truncated_but_keeps_preamble_and_tail():
    """The exclusions are what make the body safe to read, so they survive
    truncation even when the body does not."""
    huge = "x" * (instructions_module.MAX_INSTRUCTIONS_CHARS * 2)
    text = build_instructions(_Session(), render=_renderer(system_text=huge))

    assert len(text) <= instructions_module.MAX_INSTRUCTIONS_CHARS
    assert text.startswith(PREAMBLE)
    assert text.endswith(TAIL)
    assert "[Truncated due to size limits]" in text


def test_results_are_cached_per_caller_and_not_shared_across_users():
    """A cache keyed too loosely would hand one organisation's context to
    another — the one bug this cache could plausibly cause."""
    calls = []
    render = _renderer(record=calls)

    staff = _Session(email="a@example.com", organization_id="1", is_staff=True)
    customer = _Session(email="b@example.com", organization_id="2", is_staff=False)

    build_instructions(staff, render=render)
    build_instructions(staff, render=render)  # cached — no second render
    build_instructions(customer, render=render)

    assert [c[0] for c in calls] == ["staff.system", "customer.system"]


def test_the_cache_expires():
    calls = []
    render = _renderer(record=calls)
    clock = {"t": 1000.0}

    build_instructions(_Session(), render=render, now=lambda: clock["t"])
    clock["t"] += instructions_module._CACHE_TTL_SECONDS + 1
    build_instructions(_Session(), render=render, now=lambda: clock["t"])

    assert len(calls) == 2


# --------------------------------------------------------------------------
# Delivery over real ASGI
# --------------------------------------------------------------------------


class _Auth:
    """Minimal AuthService stand-in; returns staff/customer by email."""

    class _Perms:
        def __init__(self, is_staff):
            self.organization_ids = ["7"]
            self.is_staff = is_staff
            self.organization_short_name = "acme"
            self.user_id = "u1"

    async def get_user_permissions(self, email, user_id=None):
        return self._Perms(is_staff=email.startswith("staff"))

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return frozenset()


async def _exploding_list_tools(server_name):
    raise AssertionError("initialize must not touch the registry")


async def _exploding_call_tool(server_name, tool_name, arguments):
    raise AssertionError("initialize must not touch the registry")


@asynccontextmanager
async def _running_lifespan(app):
    startup, shutdown_req, shutdown_done = (asyncio.Event() for _ in range(3))

    async def receive():
        if not startup.is_set():
            return {"type": "lifespan.startup"}
        await shutdown_req.wait()
        return {"type": "lifespan.shutdown"}

    async def send(message):
        if message["type"] == "lifespan.startup.complete":
            startup.set()
        elif message["type"] == "lifespan.shutdown.complete":
            shutdown_done.set()

    task = asyncio.ensure_future(app({"type": "lifespan"}, receive, send))
    await startup.wait()
    try:
        yield
    finally:
        shutdown_req.set()
        await asyncio.wait_for(shutdown_done.wait(), timeout=5)
        await task


async def _initialize(client, token):
    return await client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "method": "initialize",
            "id": 1,
            "params": {
                "protocolVersion": "2026-06-18",
                "capabilities": {},
                "clientInfo": {"name": "probe", "version": "0"},
            },
        },
        headers={
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )


@pytest.mark.asyncio
async def test_instructions_reach_initialize_over_real_asgi(monkeypatch):
    """End to end: no client request for it, no tool call — the instructions
    ride along in the initialize response, unasked."""
    monkeypatch.setattr(
        instructions_module,
        "build_instructions",
        lambda session, **kw: f"INSTRUCTIONS FOR {session.email}",
    )
    import gateway.app as app_module

    monkeypatch.setattr(app_module, "build_instructions", lambda s, **kw: f"INSTRUCTIONS FOR {s.email}")

    app = build_asgi_app(
        secret=SECRET,
        auth_service=_Auth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _initialize(client, issue_token("staff@example.com", SECRET))

    assert response.status_code == 200
    assert "INSTRUCTIONS FOR staff@example.com" in response.text


@pytest.mark.asyncio
async def test_instructions_are_scoped_to_the_calling_user(monkeypatch):
    """The load-bearing assumption of the whole design.

    create_initialization_options() runs inside a task spawned from a task
    group built in the lifespan context. If a ContextVar set per request did
    not propagate there, this would either return nothing or — far worse —
    return whichever caller happened to set it last, handing one user another
    user's org-scoped context.
    """
    import gateway.app as app_module

    monkeypatch.setattr(app_module, "build_instructions", lambda s, **kw: f"CTX::{s.email}")

    app = build_asgi_app(
        secret=SECRET,
        auth_service=_Auth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            first, second = await asyncio.gather(
                _initialize(client, issue_token("staff@example.com", SECRET)),
                _initialize(client, issue_token("customer@example.com", SECRET)),
            )

    assert "CTX::staff@example.com" in first.text
    assert "CTX::customer@example.com" in second.text
    assert "customer@example.com" not in first.text.replace("CTX::staff@example.com", "")


@pytest.mark.asyncio
async def test_initialize_still_succeeds_when_instructions_cannot_be_built(monkeypatch):
    """A prompt-store outage must cost the client its advisory context and
    nothing else — the connection, and every tool on it, keeps working."""
    import gateway.app as app_module

    def boom(session, **kw):
        raise RuntimeError("prompt store down")

    monkeypatch.setattr(app_module, "build_instructions", boom)

    app = build_asgi_app(
        secret=SECRET,
        auth_service=_Auth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
    )

    async with _running_lifespan(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await _initialize(client, issue_token("staff@example.com", SECRET))

    assert response.status_code == 200
    assert '"serverInfo"' in response.text
