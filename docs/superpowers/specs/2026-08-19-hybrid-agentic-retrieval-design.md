# P4 — Hybrid and Agentic Retrieval

**Date:** 2026-08-19
**Covers:** the RAG rework — heuristic → agentic, under the main LLM's agency
**Depends on:** shares the ontology primer with P1's `GraphProvider` (b.3)
**Scope decision:** hybrid + agentic graph tools. Multimodal/layout-aware ingestion is explicitly out.
**Umbrella:** `2026-08-19-context-architecture-design.md`

---

## Phase 0 — retrieval is currently returning nothing

Before any of this: `RAGProvider.retrieve` calls `search_chunks_with_permissions`
with `match_threshold` / `user_role_ids` / `user_org_ids`
(`orchestrator/services/rag_provider.py:161-171`). The committed function signature
is `(query_embedding, p_organization_id integer, match_count, similarity_threshold)`
(`db/schema/chat_db.sql:709`). The call raises. The `except` falls back to
`match_rag_documents`, **which is not defined anywhere in `db/`**. That raises too,
the outer `except Exception` returns `[]`, and `_fetch_rag_context` logs a warning
and continues (`prepare_context.py:161-166`).

The result is that `# Relevant Knowledge Base Context` has been absent from every
request for an unknown period. This was written down in the 2026-08-05 plan doc at
line 2579 and never picked up.

**Do this first, and do it independently of the rest of P4:**

1. Confirm the *live* signature — the committed schema may be stale relative to
   production:
   ```sql
   SELECT p.proname, pg_get_function_identity_arguments(p.oid)
   FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
   WHERE p.proname IN ('search_chunks_with_permissions', 'match_rag_documents');
   ```
2. Reconcile caller and function. Prefer changing the caller if the live function
   matches the committed definition.
3. **Delete the `match_rag_documents` fallback entirely.** A fallback to an
   unfiltered function is a permission-bypass waiting to start working. If the
   permission-filtered path fails, retrieval must return nothing and log loudly —
   never silently widen access.
4. Add a test asserting the RPC is called with argument names the function actually
   declares, so this cannot regress silently again.

**Nothing downstream should be judged before this ships.** Any read on retrieval
quality, corpus coverage, or whether hybrid search is needed is currently a read on
an empty result set.

## Phase 1 — hybrid search

### Why

The corpus is technical: part numbers, serial codes, error codes, acronyms —
`QH611A`, `E-402`, `RP1000`. Dense vectors are poor at exact token match; a query
for `E-402` retrieves chunks that are *about errors* rather than the chunk containing
`E-402`. There is no full-text index anywhere in the schema today — `tsvector` does
not appear in it.

### Design

```sql
-- 0020_chunks_fulltext.sql
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING gin (content_tsv);
```

A generated column backfills on creation and stays correct without ingestion
changes. On a large `chunks` table the `ALTER` rewrites — check row count first and
schedule accordingly.

Then one RPC that runs both rankers and fuses with Reciprocal Rank Fusion:

```sql
CREATE OR REPLACE FUNCTION search_chunks_hybrid(
    query_embedding vector(768),
    query_text      text,
    p_org_ids       uuid[] DEFAULT NULL,
    match_count     int    DEFAULT 10,
    rrf_k           int    DEFAULT 60
) RETURNS TABLE (id uuid, document_id uuid, content text, score float, metadata jsonb)
```

RRF scores each document `sum(1 / (rrf_k + rank_r))` across rankers. It needs no
score normalisation between cosine similarity and `ts_rank`, which is exactly why it
is the standard choice — the two are not on comparable scales and any weighted blend
would need retuning per corpus.

Permission filtering joins `documents.allowed_organization_ids` identically to the
corrected `search_chunks_with_permissions`. `NULL` org ids means staff/unrestricted,
matching the existing convention.

### Threshold

The current `match_threshold: 0.7` is high for cosine similarity on a 768-dim
embedding and would have been discarding legitimate matches — had any been returned.
Hybrid retrieval ranks rather than thresholds; take top-k from the fusion and drop
the similarity floor. Keep a low floor (0.3) only to bound the candidate set.

## Phase 2 — agentic graph tools

### The constraint that shapes this

The request is explicit: keep agency with the main LLM. No separate agentic-RAG
orchestration loop. So this phase adds **tools and a primer**, not a controller.

Retrieval capability becomes something the model reaches for mid-reasoning, the way
it already reaches for `get_knowledge_module`.

### The primer — shared with P1

The model cannot ask for entity types it does not know exist. A compact ontology
description in context is the cheapest high-leverage move here: entity types with
counts, relationship types with counts, a few high-degree example entities per type.

**This is the same artifact P1's `GraphProvider` renders.** Build it once, in P1, as
a `source='graph'` context module an operator attaches to whichever prompts should
have it. P4 consumes it rather than duplicating it. If P4 ships first, build the
primer here in the shape P1 specifies (`summarize_entity_graph` RPC) so P1 can adopt
it unchanged.

### Tools

Added to the existing `knowledge_server` (`mcp_servers/servers/knowledge_server/`),
alongside `summarize_knowledge` and `get_knowledge_module`:

