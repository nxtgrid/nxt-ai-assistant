# Doc-Backed Context Modules with Per-User Access Gating

**Date:** 2026-08-20
**Branch:** `feat/knowledge-modules-from-docs`
**Depends on:** `2026-08-19-resolvable-context-modules-design.md` (P1, shipped — established `source`/`source_ref` and the provider registry)
**Supersedes in part:** P1's decision to resolve `gdoc` synchronously

---

## Problem

Three gaps, one root cause.

**1. You cannot create a doc-backed context module.** The `gdoc` plumbing from P1
exists and works, but nothing can produce a row that uses it:

- `/learn` (`context_expert` → `store_module.build_module_payload`,
  `chat_orchestrator/orchestrator/experts/handlers/context_expert/store_module.py:38`)
  hardcodes `"source": "manual"` and stores a snapshot body. When the input was a
  Google Doc, the doc id is fetched, used once, and discarded — no `source_ref`.
- The admin Context page (`anansi_app/nicegui_app/pages/knowledge_modules.py`) has
  no source selector. New modules always insert with the column default, `manual`.

So the feature is unreachable, and every module authored from a document is a
point-in-time copy that silently drifts from its source.

**2. The preview lies, and the gating is backwards.** For a provider-backed
module the dialog's Edit/Preview toggle renders the *stored* `body`
(`knowledge_modules.py:262`) — for a gdoc module that is a placeholder, never the
live document. The separate "Preview" button asks
`build_default_registry().get("gdoc")`, which returns `None` because
`jit_context_resolver.py:120` registers only `directory`/`graph`/`episodic`; and
even if it were registered, `preview_module_body` awaits `provider.resolve(...)`,
which `GDocProvider` does not implement (it is sync `body_for`). So the preview is
broken two ways.

The instinct to gate that preview on the operator's Drive access is correct but
insufficient on its own. A gdoc module resolved into `customer.system` reaches
**every user of that prompt**, regardless of who attached it. Gating the preview
while the rendered prompt stays ungated protects the smaller hole.

**3. The on-demand tool is an ungated read primitive.** `fetch_knowledge_module`
(`mcp_servers/servers/knowledge_server/knowledge_mcp_server.py:250`) resolves a
gdoc body with no caller identity at all — `_handle_get_knowledge_module` passes
only the slug. The model can name any slug and read any attached document. This
is a larger exposure than the preview and must close in the same change.

Secondary: Google Sheets have never worked as a context source anywhere.
`fetch_google_doc_markdown` → `fetch_document_with_formatting` uses the **Docs
API** (`documents().get()`), which is Docs-only. `/learn`'s `fetch_document`
rejects spreadsheets with "Unsupported file type". A CSV export path exists in
`GoogleDriveDocFetcher._download_file_content` but nothing on the markdown path
calls it.

## Approach

Move `gdoc` from the synchronous render path onto the JIT path, where per-request
caller identity already exists, and make the document's own Drive ACL the default
authorization boundary — with an explicit, attributable opt-out for content that
is genuinely meant for customers.

`JitContextResolver` was built for exactly this shape: *"graph/directory/episodic
bodies need async, permission-filtered database work"*
(`chat_orchestrator/orchestrator/services/jit_context_resolver.py:1`). Its
`ResolutionContext` already carries `user_email`, `organization_ids`, `role_ids`
and `is_staff`. `gdoc` is the one source deliberately excluded from it
(`shared/prompts/knowledge.py:28`), resolving instead on the sync path where
`RequestScope` knows only `grid` and `organization_id` — no identity whatsoever.

Preconditions verified:

- `UserContext.user_email` is a **required** field, resolved from telegram_id via
  the auth service with a 403 when unresolvable (`chat_orchestrator/handler.py:2198`).
  A real email is always present at render time.
- `_fetch_jit_context` already runs for both `staff.system` and `customer.system`
  (`chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py:306`), so the
  JIT path covers customers, not just staff.
- The admin preview already awaits `provider.resolve(module, ctx)`, so preview and
  production can share one authorization call rather than two implementations.
- The Sheets API is already wired — `get_sheets_credentials()` in
  `shared/utils/google_auth.py:110`, with `build("sheets", "v4")` used in four
  places — so tab-selective reads are available without new credential plumbing.
