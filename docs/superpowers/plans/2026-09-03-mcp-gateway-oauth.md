# MCP Gateway OAuth + Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the merged gateway (`mcp_servers/gateway/`, PR #177) addable to Claude/Codex as a real connector — deployed, reachable, and driven through an actual "click connect, sign in with Google, done" OAuth flow — rather than a manually-pasted bearer token.

**Architecture:** A minimal OAuth 2.1 authorization server (`gateway/oauth.py`) sits in front of the existing, unchanged tool-calling path. It has two redirect legs that must not be conflated: Claude Code's dynamic loopback redirect (accepted permissively, PKCE is the real security boundary) and the gateway's own single, stable, Google-registered callback (a normal server-to-server leg). Both the Google-leg correlation state and the gateway's own authorization code are self-contained, HMAC-signed values passed through query parameters — no server-side session storage needed for either, except a small single-use table to enforce that an authorization code can only be redeemed once.

**Tech Stack:** Python 3.11, `mcp==1.29.1`, `authlib` (reused from `anansi_app`'s existing pattern), Starlette, `PyJWT`, asyncpg, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-09-03-mcp-gateway-oauth-design.md`

---

## Before you start

**Confirm the unverified item first.** The spec flags that `developers.google.com` couldn't be fetched this session (three attempts, consistent backend error). Before Task 6, confirm directly in Google Cloud Console: can `anansi_app`'s existing `GOOGLE_CLIENT_ID` (a `Web application`-type client) simply get a second redirect URI added for the gateway's own callback, or does this need a separate client? If a separate client is genuinely required, note its `client_id`/`client_secret` as new env vars before Task 6 rather than discovering this mid-task.

**Re-check the spec version.** This plan was written against
`modelcontextprotocol.io/specification/2026-07-28/basic/authorization`. Load
`.../specification/latest/basic/authorization` before Task 6 and diff
against what's assumed here — the spec has already moved once during this
project's lifetime (`2025-06-18` → `2026-07-28`).

**Work in the same worktree** (`feat/mcp-gateway` already merged; branch fresh from `origin/main` for this phase):

```bash
git worktree add -b feat/mcp-gateway-oauth .worktrees/mcp-gateway-oauth origin/main
```

**Environment:** reuse the pattern from the merged gateway work — a dedicated
`.venv` (exactly that name; anything else breaks `check_test_wiring.py`'s
scan, see the original plan's "Before you start" for why) with
`mcp_servers/requirements.txt` plus `pytest pytest-asyncio`.

**The same `git add -f` trap applies to every new test file below.** It bit
this project once already, mid-session, on a file that looked identical to
every prior one. Verify with `git show HEAD:<path>` after every commit, not
`git status` or trusted terminal output — both have produced false
confidence in this project before.

---

## File Structure

**Create:**

| File | Responsibility |
|---|---|
| `mcp_servers/gateway/pkce.py` | PKCE challenge generation/verification (RFC 7636) |
| `mcp_servers/gateway/oauth_codes.py` | Signed correlation state + signed, single-use authorization codes |
| `mcp_servers/gateway/oauth_metadata.py` | RFC 9728 + RFC 8414 `.well-known` documents |
| `mcp_servers/gateway/oauth.py` | The three HTTP routes: `/oauth/authorize`, `/oauth/google-callback`, `/oauth/token` |
| `db/migrations/0032_oauth_code_single_use.sql` | Single-use enforcement table |

**Modify:**

| File | Change |
|---|---|
| `mcp_servers/gateway/app.py` | Mount the new routes; add `WWW-Authenticate` header to 401s |
| `.do/app.example.yaml` | New service + ingress rule (Task 10) |

**Test:** `mcp_servers/tests/gateway/test_{pkce,oauth_codes,oauth_metadata,oauth}.py`

---

## Task 1: PKCE challenge generation and verification

**Files:**
- Create: `mcp_servers/gateway/pkce.py`
- Test: `mcp_servers/tests/gateway/test_pkce.py`

- [ ] **Step 1: Write the failing test**

```python
"""PKCE (RFC 7636): the client proves it holds the same secret across the
authorize and token calls, which is what makes a public client's loopback
redirect safe without a client_secret.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.pkce import PkceInvalid, generate_verifier, verify_challenge, verifier_to_challenge


def test_challenge_derivation_matches_rfc7636_example():
    # RFC 7636 Appendix B's worked example.
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert verifier_to_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_generated_verifier_round_trips():
    verifier = generate_verifier()
    challenge = verifier_to_challenge(verifier)
    verify_challenge(verifier, challenge)  # must not raise


def test_wrong_verifier_is_rejected():
    challenge = verifier_to_challenge(generate_verifier())
    with pytest.raises(PkceInvalid):
        verify_challenge("wrong-verifier-entirely", challenge)


def test_generated_verifiers_are_not_reused():
    assert generate_verifier() != generate_verifier()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_pkce.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.pkce'`

- [ ] **Step 3: Write minimal implementation**

```python
"""PKCE (RFC 7636) — code_verifier / code_challenge generation and check.

Only S256 is supported. The spec allows "plain" but every real client
(including Claude Code) uses S256; supporting plain would just be an
unused, weaker code path to maintain.
"""

from __future__ import annotations

import base64
import hashlib
import secrets


class PkceInvalid(Exception):
    """The presented code_verifier does not match the stored challenge."""


def _b64url_no_pad(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_verifier() -> str:
    """A fresh, high-entropy verifier — used by the gateway itself for the
    server-to-server Google leg, where the gateway plays the client role.
    """
    return _b64url_no_pad(secrets.token_bytes(32))


def verifier_to_challenge(verifier: str) -> str:
    """S256: BASE64URL(SHA256(verifier)), no padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return _b64url_no_pad(digest)


def verify_challenge(verifier: str, challenge: str) -> None:
    """Raise PkceInvalid unless verifier hashes to challenge.

    Constant-time comparison — this is a security boundary, not just a
    correctness check.
    """
    computed = verifier_to_challenge(verifier)
    if not secrets.compare_digest(computed, challenge):
        raise PkceInvalid("code_verifier does not match code_challenge")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_pkce.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_pkce.py
git add mcp_servers/gateway/pkce.py
git commit -m "feat(gateway): PKCE challenge generation and verification"
```

---

## Task 2: Single-use table for the gateway's own authorization codes

The authorization code itself (Task 4) is a self-contained signed value —
no storage needed to *validate* it. Storage is needed for exactly one thing:
proving it hasn't been redeemed before, per OAuth 2.1's single-use
requirement. This is a genuine schema change, so per this repo's own
`db-migrations-need-manual-apply` lesson, merging it does **not** apply it —
someone with prod DB access must run it separately, and this task says so at
the point it matters (Task 9) rather than leaving it implicit.

**Files:**
- Create: `db/migrations/0032_oauth_code_single_use.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 0032: single-use enforcement for the MCP gateway's OAuth authorization
-- codes. The code itself is a self-contained, HMAC-signed value (see
-- gateway/oauth_codes.py) carrying everything needed to validate it without
-- a DB lookup -- this table exists purely to answer "has this exact code
-- already been redeemed", which a signature alone can never answer.
--
-- Row lifetime is minutes, not persistent state: a code that expired
-- (has already had its TTL checked at the signature-verification step)
-- never needs its row again. periodic cleanup can delete rows past
-- expires_at; nothing about correctness depends on that cleanup running.

CREATE TABLE IF NOT EXISTS mcp_gateway_oauth_codes (
    code_id text PRIMARY KEY,          -- the code's own embedded jti, not the code itself
    expires_at timestamptz NOT NULL,
    redeemed_at timestamptz            -- NULL until first (and only) redemption
);

CREATE INDEX IF NOT EXISTS mcp_gateway_oauth_codes_expires_at_idx
    ON mcp_gateway_oauth_codes (expires_at);
```

- [ ] **Step 2: Commit**

```bash
git add -f db/migrations/0032_oauth_code_single_use.sql
git commit -m "feat(gateway): migration for OAuth authorization-code single-use tracking"
```

(No test here — this is schema only. Task 4's tests exercise the table
through a fake, matching how the rest of the gateway keeps DB access
injectable rather than hitting real Postgres in unit tests.)

---

## Task 3: Signed correlation state for the Google leg

This is the piece that removes the need for server-side session storage
between `/oauth/authorize` and `/oauth/google-callback`: everything the
callback needs is encoded into the value passed as Google's own `state`
parameter, signed so it can't be tampered with in transit.

**Files:**
- Create: `mcp_servers/gateway/oauth_codes.py`
- Test: `mcp_servers/tests/gateway/test_oauth_codes.py`

- [ ] **Step 1: Write the failing test**

```python
"""Two self-contained signed values, both HMAC-signed JWTs so they need no
server-side storage to validate — only the authorization code additionally
needs a single-use check, which is a DB row, not the whole session.
"""

import sys
import time
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth_codes import (
    CorrelationState,
    CorrelationStateInvalid,
    IssuedCode,
    IssuedCodeInvalid,
    decode_correlation_state,
    decode_issued_code,
    encode_correlation_state,
    issue_authorization_code,
)

SECRET = "test-secret-not-a-real-key"


# --- correlation state (the Google-leg `state` parameter) ------------------


def test_correlation_state_round_trips():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="client-chosen-opaque-value",
        code_challenge="abc123",
        secret=SECRET,
    )
    decoded = decode_correlation_state(encoded, SECRET)
    assert decoded == CorrelationState(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="client-chosen-opaque-value",
        code_challenge="abc123",
    )


def test_correlation_state_tampering_is_rejected():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )
    with pytest.raises(CorrelationStateInvalid):
        decode_correlation_state(encoded, "different-secret")


def test_expired_correlation_state_is_rejected():
    encoded = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
        issued_at=time.time() - 10_000,
        ttl_seconds=60,
    )
    with pytest.raises(CorrelationStateInvalid):
        decode_correlation_state(encoded, SECRET)


# --- the gateway's own authorization code -----------------------------------


def test_issued_code_round_trips():
    issued = issue_authorization_code(
        email="user@example.com",
        code_challenge="abc123",
        secret=SECRET,
    )
    decoded = decode_issued_code(issued.code, SECRET)
    assert decoded.email == "user@example.com"
    assert decoded.code_challenge == "abc123"
    assert decoded.code_id == issued.code_id


def test_issued_code_carries_a_stable_code_id_for_single_use_tracking():
    issued = issue_authorization_code(email="a@example.com", code_challenge="c", secret=SECRET)
    decoded = decode_issued_code(issued.code, SECRET)
    assert decoded.code_id == issued.code_id
    assert len(issued.code_id) >= 16  # enough entropy to be a real PK, not a guessable counter


def test_issued_code_tampering_is_rejected():
    issued = issue_authorization_code(email="a@example.com", code_challenge="c", secret=SECRET)
    with pytest.raises(IssuedCodeInvalid):
        decode_issued_code(issued.code, "different-secret")


def test_expired_issued_code_is_rejected():
    issued = issue_authorization_code(
        email="a@example.com",
        code_challenge="c",
        secret=SECRET,
        issued_at=time.time() - 10_000,
        ttl_seconds=60,
    )
    with pytest.raises(IssuedCodeInvalid):
        decode_issued_code(issued.code, SECRET)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_codes.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.oauth_codes'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Two self-contained, HMAC-signed values that need no server-side storage
to validate on their own:

CorrelationState  passed as Google's own `state` parameter across the
                  /oauth/authorize -> Google -> /oauth/google-callback hop.
                  Carries everything the callback needs to resume the
                  client's original request.

IssuedCode        the gateway's own authorization code, returned to the
                  client's redirect_uri after the Google leg completes.
                  Single-use enforcement (db/migrations/0032) is the one
                  thing a signature alone can never provide - everything
                  else about validity (tamper-evidence, expiry, which
                  email, which code_challenge) is checked from the value
                  itself.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import jwt

_ALGORITHM = "HS256"
_CORRELATION_TTL_SECONDS = 600      # 10 minutes - covers a slow Google login
_CODE_TTL_SECONDS = 60              # short-lived, matching OAuth best practice


class CorrelationStateInvalid(Exception):
    """The state round-tripped through Google does not verify."""


class IssuedCodeInvalid(Exception):
    """The authorization code presented at /oauth/token does not verify."""


@dataclass(frozen=True)
class CorrelationState:
    client_redirect_uri: str
    client_state: str
    code_challenge: str


@dataclass(frozen=True)
class IssuedCode:
    code: str
    code_id: str


@dataclass(frozen=True)
class DecodedIssuedCode:
    email: str
    code_challenge: str
    code_id: str


def encode_correlation_state(
    *,
    client_redirect_uri: str,
    client_state: str,
    code_challenge: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = _CORRELATION_TTL_SECONDS,
) -> str:
    now = time.time() if issued_at is None else issued_at
    return jwt.encode(
        {
            "client_redirect_uri": client_redirect_uri,
            "client_state": client_state,
            "code_challenge": code_challenge,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
        },
        secret,
        algorithm=_ALGORITHM,
    )


def decode_correlation_state(encoded: str, secret: str) -> CorrelationState:
    try:
        claims = jwt.decode(encoded, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise CorrelationStateInvalid(str(exc)) from exc

    try:
        return CorrelationState(
            client_redirect_uri=claims["client_redirect_uri"],
            client_state=claims["client_state"],
            code_challenge=claims["code_challenge"],
        )
    except KeyError as exc:
        raise CorrelationStateInvalid(f"Missing claim: {exc}") from exc


def issue_authorization_code(
    *,
    email: str,
    code_challenge: str,
    secret: str,
    issued_at: Optional[float] = None,
    ttl_seconds: int = _CODE_TTL_SECONDS,
) -> IssuedCode:
    import secrets as _secrets

    now = time.time() if issued_at is None else issued_at
    code_id = _secrets.token_urlsafe(24)
    code = jwt.encode(
        {
            "email": email,
            "code_challenge": code_challenge,
            "code_id": code_id,
            "iat": int(now),
            "exp": int(now + ttl_seconds),
        },
        secret,
        algorithm=_ALGORITHM,
    )
    return IssuedCode(code=code, code_id=code_id)


def decode_issued_code(code: str, secret: str) -> DecodedIssuedCode:
    try:
        claims = jwt.decode(code, secret, algorithms=[_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise IssuedCodeInvalid(str(exc)) from exc

    try:
        return DecodedIssuedCode(
            email=claims["email"],
            code_challenge=claims["code_challenge"],
            code_id=claims["code_id"],
        )
    except KeyError as exc:
        raise IssuedCodeInvalid(f"Missing claim: {exc}") from exc
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_codes.py -v
```

Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_oauth_codes.py
git add mcp_servers/gateway/oauth_codes.py
git commit -m "feat(gateway): signed correlation state and authorization codes"
```

---

## Task 4: Single-use enforcement

The check the signed code itself can never do: has this exact code already
been redeemed. This is the only place `db/migrations/0032`'s table is
touched, and — matching every other gateway module — it's DI'd behind a
tiny interface so it's testable with a fake, never a real DB connection.

**Files:**
- Create: `mcp_servers/gateway/oauth_single_use.py`
- Test: `mcp_servers/tests/gateway/test_oauth_single_use.py`

- [ ] **Step 1: Write the failing test**

```python
"""Single-use enforcement for an issued authorization code's code_id.

A fake in-memory store stands in for db/migrations/0032's table - this
module never constructs a real DB connection itself, matching every other
piece of the gateway.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth_single_use import CodeAlreadyRedeemed, redeem_once


class _FakeStore:
    def __init__(self):
        self.redeemed: set[str] = set()

    async def try_redeem(self, code_id: str, expires_at) -> bool:
        """Mirrors an atomic UPDATE ... WHERE redeemed_at IS NULL RETURNING:
        True if this call claimed it, False if already claimed.
        """
        if code_id in self.redeemed:
            return False
        self.redeemed.add(code_id)
        return True


@pytest.mark.asyncio
async def test_first_redemption_succeeds():
    store = _FakeStore()
    await redeem_once("code-1", store)  # must not raise
    assert "code-1" in store.redeemed


@pytest.mark.asyncio
async def test_second_redemption_of_the_same_code_is_rejected():
    store = _FakeStore()
    await redeem_once("code-1", store)

    with pytest.raises(CodeAlreadyRedeemed):
        await redeem_once("code-1", store)


@pytest.mark.asyncio
async def test_different_codes_do_not_interfere():
    store = _FakeStore()
    await redeem_once("code-1", store)
    await redeem_once("code-2", store)  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_single_use.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.oauth_single_use'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Single-use enforcement, DI'd behind a tiny protocol so no real DB
connection is ever constructed in a test.

The real store (wired in gateway/app.py's production path only) does this
with one atomic statement:

    INSERT INTO mcp_gateway_oauth_codes (code_id, expires_at)
    VALUES ($1, $2)
    ON CONFLICT (code_id) DO NOTHING
    RETURNING code_id

- a non-empty result means this call won the race and claimed the code; an
empty result means it was already claimed (by a legitimate first exchange,
or a replay attempt). This is deliberately an INSERT, not the
UPDATE-with-redeemed_at-column shape sketched in the migration comment,
because INSERT ... ON CONFLICT DO NOTHING is atomic under concurrent
callers without needing a transaction or row lock - two simultaneous
redemption attempts for the same code_id can never both succeed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional, Protocol


class CodeAlreadyRedeemed(Exception):
    """This authorization code has already been exchanged for a token."""


class SingleUseStore(Protocol):
    async def try_redeem(self, code_id: str, expires_at: datetime) -> bool:
        """Atomically claim code_id. True if this call claimed it."""
        ...


async def redeem_once(
    code_id: str, store: SingleUseStore, expires_at: Optional[datetime] = None
) -> None:
    """Raise CodeAlreadyRedeemed unless this is the first redemption.

    expires_at is for the row's own cleanup convenience only - it is
    deliberately NOT the issued code's own exp claim (decode_issued_code
    never surfaces that; only email/code_challenge/code_id). The real
    production store should default this to "now + a fixed retention
    window" (an hour is generous given codes live 60 seconds) rather than
    trying to thread the code's actual expiry through - db/migrations/
    0032's own comment is explicit that correctness never depends on
    cleanup running at all, only on the PRIMARY KEY / ON CONFLICT check.
    """
    claimed = await store.try_redeem(code_id, expires_at)
    if not claimed:
        raise CodeAlreadyRedeemed(f"Authorization code {code_id!r} was already redeemed")
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_single_use.py -v
```

Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_oauth_single_use.py
git add mcp_servers/gateway/oauth_single_use.py
git commit -m "feat(gateway): atomic single-use enforcement for authorization codes"
```

---

## Task 5: RFC 9728 + RFC 8414 discovery metadata

The two static `.well-known` documents that make the whole flow
self-describing to a client.

**Files:**
- Create: `mcp_servers/gateway/oauth_metadata.py`
- Test: `mcp_servers/tests/gateway/test_oauth_metadata.py`

- [ ] **Step 1: Write the failing test**

```python
"""RFC 9728 Protected Resource Metadata and RFC 8414 Authorization Server
Metadata - both static JSON, generated from the gateway's own base URL.
"""

import sys
from pathlib import Path

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

from gateway.oauth_metadata import authorization_server_metadata, protected_resource_metadata

BASE_URL = "https://mcp.example.com"


def test_protected_resource_metadata_points_at_the_authorization_server():
    metadata = protected_resource_metadata(BASE_URL)
    assert metadata["resource"] == "https://mcp.example.com/mcp"
    assert metadata["authorization_servers"] == ["https://mcp.example.com"]


def test_authorization_server_metadata_advertises_the_three_endpoints():
    metadata = authorization_server_metadata(BASE_URL)
    assert metadata["issuer"] == "https://mcp.example.com"
    assert metadata["authorization_endpoint"] == "https://mcp.example.com/oauth/authorize"
    assert metadata["token_endpoint"] == "https://mcp.example.com/oauth/token"


def test_authorization_server_metadata_declares_pkce_s256_only():
    metadata = authorization_server_metadata(BASE_URL)
    assert metadata["code_challenge_methods_supported"] == ["S256"]


def test_authorization_server_metadata_declares_no_client_secret_required():
    # Public client, PKCE-secured - matches how Claude Code (loopback,
    # no stored secret) will call this.
    metadata = authorization_server_metadata(BASE_URL)
    assert "none" in metadata["token_endpoint_auth_methods_supported"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_metadata.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.oauth_metadata'`

- [ ] **Step 3: Write minimal implementation**

```python
"""RFC 9728 Protected Resource Metadata and RFC 8414 Authorization Server
Metadata - the two static discovery documents a client fetches before ever
talking to /oauth/authorize.
"""

from __future__ import annotations

from typing import Any, Dict


def protected_resource_metadata(base_url: str) -> Dict[str, Any]:
    """Served at /.well-known/oauth-protected-resource.

    Tells a client which authorization server issues tokens valid for this
    MCP server, and the canonical resource URI those tokens must be bound to
    (RFC 8707) - here, the gateway acts as its own authorization server, so
    both fields point at the same base_url.
    """
    return {
        "resource": f"{base_url}/mcp",
        "authorization_servers": [base_url],
    }


def authorization_server_metadata(base_url: str) -> Dict[str, Any]:
    """Served at /.well-known/oauth-authorization-server.

    token_endpoint_auth_methods_supported includes "none": this is a public
    client flow (Claude Code holds no client_secret at all - PKCE is the
    security boundary, not a confidential-client secret).
    """
    return {
        "issuer": base_url,
        "authorization_endpoint": f"{base_url}/oauth/authorize",
        "token_endpoint": f"{base_url}/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth_metadata.py -v
```

Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_oauth_metadata.py
git add mcp_servers/gateway/oauth_metadata.py
git commit -m "feat(gateway): RFC 9728 + RFC 8414 discovery metadata"
```

---

## Task 6: The three OAuth routes

This is where the pieces built so far compose. **Do the "Before you start"
Google Cloud Console check before this task**, not during it.

**Files:**
- Create: `mcp_servers/gateway/oauth.py`
- Test: `mcp_servers/tests/gateway/test_oauth.py`

- [ ] **Step 1: Write the failing test**

```python
"""The three OAuth routes, tested as plain async functions - not over ASGI.
Real Starlette Request/Response handling is app.py's job (see its own
docstring on why that layer stays thin and largely untested at unit level);
everything decidable without touching a real HTTP request is tested here.
"""

import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

_MCP_ROOT = Path(__file__).resolve().parents[2]
if str(_MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(_MCP_ROOT))

import pytest
from gateway.oauth import (
    AuthorizeResult,
    GoogleCallbackResult,
    TokenResult,
    build_authorize_redirect,
    handle_google_callback,
    handle_token_request,
)
from gateway.oauth_codes import decode_correlation_state, encode_correlation_state, issue_authorization_code
from gateway.oauth_single_use import CodeAlreadyRedeemed
from gateway.pkce import verifier_to_challenge
from gateway.session import GatewaySession
from gateway.signin import SignInRejected
from gateway.tokens import verify_token

SECRET = "test-secret-not-a-real-key"
BASE_URL = "https://mcp.example.com"


class _FakeGoogleOAuth:
    """Stands in for authlib's Google client."""

    def __init__(self, email="user@example.com"):
        self.email = email
        self.authorize_redirect_args = None

    def build_authorize_url(self, redirect_uri, state):
        self.authorize_redirect_args = (redirect_uri, state)
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    async def fetch_verified_email(self, callback_query):
        return self.email


