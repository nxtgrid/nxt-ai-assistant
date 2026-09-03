# MCP Gateway — Per-User Access to nxt Tools from Claude / Codex

## Problem

We want the assistant's ~107 MCP tools available to external MCP clients
(Claude, Codex) under **per-user** authentication, with the same org/grid
filtering the Telegram path applies today.

The blocker is not tool count. It is that **the trust boundary currently sits
in the orchestrator, not in the MCP servers** — verified by reading the code:

- `chat_orchestrator/orchestrator/services/tool_executor.py:209-227` injects
  `organization_id` / `user_email` / `chat_id` / `topic_id` into every tool
  call, with an explicit comment: these values "come from the webhook request
  and **CANNOT** be controlled by the LLM". The spread-then-overwrite order
  (`{**arguments, "organization_id": ...}`) is what makes injection
  authoritative.
- `mcp_servers/server_registry.py:211` — `call_tool(server_name, tool_name,
  arguments)` accepts **no user context at all**. Scope arrives only as
  ordinary arguments.
- `mcp_servers/bridge.py:37` authenticates with a **single shared `API_KEY`**.
  One key, no identity, no per-user filtering.

Consequence: expose the servers directly and the *client* becomes the caller,
so the client controls the arguments that scope the query. The model does not
degrade gracefully — it inverts.

### How far the gap goes

Nine of seventeen servers never reference `organization_id` in any file:
`codebase`, `equipment_control`, `grafana`, `logs`, `messaging`,
`payment_processor`, `reference`, `solar`, `vrm`. There is no tenant isolation
to inherit — it was never needed, because the orchestrator was the only caller.
`equipment_control` carries real write actions (`restart_inverter`,
`restart_comms_chain`).

Only 12 of 107 tools declare `organization_id` in their `inputSchema`; the rest
receive it as an undeclared extra argument, and most ignore it.

### Scaffolding that looks like a solution and is not

Three things in-tree read as an existing per-user permission system. None is
production code. Budgeting as if they are a head start is the main estimation
risk on this project:

- `shared/auth/user_context.py` — `UserContextManager._load_user_context`
  returns hardcoded `admin`/`manager`/`analyst`/`viewer` records under the
  comment "In a real implementation, this would query your user database".
- `mcp_servers/mcp_launcher.py:612` — `_get_mock_user_context`, plus
  role-to-tool restriction tables. Demo path only.
- `user_permissions.filter_tools_for_user` is referenced by **five**
  docstrings (`customer_server/tool_schemas.py:15`,
  `grid_design_server/tool_schemas.py:12`, and others). **The function does not
  exist.** The real one is the private
  `UserPermissionsService._filter_and_convert_tools`
  (`chat_orchestrator/orchestrator/services/user_permissions.py:271`).

## Approach

Build **one** new MCP server — the gateway — that re-creates the orchestrator's
injection boundary in front of the existing registry. No edits to the 17
servers.

```
Claude / Codex (MCP client)
     |  bearer token (issued after Google sign-in)
     v
nxt-mcp-gateway                      <- the only new component
     |- session:    email -> AuthService.get_user_permissions(email)   [reused]
     |- list_tools: 107 defs -> tier filter + visibility filter
     |- call_tool:  SCOPE GUARD, then delegate
     v
server_registry.call_tool(...)       <- unchanged
     v
17 MCP servers                       <- unchanged, zero edits
```

The load-bearing insight: **the tenancy surface is ~12 argument names, not 107
tools.** Counted across every `inputSchema` in `tool_definitions.json`:

| count | argument            | class |
|-------|---------------------|-------|
| 20    | `grid_name`         | B     |
| 16    | `user_email`        | A     |
| 12    | `organization_id`   | A     |
| 11    | `meter_number`      | C     |
| 8     | `organization`      | A     |
| 3     | `grid`              | B     |
| 3     | `meter_no`          | C     |
| 1     | `grid_names`        | B     |
| 1     | `user_name`         | A     |
| 1     | `customer_id`       | C     |
| 1     | `customer_name`     | C     |
| 1     | `organization_name` | A     |

So the work is one guard table with ~12 entries, not 107 per-tool audits.

### The scope guard

**Class A — INJECT.** `organization_id`, `user_email`, `user_name`,
`organization`, `organization_name`. Client value is discarded and overwritten
from the session, mirroring `tool_executor.py:219`.

**Class B — VALIDATE.** `grid_name`, `grid`, `grid_names`. The client supplies
a value; the gateway resolves it **against the caller's allowed grid names
only**, then forwards the canonical name.

Two traps, both confirmed in code:

- `UserPermissions.grid_ids` holds **numeric ids as strings**
  (`auth_service.py:241`, `[str(row["id"]) for row in grid_rows]`), but 20
  tools take a grid *name*. The allowed-name set must come from
  `AuthService.get_grid_names_for_organization(org_id)` (`auth_service.py:756`),
  which returns names and already has an `include_all` staff mode.