- `tool_executor` injects `user_email` into every MCP tool call's arguments
  (`chat_orchestrator/orchestrator/services/tool_executor.py:222`). The knowledge
  handler simply ignores it.

### Audience model

Every doc-backed module carries an explicit audience decision, made by the
operator at attach time:

| `doc_audience` | Behaviour | Default |
|---|---|---|
| `acl_mirror` | Resolve only for a caller whose email can read the file in Drive. Denied → the module contributes nothing, per-user, silently. | ✅ |
| `published` | Resolve for everyone the prompt serves. No per-caller check. | |

`acl_mirror` makes doc-backed modules staff-only in practice — a customer's email
is not in the ACL of an internal ops document. That is the safe default and the
common case. `published` preserves the ability to author customer-facing context
from a document, and records **who** made that call so the decision is
attributable rather than accidental.

## Design

### 1. Resolution moves to the JIT path

`shared/prompts/knowledge.py`:

```python
JIT_SOURCES: Tuple[str, ...] = ("gdoc", "graph", "directory", "episodic")
```

That single change cascades correctly: `_compose_knowledge` already filters
`chosen = [m for m in chosen if not m.is_jit]`, so the sync path stops handling
docs on its own. `_with_resolved_body` and the `gdoc_module_provider=GDocProvider()`
wiring in `shared/prompts/core.py:174,314` are **deleted**, not modified.

`shared/prompts/providers_gdoc.py` — `GDocProvider` implements the async
`ContextProvider` protocol:

```python
async def resolve(self, module, ctx) -> Optional[str]:
    file_id = module.source_ref
    if not file_id:
        return None
    meta = await self._meta(file_id)                    # one files.get: id, mimeType, permissions
    if module.doc_audience == "acl_mirror":
        # `meta` is passed in so the ACL check and the mime branch share one
        # Drive round trip rather than each making their own.
        if not await user_can_access(file_id, ctx.user_email, strict=True, meta=meta):
            return None
    return await self._fetch_markdown(file_id, meta["mimeType"], module.source_tab)

async def visible_to(self, module, ctx) -> bool:
    """Access only — no content fetch. Gates the on-demand catalog line."""
```

Registered in `build_default_registry()` alongside the other three.

Two caches with distinct jobs:

| Cache | Key | TTL | Purpose |
|---|---|---|---|
| Content | `(file_id, tab)` | 300s (existing) | Avoid refetching document text |
| Access | `(file_id, user_email)` | 60s, configurable | **This TTL is the revocation lag** |

### 2. `strict=True` on `user_can_access`

New mode on `shared/utils/drive_permissions.py`. Narrower than the name suggests:
it still performs the `files.get()` metadata fetch and still reads whatever
permissions the API returns. It removes **only** the final blanket grant —

```python
# Service account reached the file but sees no explicit permissions
# → inherited/link-share access. Grant read; ...
if not need_write and meta.get("id"):
    return True
```

— which would otherwise let every customer through on any link-shared or Shared
Drive document, i.e. precisely the documents most likely to be used. Everything
above that branch (explicit email match, `type: "anyone"` for read) is unchanged,
so Shared Drive enumeration keeps working wherever Drive actually reports it.

Two corrections to the same function, needed for strict mode to be usable:

- **Honour `type: "domain"`** when the user's email domain matches the permission's
  domain. Not handled today at all. "Shared with everyone at the company" is the
  most likely sharing mode for an ops document; without this, strict mode
  false-denies most staff.
- **`type: "group"` remains a documented false-deny.** Expanding group membership
  needs the Admin SDK, which is not wired up. Workaround stays: share directly.
  This is already documented in the module docstring; keep it accurate.

### 3. Closing the on-demand hole

Three gates, all using the same provider call:

**a. The MCP tool.** `_handle_get_knowledge_module` reads
`arguments.get("user_email")` — already injected — and `fetch_knowledge_module`
takes it as a parameter, running the access check before returning any gdoc body.
A denied fetch returns a plain refusal string, not an empty body (matching the
existing convention for JIT modules at `knowledge_mcp_server.py:273`, which the
model would otherwise read as "this module has no content").

Note the existing `if module.is_jit: return "...cannot be fetched on demand here"`
branch will now catch `gdoc`, since gdoc becomes JIT. That branch must be narrowed
to the three genuinely-uncacheable sources so on-demand doc modules still work.