class _FakeSingleUseStore:
    def __init__(self):
        self.redeemed = set()

    async def try_redeem(self, code_id, expires_at=None):
        if code_id in self.redeemed:
            return False
        self.redeemed.add(code_id)
        return True


class _FakeAuth:
    async def get_user_permissions(self, email, user_id=None):
        class _P:
            organization_ids = ["4"]
            is_staff = False
            user_id = "u1"
            organization_short_name = "testorg"

        return _P()

    async def get_grid_names_for_organization(self, organization_id=None, include_all=False):
        return ["Alpha Site"]


# --- /oauth/authorize --------------------------------------------------------


def test_authorize_redirects_to_google_with_signed_state():
    google = _FakeGoogleOAuth()

    result = build_authorize_redirect(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="opaque-client-value",
        code_challenge="challenge123",
        base_url=BASE_URL,
        secret=SECRET,
        google_oauth=google,
    )

    assert isinstance(result, AuthorizeResult)
    assert result.redirect_url.startswith("https://accounts.google.com/")
    redirect_uri, state = google.authorize_redirect_args
    assert redirect_uri == f"{BASE_URL}/oauth/google-callback"

    decoded = decode_correlation_state(state, SECRET)
    assert decoded.client_redirect_uri == "http://127.0.0.1:54321/callback"
    assert decoded.client_state == "opaque-client-value"
    assert decoded.code_challenge == "challenge123"