- `AuthService.get_grid_portal_id` **fuzzy-matches** grid names
  (`auth_service.py:907`, rapidfuzz, score-based). A raw client string must
  never reach a server that will fuzzy-match it downstream — a near-miss could
  resolve onto another org's grid. Fuzzy resolution therefore happens *inside
  the gateway, against the allowed set*, and only the exact canonical name is
  forwarded.

**Class C — DELEGATE.** `meter_number`, `meter_no`, `customer_id`,
`customer_name`. These cannot be validated at the gateway:
`UserPermissions.meter_ids` is **always empty by design** —
`auth_service.py:252` states "Meters are filtered at MCP tool execution time
using organization_id. No need to pre-load all meter IDs here". So they are safe
only where the server itself filters by org, which produces the tiering below.

### Server tiers

| Tier | Servers | Rationale |
|------|---------|-----------|
| **1 — expose** | `customer`, `equipment_diagnostics`, `grid_design`, `jira`, `knowledge`, `meta`, `meters`, `schedule` | consume `organization_id`; Class A injection genuinely enforces |
| **2 — expose, Class B enforced** | `grafana`, `logs`, `reference`, `solar`, `vrm`, `codebase` | internally unscoped, but their scope-bearing args are grid-shaped |
| **3 — denied in v1** | `equipment_control`, `payment_processor`, `messaging` | side-effecting **and** unscoped |

Tiering is one line per server, not a per-tool rewrite. That is what keeps this
at weeks rather than months. Tier 3 is where genuine per-server isolation work
remains; deferring it costs 3 servers, not 17.

### Authentication

Google OAuth is sufficient, and the pattern already exists in-tree:

- `_get_user_permissions_direct(email)` (`auth_service.py:290`) resolves
  organization, grids and staff status from `public.accounts.email` alone.
  `is_staff` is `organization_id == STAFF_ORG_ID`. A verified email is the only
  input the permission model needs.
- `anansi_app/nicegui_app/auth.py` already implements Google OAuth (Authlib) with
  `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` and an `/oauth2callback` route,
  delegating authorization to `grid_app.lib.perms.is_authorized(email)`. The
  same OAuth client registration is reused.

**Fail-closed requirement.** When the email is absent from `public.accounts`,
`_get_user_permissions_direct` does not raise — it returns
`UserPermissions(user_id=email, email=email)` with **empty `organization_ids`**
(`auth_service.py:302`). A gateway that forwarded that would send
`organization_id=None` to the nine unscoped servers. Empty `organization_ids`
must therefore deny the session outright.

**Transport for v1: bearer token, not full MCP OAuth.** The user signs in with
Google on a small gateway page and receives a token to paste into their MCP
client config. Rationale: remote-MCP OAuth requires the server to act as an
OAuth 2.x resource server with discovery and dynamic client registration, which
Google does not provide for arbitrary third-party clients — it needs a
federating authorization layer. That is a project in itself, the MCP auth spec
has moved since this codebase was written, and it should be re-checked against
the current specification before being scheduled. A bearer token gets the same
identity guarantee with none of that surface. Full OAuth stays a follow-on.

### Call-time deny, not just list-time hiding

`_filter_and_convert_tools` (`user_permissions.py:271`) hides `persistent_only`
and `internal_only` tools from the LLM, and its docstring is explicit that
"internal_only tools remain callable via server_registry.call_tool — this only
hides them from the LLM's tool list." Hiding a tool from `list_tools` therefore
does not make it unreachable. The gateway must re-check every gate at
`call_tool` time: tier, `internal_only`, `persistent_only`, `visible_to_customer`,
and `ActionFlags` server/tool enablement.

## Non-goals for v1

- **Context assembly.** `prepare_context.py` (533 lines) composes expert
  instructions, troubleshooting procedures, RAG, knowledge modules and user
  preferences into Gemini's system prompt. MCP clients own their own context
  window; exposing this as MCP resources is a separate piece of work. Tools will
  function without it, but will lack the domain steering the Telegram bot has.
- **Safety parity.** `safety_check`, `check_escalation`, response verification
  and the escalation flow are orchestrator nodes and are bypassed by direct tool
  calls.
- **Tier 3 servers.**
- **Write-tool parity.** v1 is read-oriented; Tier 1 writes stay gated behind
  `ActionFlags` exactly as today.

## Risks

1. **The threat model changes even though the code path does not.** Today the
   unscoped servers are protected in part by "Gemini does not try". A human
   driving Claude will enumerate. Class B validation is what closes this; Tier 2
   must not ship without it.
2. **Stale-docstring hazard.** Five docstrings name a function that does not
   exist. Verify every symbol against the definition before calling it.
3. **`tool_definitions.json` is what production serves**, exported wholesale per
   server and not merged with code definitions. It currently carries 12 of the
   17 servers. Any tool-schema change requires re-running
   `mcp_servers/scripts/export_tools.py`.