**b. The catalog line.** `JitContextResolver.resolve_for_prompt` emits
`- slug — summary` for on-demand modules without resolving them. The summary can
itself be sensitive, and the model will burn a turn fetching something it will be
refused. Filter on-demand modules through `visible_to()` before rendering catalog
lines. Add `visible_to` to the `ContextProvider` protocol with a `True` default so
the other three providers need no change.

**c. The size budget.** Today `_with_resolved_body` runs *before* `budget_pinned`,
so a resolved doc body counts against `PINNED_BUDGET_CHARS` (20,000).
`resolve_for_prompt` joins resolved bodies with no cap at all — so moving gdoc to
JIT would silently remove its budget and let one large document blow out every
prompt. **The JIT block needs its own budget**, mirroring `budget_pinned`'s
whole-module-drop behaviour and its warning log.

### 4. Data model — migration `0018`

Applied by hand against `chat_db`, per the repo's standing practice (merging the
SQL does not apply it).

```sql
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience        text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS doc_audience_set_by text;
ALTER TABLE knowledge_modules ADD COLUMN IF NOT EXISTS source_tab          text;

-- Safe default for anything that already exists.
UPDATE knowledge_modules SET doc_audience = 'acl_mirror'
    WHERE source = 'gdoc' AND doc_audience IS NULL;

-- A gdoc module stores no body. Today's constraint exempts only
-- graph/directory/episodic, forcing a stored body on exactly the source
-- that must not have one.
ALTER TABLE knowledge_modules DROP CONSTRAINT IF EXISTS knowledge_modules_body_required_chk;
ALTER TABLE knowledge_modules ADD CONSTRAINT knowledge_modules_body_required_chk
    CHECK (source IN ('gdoc', 'graph', 'directory', 'episodic') OR body IS NOT NULL);

ALTER TABLE knowledge_modules ADD CONSTRAINT knowledge_modules_doc_audience_chk
    CHECK ((source = 'gdoc' AND doc_audience IN ('acl_mirror', 'published'))
           OR (source <> 'gdoc' AND doc_audience IS NULL));

ALTER TABLE knowledge_modules ADD CONSTRAINT knowledge_modules_gdoc_ref_chk
    CHECK (source <> 'gdoc' OR source_ref IS NOT NULL);
```

`doc_audience_set_by` is separate from `updated_by` deliberately: `updated_by` is
clobbered by any later title edit, and the question worth answering later is "who
decided customers should see this document?"

**One source value, `gdoc`, covers both Docs and Sheets**, with the provider
branching on `mimeType` from the metadata call it already makes. A file's type is
a property of the file, not of our configuration — this way a Doc converted to a
Sheet keeps working. The UI labels it "Google Doc or Sheet".

Migration reports the count of pre-existing `source='gdoc'` rows. Any that exist
tighten from "everyone" to "ACL-gated" — a real behaviour change, though no code
path can currently create one, so the expected count is zero.

### 5. Sheets

New `fetch_google_sheet_markdown(file_id, tab)` in `shared/utils/gdrive_doc_fetcher.py`,
using `spreadsheets().values().get()` (tab-selective, unlike the first-tab-only
CSV export):

- Header row + data rows → markdown table.
- Defaults to the first tab when `source_tab` is null.
- Hard row and character caps, with an explicit
  `_(truncated: showing first 200 of 4,312 rows)_` footer — so neither the
  operator nor the model mistakes a cap for the whole table.

`/learn`'s `fetch_document` mime dispatch gains a spreadsheet branch alongside the
existing Doc and PDF branches.

### 6. Authoring surfaces

**Admin Context page** (`anansi_app/nicegui_app/pages/knowledge_modules.py`):

- New/Edit dialog gains a **Source** select: `Typed` | `Google Doc or Sheet`.
- Choosing Google reveals: URL/ID input, an optional **Tab** field (shown only
  once the file resolves as a spreadsheet), and an **Audience** radio —
  "Mirror the document's sharing" (default) / "Publish to everyone this prompt
  serves".
- Save validates that the operator can themselves read the file. You cannot
  attach a document you cannot open.