# --- /oauth/google-callback ---------------------------------------------------


@pytest.mark.asyncio
async def test_google_callback_issues_a_code_and_redirects_to_the_client():
    google = _FakeGoogleOAuth(email="user@example.com")
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:54321/callback",
        client_state="opaque-client-value",
        code_challenge="challenge123",
        secret=SECRET,
    )

    result = await handle_google_callback(
        state=correlation_state,
        callback_query={},
        secret=SECRET,
        google_oauth=google,
        is_authorized=lambda email: True,
        auth_service=_FakeAuth(),
    )

    assert isinstance(result, GoogleCallbackResult)
    parsed = urlparse(result.redirect_url)
    assert parsed.netloc == "127.0.0.1:54321"
    assert parsed.path == "/callback"
    query = parse_qs(parsed.query)
    assert query["state"] == ["opaque-client-value"]  # the CLIENT's own state, unchanged
    assert "code" in query


@pytest.mark.asyncio
async def test_google_callback_rejects_a_tampered_state():
    google = _FakeGoogleOAuth()
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )

    with pytest.raises(Exception):
        await handle_google_callback(
            state=correlation_state,
            callback_query={},
            secret="different-secret",
            google_oauth=google,
            is_authorized=lambda email: True,
            auth_service=_FakeAuth(),
        )


