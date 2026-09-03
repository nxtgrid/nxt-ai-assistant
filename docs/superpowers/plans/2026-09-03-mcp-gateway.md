# MCP Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing ~107 MCP tools to external MCP clients (Claude, Codex) under per-user Google identity, re-creating the orchestrator's argument-injection trust boundary inside a new gateway server so the 17 existing MCP servers need zero edits.

**Architecture:** One new MCP server (`mcp_servers/gateway/`) resolves a caller's email to `UserPermissions` via the existing transport-neutral `AuthService.get_user_permissions(email)`, then applies a ~12-entry scope guard to every tool call before delegating to the unchanged `server_registry.call_tool`. Arguments are classified INJECT (overwritten from session), VALIDATE (resolved against the caller's own grid set) or DELEGATE (left to servers that filter by org). A three-tier server allowlist gates the servers that carry no internal scoping.

**Tech Stack:** Python 3.11, `mcp>=1.27,<2` (low-level `Server` API), asyncpg, PyJWT, rapidfuzz, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-03-mcp-gateway-design.md`

---

## Before you start

**Work in a dedicated worktree** branched fresh from `origin/main`:

```bash
git worktree add -b feat/mcp-gateway .worktrees/mcp-gateway origin/main
```

Branching from another feature branch's tip inflates the eventual PR diff with
that branch's whole changeset — always branch from `origin/main`.

**Environment.** `mcp_servers` needs its own Python 3.11 venv, separate from
any venv another project in this repo (chat_orchestrator, etc.) already has —
reusing one of those can silently drop whole servers when deps are missing.
Create it at this worktree's root as exactly `.venv`: the repo's
`check_test_wiring.py` pre-commit hook only excludes literal `.venv/**` from
its test-file scan, so any other name (`.venv-mcp`, `venv`, ...) makes every
`test_*.py` inside your installed dependencies' site-packages show up as an
"untracked test file" and fail the hook:

```bash
python3.11 -m venv .venv && .venv/bin/pip install -r mcp_servers/requirements.txt pytest pytest-asyncio
```

**Three repo gotchas that will bite you:**

1. **New test files under any `tests/` directory need `git add -f`.** The repo's
   `.gitignore` denies `tests/` by default. A plain `git add` on a new test file
   is a *silent no-op* — the commit succeeds, the file never reaches the remote,
   and CI never runs it. Same applies to `docs/superpowers/plans/` and
   `docs/superpowers/specs/`.
2. **Async tests must carry `@pytest.mark.asyncio` explicitly.** A test under a
   `tests/` dir can resolve the repo-root `pyproject.toml` (which sets no
   `asyncio_mode`) instead of the project's, and silently not run.
3. **Run `pre-commit run --all-files` before claiming anything is done.**
   `pytest` and `ruff check .` both skip gitignored files, so an un-force-added
   test file gets zero linting and zero CI coverage while looking green locally.

**Never trust a docstring's symbol name in this repo.** Five docstrings
reference `user_permissions.filter_tools_for_user`, which does not exist. Verify
against the definition before calling anything.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `mcp_servers/gateway/__init__.py` | package marker |
| `mcp_servers/gateway/session.py` | `GatewaySession`, email → session, fail-closed |
| `mcp_servers/gateway/tiers.py` | server tier allowlist |
| `mcp_servers/gateway/scope_guard.py` | the ~12-entry argument guard |
| `mcp_servers/gateway/catalog.py` | tool listing + gate checks shared by list and call |
| `mcp_servers/gateway/tokens.py` | bearer token issue/verify |
| `mcp_servers/gateway/signin.py` | Google-verified email → bearer token |
| `mcp_servers/gateway/server.py` | MCP server entrypoint wiring the above |

**Test:** `mcp_servers/tests/gateway/test_{session,tiers,scope_guard,catalog,tokens,server,signin}.py`

---

## Task 1: Gateway session with fail-closed denial

**Files:**
- Create: `mcp_servers/gateway/__init__.py`, `mcp_servers/gateway/session.py`
- Test: `mcp_servers/tests/gateway/test_session.py`

- [ ] **Step 1: Write the failing test**

Create `mcp_servers/tests/gateway/__init__.py` (empty) and
`mcp_servers/tests/gateway/test_session.py`:

```python
"""Gateway session resolution.

The fail-closed case is the important one: AuthService returns empty
organization_ids rather than raising when an email is absent from
public.accounts, so a permissive gateway would forward organization_id=None
to servers that never filter by org.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest

from gateway.session import GatewaySession, SessionDenied, resolve_session


class _FakeAuth:
    def __init__(self, permissions, grid_names=None):
        self._permissions = permissions
        self._grid_names = grid_names or []
        self.grid_call = None

    async def get_user_permissions(self, email, user_id=None):
        return self._permissions

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        self.grid_call = (organization_id, include_all)
        return list(self._grid_names)


class _Perms:
    def __init__(self, organization_ids, is_staff=False, user_id="u1", email="a@example.com"):
        self.organization_ids = organization_ids
        self.is_staff = is_staff
        self.user_id = user_id
        self.email = email
        self.organization_short_name = "testorg"


@pytest.mark.asyncio
async def test_resolve_session_builds_allowed_grid_set():
    auth = _FakeAuth(_Perms(["4"]), grid_names=["Alpha Site", "Beta Site"])

    session = await resolve_session("a@example.com", auth)

    assert isinstance(session, GatewaySession)
    assert session.organization_id == "4"
    assert session.grid_names == frozenset({"Alpha Site", "Beta Site"})
    assert session.is_staff is False
    assert auth.grid_call == ("4", False)


@pytest.mark.asyncio
async def test_staff_session_requests_all_grids():
    auth = _FakeAuth(_Perms(["1"], is_staff=True), grid_names=["Alpha Site"])

    session = await resolve_session("staff@example.com", auth)

    assert session.is_staff is True
    assert auth.grid_call == ("1", True)


@pytest.mark.asyncio
async def test_unknown_email_is_denied_not_unscoped():
    auth = _FakeAuth(_Perms([]))

    with pytest.raises(SessionDenied):
        await resolve_session("stranger@example.com", auth)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_session.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/__init__.py`:

```python
"""Per-user MCP gateway over the existing server registry."""
```

Create `mcp_servers/gateway/session.py`:

```python
"""Resolve an authenticated email into a scoped gateway session.

AuthService.get_user_permissions is transport-neutral — the Telegram-specific
entry points (resolve_permissions_from_chat, get_org_id_for_telegram_user) all
funnel into it after resolving an email. That makes email the reuse seam for a
non-Telegram transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet


class SessionDenied(Exception):
    """The caller could not be granted a scoped session."""


@dataclass(frozen=True)
class GatewaySession:
    """One authenticated caller's resolved scope."""

    email: str
    user_id: str
    organization_id: str
    organization_short_name: str | None
    grid_names: FrozenSet[str]
    is_staff: bool


async def resolve_session(email: str, auth_service) -> GatewaySession:
    """Build a GatewaySession, or raise SessionDenied.

    Fails closed on empty organization_ids: AuthService returns that (rather
    than raising) for an email with no row in public.accounts, and forwarding it
    would send organization_id=None to servers that never filter by org.
    """
    permissions = await auth_service.get_user_permissions(email)

    if not permissions.organization_ids:
        raise SessionDenied(f"No organization resolved for {email}")

    organization_id = str(permissions.organization_ids[0])
    is_staff = bool(permissions.is_staff)

    grid_names = await auth_service.get_grid_names_for_organization(
        organization_id, include_all=is_staff
    )

    return GatewaySession(
        email=email,
        user_id=str(permissions.user_id),
        organization_id=organization_id,
        organization_short_name=getattr(permissions, "organization_short_name", None),
        grid_names=frozenset(grid_names),
        is_staff=is_staff,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_session.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/__init__.py mcp_servers/tests/gateway/test_session.py
git add mcp_servers/gateway/__init__.py mcp_servers/gateway/session.py
git commit -m "feat(gateway): resolve email to a scoped session, failing closed"
```

---

## Task 2: Server tier allowlist

**Files:**
- Create: `mcp_servers/gateway/tiers.py`
- Test: `mcp_servers/tests/gateway/test_tiers.py`

- [ ] **Step 1: Write the failing test**

```python
"""Server tiering.

Tier 3 is denied because those servers are side-effecting AND carry no
organization_id handling of their own, so Class A injection enforces nothing
for them.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.tiers import TIER_1, TIER_2, TIER_3_DENIED, is_server_allowed


def test_org_aware_servers_are_tier_1():
    assert "customer" in TIER_1
    assert "meters" in TIER_1
    assert is_server_allowed("customer") is True


def test_grid_shaped_servers_are_tier_2():
    assert "vrm" in TIER_2
    assert "grafana" in TIER_2
    assert is_server_allowed("vrm") is True


def test_side_effecting_unscoped_servers_are_denied():
    assert "equipment_control" in TIER_3_DENIED
    assert "payment_processor" in TIER_3_DENIED
    assert "messaging" in TIER_3_DENIED
    assert is_server_allowed("equipment_control") is False


def test_unknown_server_is_denied():
    assert is_server_allowed("some_new_server") is False


def test_tiers_do_not_overlap():
    assert not (TIER_1 & TIER_2)
    assert not (TIER_1 & TIER_3_DENIED)
    assert not (TIER_2 & TIER_3_DENIED)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_tiers.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.tiers'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/tiers.py`:

```python
"""Which servers the gateway will expose, and why.

Tier 1  consume organization_id internally, so Class A injection genuinely
        scopes their queries.
Tier 2  carry no internal org filtering, but every scope-bearing argument they
        take is grid-shaped, so Class B validation covers them.
Tier 3  side-effecting AND unscoped. Denied in v1; exposing them needs real
        per-server tenant isolation, which does not exist yet.

Deny-by-default: a server absent from every tier is not exposed, so a newly
added server cannot leak in without an explicit decision here.
"""

from __future__ import annotations

from typing import FrozenSet

TIER_1: FrozenSet[str] = frozenset(
    {
        "customer",
        "equipment_diagnostics",
        "grid_design",
        "jira",
        "knowledge",
        "meta",
        "meters",
        "schedule",
    }
)

TIER_2: FrozenSet[str] = frozenset(
    {
        "codebase",
        "grafana",
        "logs",
        "reference",
        "solar",
        "vrm",
    }
)

TIER_3_DENIED: FrozenSet[str] = frozenset(
    {
        "equipment_control",
        "payment_processor",
        "messaging",
    }
)

ALLOWED_SERVERS: FrozenSet[str] = TIER_1 | TIER_2


def is_server_allowed(server_name: str) -> bool:
    """Whether the gateway exposes this server at all."""
    return server_name in ALLOWED_SERVERS
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_tiers.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_tiers.py
git add mcp_servers/gateway/tiers.py
git commit -m "feat(gateway): deny-by-default server tier allowlist"
```

---

## Task 3: Scope guard — Class A injection

**Files:**
- Create: `mcp_servers/gateway/scope_guard.py`
- Test: `mcp_servers/tests/gateway/test_scope_guard.py`

- [ ] **Step 1: Write the failing test**

```python
"""Class A: identity arguments are injected, never accepted from the caller.

Mirrors tool_executor.py's spread-then-overwrite ordering, which is what makes
injection authoritative over anything the caller supplied.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest

from gateway.scope_guard import apply_scope_guard
from gateway.session import GatewaySession

SESSION = GatewaySession(
    email="user@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site", "Beta Site"}),
    is_staff=False,
)


def test_injects_identity_arguments():
    guarded = apply_scope_guard({"limit": 10}, SESSION)

    assert guarded["organization_id"] == 4
    assert guarded["user_email"] == "user@example.com"
    assert guarded["limit"] == 10


def test_caller_supplied_organization_id_is_overwritten():
    guarded = apply_scope_guard({"organization_id": 99}, SESSION)

    assert guarded["organization_id"] == 4


def test_caller_supplied_email_is_overwritten():
    guarded = apply_scope_guard({"user_email": "attacker@example.com"}, SESSION)

    assert guarded["user_email"] == "user@example.com"


def test_org_name_overwritten_only_when_tool_asked_for_it():
    assert "organization" not in apply_scope_guard({"limit": 1}, SESSION)

    guarded = apply_scope_guard({"organization": "someone else"}, SESSION)
    assert guarded["organization"] == "testorg"


def test_original_arguments_are_not_mutated():
    original = {"organization_id": 99}
    apply_scope_guard(original, SESSION)
    assert original == {"organization_id": 99}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_scope_guard.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.scope_guard'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/scope_guard.py`:

```python
"""Re-create the orchestrator's injection boundary for external MCP callers.

The tenancy surface across all 107 tools is ~12 argument names, so this is a
small table rather than a per-tool audit. Arguments fall into three classes:

  A INJECT    identity — overwritten from the session, caller value discarded
  B VALIDATE  grid references — resolved against the caller's own grid set
  C DELEGATE  meter/customer references — left to servers that filter by org

Class C is only safe for Tier 1 servers; see gateway/tiers.py.
"""

from __future__ import annotations

from typing import Any, Dict

from gateway.session import GatewaySession


class ScopeViolation(Exception):
    """The caller referenced an entity outside their permissions."""


# Class A — always injected, matching tool_executor.py's injected set.
ALWAYS_INJECTED = ("organization_id", "user_email", "user_name")

# Class A — overwritten only when the tool's schema actually uses them, so we
# do not add stray keys that a server might branch on.
INJECTED_IF_PRESENT = ("organization", "organization_name")


def apply_scope_guard(arguments: Dict[str, Any], session: GatewaySession) -> Dict[str, Any]:
    """Return a copy of ``arguments`` with caller-controlled scope removed.

    Spread first, overwrite second — the injected values must win.
    """
    guarded: Dict[str, Any] = {
        **arguments,
        "organization_id": int(session.organization_id),
        "user_email": session.email,
        "user_name": session.email,
    }

    for key in INJECTED_IF_PRESENT:
        if key in arguments:
            guarded[key] = session.organization_short_name

    return guarded
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_scope_guard.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_scope_guard.py
git add mcp_servers/gateway/scope_guard.py
git commit -m "feat(gateway): inject identity arguments over caller-supplied values"
```

---

## Task 4: Scope guard — Class B grid validation

Class B is what makes Tier 2 safe. Without it those servers accept any grid
name from any caller.

**Files:**
- Modify: `mcp_servers/gateway/scope_guard.py`
- Test: `mcp_servers/tests/gateway/test_scope_guard.py`

- [ ] **Step 1: Write the failing test**

Append to `mcp_servers/tests/gateway/test_scope_guard.py`:

```python
def test_exact_grid_name_passes_through():
    guarded = apply_scope_guard({"grid_name": "Alpha Site"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_case_insensitive_grid_name_is_canonicalised():
    guarded = apply_scope_guard({"grid_name": "alpha site"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_near_miss_resolves_within_allowed_set_only():
    # A typo resolves to the caller's own grid, and the CANONICAL name is
    # forwarded — downstream fuzzy matching must never see the raw string.
    guarded = apply_scope_guard({"grid_name": "Alpha Sight"}, SESSION)
    assert guarded["grid_name"] == "Alpha Site"


def test_grid_outside_permissions_is_rejected():
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_name": "Gamma Site"}, SESSION)


def test_grid_list_argument_is_validated_elementwise():
    guarded = apply_scope_guard({"grid_names": ["Alpha Site", "beta site"]}, SESSION)
    assert guarded["grid_names"] == ["Alpha Site", "Beta Site"]

    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_names": ["Alpha Site", "Gamma Site"]}, SESSION)


def test_short_alias_grid_argument_is_validated():
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid": "Gamma Site"}, SESSION)


def test_session_with_no_grids_rejects_any_grid_reference():
    empty = GatewaySession(
        email="user@example.com",
        user_id="u1",
        organization_id="4",
        organization_short_name="testorg",
        grid_names=frozenset(),
        is_staff=False,
    )
    with pytest.raises(ScopeViolation):
        apply_scope_guard({"grid_name": "Alpha Site"}, empty)
```

Add the import at the top of the file:

```python
from gateway.scope_guard import ScopeViolation, apply_scope_guard
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_scope_guard.py -v
```

Expected: FAIL — grid arguments pass through unvalidated; `ScopeViolation` not raised

- [ ] **Step 3: Write minimal implementation**

Add to `mcp_servers/gateway/scope_guard.py`, after `INJECTED_IF_PRESENT`:

```python
# Class B — caller supplies, gateway validates against their own grid set.
GRID_SCALAR_ARGS = ("grid_name", "grid")
GRID_LIST_ARGS = ("grid_names",)

# Minimum rapidfuzz score to accept a near-miss within the allowed set.
_FUZZY_CUTOFF = 85


def _resolve_grid(value: str, session: GatewaySession) -> str:
    """Resolve a caller's grid string to a canonical name they may access.

    Fuzzy matching happens HERE, against the allowed set only. AuthService's
    get_grid_portal_id fuzzy-matches downstream against ALL grids, so a raw
    caller string must never reach it — a near-miss could otherwise land on
    another organization's grid.
    """
    if not isinstance(value, str):
        raise ScopeViolation(f"Grid reference must be a string, got {type(value).__name__}")

    allowed = session.grid_names
    if not allowed:
        raise ScopeViolation("Session has no accessible grids")

    if value in allowed:
        return value

    lowered = {name.lower(): name for name in allowed}
    if value.lower() in lowered:
        return lowered[value.lower()]

    from rapidfuzz import fuzz, process

    match = process.extractOne(
        value, list(allowed), scorer=fuzz.WRatio, score_cutoff=_FUZZY_CUTOFF
    )
    if match:
        return match[0]

    raise ScopeViolation(f"Grid not accessible to this user: {value!r}")
```

Then insert this block into `apply_scope_guard`, immediately before the
`return guarded` line:

```python
    for key in GRID_SCALAR_ARGS:
        if key in arguments and arguments[key] is not None:
            guarded[key] = _resolve_grid(arguments[key], session)

    for key in GRID_LIST_ARGS:
        if key in arguments and arguments[key] is not None:
            values = arguments[key]
            if not isinstance(values, (list, tuple)):
                raise ScopeViolation(f"{key} must be a list")
            guarded[key] = [_resolve_grid(v, session) for v in values]
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_scope_guard.py -v
```

Expected: 12 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_scope_guard.py
git add mcp_servers/gateway/scope_guard.py
git commit -m "feat(gateway): validate grid arguments against the caller's own grids"
```

---

## Task 5: Tool catalog with call-time gate parity

Hiding a tool from `list_tools` does **not** make it unreachable —
`_filter_and_convert_tools`'s own docstring says `internal_only` tools "remain
callable via server_registry.call_tool". One predicate serves both paths so
they cannot drift.

**Files:**
- Create: `mcp_servers/gateway/catalog.py`
- Test: `mcp_servers/tests/gateway/test_catalog.py`

- [ ] **Step 1: Write the failing test**

```python
"""Tool visibility, and the same gate enforced at call time."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest

from gateway.catalog import ToolDenied, assert_tool_callable, is_tool_exposed
from gateway.session import GatewaySession

CUSTOMER = GatewaySession(
    email="c@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=False,
)
STAFF = GatewaySession(
    email="s@example.com",
    user_id="u2",
    organization_id="1",
    organization_short_name="staff",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=True,
)

PUBLIC = {"name": "get_status", "visible_to_customer": True}
STAFF_ONLY = {"name": "get_internals", "visible_to_customer": False}
INTERNAL = {"name": "sync_cache", "visible_to_customer": True, "internal_only": True}
PERSISTENT = {"name": "watch_loop", "visible_to_customer": True, "persistent_only": True}


def test_customer_sees_only_customer_visible_tools():
    assert is_tool_exposed("customer", PUBLIC, CUSTOMER) is True
    assert is_tool_exposed("customer", STAFF_ONLY, CUSTOMER) is False


def test_staff_sees_staff_only_tools():
    assert is_tool_exposed("customer", STAFF_ONLY, STAFF) is True


def test_internal_and_persistent_tools_are_never_exposed():
    assert is_tool_exposed("customer", INTERNAL, STAFF) is False
    assert is_tool_exposed("customer", PERSISTENT, STAFF) is False


def test_tier_3_server_tools_are_never_exposed():
    assert is_tool_exposed("equipment_control", PUBLIC, STAFF) is False


def test_call_time_rejects_what_listing_hid():
    # The whole point: internal_only stays callable through server_registry,
    # so the gate must be re-checked here, not only at list time.
    with pytest.raises(ToolDenied):
        assert_tool_callable("customer", INTERNAL, STAFF)

    with pytest.raises(ToolDenied):
        assert_tool_callable("equipment_control", PUBLIC, STAFF)

    with pytest.raises(ToolDenied):
        assert_tool_callable("customer", STAFF_ONLY, CUSTOMER)


def test_call_time_allows_an_exposed_tool():
    assert_tool_callable("customer", PUBLIC, CUSTOMER)


def test_disabled_server_is_not_exposed(monkeypatch):
    monkeypatch.setenv("CUSTOMER_ENABLED", "false")
    assert is_tool_exposed("customer", PUBLIC, STAFF) is False


def test_disabled_tool_is_not_exposed(monkeypatch):
    # ActionFlags caches the parsed JSON but keys the cache on the raw env
    # string, so changing it here invalidates the cache automatically.
    monkeypatch.setenv("MCP_DISABLED_TOOLS", '["customer:get_status"]')
    assert is_tool_exposed("customer", PUBLIC, STAFF) is False
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_catalog.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.catalog'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/catalog.py`:

```python
"""Which tools a session may see, and may call.

is_tool_exposed is the single predicate; assert_tool_callable re-runs it at
call time. They must never diverge: server_registry.call_tool has no gate of
its own, so anything not re-checked here is reachable by name.
"""

from __future__ import annotations

from typing import Any, Dict, List

from gateway.session import GatewaySession
from gateway.tiers import is_server_allowed
from shared_code.config.action_flags import ActionFlags


class ToolDenied(Exception):
    """The session may not call this tool."""


def is_tool_exposed(server_name: str, tool: Dict[str, Any], session: GatewaySession) -> bool:
    """Whether ``tool`` is available to ``session``."""
    if not is_server_allowed(server_name):
        return False

    if tool.get("internal_only", False) or tool.get("persistent_only", False):
        return False

    # Same operator kill-switches the orchestrator honours, so disabling a
    # server or tool takes effect on this transport too.
    if not ActionFlags.is_server_enabled(server_name):
        return False

    if ActionFlags.is_tool_disabled(server_name, tool.get("name", "")):
        return False

    if not session.is_staff and not tool.get("visible_to_customer", False):
        return False

    return True


def assert_tool_callable(
    server_name: str, tool: Dict[str, Any], session: GatewaySession
) -> None:
    """Raise ToolDenied unless ``session`` may call ``tool``."""
    if not is_tool_exposed(server_name, tool, session):
        raise ToolDenied(f"{server_name}.{tool.get('name', '?')} is not available to this user")


def list_exposed_tools(
    tools_by_server: Dict[str, List[Dict[str, Any]]], session: GatewaySession
) -> List[Dict[str, Any]]:
    """Flatten to MCP tool definitions this session may see.

    Names are namespaced ``{server}__{tool}`` so the gateway can route a call
    back to its server without a second lookup.
    """
    exposed: List[Dict[str, Any]] = []

    for server_name, server_tools in (tools_by_server or {}).items():
        for tool in server_tools or []:
            if not is_tool_exposed(server_name, tool, session):
                continue
            exposed.append(
                {
                    "name": f"{server_name}__{tool['name']}",
                    "description": tool.get("description", ""),
                    "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                }
            )

    return exposed
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_catalog.py -v
```

Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_catalog.py
git add mcp_servers/gateway/catalog.py
git commit -m "feat(gateway): one gate predicate for tool listing and calling"
```

---

## Task 6: Bearer tokens

**Files:**
- Create: `mcp_servers/gateway/tokens.py`
- Test: `mcp_servers/tests/gateway/test_tokens.py`

- [ ] **Step 1: Write the failing test**

```python
"""Bearer tokens carrying a Google-verified email."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import time

import pytest

from gateway.tokens import TokenInvalid, issue_token, verify_token

SECRET = "test-secret-not-a-real-key"


def test_round_trip_returns_the_email():
    token = issue_token("user@example.com", SECRET)
    assert verify_token(token, SECRET) == "user@example.com"


def test_token_signed_with_another_secret_is_rejected():
    token = issue_token("user@example.com", SECRET)
    with pytest.raises(TokenInvalid):
        verify_token(token, "different-secret")


def test_expired_token_is_rejected():
    token = issue_token("user@example.com", SECRET, issued_at=time.time() - 100_000, ttl_seconds=60)
    with pytest.raises(TokenInvalid):
        verify_token(token, SECRET)


def test_garbage_is_rejected():
    with pytest.raises(TokenInvalid):
        verify_token("not-a-token", SECRET)


def test_token_without_email_claim_is_rejected():
    import jwt

    token = jwt.encode({"exp": time.time() + 600}, SECRET, algorithm="HS256")
    with pytest.raises(TokenInvalid):
        verify_token(token, SECRET)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_tokens.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.tokens'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/tokens.py`:

```python
"""Bearer tokens for MCP clients.

The user signs in with Google (reusing anansi_app's existing OAuth client), and
the gateway issues a token they paste into their MCP client config. This buys
the same identity guarantee as full remote-MCP OAuth without standing up an
authorization server that federates to Google — see the spec's Authentication
section for why that is deferred.

The token asserts only a verified email. All authorization is re-resolved from
the database per session, so revoking access in public.accounts takes effect
without reissuing tokens.
"""

from __future__ import annotations

import time
from typing import Optional

import jwt

_ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class TokenInvalid(Exception):
    """The presented token was missing, malformed, expired or missigned."""


def issue_token(
    email: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Mint a bearer token asserting a Google-verified email."""
    now = time.time() if issued_at is None else issued_at
    return jwt.encode(
        {"email": email, "iat": int(now), "exp": int(now + ttl_seconds)},
        secret,
        algorithm=_ALGORITHM,
    )


def verify_token(token: str, secret: str) -> str:
    """Return the email a valid token asserts, else raise TokenInvalid."""
    try:
        claims = jwt.decode(token, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise TokenInvalid(str(exc)) from exc

    email = claims.get("email")
    if not email:
        raise TokenInvalid("Token carries no email claim")

    return str(email)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_tokens.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_tokens.py
git add mcp_servers/gateway/tokens.py
git commit -m "feat(gateway): bearer tokens asserting a Google-verified email"
```

---

## Task 7: Wire the MCP server

**Files:**
- Create: `mcp_servers/gateway/server.py`
- Test: `mcp_servers/tests/gateway/test_server.py`

- [ ] **Step 1: Write the failing test**

```python
"""End-to-end dispatch: guard applied, then delegate to the registry."""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest

from gateway.catalog import ToolDenied
from gateway.scope_guard import ScopeViolation
from gateway.server import dispatch_tool_call
from gateway.session import GatewaySession

SESSION = GatewaySession(
    email="user@example.com",
    user_id="u1",
    organization_id="4",
    organization_short_name="testorg",
    grid_names=frozenset({"Alpha Site"}),
    is_staff=False,
)

TOOLS = {
    "customer": [{"name": "get_status", "visible_to_customer": True}],
    "equipment_control": [{"name": "restart_inverter", "visible_to_customer": True}],
}


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, server_name, tool_name, arguments):
        self.calls.append((server_name, tool_name, arguments))
        return {"success": True}


@pytest.mark.asyncio
async def test_guarded_arguments_reach_the_registry():
    registry = _Recorder()

    await dispatch_tool_call(
        "customer__get_status",
        {"grid_name": "alpha site", "organization_id": 99},
        SESSION,
        TOOLS,
        registry,
    )

    server_name, tool_name, arguments = registry.calls[0]
    assert (server_name, tool_name) == ("customer", "get_status")
    assert arguments["organization_id"] == 4          # caller's 99 discarded
    assert arguments["grid_name"] == "Alpha Site"     # canonicalised
    assert arguments["user_email"] == "user@example.com"


@pytest.mark.asyncio
async def test_tier_3_tool_is_refused_before_the_registry():
    registry = _Recorder()

    with pytest.raises(ToolDenied):
        await dispatch_tool_call(
            "equipment_control__restart_inverter", {}, SESSION, TOOLS, registry
        )

    assert registry.calls == []


@pytest.mark.asyncio
async def test_out_of_scope_grid_is_refused_before_the_registry():
    registry = _Recorder()

    with pytest.raises(ScopeViolation):
        await dispatch_tool_call(
            "customer__get_status", {"grid_name": "Gamma Site"}, SESSION, TOOLS, registry
        )

    assert registry.calls == []


@pytest.mark.asyncio
async def test_unknown_tool_is_refused():
    registry = _Recorder()

    with pytest.raises(ToolDenied):
        await dispatch_tool_call("customer__nope", {}, SESSION, TOOLS, registry)

    assert registry.calls == []
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_server.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.server'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/server.py`:

```python
"""Gateway dispatch.

Order matters: resolve the tool, re-check the gate, apply the scope guard, and
only then delegate. Every refusal must happen before server_registry.call_tool,
which has no gate of its own.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, List

from gateway.catalog import ToolDenied, assert_tool_callable
from gateway.scope_guard import apply_scope_guard
from gateway.session import GatewaySession

RegistryCall = Callable[[str, str, Dict[str, Any]], Awaitable[Dict[str, Any]]]


def _split_tool_name(namespaced: str) -> tuple[str, str]:
    server_name, separator, tool_name = namespaced.partition("__")
    if not separator or not tool_name:
        raise ToolDenied(f"Malformed tool name: {namespaced!r}")
    return server_name, tool_name


def _find_tool(
    tools_by_server: Dict[str, List[Dict[str, Any]]], server_name: str, tool_name: str
) -> Dict[str, Any]:
    for tool in tools_by_server.get(server_name) or []:
        if tool.get("name") == tool_name:
            return tool
    raise ToolDenied(f"Unknown tool: {server_name}.{tool_name}")


async def dispatch_tool_call(
    namespaced_name: str,
    arguments: Dict[str, Any],
    session: GatewaySession,
    tools_by_server: Dict[str, List[Dict[str, Any]]],
    registry_call: RegistryCall,
) -> Dict[str, Any]:
    """Gate, guard, then delegate one tool call."""
    server_name, tool_name = _split_tool_name(namespaced_name)
    tool = _find_tool(tools_by_server, server_name, tool_name)

    assert_tool_callable(server_name, tool, session)
    guarded = apply_scope_guard(arguments or {}, session)

    return await registry_call(server_name, tool_name, guarded)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_server.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_server.py
git add mcp_servers/gateway/server.py
git commit -m "feat(gateway): gate, guard and delegate one tool call"
```

---

## Task 8: Google sign-in endpoint that mints a token

Without this there is no way for a user to obtain a token, so the gateway is
unreachable. Reuses the OAuth client already registered for the admin app.

**Files:**
- Create: `mcp_servers/gateway/signin.py`
- Test: `mcp_servers/tests/gateway/test_signin.py`

- [ ] **Step 1: Write the failing test**

```python
"""Turning a Google-verified email into a gateway bearer token.

Two gates, both required: the shared RBAC whitelist (may this person log in at
all) and session resolution (do they map to an organization). The second is not
redundant — AuthService returns empty organization_ids rather than raising for
an email with no accounts row.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest

from gateway.signin import SignInRejected, mint_token_for_email
from gateway.tokens import verify_token

SECRET = "test-secret-not-a-real-key"


class _Auth:
    def __init__(self, organization_ids):
        self._organization_ids = organization_ids

    async def get_user_permissions(self, email, user_id=None):
        class _P:
            organization_ids = self._organization_ids
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _P()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return ["Alpha Site"]


@pytest.mark.asyncio
async def test_authorized_user_receives_a_usable_token():
    token = await mint_token_for_email(
        "user@example.com", SECRET, _Auth(["4"]), is_authorized=lambda e: True
    )
    assert verify_token(token, SECRET) == "user@example.com"


@pytest.mark.asyncio
async def test_user_outside_the_whitelist_is_rejected():
    with pytest.raises(SignInRejected):
        await mint_token_for_email(
            "stranger@example.com", SECRET, _Auth(["4"]), is_authorized=lambda e: False
        )


@pytest.mark.asyncio
async def test_whitelisted_user_with_no_organization_is_rejected():
    with pytest.raises(SignInRejected):
        await mint_token_for_email(
            "ghost@example.com", SECRET, _Auth([]), is_authorized=lambda e: True
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_signin.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.signin'`

- [ ] **Step 3: Write minimal implementation**

Create `mcp_servers/gateway/signin.py`:

```python
"""Exchange a Google-verified email for a gateway bearer token.

The OAuth dance itself is anansi_app/nicegui_app/auth.py's, unchanged — same
GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET and the same /oauth2callback path, so
the existing Google OAuth client registration keeps working. This module owns
only what happens once an email is verified.

is_authorized is REQUIRED, not defaulted. grid_app.lib.perms — the shared RBAC
whitelist this should delegate to — lives under anansi_app/, a sibling project
tree mcp_servers' own sys.path cannot reach; a lazy cross-project import would
either fail at runtime or silently depend on how the process happened to be
launched. The HTTP layer that wires the real OAuth callback to this function
(a follow-on, not in this plan — see "Deferred") is what imports perms.is_authorized
and passes it in explicitly.
"""

from __future__ import annotations

from typing import Callable

from gateway.session import SessionDenied, resolve_session
from gateway.tokens import issue_token


class SignInRejected(Exception):
    """The verified email may not be issued a gateway token."""


async def mint_token_for_email(
    email: str,
    secret: str,
    auth_service,
    is_authorized: Callable[[str], bool],
) -> str:
    """Issue a bearer token, or raise SignInRejected.

    Rejecting here rather than at first tool call means a user finds out at
    sign-in time, instead of seeing an empty tool list with no explanation.
    """
    if not is_authorized(email):
        raise SignInRejected(f"{email} is not authorized for this application")

    try:
        await resolve_session(email, auth_service)
    except SessionDenied as exc:
        raise SignInRejected(f"{email} maps to no organization") from exc

    return issue_token(email, secret)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_signin.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_signin.py
git add mcp_servers/gateway/signin.py
git commit -m "feat(gateway): mint a bearer token from a Google-verified email"
```

---

## Task 9: Full suite and pre-commit

- [ ] **Step 1: Run the gateway suite**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/ -v
```

Expected: 40 passed

- [ ] **Step 2: Confirm nothing else regressed**

```bash
.venv/bin/python -m pytest mcp_servers/tests/ -q
```

Expected: no new failures versus `origin/main`

- [ ] **Step 3: Run pre-commit across the repo**

```bash
pre-commit run --all-files
```

If `test-wiring` reports untracked files under `tests/`, force-add each one and
re-run until clean. This is the hook that catches a silently-dropped test file.

- [ ] **Step 4: Verify the commits actually contain the tests**

```bash
git ls-tree -r HEAD --name-only | grep -c "mcp_servers/tests/gateway"
```

Expected: 8 — seven test files plus `__init__.py`. This checks what git actually
*tracks*, which is the real failure mode: a plain `git add` on a gitignored test
path succeeds silently, the commit looks fine, and CI never runs the suite.

- [ ] **Step 5: Commit any fixes**

```bash
git commit -am "chore(gateway): pre-commit fixes"
```

---

## Deferred — not in this plan

- **The HTTP sign-in route itself.** Task 8 covers only the pure function
  `mint_token_for_email`. Wiring an actual endpoint — reusing
  `anansi_app/nicegui_app/auth.py`'s OAuth callback, importing
  `grid_app.lib.perms.is_authorized`, and returning the token to the user —
  is a follow-on. `mint_token_for_email`'s `is_authorized` parameter is the
  seam that endpoint calls through.
- ~~**The actual MCP protocol transport.**~~ **Built** (post-plan, same
  branch): `gateway/transport.py` (per-request auth extraction + the two
  request flows, headers-in/dicts-out, fully unit-tested) and `gateway/app.py`
  (`build_asgi_app` factory wiring a real `mcp.server.Server` over Streamable
  HTTP via `StreamableHTTPSessionManager(stateless=True)`, verified over real
  ASGI with httpx's `ASGITransport`). `run_gateway()`'s production wiring
  (real `AuthService`, real `server_registry`) is the one piece left
  unverified by a unit test — everything upstream of it is DI'd and covered.
- **DO App Platform ingress for the gateway.** Confirmed by reading the live
  deployment config, not assumed: `mcp_servers` has **no existing ingress
  route today**. `.do/app.example.yaml`'s `services:` list has exactly two
  entries (`chat-orchestrator`, `anansi-app`); `bridge.py`'s FastAPI HTTP
  surface is dormant in production — `tool_executor.py`'s
  `DIRECT_REGISTRY_AVAILABLE` check means the direct-Python-import path wins
  whenever it's available, which it is (mcp_servers ships inside
  chat-orchestrator's own image). The only other mcp_servers deployment
  target is `project.yml`'s DO Functions package (`tools-service`,
  `handler.main`) — a stateless, one-shot invocation model, a poor fit for an
  MCP protocol server. **This means the gateway would be the first
  internet-facing route this part of the codebase has ever had**, with the
  bearer token as the sole gate — no `API_KEY` layer in front of it the way
  `bridge.py` has.

  When that follow-on work happens:
  1. New, dedicated `services:` entry (own Dockerfile, own container) — don't
     fold this into `chat-orchestrator`'s service. That process has no
     bearer-token auth model today; sharing its deployment lifecycle raises
     the chance of an ingress mistake bleeding one surface's auth into the
     other's.
  2. New, explicit `ingress: rules:` entry with its own path prefix,
     `preserve_path_prefix: true` (mandatory — DO strips the matched prefix
     by default; omitting this on just one rule broke every nested route
     under `/chat` in production once already, per the comment above that
     rule in `.do/app.example.yaml`), placed anywhere before the catch-all
     `prefix: /` → `anansi-app` rule (rules evaluate in order; the catch-all
     is already last).
  3. Verify the chosen prefix against the *live* deployed spec, not just this
     template — it can drift from what's actually running. `/chat`,
     `/mini-app`, `/api/mini-app`, `/webhook` are already taken.
  4. A dedicated secret (e.g. `MCP_GATEWAY_TOKEN_SECRET`), never reusing
     `API_KEY` or `IDENTITY_ASSERTION_KEY` — this secret can mint a token for
     *any* email (see the spec's security-review addendum), so it warrants
     its own rotation plan independent of every other secret's blast radius.
  5. Roll out gated: deploy with `is_authorized` wired to a hardcoded
     deny-all (or a 1-2 account allowlist) first; confirm the ingress rule
     actually reaches the new service rather than being swallowed by the
     catch-all (an unauthenticated health-check path is enough to prove
     routing); confirm tiering and the scope guard against a real staging
     session; only then open `is_authorized` to the real whitelist.
- **Tier 3 servers** (`equipment_control`, `payment_processor`, `messaging`) —
  each needs genuine per-server tenant isolation first.
- **Full remote-MCP OAuth**, replacing pasted bearer tokens. Re-check the
  current MCP authorization specification before scheduling; it has moved.
- **Safety parity** — `safety_check`, `check_escalation` and response
  verification are orchestrator nodes that direct tool calls bypass.
