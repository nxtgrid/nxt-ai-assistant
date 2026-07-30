# Prompt Library & Composable Knowledge — Design

**Status:** draft
**Date:** 2026-07-30

## Problem

Anansi's prompts live in four unrelated places, are loaded by five hand-rolled
parsers, and have no version identity. Nobody can answer "which prompt produced
this answer", roll back a bad edit, review a prompt change in a PR, or grant one
teammate the right to edit customer tone without also granting the right to edit
the alert-correlation policy.

### Inventory

| Kind | Where | Count |
|---|---|---|
| Google Doc, addressed by env-var id | `CUSTOMER_SUPPORT_DOC_ID`, `STAFF_SUPPORT_DOC_ID`, `EXPERT_INSTRUCTIONS_DOC_ID`, `TROUBLESHOOTING_PROCEDURES_DOC_ID`, `VERIFICATION_DOC_ID` | 5 |
| Bundled markdown fallback | `chat_orchestrator/instructions/*.md` | 4 files, 99 KB |
| Prompt text held in an env var | `GRAFANA_PANEL_DESCRIPTION_PROMPT` | 1 |
| Literal at the call site | ingestion expert (5), `verification_service`, `context_filter`, `conversation_summarizer`, `thread_assignment`, `intent_router`, `correlator`, `procedure_provider`, `doc_editing`, `knowledge_mcp_server`, `jira_issue_types`, `process_doc_edits`, `gtr_analysis_conversation` | ~20 |

### Evidence

**The load-and-parse logic is written three times.**
`_load_fallback_instructions` (`instructions_provider.py:40`),
`_load_fallback_expert_instructions` (`expert_instructions_provider.py:57`), and
`get_correlation_instructions` (`correlation_rules.py:57`, which imports the
first one across a service boundary). Each re-implements "strip HTML comments,
split on `# ` headings, lowercase-and-underscore the key".

**Three caches, three lifetimes.** `_GDOC_CACHE_TTL = 3600`
(`artifacts_provider.py:29`), `CACHE_TTL_SECONDS = 3600`
(`expert_instructions_provider.py:92`), and `ProcedureProvider._cached_procedures`
(`procedure_provider.py:52`) which never expires — only an explicit
`clear_cache()` call reloads it, and nothing calls it on a schedule.

**The customer and staff loaders are near-duplicates.**
`get_customer_instructions` (`instructions_provider.py:325-380`) and
`_get_staff_instructions_from_doc` (`instructions_provider.py:485-540`) share
~55 lines of identical section-composition, examples-truncation and
context-truncation logic, including two separate copies of
`MAX_EXAMPLES_WORDS = 5000`. They have already drifted: the customer path
re-raises on a missing `System Instructions` section (`:323`), the staff path
logs a warning and silently substitutes `_default_instructions["general"]`
(`:554`) — a generic "You are a helpful AI assistant" that mentions no
mini-grid context at all.

**Truncation is silent and mid-document.** `instructions_provider.py:369` cuts
`context_message` at `MAX_CONTEXT_CHARS` and appends a marker. Whatever fell off
the end — often the most recently added knowledge — is gone with a single
WARNING line.

**Three of five doc ids cannot be set from the settings UI.**
`CUSTOMER_SUPPORT_DOC_ID`, `STAFF_SUPPORT_DOC_ID` and
`TROUBLESHOOTING_PROCEDURES_DOC_ID` are registered `editable=False`
(`flag_registry.py:1151-1174`).

**No version identity.** Google Docs keeps revision history, but the running bot
cannot report which revision it used, cannot pin a known-good revision, and a
doc edit never appears in a PR diff. On fetch failure the bundled file — which
may be many months stale — is substituted at WARNING level and the request
proceeds.

**The right distinction already exists but is invisible.**
`correlation_rules.py:1-7` deliberately refuses a doc override: *"Deployments
may disable correlation with the kill switch, but cannot silently substitute
different grouping rules, confidence bounds, or prompt limits."* That
policy-versus-content line is correct and is expressed nowhere else.

**`bot_artifacts` was built for this and is dormant.** `db/schema/chat_db.sql:288`
defines a table with `artifact_type`, `bot_mode`, `version`, `is_active`,
`tags`, `priority` and sync-source columns, described in its own comment as
"stores versioned system instructions... If you use Google Docs for
instructions, this table can be empty."

### The knowledge problem

