# P1 — Resolvable Context Modules

**Date:** 2026-08-19
**Covers:** b.2 (Google Doc as module), b.3 (GraphRAG entity summary), b.4 (grids/orgs/users directory), d.1 (episodic per-grid/per-org distillation)
**Depends on:** nothing
**Umbrella:** `2026-08-19-context-architecture-design.md`

---

## Problem

`KnowledgeModule.body` is a `str` read straight from a DB row
(`shared/prompts/knowledge.py:26-38`), and `KnowledgeStore.all_modules()` selects
`id, slug, title, summary, body, tags, scope, mode` — never the `source` or
`source_ref` columns that already exist on the table.

Four requested features are the same shape: *a module whose body is computed at
render time under the caller's permissions.* None can be expressed today. Two of
them are currently implemented as hardcoded string builders that no operator can
see, reorder, or detach from a prompt:

- `ContextEnrichmentProvider` (`orchestrator/services/context_enrichment.py`) builds
  "Available grids: …", "Jira Ops team members: …", "Available JIRA organizations: …"
  and appends it to every request's context at `prepare_context.py:329`.
- Nothing at all exists for the GraphRAG entity graph or for episodic distillation.

## Approach

Keep one module list. Vary only how the body is produced, keyed on the existing
`knowledge_modules.source` column.

| `source` | body comes from | resolution | request |
|---|---|---|---|
| `manual` | the `body` column | static | b.1 (shipped) |
| `gdoc` | Google Doc named by `source_ref` | sync, TTL-cached | b.2 |
| `graph` | entity/relationship summary | async, permission-filtered | b.3 |
| `directory` | grids + organizations + users | async, permission-filtered | b.4 |
| `episodic` | stored distillation for `source_ref` | async, permission-filtered | d.1 |

Everything else — the admin list, per-prompt pinning via `prompt_knowledge_overrides`,
the pinned/on-demand tiers, `budget_pinned`, the `scope` gate, the
`get_knowledge_module` MCP tool — is unchanged and already works.

### The sync/async boundary — the one real design constraint

`PromptLibrary.render()` and `_compose_knowledge()` are **synchronous**
(`shared/prompts/core.py:150,199`). Graph, directory and episodic bodies require
async DB and API calls under the caller's identity. Making `render()` async ripples
through every caller of `PROMPTS.render`/`PROMPTS.text` across four services.

**Decision: split resolution by whether it can be done synchronously.**

- `manual` and `gdoc` resolve **inside `PromptLibrary`**. `gdoc` mirrors the
  synchronous TTL-cached `GDocStore.body_for` that already backs prompt-level doc
  overrides (`shared/prompts/gdoc.py`), so this adds no new I/O model.
- `graph`, `directory` and `episodic` resolve in a new **async `JitContextResolver`**
  in the orchestrator, which reads the *same* `prompt_knowledge_overrides` pins and
  emits its blocks into `context_message`.

This keeps `PromptLibrary` sync and pure, puts async I/O where async I/O already
lives, and requires no signature change to any existing caller.

**Cost of this split, stated plainly:** the composition of a single prompt's context
is now assembled in two places rather than one. To keep that honest, `JitContextResolver`
must read pins through `KnowledgeStore.overrides_for()` — the same call
`_compose_knowledge` uses — never through a parallel mechanism, and both must report
the slugs they used into the same `knowledge_used` provenance list.

Rejected alternative: make everything async. Cleaner conceptually, but it converts
`PROMPTS.text()` — used in dozens of sync call sites including scripts and MCP
servers — into an async API for the benefit of three module types.

## Data model

```sql
-- 0017_context_module_providers.sql
ALTER TABLE knowledge_modules
    DROP CONSTRAINT IF EXISTS knowledge_modules_source_chk;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_source_chk
        CHECK (source IN ('manual', 'gdoc', 'ingested', 'graph', 'directory', 'episodic'));

-- A provider-backed module stores no body. Existing rows are all 'manual'
-- or 'ingested' and unaffected.
ALTER TABLE knowledge_modules
    ALTER COLUMN body DROP NOT NULL;

ALTER TABLE knowledge_modules
    ADD CONSTRAINT knowledge_modules_body_required_chk
        CHECK (source IN ('graph', 'directory', 'episodic') OR body IS NOT NULL);
```

`KnowledgeModule` gains `source: str = "manual"` and `source_ref: Optional[str] = None`,
and `body` becomes `Optional[str]`. `all_modules()` selects both new columns.

Add a derived property, since three call sites need this test:

```python
@property
def is_jit(self) -> bool:
    return self.source in ("graph", "directory", "episodic")
```

**Migrations are applied by hand**, and the number here is indicative (see D0 in
the umbrella spec). Per `MEMORY.md`, merging a file into
`db/migrations/` does not apply it to production chat_db. Migration 0016 is still
unapplied and producing live PGRST204 errors. Confirm application before any
code depending on it deploys.