@pytest.mark.asyncio
async def test_google_callback_rejects_an_unauthorized_email():
    google = _FakeGoogleOAuth(email="stranger@example.com")
    correlation_state = encode_correlation_state(
        client_redirect_uri="http://127.0.0.1:1/callback",
        client_state="s",
        code_challenge="c",
        secret=SECRET,
    )

    with pytest.raises(SignInRejected):
        await handle_google_callback(
            state=correlation_state,
            callback_query={},
            secret=SECRET,
            google_oauth=google,
            is_authorized=lambda email: False,
            auth_service=_FakeAuth(),
        )


# --- /oauth/token -------------------------------------------------------------


@pytest.mark.asyncio
async def test_token_exchange_succeeds_with_correct_verifier():
    verifier = "test-verifier-abc"
    challenge = verifier_to_challenge(verifier)
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    result = await handle_token_request(
        code=issued.code,
        code_verifier=verifier,
        secret=SECRET,
        single_use_store=store,
        auth_service=_FakeAuth(),
    )

    assert isinstance(result, TokenResult)
    assert verify_token(result.access_token, SECRET) == "user@example.com"
    assert result.token_type == "Bearer"


@pytest.mark.asyncio
async def test_token_exchange_rejects_wrong_verifier():
    challenge = verifier_to_challenge("correct-verifier")
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    with pytest.raises(Exception):
        await handle_token_request(
            code=issued.code,
            code_verifier="wrong-verifier",
            secret=SECRET,
            single_use_store=store,
            auth_service=_FakeAuth(),
        )


