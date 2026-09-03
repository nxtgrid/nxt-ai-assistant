# MCP Gateway — Connector-Style OAuth + Deployment

## Problem

The merged gateway (`mcp_servers/gateway/`, PR #177) issues bearer tokens via
a pure function (`mint_token_for_email`) with no HTTP endpoint in front of it,
and is not deployed anywhere — `.do/app.example.yaml` has no service or
ingress entry for it. A user cannot add it to Claude/Codex as a connector:
there is nothing running, nothing reachable, and no OAuth flow for a client
to drive.

The original spec's Authentication section called full remote-MCP OAuth "a
project in itself" and explicitly flagged that the spec should be re-checked
before scheduling, since it had moved since this codebase was written. That
re-check is what this document is. Verified live against
[modelcontextprotocol.io](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
(spec version `2026-07-28`, current as of this writing — the earlier
`2025-06-18` version this repo would otherwise have been checked against is
now one revision behind) and against
[Anthropic's own connector-building docs](https://claude.com/docs/connectors/building).
Conclusion: it is real work, but substantially smaller than "a project in
itself" — most of the hard part (session resolution, token issuance) is
already built. What is missing is the OAuth-protocol envelope around it.

## The one thing that resolves the earlier confusion

A connector-style flow has **two separate OAuth hops**, and the original
assessment conflated them:

```
Claude Code  <---loopback redirect--->  Gateway's own authorization server
                                                |
                                                | (server-to-server leg,
                                                |  ONE stable redirect URI,
                                                |  registered once)
                                                v
                                              Google
```

1. **Claude Code ↔ the gateway's own authorization server.** Per Anthropic's
   docs: "OAuth callback: ... loopback redirect for Claude Code." The redirect
   URI here is dynamic, chosen by Claude Code at connect time, and never seen
   by Google at all. The gateway's own authorization-server code decides
   whether to accept it — a permissive "any loopback address for a public
   client using PKCE" policy is the standard native-app pattern
   ([RFC 8252](https://datatracker.ietf.org/doc/html/rfc8252)) and needs no
   registration anywhere.
2. **The gateway's own authorization server ↔ Google.** This is a normal
   server-to-server OAuth leg with exactly **one** stable redirect URI — the
   gateway's own callback endpoint at wherever it's deployed. Registered
   **once**, in Google Cloud Console, regardless of how many different MCP
   clients (Claude Code, Claude.ai, Codex, ...) connect through hop 1.

The redirect-URI concern raised earlier — "wouldn't every test need its own
Google registration" — does not apply once these are recognized as separate
hops. Only hop 2 touches Google, and it needs one registration, not one per
client or per test.

**Unverified, flag before implementing:** which Google OAuth client type
(`Web application` vs `Desktop app`) is right for hop 2, and whether
`anansi_app`'s existing client can just get a second redirect URI added to
it rather than needing a new client. This is well-established, stable
platform behavior I'm highly confident about from general knowledge, but
`developers.google.com` returned a backend error on every fetch attempt this
session (three tries, distinct from the transient errors other domains hit
once and recovered from) — so it's asserted, not freshly verified. Confirm
directly in Google Cloud Console before Task 1 below.

## What Claude actually requires (verified against its own docs)

- **Transport:** Streamable HTTP — already what `gateway/app.py` implements.
  No change needed there.
- **Dynamic Client Registration: supported, not required.** Claude's docs
  list "Custom credentials for non-DCR servers" as an explicit, first-class
  path — the user manually enters a client ID (and secret, if confidential)
  when adding the connector. This means the gateway does **not** need to
  implement true multi-tenant `RFC 7591` registration for a working v1: a
  single pre-configured client ID, entered once when the connector is added,
  is a fully supported, spec-compliant path. A minimal `/register` endpoint
  that always returns the same fixed client ID is worth adding later for a
  smoother "just click connect" experience, but is not required to be
  testable.
- **Discovery is mandatory regardless:** `RFC 9728` Protected Resource
  Metadata (the gateway MUST publish this) and — per spec `2026-07-28` — the
  gateway's authorization server MUST publish `RFC 8414` OR OpenID Connect
  Discovery (at least one). These are the two `.well-known` documents that
  make the whole thing self-describing to a client.

## What's reused vs. genuinely new

**Reused as-is** — none of this changes:

- `gateway/session.py` (`resolve_session`) — fail-closed session resolution.
- `gateway/tokens.py` (`issue_token`/`verify_token`) — becomes the gateway's
  own *access* token, exactly as already built.
- `gateway/signin.py` (`mint_token_for_email`) — becomes the function the new
  `/token` endpoint calls once the Google leg completes and PKCE validates.
- `gateway/transport.py`, `gateway/catalog.py`, `gateway/scope_guard.py`,
  `gateway/tiers.py`, `gateway/server.py` — entirely unchanged. The
  authorization layer sits in front of `gateway/app.py`; nothing downstream
  of "here is a valid bearer token" changes at all.

**New — the OAuth protocol envelope** (`gateway/oauth.py` + new routes on the
existing `gateway/app.py` Starlette app):

| Endpoint | Purpose |
|---|---|
| `GET /.well-known/oauth-protected-resource` | RFC 9728 — advertises the authorization server's location. Static JSON. |
| `GET /.well-known/oauth-authorization-server` | RFC 8414 — advertises `/authorize`, `/token`, (later) `/register`. Static JSON. |
| `GET /oauth/authorize` | Accepts the client's PKCE challenge + loopback `redirect_uri` + `state`; redirects to Google with the gateway's *own* stable callback as Google's redirect URI, stashing the client's original request server-side keyed by a short-lived correlation id. |
| `GET /oauth/google-callback` | Google's registered redirect target (hop 2, above). Exchanges Google's code for the verified email, mints the gateway's own short-lived authorization code, redirects back to the *client's* original loopback `redirect_uri` with that code + `state`. |
| `POST /oauth/token` | Validates the PKCE `code_verifier` against the code minted above, then calls `mint_token_for_email` (existing, unchanged) and returns the result as a standard OAuth token response. |

Every 401 `gateway/app.py`'s `_call_tool`/`_list_tools` handlers currently
produce (via `TokenInvalid`/`SessionDenied` propagating to the SDK's own
error wrapper) needs a `WWW-Authenticate: Bearer resource_metadata="..."`
header per the spec's discovery-handshake requirement — this is the one
change to existing gateway code, everything else above is additive.

## Deployment (unavoidable — OAuth doesn't remove this)

A real, stable, HTTPS-reachable deployment is a hard requirement for hop 2
regardless of anything else: Google's own redirect-URI rules require it (or
`localhost`, which doesn't make sense for a server-to-server leg). This is
the DO App Platform work the original plan's Deferred section already
scoped — new dedicated service, explicit `preserve_path_prefix: true`
ingress rule ahead of the catch-all, dedicated secret, staged rollout
(deny-all → routing check → real whitelist). That plan doesn't change; it's
now a hard prerequisite rather than an optional next step, since `/oauth/
google-callback` needs a real domain to register with Google at all.

## Non-goals for this phase

- **True multi-tenant DCR** (`POST /register` generating a distinct client
  per caller). The fixed-client-ID path is spec-compliant and sufficient;
  real DCR is a smoother-UX follow-on, not a blocker.
- **Non-Google identity providers.** Out of scope; `AuthService` and this
  design are Google-specific throughout.
- **Refresh tokens.** The spec treats these as optional (`MAY`); the existing
  30-day `DEFAULT_TTL_SECONDS` access token is used as-is. Re-running the
  Google leg on expiry is acceptable for this phase.

## Risks

1. **Spec churn.** This is the second time this document has had to
   re-verify against a moved spec (`2025-06-18` → `2026-07-28` since the
   original gateway spec was written). Re-check
   `modelcontextprotocol.io/specification/latest/basic/authorization`
   immediately before implementation, not from this document alone.
2. **The unverified Google client-type question above** — confirm in Google
   Cloud Console before Task 1, not assumed from this spec.
3. **Authorization-code and PKCE state need real storage**, not an
   in-process dict — `stateless=True` on the MCP session manager means the
   *tool-calling* path has no server-affinity requirement, but the
   `/authorize` → `/oauth/google-callback` → `/oauth/token` correlation
   **does** span multiple requests and must survive a restart or a
   multi-instance deployment. A short-TTL row in the existing chat DB
   (or a new small table) is simplest; an in-memory dict works for local
   testing only and must not be mistaken for the production design.