## Component design

### `shared/prompts/providers.py` (new)

```python
class ContextProvider(Protocol):
    source: str
    async def resolve(self, module: KnowledgeModule, ctx: ResolutionContext) -> Optional[str]: ...
```

`ResolutionContext` is a new frozen dataclass carrying what providers need for RLS —
deliberately separate from `RequestScope`, which carries only `grid` and
`organization_id` and is a *selection* input, not an *authorization* input:

```python
@dataclass(frozen=True)
class ResolutionContext:
    scope: RequestScope
    user_email: Optional[str] = None
    organization_ids: tuple[str, ...] = ()
    role_ids: tuple[str, ...] = ()
    is_staff: bool = False
```

Conflating the two would let a module's *visibility* rule silently become its
*authorization* rule. They are different questions and stay different types.

### `JitContextResolver` (new, orchestrator)

One method, called from `prepare_context`:

```python
async def resolve_for_prompt(prompt_id: str, ctx: ResolutionContext) -> tuple[str, list[str]]
```

It selects pinned modules with `select_for_prompt` (reused, unchanged), filters to
`m.is_jit`, dispatches each to its provider **concurrently via `asyncio.gather`**,
drops any that returned `None` or raised, and renders survivors into a
`# Live Context` block. Returns `(text, slugs_used)`.

On-demand JIT modules contribute a catalog line exactly as on-demand manual modules
do, and their body is fetched through `get_knowledge_module` — which means that tool
must also learn to resolve providers. See "MCP tool" below.

### The four providers

**`GDocProvider` (b.2)** — sync, lives in `PromptLibrary`. Resolves `source_ref` as
a Drive file id through the existing Google service-account credentials, exports as
Markdown, TTL-cached at 300s to match `GDocStore`. Supports Docs, Sheets (exported
as Markdown tables) and PDFs in Drive (text layer only). A doc that 404s or that the
service account cannot read logs a warning and contributes nothing.

**`GraphProvider` (b.3)** — async. This one has a hard constraint discovered during
exploration:

> `entities`, `relationships`, `entity_mentions` and `relationship_evidence` have
> **no permission columns of any kind** (`db/schema/chat_db.sql:373-427`). The only
> path to row-level permission is
> `entities → entity_mentions → documents.allowed_organization_ids`.
> The `search_entities` / `get_entity_graph` RPCs that would have helped were dropped
> as never-wired-up dead code on 2026-07-11, along with the ivfflat index on
> `entities.embedding`.

So the provider needs a new RPC:

```sql
CREATE OR REPLACE FUNCTION summarize_entity_graph(
    p_org_ids   uuid[],
    p_max_types int DEFAULT 20,
    p_max_edges int DEFAULT 40
) RETURNS TABLE (entity_type text, entity_count bigint, relationship_type text, edge_count bigint)
```

which aggregates entity types and relationship types over only those entities having
at least one `entity_mention` in a document the caller's orgs may read. Staff
(`is_staff`) pass `NULL` to mean unrestricted, matching the existing
`search_chunks_with_permissions` convention.

The rendered body is an **ontology primer**, not a data dump: entity types with
counts, relationship types with counts, and a handful of high-degree example entity
names per type. Target under 1,500 chars. This is deliberately the same artifact P4
puts in front of the agentic graph tools — build it once here, reuse it there.

**`DirectoryProvider` (b.4)** — async. Replaces `ContextEnrichmentProvider` as a
context source. Its grid and Jira-organization logic moves across essentially
unchanged, including the existing module-level TTL cache, which must be re-keyed to
include the permission set per D3.

*Assumption flagged for confirmation:* the request says "grids, organizations and
users". Today's code emits grids, Jira **schedule participants** (not users), and
Jira **organizations** (not auth_db organizations). This spec reads "users" as the
staff directory currently sourced from `jira_get_schedule_participants`, plus
organization members from `auth_db.members` where the caller's permissions admit
them. If "users" was meant to include customer end-users, say so — that is a
materially larger surface with real privacy implications and should be its own
decision.

**`EpisodicProvider` (d.1)** — async, read-only. Reads a stored distillation; it does
not generate one. Generation is a scheduled batch job (below).

```sql
-- 0018_episodic_distillations.sql
CREATE TABLE IF NOT EXISTS episodic_distillations (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    anchor_type    text NOT NULL,   -- 'grid' | 'organization'
    anchor_id      text NOT NULL,
    anchor_name    text,
    summary        text NOT NULL,
    message_count  integer NOT NULL DEFAULT 0,
    covers_from    timestamptz,
    covers_to      timestamptz,
    generated_at   timestamptz NOT NULL DEFAULT now(),
    edited_by      text,
    CONSTRAINT episodic_anchor_type_chk CHECK (anchor_type IN ('grid', 'organization')),
    UNIQUE (anchor_type, anchor_id)
);
```