@pytest.mark.asyncio
async def test_token_exchange_rejects_a_replayed_code():
    verifier = "test-verifier-abc"
    challenge = verifier_to_challenge(verifier)
    issued = issue_authorization_code(email="user@example.com", code_challenge=challenge, secret=SECRET)
    store = _FakeSingleUseStore()

    await handle_token_request(
        code=issued.code, code_verifier=verifier, secret=SECRET,
        single_use_store=store, auth_service=_FakeAuth(),
    )

    with pytest.raises(CodeAlreadyRedeemed):
        await handle_token_request(
            code=issued.code, code_verifier=verifier, secret=SECRET,
            single_use_store=store, auth_service=_FakeAuth(),
        )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'gateway.oauth'`

- [ ] **Step 3: Write minimal implementation**

```python
"""The three OAuth routes' logic, kept as plain async functions taking a
google_oauth/auth_service/single_use_store dependency each - app.py wires
the real Starlette Request/Response and the real authlib client around
these; nothing here touches either directly, matching the DI discipline
transport.py already established for the tool-calling path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlencode

from gateway.oauth_codes import decode_correlation_state, decode_issued_code, issue_authorization_code
from gateway.oauth_single_use import redeem_once
from gateway.pkce import PkceInvalid, verify_challenge
from gateway.session import SessionDenied, resolve_session
from gateway.signin import SignInRejected
from gateway.tokens import issue_token


@dataclass(frozen=True)
class AuthorizeResult:
    redirect_url: str


@dataclass(frozen=True)
class GoogleCallbackResult:
    redirect_url: str


@dataclass(frozen=True)
class TokenResult:
    access_token: str
    token_type: str = "Bearer"
    expires_in: int = 30 * 24 * 3600  # matches gateway.tokens.DEFAULT_TTL_SECONDS


def build_authorize_redirect(
    *,
    client_redirect_uri: str,
    client_state: str,
    code_challenge: str,
    base_url: str,
    secret: str,
    google_oauth: Any,
) -> AuthorizeResult:
    """Start the Google leg. The CLIENT's redirect_uri (Claude Code's own
    loopback address) is never sent to Google at all - only encoded into the
    signed correlation state Google faithfully round-trips back to us.
    """
    from gateway.oauth_codes import encode_correlation_state

    state = encode_correlation_state(
        client_redirect_uri=client_redirect_uri,
        client_state=client_state,
        code_challenge=code_challenge,
        secret=secret,
    )
    redirect_url = google_oauth.build_authorize_url(
        redirect_uri=f"{base_url}/oauth/google-callback",
        state=state,
    )
    return AuthorizeResult(redirect_url=redirect_url)


async def handle_google_callback(
    *,
    state: str,
    callback_query: Dict[str, Any],
    secret: str,
    google_oauth: Any,
    is_authorized: Callable[[str], bool],
    auth_service: Any,
) -> GoogleCallbackResult:
    """Google's own redirect target. Verifies the correlation state, gets
    the verified email from Google, checks the same two gates signin.py's
    mint_token_for_email checks (whitelist + resolvable session) - but
    doesn't call that function directly, since it issues a long-lived
    access token immediately; here we only issue a short-lived
    authorization CODE, matching the authorization_code grant shape.
    """
    correlation = decode_correlation_state(state, secret)
    email = await google_oauth.fetch_verified_email(callback_query)

    if not is_authorized(email):
        raise SignInRejected(f"{email} is not authorized for this application")

    try:
        await resolve_session(email, auth_service)
    except SessionDenied as exc:
        raise SignInRejected(f"{email} maps to no organization") from exc

    issued = issue_authorization_code(
        email=email, code_challenge=correlation.code_challenge, secret=secret
    )

    query = urlencode({"code": issued.code, "state": correlation.client_state})
    return GoogleCallbackResult(redirect_url=f"{correlation.client_redirect_uri}?{query}")


async def handle_token_request(
    *,
    code: str,
    code_verifier: str,
    secret: str,
    single_use_store: Any,
    auth_service: Any,
) -> TokenResult:
    """Exchange a code for the gateway's own long-lived access token.

    Order matters: decode (tamper/expiry check) and verify PKCE BEFORE
    touching the single-use store, so a malformed or forged code can never
    burn a legitimate code_id's single-use slot.
    """
    decoded = decode_issued_code(code, secret)
    verify_challenge(code_verifier, decoded.code_challenge)

    await redeem_once(decoded.code_id, single_use_store)

    access_token = issue_token(decoded.email, secret)
    return TokenResult(access_token=access_token)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_oauth.py -v
```

Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add -f mcp_servers/tests/gateway/test_oauth.py
git add mcp_servers/gateway/oauth.py
git commit -m "feat(gateway): the three OAuth routes - authorize, google-callback, token"
```

---

## Task 7: Wire the OAuth routes and discovery documents into app.py

**Files:**
- Modify: `mcp_servers/gateway/app.py`
- Test: `mcp_servers/tests/gateway/test_app.py` (extend)

- [ ] **Step 1: Write the failing test**

Add to `mcp_servers/tests/gateway/test_app.py`:

```python
@pytest.mark.asyncio
async def test_protected_resource_metadata_is_served():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-protected-resource")

    assert response.status_code == 200
    assert response.json()["resource"] == "https://mcp.example.com/mcp"


@pytest.mark.asyncio
async def test_authorization_server_metadata_is_served():
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/.well-known/oauth-authorization-server")

    assert response.status_code == 200
    assert response.json()["token_endpoint"] == "https://mcp.example.com/oauth/token"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: FAIL — `build_asgi_app() got an unexpected keyword argument 'base_url'`

- [ ] **Step 3: Write minimal implementation**

In `mcp_servers/gateway/app.py`, add the import and extend `build_asgi_app`:

```python
from gateway.oauth_metadata import authorization_server_metadata, protected_resource_metadata
```

Change the signature and add two routes — modify the existing function:

```python
def build_asgi_app(
    secret: str,
    auth_service: Any,
    registry_list_tools: RegistryListTools,
    registry_call_tool: RegistryCallTool,
    allowed_servers: Optional[List[str]] = None,
    base_url: str = "http://localhost:8080",
) -> Starlette:
```

And add these two handlers plus routes alongside the existing `healthz`:

```python
    async def protected_resource_metadata_route(request):
        return JSONResponse(protected_resource_metadata(base_url))

    async def authorization_server_metadata_route(request):
        return JSONResponse(authorization_server_metadata(base_url))
```

```python
    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/.well-known/oauth-protected-resource", protected_resource_metadata_route),
            Route("/.well-known/oauth-authorization-server", authorization_server_metadata_route),
            Mount("/mcp", app=session_manager.handle_request),
        ],
        lifespan=lifespan,
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: 4 passed (2 existing + 2 new)

- [ ] **Step 5: Commit**

```bash
git add mcp_servers/gateway/app.py
git add -f mcp_servers/tests/gateway/test_app.py
git commit -m "feat(gateway): serve RFC 9728 + RFC 8414 discovery documents"
```

---

## Task 8: Wire the three OAuth HTTP routes themselves

**Files:**
- Modify: `mcp_servers/gateway/app.py`
- Test: `mcp_servers/tests/gateway/test_app.py` (extend)

This task wires `gateway/oauth.py`'s three functions to real Starlette
routes with a real `authlib` Google client — the one place this whole plan
touches a real Starlette `Request` directly for the OAuth surface, matching
`app.py`'s existing, deliberate untested-at-the-unit-level boundary.

- [ ] **Step 1: Write the failing test**

Add to `mcp_servers/tests/gateway/test_app.py`:

```python
@pytest.mark.asyncio
async def test_authorize_route_redirects_towards_google(monkeypatch):
    app = build_asgi_app(
        secret="test-secret-not-a-real-key",
        auth_service=_FakeAuth(),
        registry_list_tools=_exploding_list_tools,
        registry_call_tool=_exploding_call_tool,
        allowed_servers=["customer"],
        base_url="https://mcp.example.com",
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test", follow_redirects=False) as client:
        response = await client.get(
            "/oauth/authorize",
            params={
                "redirect_uri": "http://127.0.0.1:54321/callback",
                "state": "client-state",
                "code_challenge": "abc123",
                "code_challenge_method": "S256",
            },
        )

    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: FAIL — 404, no `/oauth/authorize` route registered yet

- [ ] **Step 3: Write minimal implementation**

Add to `mcp_servers/gateway/app.py`:

```python
from starlette.responses import RedirectResponse

from gateway.oauth import build_authorize_redirect, handle_google_callback, handle_token_request
```

Inside `build_asgi_app`, add a `google_oauth_client` parameter and the three
route handlers:

```python
def build_asgi_app(
    secret: str,
    auth_service: Any,
    registry_list_tools: RegistryListTools,
    registry_call_tool: RegistryCallTool,
    allowed_servers: Optional[List[str]] = None,
    base_url: str = "http://localhost:8080",
    google_oauth_client: Optional[Any] = None,
    is_authorized: Optional[Callable[[str], bool]] = None,
    single_use_store: Optional[Any] = None,
) -> Starlette:
```

```python
    async def oauth_authorize_route(request):
        result = build_authorize_redirect(
            client_redirect_uri=request.query_params["redirect_uri"],
            client_state=request.query_params.get("state", ""),
            code_challenge=request.query_params["code_challenge"],
            base_url=base_url,
            secret=secret,
            google_oauth=google_oauth_client,
        )
        return RedirectResponse(result.redirect_url, status_code=302)

    async def oauth_google_callback_route(request):
        result = await handle_google_callback(
            state=request.query_params["state"],
            callback_query=dict(request.query_params),
            secret=secret,
            google_oauth=google_oauth_client,
            is_authorized=is_authorized,
            auth_service=auth_service,
        )
        return RedirectResponse(result.redirect_url, status_code=302)

    async def oauth_token_route(request):
        form = await request.form()
        result = await handle_token_request(
            code=form["code"],
            code_verifier=form["code_verifier"],
            secret=secret,
            single_use_store=single_use_store,
            auth_service=auth_service,
        )
        return JSONResponse(
            {
                "access_token": result.access_token,
                "token_type": result.token_type,
                "expires_in": result.expires_in,
            }
        )
```

Add the three routes to the `routes=[...]` list, before the `Mount("/mcp", ...)`:

```python
            Route("/oauth/authorize", oauth_authorize_route),
            Route("/oauth/google-callback", oauth_google_callback_route),
            Route("/oauth/token", oauth_token_route, methods=["POST"]),
```

Add `Callable` to the existing `from typing import ...` line.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_servers/gateway/app.py
git add -f mcp_servers/tests/gateway/test_app.py
git commit -m "feat(gateway): wire the three OAuth HTTP routes"
```

---

## Task 9: WWW-Authenticate on unauthenticated tool calls

Per the spec: a 401 without `WWW-Authenticate: Bearer resource_metadata="..."`
leaves a client with no way to discover where to authenticate at all — this
is the header a client parses to find `/.well-known/oauth-protected-resource`
in the first place.

**Files:**
- Modify: `mcp_servers/gateway/app.py`
- Test: `mcp_servers/tests/gateway/test_app.py` (extend)

- [ ] **Step 1: Write the failing test**

This is the one thing genuinely hard to test through the MCP `Server`
wrapper (Task 7's discovery findings on how it converts exceptions).
Add a direct test of the header-producing helper instead of round-tripping
through a real MCP client:

```python
def test_www_authenticate_header_names_the_resource_metadata_url():
    from gateway.app import unauthorized_www_authenticate_header

    header = unauthorized_www_authenticate_header("https://mcp.example.com")
    assert header == (
        'Bearer resource_metadata="https://mcp.example.com/.well-known/oauth-protected-resource"'
    )
```

- [ ] **Step 2: Run test to verify it fails**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: FAIL — `ImportError: cannot import name 'unauthorized_www_authenticate_header'`

- [ ] **Step 3: Write minimal implementation**

Add to `mcp_servers/gateway/app.py`, at module level (not inside
`build_asgi_app`, since it needs no closure state):

```python
def unauthorized_www_authenticate_header(base_url: str) -> str:
    """The header a client parses to discover where to authenticate.

    Per the spec's discovery sequence, this is what turns a bare 401 into
    something a client can act on - without it, a client has a token
    requirement with no way to find out how to satisfy it.
    """
    return f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource"'
```

Note: wiring this header onto the MCP `Server`'s own error responses (as
opposed to the plain Starlette routes above, where it's straightforward)
depends on exactly how `StreamableHTTPSessionManager` surfaces a raised
`TokenInvalid` as an HTTP status — verify this against the installed
`mcp` package's source (`mcp.server.streamable_http`) the same way Task 7
of the original plan verified `request_context.request`, rather than
assuming the shape. This step intentionally stops at the header-producing
helper; wiring it onto the actual 401 path is this task's remaining step
once that's confirmed.

- [ ] **Step 4: Run test to verify it passes**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/test_app.py -v
```

Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add mcp_servers/gateway/app.py
git add -f mcp_servers/tests/gateway/test_app.py
git commit -m "feat(gateway): WWW-Authenticate header helper for 401 discovery"
```

---

## Task 10: DO App Platform deployment

Executes the plan the original spec's Deferred section already wrote out —
nothing new to design here, just applying it. **This is the task that
requires the Google Cloud Console check from "Before you start" to have
already happened**, since the ingress rule's domain is what gets registered
as the Google redirect URI.

**Files:**
- Modify: `.do/app.example.yaml`

- [ ] **Step 1: Add the service entry**

Add under the existing `services:` list, alongside `chat-orchestrator` and
`anansi-app`:

```yaml
  # ---------------------------------------------------------------------------
  # Service 3: MCP Gateway
  # Per-user MCP access for external clients (Claude, Codex). Dedicated
  # service and ingress rule - deliberately NOT folded into chat-orchestrator,
  # which has no bearer-token auth model of its own.
  # ---------------------------------------------------------------------------
  - name: mcp-gateway
    github:
      repo: your-org/anansi
      branch: main
      deploy_on_push: true
    dockerfile_path: mcp_servers/gateway/Dockerfile
    http_port: 8080
    instance_count: 1
    instance_size_slug: basic-xxs
    health_check:
      http_path: /healthz
      initial_delay_seconds: 10
      period_seconds: 30
    envs:
      - key: MCP_GATEWAY_TOKEN_SECRET
        value: "${MCP_GATEWAY_TOKEN_SECRET}"
        scope: RUN_TIME
        type: SECRET
      # Reuses anansi_app's existing Google OAuth client - confirm in
      # Google Cloud Console (see this plan's "Before you start") whether
      # that means these two are literally the same values as
      # anansi_app's GOOGLE_CLIENT_ID/SECRET, or a second registered client.
      - key: GOOGLE_CLIENT_ID
        value: "${GOOGLE_CLIENT_ID}"
        scope: RUN_TIME
      - key: GOOGLE_CLIENT_SECRET
        value: "${GOOGLE_CLIENT_SECRET}"
        scope: RUN_TIME
        type: SECRET
```

- [ ] **Step 2: Add the ingress rule, staged deny-first**

Add to the `ingress: rules:` list, before the catch-all `prefix: /` rule
(anywhere above it works — rules evaluate in order and the catch-all is
already last):

```yaml
    - component:
        name: mcp-gateway
        preserve_path_prefix: true
      match:
        path:
          prefix: /mcp-gateway
```

`preserve_path_prefix: true` is mandatory here — see the comment above the
existing `/chat` rule for why omitting it silently breaks every nested
route.

- [ ] **Step 3: Deploy with `is_authorized` denying everyone, verify routing**

Before wiring the real whitelist, confirm the ingress rule actually reaches
the new service rather than being swallowed by the catch-all — the exact
failure mode documented for the Jira webhook incident:

```bash
curl -s https://your-app.example.com/mcp-gateway/healthz
```

Expected: `{"status": "ok"}`. Anything else (a 404, or `anansi-app`'s login
page HTML) means the ingress rule isn't matching — check rule order and the
`preserve_path_prefix` flag before anything else.

- [ ] **Step 4: Only then wire the real whitelist and commit**

Once routing is confirmed, `is_authorized` should point at the real
`grid_app.lib.perms.is_authorized` (this needs `anansi_app` importable from
wherever the gateway container runs — check `mcp_servers/gateway/Dockerfile`,
not written by this plan, needs to `COPY` `anansi_app/grid_app/` in, matching
the `image-copy-sets-cause-silent-import-failures` lesson this repo has
already hit three times).

```bash
git add .do/app.example.yaml
git commit -m "feat(gateway): DO App Platform service and ingress rule"
```

---

## Task 11: Full suite, pre-commit, end-to-end check

- [ ] **Step 1: Run the full gateway suite**

```bash
.venv/bin/python -m pytest mcp_servers/tests/gateway/ -v
```

Expected: all tests pass (57 from the merged PR + this plan's new tests)

- [ ] **Step 2: Full mcp_servers suite — no regressions**

```bash
.venv/bin/python -m pytest mcp_servers/tests/ -q
```

- [ ] **Step 3: pre-commit**

```bash
pre-commit run --all-files
```

- [ ] **Step 4: Apply the migration to prod — manual, not automatic**

Per this repo's own `db-migrations-need-manual-apply` lesson: merging
`db/migrations/0032_oauth_code_single_use.sql` does **not** apply it.
Someone with prod DB access runs it separately. Confirm applied before
Task 10's deployment goes live, not after — an unapplied migration here
doesn't fail loudly, it just means every token exchange 500s on a missing
table.

- [ ] **Step 5: The actual end-to-end test**

With Task 10 deployed and the migration applied:

```bash
claude mcp add --transport http mcp-gateway https://your-app.example.com/mcp-gateway/mcp
```

This is the real test the whole plan exists to make possible — Claude Code
should redirect to Google, the user signs in with their own real work
email through their own real browser, and the connector should show as
connected with no token ever pasted anywhere.

---

## Deferred — still not in this plan

- **True multi-tenant DCR** (`POST /register`). The fixed-client-ID path
  built here is spec-compliant and sufficient for v1.
- **Refresh tokens.** The 30-day access token is re-obtained by re-running
  the Google leg on expiry.
- **Everything the original plan's Deferred section still defers**: Tier 3
  servers, context assembly, safety parity.