| tool | purpose |
|---|---|
| `get_graph_schema()` | entity types, relationship types, counts — the whole ontology compactly |
| `search_entities(text, type_filter?)` | fuzzy entry point; resolves a name to real entity ids |
| `get_entity_neighbors(entity_id, relationship_type?, limit?)` | one-hop traversal with edge descriptions |
| `get_entity_evidence(entity_id, limit?)` | the `entity_mentions` / `relationship_evidence` chunks behind an entity — how a claim gets grounded |

A typical trajectory the tool descriptions should make obvious:
`get_graph_schema()` → `search_entities("DCU")` → `get_entity_neighbors(<id>, "connected_to")`
→ `get_entity_evidence(<id>)` → answer.

### Permissions — the same hard constraint as P1

`entities`, `relationships`, `entity_mentions` and `relationship_evidence` carry
**no permission columns whatsoever** (`db/schema/chat_db.sql:373-427`). Every one of
these tools must filter through
`entities → entity_mentions → documents.allowed_organization_ids`. An entity whose
only mentions live in another org's documents must not appear in `search_entities`,
must not appear as a neighbour, and must not be nameable in an error message.

That last one is the subtle failure: a "did you mean 'Author'?" suggestion built from
unfiltered entity names leaks the existence of entities the caller cannot see. Build
suggestions from the caller's already-filtered candidate set only.

`entities.embedding` exists but its ivfflat index was dropped on 2026-07-11 as
unused. `search_entities` should start as trigram/`ILIKE` matching on `name` — the
entity count is in the hundreds, not millions. Re-add the vector index only if
name matching proves insufficient.

### Errors are part of the interface

An agent self-corrects from a good error and gives up on a bad one. Every tool
returns actionable failures:

```
No entity type 'Meter_Device'. Valid types: Meter, DCU, BaseStation, Grid, Inverter.
```

```
No entity matching 'DCU-7712'. Closest by name: DCU-7721, DCU-7112.
```

Never a bare empty list, and never a stack trace. This is the cheapest correctness
mechanism in the whole phase.

## What happens to the automatic prefetch

`_fetch_rag_context` runs on every message with the raw user input as the query
(`prepare_context.py:141-166`). That is the "heuristic" half being replaced.

**Recommendation: keep it, make it hybrid, and stop pretending it is sufficient.**

- Automatic hybrid prefetch stays as a baseline — one query, cheap, gives the model
  something to work from without a round trip. Removing it entirely bets the whole
  system on the model reliably choosing to search, and the model has never had a
  working search to learn that habit from.
- Tools handle depth: when the prefetch is thin or the question is diagnostic, the
  model iterates.
- The prefetch's output block should say what it is — a first-pass result, with
  better tools available — rather than presenting as the complete knowledge base.

Revisit once there is telemetry on how often the model calls the tools unprompted. If
it reliably does, the prefetch becomes redundant and can be dropped.

## Not in scope

**Multimodal / layout-aware ingestion.** Genuinely valuable for schematics, pinout
charts and exploded parts diagrams, which text extraction destroys. But it needs a
new parser dependency, a full re-ingestion pass over the corpus, image storage, and a
second embedding path — and it changes ingestion, not retrieval. Its own project,
sequenced after P4 establishes whether text retrieval is the binding constraint.

Worth noting the corpus is small: 21 documents, 1,174 chunks, 225 entities as of
2026-08-05, and 14 of those documents moved into `knowledge_modules`. The largest
remaining item is `CET-rules.pdf` at 1,131 chunks — i.e. the corpus is *one large
regulatory PDF plus six support examples*. At that size, retrieval quality is
probably not the binding constraint on answer quality; corpus size is. That should
inform how much is invested here before ingesting more.

## Testing

- **Phase 0 regression test** asserting RPC argument names match the live function
  signature. This is the test that would have caught the original break.
- **Permission tests are the load-bearing ones.** Two orgs, a deliberate cross-org
  entity, and a document only one org may read. Assert: hybrid search excludes it,
  `search_entities` excludes it, `get_entity_neighbors` excludes it, and the
  "did you mean" suggestion never names it.
- **Exact-match retrieval:** a chunk containing `E-402` ranks top for the query
  `E-402`. This is the whole justification for Phase 1; test it directly.
- **RRF fusion:** a document ranked highly by exactly one ranker still surfaces.
- **Error quality:** unknown type and unknown entity both return the actionable form,
  not an empty list.

Per `MEMORY.md`: after editing `tool_schemas.py`, regenerate
`mcp_servers/tool_definitions.json` via `export_tools.py` — that file is what
production serves — and verify no server was silently dropped from the export. Also
note there is no `conftest.py` under `mcp_servers/tests/`; several test files rely on
collection-order side effects for `sys.path`, so run the full suite, not a
subdirectory.

Per `CLAUDE.md`: `git add -f` new test files, `pre-commit run --all-files` before
claiming done.

## Sequencing

1. **Phase 0** — verify signature, fix caller, delete the unfiltered fallback, add
   the regression test. Ships alone, immediately, ahead of everything else in this
   programme.
2. Measure. With retrieval actually working, get a baseline on the current corpus
   before adding machinery.
3. **Phase 1** — `content_tsv` + GIN + `search_chunks_hybrid` + RRF. Swap the
   provider over. Compare against the Phase 0 baseline.
4. **Phase 2a** — ontology primer, via P1's `GraphProvider` if it exists.
5. **Phase 2b** — the four graph tools, permission-filtered, with actionable errors.
6. Telemetry on tool-call frequency; decide the prefetch's future on evidence.