Sector and site knowledge (how power availability affects comms on a mini-grid,
per-site peculiarities, troubleshooting lore) currently reaches the model by one
of two routes, and neither is right:

- **Pasted into a Google Doc**, where it is included in full on every request,
  grows without bound, and cannot be targeted at some prompts and not others.
- **Ingested into the RAG index** (`documents`/`chunks`, `chat_db.sql:317`),
  where it is retrieved by embedding similarity — which is unreliable when the
  requirement is "always consider this when diagnosing site ABC".

There is no middle tier: a curated, named, addressable knowledge module that a
specific prompt can deliberately pin.

## Goals

1. One prompt library, one loader, one resolution order, one cache policy.
2. Every prompt is editable by a non-engineer, live, without a redeploy.
3. Every rendered prompt carries provenance — id, source, version, checksum —
   into logs and traces.
4. Rollback is one operation, and reverting to the shipped default is always
   available.
5. Prompts that must not be edited live stay uneditable, by construction.
6. Technical knowledge is composed into prompts by declaration, not by paste,
   and the long tail is reachable without being resident in context.
7. Edit and publish rights are grantable per prompt, per group.

## Non-goals

- Replacing the RAG index. Knowledge modules sit beside it, not over it.
- Adopting an external prompt-management SaaS (see "Rejected alternatives").
- Migrating the app's other four permission whitelists to a database.
- Prompt A/B testing or traffic splitting. Labels leave room for it; nothing
  in this design implements it.

## Decisions taken

| Question | Decision |
|---|---|
| Who edits, and how? | Non-engineers, live, no redeploy. Repo holds the versioned default; a DB override layer holds live edits. |
| Edit surface? | In-app Prompts page. Google Docs demoted to an optional override/import source behind one adapter. |
| Which prompts enter the library? | All of them. Overridability is declared per prompt. |
| How does knowledge attach? | Tagged modules with two tiers — pinned, and on-demand via progressive disclosure. |
| Where does group membership live? | Env-var whitelists, extending `perms.py`. Per-prompt bindings are DB-backed and UI-editable. |

## Design

### 1. Prompt file format

Prompt defaults ship as files under `shared/prompts/library/`, one per prompt,
named `<id>.prompt`. `shared/` rather than `chat_orchestrator/` because
`mcp_servers`, `anansi_app` and `rag_pipeline` all carry prompts too.