A nightly job distills recent `chat_messages` per anchor into `summary`, reusing
`entity_fanout.py`'s existing "every eligible grid / organization" enumeration rather
than adding a fifth scheduler — the same decision 0013 made for skill scheduling.
`edited_by` exists so an operator can correct a distillation; a hand-edited row is
not overwritten by the next run.

The provider selects the distillation matching `ctx.scope.grid` or
`ctx.scope.organization_id`, and returns nothing when the scope names neither. This
is the one provider whose output is empty for most requests, by design.

## MCP tool

`get_knowledge_module` (`mcp_servers/servers/knowledge_server/`) currently returns
`body` from the row. For a JIT module that column is NULL. The tool must resolve
through the same providers, using the calling identity already available to the MCP
server for permission filtering, and return a clear error for a slug whose provider
fails rather than an empty body.

Per `MEMORY.md`: after editing `tool_schemas.py`, regenerate
`mcp_servers/tool_definitions.json` via `export_tools.py` — that JSON is what
production actually serves — and verify no server was silently dropped.

## Admin UI

`anansi_app/nicegui_app/pages/knowledge_modules.py`:

- Rows show a source badge alongside the existing `slug · scope · mode · chars` line.
  Character count is meaningless for JIT modules; show `live` instead.
- The edit dialog's body textarea is **read-only and greyed** for any non-`manual`
  source, with a one-line explanation of where the content comes from. Title,
  summary, scope, mode, tags and prompt pins stay editable — this is what makes a JIT
  module "a placeholder to allow attaching to different prompts".
- Delete is disabled for `graph` and `directory` (there is exactly one of each, and
  deleting it just makes the capability unreachable). `gdoc` and `episodic` rows are
  deletable.
- A **Preview** button resolves the module against the *current operator's*
  permissions and shows the result. Without this an operator has no way to know what
  a JIT module actually contributes, which would make the whole feature unauditable.

## Seeding

The `graph` and `directory` modules are singletons created by an idempotent seed
script (mirroring `scripts/seed_knowledge_modules.py`), pinned to no prompts
initially. Attaching them is a deliberate operator action.

`DirectoryProvider` reaching parity is what allows `_fetch_enrichment` to be deleted
from `prepare_context.py`. Do not delete it in the same change that adds the
provider — ship the provider, attach the module, confirm parity in production, then
remove the hardcoded path in a follow-up.

## Failure modes

| failure | behaviour |
|---|---|
| provider raises | log warning, module contributes nothing, prompt still renders |
| provider slow | 5s per-provider timeout, treated as empty |
| gdoc unreachable / 404 | log, contribute nothing |
| graph RPC missing (0017 unapplied) | log, contribute nothing |
| no distillation for this grid | contribute nothing — normal, not an error |
| all providers fail | prompt renders exactly as it does today |

Every case is fail-open per D5. There is no path where a provider failure blocks a
response.

## Testing

- **Pure functions, no DB:** provider dispatch on `source`, `is_jit`, budget
  interaction when a JIT body resolves larger than expected, `ResolutionContext`
  construction from a `UserContext`.
- **Permission, the load-bearing tests:** a `graph` module resolved for org A must
  not surface entity types whose only mentions live in org B's documents. Same for
  `directory` and `episodic`. Each needs a fixture with two orgs and a deliberate
  cross-org entity.
- **Fail-open:** each provider raising, timing out, and returning `None` — assert the
  prompt still renders and other modules are unaffected.
- **Parity:** `DirectoryProvider` output vs `ContextEnrichmentProvider._format_enrichment_text`
  for the same inputs, so the deletion is evidence-backed.

Per `CLAUDE.md`: new files under any `tests/` directory need `git add -f`, and
`pre-commit run --all-files` must be clean before claiming done — a plain `git add`
on a new test file is a silent no-op.

Per `MEMORY.md`: do not import the `shared.prompts.PROMPTS` singleton in tests that
need deterministic bundled content. Construct a bare `PromptLibrary()`, or
monkeypatch `_db_body_for`/`_gdoc_body_for` to `None`.

## Sequencing

1. Migration 0017 + model changes (`source`/`source_ref`/`is_jit`), no providers yet — ships green, changes nothing observable.
2. `ContextProvider` protocol, `ResolutionContext`, `JitContextResolver` with zero registered providers — wired into `prepare_context`, still a no-op.
3. `DirectoryProvider` + seed + parity tests. Attach in production. Verify. Then delete `_fetch_enrichment`.
4. `GDocProvider` — unblocks P2's live-linked procedures.
5. `GraphProvider` + `summarize_entity_graph` RPC (its own migration). Hand off the primer format to P4.
6. `EpisodicProvider` + `episodic_distillations` table (0018) + nightly job.

Each step ends green and is independently shippable.