- **Preview renders the live resolved markdown** via `provider.resolve()` with a
  `ResolutionContext` built from the operator's real email — replacing the
  hardcoded `is_staff=True` at `knowledge_modules.py:238`. Preview and production
  become the same call, which is what makes the preview trustworthy rather than a
  second gate to get wrong.
- Attach-time warning when an `acl_mirror` module is pinned to `customer.system`:
  it will provably resolve to nothing for customers. Fail loudly at attach rather
  than silently at render.

**`/learn`** (`context_expert` workflow, defined in
`shared/prompts/library/experts.definitions.prompt:377`):

- `fetch_document` already extracts Drive ids and already runs `user_can_access`
  (`fetch_document.py:355`), so the entry point exists.
- When the input is a Drive link, ask: **link it live**, or copy the text in now?
  Live is the default.
- On the live path, **skip `improve_content`** — rewriting text that is discarded
  at render time is pure waste, and the rewrite would misrepresent the document.
- `store_module.build_module_payload` stops hardcoding `source='manual'`; it takes
  source / `source_ref` / `source_tab` / `doc_audience` from workflow state.
- Audience is asked during the approval step, defaulting to `acl_mirror`.

### 7. Scope: `sector` → `global`

`scope` is read in exactly one place — `select_for_prompt` → `RequestScope.matches`
(`shared/prompts/types.py:41`). It has **no** relationship to Telegram chats,
topics, or run routing; it only filters whether a module's text is included in a
render.

Current behaviour:

| Value | Effect |
|---|---|
| `sector` | Always included |
| `org:<id>` | Included when the caller's organization matches. Works. |
| `site:<name>` | **Dead.** Nothing populates `RequestScope.grid` — `instructions_provider` constructs `RequestScope(organization_id=...)` only, and `prepare_context.py:306` calls `_fetch_jit_context(prompt_id, user_context)` without the `grid` argument. Matches nothing, ever. |

Changes:

- Rename the stored value to `global` (consistent with `SCOPE_GLOBAL` in
  `shared/config/flag_registry.py`), update the column default, and backfill.
- `matches()` accepts **both** `sector` and `global`, permanently. It fails closed
  on an unknown scope, so a missed row would go silently dark.
- Replace the free-text scope input with a dropdown: **Everywhere** /
  **One organization** / **One grid** *(disabled — "not currently wired up")*,
  each with a one-line caption stating its effect. This is honest labelling of
  dead code rather than a text box that accepts `site:FOO` and yields a module
  that never fires.

## Out of scope

- **Reviving `site:` scope.** Dead for reasons unrelated to this work; deserves
  its own decision about whether grid should be threaded into `RequestScope`.
- **The prompts page's unchecked doc binding** (`prompts.py:373` writes a doc id
  with no access check). Same class of gap, different feature.
- **Admin SDK group expansion.** Would fix the `type: "group"` false-deny; needs
  new credentials and scopes.

## Risks

- **Drive outage fails closed**, so the assistant quietly knows less rather than
  leaking. Correct default, but log at WARNING per dropped module so it is
  diagnosable; never surface module slugs to customers.
- **Latency.** Up to one Drive call per module per message, against the resolver's
  existing 5s timeout and `asyncio.gather` parallelism. The access cache absorbs
  most of it; worth measuring before and after.
- **Group-granted staff hit false denies** until someone shares directly. The most
  likely support complaint from this change.
- **`published` is a real hole by design.** It is bounded by being explicit,
  defaulted off, attributable via `doc_audience_set_by`, and requiring the setter
  to have access themselves.

## Testing

- `strict=True` denies on the SA-reachable-but-no-explicit-permission case and
  still allows on explicit email match, `type: "anyone"`, and matching
  `type: "domain"`.
- A denied module drops from pinned context, from the on-demand catalog, and from
  `fetch_knowledge_module` — three separate assertions, since they are three
  separate gates.
- `published` resolves for a caller with no Drive access.
- The JIT budget drops whole modules and logs, matching `budget_pinned`.
- `matches()` accepts `sector` and `global` and still rejects unknown values.
- Sheet truncation emits the footer and respects the cap.
- Round-trip: a `/learn` live-link creates `source='gdoc'` with `source_ref` set,
  no stored body, and `doc_audience='acl_mirror'`.

Per `CONTRIBUTING.md`, new test files under any `tests/` directory need
`git add -f`, and `pre-commit run --all-files` must pass before pushing.