The format follows the [Dotprompt](https://github.com/google/dotprompt)
convention — YAML frontmatter, then a markdown body — without adopting its
Handlebars runtime. The repo has no templating engine today; taking the file
shape now costs nothing and leaves the door open to the full spec later.

```yaml
---
id: customer.system
description: Customer-mode system instructions for Telegram support.
owner: ops
overridable: true
output: text                  # `json` enables schema validation on save
schema: null                  # JSON Schema, required when output: json
model: null                   # optional per-prompt model override
variables: [user_name, grid_name]
sections: [system_instructions, examples]
knowledge_tags: [grid_ops, comms, troubleshooting]
access:
  view:    [ops, eng]
  edit:    [ops]
  publish: [eng]
---
You are ...
```

Body templating is limited on purpose:

- `{{variable}}` — substituted from the `vars` dict. Every placeholder must be
  declared in `variables`; an undeclared placeholder or a missing value is an
  error at render time, not a silently empty string.
- `{{> partial_id}}` — inlines another library entry whose id starts with
  `partials.`. This is the composability primitive that lets the shared blocks
  currently duplicated between the customer and staff docs live in one place.
  Includes are resolved depth-first with a cycle check and a depth cap of 3.

`sections` preserves the existing two-channel split: named sections of the body
route to the provider's system-instruction channel versus the first user
message, replacing the implicit "everything that isn't `System Instructions`
becomes a context message" rule that is currently re-derived in two places.

### 2. The loader

A single service, `shared/prompts/library.py`:

```python
rendered = PROMPTS.render("customer.system", vars={...}, scope=RequestScope(grid="ABC"))
# RenderedPrompt(system_text, context_text, source, version, checksum, knowledge_used)
```

Resolution order per id:

1. **DB override** — the version carrying the `production` label, if the prompt
   is `overridable`.
2. **Attached Google Doc** — if the prompt has a doc binding, fetched through
   one adapter shared by all prompts.
3. **Bundled `.prompt` file** — always present, always the floor.

Rules:

- The bundled file is the schema authority. Frontmatter always comes from it;
  an override supplies body text only. This makes it impossible for a UI edit
  to change a prompt's `overridable` flag, output schema or access lists.
- Every resolution returns provenance. It is logged on the LLM call and stamped
  onto the Langfuse trace (`langfuse_utils.update_generation` already exists and
  is called from both gateways), so prompt version to output linkage lands in
  the observability tool already running.
- If the DB is unreachable, resolution falls through to the doc and then the
  bundled file, at WARNING, with `source` recorded accurately. Rendering never
  fails because a database is down.

**Cache coherence across processes.** The admin app and the bot run as separate
services, so a save in one must be seen by the other. Two-level cache:

- Label table — one query returning `(prompt_id, version, checksum)` for every
  prompt, polled on a 60-second TTL. Small and constant-size.
- Body cache — content-addressed by checksum, effectively immutable, no TTL.

A save therefore becomes visible within 60 seconds without a restart, and the
common case costs one cheap query per minute per process. The Prompts page
offers an explicit "reload now" that bumps a cache epoch for operators who do
not want to wait.

### 3. Storage

New tables. `bot_artifacts` is left alone (and can be dropped once confirmed
unused): its `bot_mode` enum does not fit prompts like `intent_router`, its
`google_sheets_*` columns are dead weight here, and its mutable `is_active` flag
is the mutable-in-place model this design is specifically avoiding.

```sql
CREATE TABLE prompt_versions (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    prompt_id    text NOT NULL,
    version      integer NOT NULL,
    body         text NOT NULL,
    checksum     text NOT NULL,
    note         text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    created_by   text NOT NULL,
    created_via  text NOT NULL DEFAULT 'ui',   -- 'ui' | 'api' | 'import'
    UNIQUE (prompt_id, version)
);

CREATE TABLE prompt_labels (
    prompt_id    text NOT NULL,
    label        text NOT NULL,               -- 'production'
    version      integer NOT NULL,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text NOT NULL,
    PRIMARY KEY (prompt_id, label)
);

CREATE TABLE prompt_doc_bindings (
    prompt_id    text PRIMARY KEY,
    doc_id       text NOT NULL,
    last_synced_at timestamptz
);
```

`prompt_versions` is append-only; there is no UPDATE path. Rollback repoints a
label. Revert-to-default deletes the label row, which drops resolution back to
the bundled file. Both are single statements and both are auditable.

### 4. Knowledge modules

```sql
CREATE TABLE knowledge_modules (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug         text NOT NULL UNIQUE,
    title        text NOT NULL,
    summary      text NOT NULL,          -- the one-liner in the on-demand catalog
    body         text NOT NULL,
    tags         text[] NOT NULL DEFAULT '{}',
    scope        text NOT NULL DEFAULT 'sector',   -- 'sector' | 'site:<name>' | 'org:<id>'
    mode         text NOT NULL DEFAULT 'pinned',   -- 'pinned' | 'on_demand'
    source       text NOT NULL DEFAULT 'manual',   -- 'manual' | 'gdoc' | 'ingested'
    source_ref   text,
    edit_groups  text[] NOT NULL DEFAULT '{}',
    version      integer NOT NULL DEFAULT 1,
    is_active    boolean NOT NULL DEFAULT true,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    updated_by   text
);

CREATE TABLE prompt_knowledge_overrides (
    prompt_id    text NOT NULL,
    module_id    uuid NOT NULL REFERENCES knowledge_modules (id) ON DELETE CASCADE,
    pinned       boolean NOT NULL,        -- true = force on, false = force off
    updated_by   text,
    PRIMARY KEY (prompt_id, module_id)
);
```

**Resolution.** For a prompt with `knowledge_tags: [grid_ops, comms]` rendered
under `scope=site:ABC`:

1. Select active modules whose `tags` intersect the prompt's `knowledge_tags`
   **and** whose `scope` is `sector` or matches the request scope.
2. Apply `prompt_knowledge_overrides` — explicit `pinned=true` adds a module the
   tags did not select; `pinned=false` removes one they did.
3. Partition by `mode`.

**Pinned** module bodies are concatenated into the context channel under a
`# Technical Knowledge` heading, ordered by scope specificity (site before
sector) so the most specific material survives if the budget binds. The budget
is enforced by dropping whole modules, never by cutting mid-document, and every
drop is logged by slug at WARNING with the overflow size. This replaces the
current silent mid-string truncation.

**On-demand** modules contribute only `slug`, `title` and `summary` to a compact
catalog block. The knowledge MCP server gains `get_knowledge_module(slug)` so
the model fetches a body when it decides it needs one. This is the progressive
disclosure pattern: the resident cost of a module is one line, and its body
enters the window only for the turns that need it — which matters because an
agentic loop re-sends its window every step.

Google Docs remain usable here. A module with `source='gdoc'` and a `source_ref`
is refreshed on a schedule, preserving the existing Drive-embedding workflow
while making the content addressable, taggable and individually attachable.

The tag-and-scope indirection is the point. Attaching knowledge by per-prompt
checkbox over a flat list is O(prompts x modules) to maintain — every new
document means revisiting every prompt. Tags make it O(prompts + modules), and
the checkbox UI is preserved as a derived, overridable view of the match.

### 5. Access control

Group membership extends the existing whitelist model in
`anansi_app/grid_app/lib/perms.py` — three new env vars parsed by the same
tolerant `_parse`, re-read on every call so changes take effect without a
restart:

```
PROMPT_EDITORS_OPS
PROMPT_EDITORS_ENG
PROMPT_ADMINS
```

Bindings are per prompt, defaulted in frontmatter, overridable in the DB and
editable in the UI. Three verbs:

| Verb | Grants |
|---|---|
| `view` | See the prompt body, its history and its resolved knowledge |
| `edit` | Create a new version. A draft; not live |
| `publish` | Move the `production` label — make a version live |

New functions in `perms.py`, following the existing per-resource `can_edit`
shape:

```python
def can_view_prompt(prompt_id: str, email: str | None = None) -> bool
def can_edit_prompt(prompt_id: str, email: str | None = None) -> bool
def can_publish_prompt(prompt_id: str, email: str | None = None) -> bool
```

#### Precedence rules

These are stated explicitly because each is a plausible bug:

1. **`admin` is implicit and never listed.** Members of `PROMPT_ADMINS` pass
   every verb on every prompt. Requiring `admin` to appear in each frontmatter
   `access` block would eventually lock everyone out of a prompt via one bad
   edit.

2. **`overridable: false` beats every grant, including admin.** A PR-only
   prompt is PR-only. Admin's universal access means universal *`view`* there.
   Without this rule, "admins have access to all prompts" quietly reopens the
   door `correlation_rules.py` deliberately closed.

3. **Access control governs the admin UI and the write API only. It never
   touches the render path.** `perms.py` fails closed — an unset whitelist
   grants nobody anything — which is correct for editing and catastrophic for
   `PROMPTS.render()`, which serves users who are not logged into the admin app
   at all. These must not share a code path.

4. **Editing a module's content is governed by that module's `edit_groups`.
   Pinning or unpinning a module on a prompt is governed by that prompt's
   `edit` verb.** Pinning changes what a prompt sends, so it must not be a back
   door around the prompt's own permissions.

Denials are logged with email, prompt id and verb.

#### Starting classification

`overridable: false` at introduction — visible and diffable in the UI, editable
only by PR:

- `ticketing.correlator.*` — the policy `correlation_rules.py` already protects
- `verification.*` — the LLM-as-judge that gates customer-facing output
- `ingestion.classify_document`, `ingestion.detect_contradictions`,
  `ingestion.detect_duplicates`, `ingestion.extract_entities` — JSON output
  consumed by parsers
- `intent_router.*`, `thread_assignment.*`, `command_parser.*` — structured
  routing output

Everything else starts `overridable: true`.

### 6. Admin UI

A **Prompts** page in the NiceGUI admin app, gated as the other admin pages are
(`perms.can_view_bot_admin`) plus the per-prompt `view` verb:

- **List** — id, description, owner, source badge (Default / Overridden /
  Google Doc), last edited by and when, an overridable indicator, and search.
- **Detail** — editor; live diff against the shipped default; declared-variable
  checklist; JSON Schema validation on save when `output: json`; version history
  with restore; revert-to-default. Save and Publish are separate actions, gated
  by `edit` and `publish` respectively; a user with `edit` but not `publish`
  leaves a draft version and the page says so.
- **Knowledge tab** — the resolved module list as checkboxes, showing which are
  tag-derived versus explicitly overridden, pinned versus on-demand, with a
  running token estimate against the pinned budget.

A separate **Knowledge Modules** page provides CRUD, tags, scope, mode and
Google Doc attachment, gated by `edit_groups`.

### 7. Programmatic writes

The library exposes a write API so the backend — or a later self-improvement
loop — can author versions:

```python
PROMPTS.propose(prompt_id, body, note, actor)  -> version
PROMPTS.publish(prompt_id, version, actor)     -> None
```

`propose` records `created_via='api'`. **Backend callers may propose but never
publish**: automated writes create a version and stop, and a human with
`publish` promotes it. This is the eval gate the industry consensus treats as
the difference between a prompt registry and a config store, and it is far
cheaper to enforce from the start than to retrofit.

## Rejected alternatives

**Adopt Langfuse prompt management.** Langfuse is already a dependency
(`shared/utils/langfuse_utils.py`, called from both LLM gateways behind
`LANGFUSE_ENABLED`), and its prompt product would supply versions, labels,
composability and a playground for free. Rejected as the primary store because
it does not offer per-prompt group ACLs — its RBAC is project-scoped — it puts
the editing surface in a second application rather than the admin app operators
already use, it has no place for the knowledge-module tagging layer, and it adds
a runtime fetch on the serving path. Its concepts are adopted wholesale
(immutable versions, labels, fallback-to-bundled, composability), and its
tracing is used to carry prompt provenance.

**Keep Google Docs as the only surface.** Least migration, but preserves every
failure mode: no rollback, no diff in review, no validation, no version
identity, and prompt state living outside the product.

**Retrofit `bot_artifacts`.** Its shape is close but its mutable `is_active`
model is exactly the config-store pattern this design rejects, and its
`bot_mode` enum does not cover machinery prompts.

## Migration

| Phase | Content | Ships |
|---|---|---|
| 1 | Library, loader, bundled `.prompt` files, all call sites converted. Behavior identical; Google Doc overrides still honored through the new adapter. No UI. | Nothing user-visible |
| 2 | `prompt_versions` / `prompt_labels`, resolution order, provenance logging + Langfuse stamping, Prompts page, access control | Live editing, rollback, per-prompt grants |
| 3 | `knowledge_modules`, tags, scope, pinned tier, budget enforcement, Knowledge Modules page | Composable knowledge |
| 4 | On-demand tier, catalog block, `get_knowledge_module` MCP tool | Long-tail knowledge without context cost |
| 5 | Golden-set eval gate — ~30 recorded conversations replayed on publish, blocking promotion on regression | Safe self-service editing |

**Phase 1 is the risk.** It touches every LLM call site, is a pure refactor, and
delivers nothing a user can see — the classic profile for a change that gets
abandoned half-done, leaving a sixth mechanism beside the five that exist. It
should ship complete or not start. Phases 2-5 are independently valuable and can
stop at any point.

Access control ships in Phase 2 rather than later: introducing live editing
without it means a window in which anyone with admin-app access can edit any
prompt.

## Testing

- **Parity harness, Phase 1.** For each of the ~30 prompts, assert the string
  produced by the new loader is byte-identical to the string produced by the old
  path, given the same inputs. This is the only real defense for a refactor of
  this shape.
- Resolution order: DB over doc over bundled; DB unreachable falls through;
  `overridable: false` ignores a DB row if one exists.
- Frontmatter always sourced from the bundled file, never from an override.
- Undeclared `{{placeholder}}` and missing variable both raise.
- Partial cycle detection and depth cap.
- Knowledge selection: tag intersection, scope matching, override add and
  remove, budget overflow drops whole modules in specificity order.
- Access: each verb against each group; admin bypass; `overridable: false`
  precedence over admin; render path unaffected by empty whitelists.
- Cache: a version published in one process is visible in another within TTL.

New test files under any `tests/` directory need `git add -f` — the repo's
`.gitignore` denies `tests/` by default, a plain `git add` is a silent no-op,
and the gap only surfaces in `pre-commit run --all-files`. See `CLAUDE.md`.

## Open questions

1. **Should `staging` labels ship in Phase 2 or wait?** The schema supports
   them. Shipping only `production` first keeps the UI simpler; adding
   `staging` later is additive.
2. **Google Doc refresh cadence for `source='gdoc'` knowledge modules** —
   scheduled poll, or manual "sync now" only. Poll costs Drive API quota against
   a doc set that changes rarely.
3. **Do MCP server processes get DB access for overrides**, or resolve
   bundled-only? Bundled-only is simpler and safe; it means a prompt edit does
   not reach MCP-hosted prompts (currently just `knowledge_mcp_server`).
