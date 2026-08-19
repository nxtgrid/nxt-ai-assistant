# P4 — Hybrid and Agentic Retrieval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

---

## ⚠️ Before Task 1 — check the prior stages

**Stage 4 of 4.** Prior stages: P1 (`2026-08-20-p1-resolvable-context-modules.md`), P2 (`2026-08-21-p2-procedures-to-context-modules.md`), P3 (`2026-08-22-p3-skills-lifecycle-and-function-steps.md`).

```bash
gh pr list --search "context" --state all --limit 10
gh pr view <PR#> --json title,body,state,mergedAt
ls db/migrations/ | tail -5
```

| assumption | how to verify | if it changed |
|---|---|---|
| `summarize_entity_graph` RPC exists (P1 Task 17) | `SELECT proname FROM pg_proc WHERE proname = 'summarize_entity_graph';` | **Phase 3 reuses it.** If P1 did not ship, build it here from P1's Task 17 verbatim and tell P1 to adopt it rather than writing a second one |
| the `entity-graph` context module is seeded and attached | open `/knowledge-modules` | if absent, Phase 3's primer has no delivery path — seed it with P1's `scripts/seed_context_provider_modules.py` |
| `GraphProvider.render_primer` exists | `ls chat_orchestrator/orchestrator/services/providers/graph_provider.py` | Task 12 imports it; if absent, move `render_primer` here and have P1 import it |
| next free migration number | `ls db/migrations/ \| tail -5` | Tasks 4 and 9 take the next two |
| P2 moved procedures out of `documents` | `gh pr view <P2 PR#> --json body` | procedures are now `knowledge_modules`, not RAG chunks — do not expect to retrieve them via hybrid search |

**Phase 0 does not depend on any prior stage and should ship immediately,
regardless of where this plan sits in the queue.**

---

**Goal:** Restore permission-filtered retrieval (currently returning nothing on every request), then add exact-match capability via hybrid BM25+vector search with RRF, then expose the entity graph as tools the main LLM drives itself.

**Architecture:** Phase 0 fixes an RPC signature mismatch and deletes an unfiltered fallback. Phase 1 adds a generated `tsvector` column with a GIN index and a `search_chunks_hybrid` RPC fusing both rankers by Reciprocal Rank Fusion. Phase 3 adds four permission-filtered graph tools to the existing knowledge MCP server, with actionable errors so the model self-corrects. No separate agentic-RAG orchestration loop — agency stays with the main LLM.

**Tech Stack:** Python 3.12+, Supabase (postgres + pgvector + tsvector/GIN), MCP servers, pytest, pre-commit.

**Spec:** `docs/superpowers/specs/2026-08-19-hybrid-agentic-retrieval-design.md`

---

## Critical Context for the Implementer

### Retrieval currently returns nothing. Every request.

`RAGProvider.retrieve` (`chat_orchestrator/orchestrator/services/rag_provider.py:161-171`)
calls:

```python
client.rpc("search_chunks_with_permissions", {
    "query_embedding": embedding,
    "match_threshold": 0.7,
    "match_count": limit * 2,
    "user_role_ids": user_role_ids,
    "user_org_ids": user_org_ids,
})
```

The committed function signature (`db/schema/chat_db.sql:709`) is:

```sql
search_chunks_with_permissions(
    query_embedding vector(768),
    p_organization_id integer DEFAULT NULL,
    match_count int DEFAULT 10,
    similarity_threshold float DEFAULT 0.5
)
```

`match_threshold`, `user_role_ids` and `user_org_ids` do not exist on it. The call
raises, the `except` falls back to `match_rag_documents` — **which is defined
nowhere in `db/`** — that raises too, and the outer `except Exception` returns `[]`.
`_fetch_rag_context` logs a warning and continues.

This was documented in `docs/superpowers/plans/2026-08-05-context-knowledge-consolidation.md`
line 2579 and never picked up.

**Consequence: no observation about answer quality is evidence about the corpus or
the retrieval approach.** Fix Phase 0, measure, and only then judge whether the rest
of this plan is worth building.

### The corpus is small

21 documents / 1,174 chunks / 225 entities as of 2026-08-05, and 14 of those
documents were migrated out to `knowledge_modules`. What remains is essentially
`CET-rules.pdf` (1,131 chunks — a regulatory PDF) plus six support examples.

Retrieval quality is probably not the binding constraint on answer quality at this
size; corpus size is. Keep that in view before investing heavily past Phase 1.

### The entity graph has no permission columns

`entities`, `relationships`, `entity_mentions`, `relationship_evidence`
(`db/schema/chat_db.sql:373-427`) carry nothing. The only path is
`entities → entity_mentions → documents.allowed_organization_ids`.

This includes error messages. A "did you mean 'Author'?" suggestion built from
unfiltered names leaks the existence of entities the caller cannot see. Build
suggestions only from the caller's already-filtered candidate set.

### `entities.embedding` has no index

Dropped 2026-07-11 as unused, along with the `search_entities`/`get_entity_graph`
RPCs. With hundreds of entities, trigram/`ILIKE` name matching is enough. Do not
re-add the ivfflat index speculatively.

### MCP manifest and test-path gotchas

After editing `tool_schemas.py`, regenerate `mcp_servers/tool_definitions.json` via
`export_tools.py` — that JSON is what production serves, wholesale per server, not
merged with code. Verify no server vanished from the export; the repo-root `.venv`
can silently drop whole servers when their deps are missing.

There is no `conftest.py` under `mcp_servers/tests/`; several files rely on
collection-order side effects for `sys.path`. Run the whole directory, never a
single file.

### `.gitignore` denies `tests/`

`git add -f` every new test file. `pre-commit run --all-files` before claiming done.

---

## File Structure

**Phase 0 — the fix**
- Modify: `chat_orchestrator/orchestrator/services/rag_provider.py:120-200`
- Create: `chat_orchestrator/tests/test_rag_provider_rpc_contract.py`

**Phase 1 — hybrid**
- Create: `db/migrations/00NN_chunks_fulltext.sql`
- Create: `db/migrations/00NN_search_chunks_hybrid.sql`
- Modify: `chat_orchestrator/orchestrator/services/rag_provider.py`
- Create: `chat_orchestrator/tests/test_rag_provider_hybrid.py`

**Phase 2 — graph RPCs**
- Create: `db/migrations/00NN_graph_query_rpcs.sql`

**Phase 3 — graph tools**
- Modify: `mcp_servers/servers/knowledge_server/tool_schemas.py`
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`
- Create: `mcp_servers/tests/servers/knowledge_server/test_graph_tools.py`
- Modify: `mcp_servers/tool_definitions.json` (regenerated)

---

# Phase 0 — Make retrieval work at all

### Task 1: Establish the live signature

- [ ] **Step 1: Query production**

```sql
SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE p.proname IN ('search_chunks_with_permissions', 'match_rag_documents', 'search_chunks');
```

- [ ] **Step 2: Record the result**

Write the exact output into the PR description. Three outcomes:

| result | action |
|---|---|
| signature matches `db/schema/chat_db.sql:709` | fix the **caller** (Task 2) — the schema file is accurate |
| signature differs from the committed schema | fix the caller to match **production**, then correct `db/schema/chat_db.sql` to match reality |
| function absent entirely | apply the committed definition from `db/schema/chat_db.sql:709` first |

`match_rag_documents` is expected to be absent. That is not a bug to fix by
defining it — see Task 3.

- [ ] **Step 3: Confirm the corpus is non-empty**

```sql
SELECT count(*) AS chunks, count(embedding) AS embedded FROM chunks;
SELECT count(*) FROM documents WHERE allowed_organization_ids IS NOT NULL;
```

If `embedded` is far below `chunks`, retrieval will stay thin after the fix for a
different reason — record it.

---

### Task 2: Fix the caller

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/rag_provider.py:161-171`
- Test: `chat_orchestrator/tests/test_rag_provider_rpc_contract.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_rag_provider_rpc_contract.py`:

```python
"""The RPC contract that broke retrieval silently for weeks.

RAGProvider called search_chunks_with_permissions with argument names the
function does not declare. Both that call and its fallback raised, and
retrieve() returned [] on every request without anyone noticing. These tests
pin the argument names.
"""

import pytest

from orchestrator.services.rag_provider import (
    SEARCH_RPC_ARGUMENTS,
    RAGProvider,
    build_search_arguments,
)


def test_the_rpc_argument_names_are_pinned():
    """These must match the SQL function's declared parameters exactly.

    If you change this set, change db/schema/chat_db.sql in the same commit
    and apply the migration -- a mismatch fails silently at runtime.
    """
    assert SEARCH_RPC_ARGUMENTS == {
        "query_embedding",
        "p_organization_id",
        "match_count",
        "similarity_threshold",
    }


def test_build_search_arguments_emits_only_declared_names():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7"], limit=5, threshold=0.3
    )
    assert set(args) == SEARCH_RPC_ARGUMENTS


def test_organization_id_is_the_first_org():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7", "9"], limit=5, threshold=0.3
    )
    assert args["p_organization_id"] == "7"


def test_staff_pass_null_for_unrestricted_access():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=[], limit=5, threshold=0.3, is_staff=True
    )
    assert args["p_organization_id"] is None


def test_a_non_staff_caller_with_no_orgs_is_refused_not_widened():
    """The failure mode that matters: no orgs must never mean unrestricted."""
    with pytest.raises(ValueError, match="no organizations"):
        build_search_arguments(
            embedding=[0.1] * 768, organization_ids=[], limit=5, threshold=0.3, is_staff=False
        )


def test_match_count_over_fetches_for_reranking():
    args = build_search_arguments(
        embedding=[0.1] * 768, organization_ids=["7"], limit=5, threshold=0.3
    )
    assert args["match_count"] > 5


@pytest.mark.asyncio
async def test_retrieve_returns_empty_and_logs_when_the_rpc_fails():
    """Fail closed: a broken permission filter must never widen access."""

    class _Client:
        def rpc(self, *_a, **_k):
            raise RuntimeError("function does not exist")

    provider = RAGProvider()
    provider._rag_client = _Client()

    class _Perms:
        organization_ids = ["7"]
        roles = []
        is_staff = False

    docs = await provider.retrieve(
        "query", "tech@example.com", limit=5, user_permissions=_Perms()
    )
    assert docs == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_rag_provider_rpc_contract.py -v`
Expected: FAIL with `ImportError: cannot import name 'SEARCH_RPC_ARGUMENTS'`

- [ ] **Step 3: Implement**

Add near the top of `rag_provider.py`:

```python
# The exact parameter names search_chunks_with_permissions declares. Pinned
# and tested (tests/test_rag_provider_rpc_contract.py) because a mismatch
# here does not fail loudly: PostgREST rejects the call, the except swallows
# it, and retrieval silently returns nothing on every request. That is
# exactly what happened between the function's last signature change and
# 2026-08-19.
SEARCH_RPC_ARGUMENTS = {
    "query_embedding",
    "p_organization_id",
    "match_count",
    "similarity_threshold",
}

# Over-fetch so the caller can trim to `limit` after ranking.
OVERFETCH_FACTOR = 2

# Low floor, not a filter. The previous 0.7 was high enough to discard
# legitimate matches on a 768-dim cosine space -- had any been returned.
DEFAULT_SIMILARITY_THRESHOLD = 0.3


def build_search_arguments(
    embedding: List[float],
    organization_ids: List[str],
    limit: int,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    is_staff: bool = False,
) -> Dict[str, Any]:
    """Arguments for search_chunks_with_permissions.

    NULL p_organization_id means unrestricted and is reserved for staff. A
    non-staff caller with no organizations is an error, never a widening:
    passing NULL there would hand a customer the entire corpus.
    """
    if not is_staff and not organization_ids:
        raise ValueError(
            "cannot build a permission-filtered search for a non-staff caller "
            "with no organizations"
        )
    return {
        "query_embedding": embedding,
        "p_organization_id": None if is_staff else organization_ids[0],
        "match_count": limit * OVERFETCH_FACTOR,
        "similarity_threshold": threshold,
    }
```

Then replace the try/except RPC block in `retrieve` (lines ~161-181):

```python
            try:
                arguments = build_search_arguments(
                    embedding=embedding,
                    organization_ids=user_org_ids,
                    limit=limit,
                    is_staff=bool(getattr(permissions, "is_staff", False)),
                )
            except ValueError as e:
                LOGGER.warning(f"RAG retrieval refused for {user_email}: {e}")
                return []

            try:
                results = client.rpc("search_chunks_with_permissions", arguments).execute()
            except Exception as e:
                # Deliberately no fallback. The previous code fell back to
                # match_rag_documents, which applies no permission filter at
                # all -- a bypass waiting to start working the moment
                # somebody defined it. Failing closed is correct here.
                LOGGER.error(
                    f"Permission-filtered RAG search failed for {user_email}; "
                    f"returning no results rather than falling back to an "
                    f"unfiltered search: {e}"
                )
                return []
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/test_rag_provider_rpc_contract.py -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Adjust to production reality**

If Task 1 found a signature differing from the committed schema, change
`SEARCH_RPC_ARGUMENTS` and `build_search_arguments` to match **production**, update
the test's expected set, and correct `db/schema/chat_db.sql` in the same commit.

- [ ] **Step 6: Commit**

```bash
git add chat_orchestrator/orchestrator/services/rag_provider.py db/schema/chat_db.sql
git add -f chat_orchestrator/tests/test_rag_provider_rpc_contract.py
git commit -m "fix(rag): repair the search RPC contract and fail closed"
```

---

### Task 3: Remove every reference to the unfiltered fallback

**Files:**
- Modify: `chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py:217`

- [ ] **Step 1: Find the other callers**

Run: `command grep -rn "match_rag_documents\|user_org_ids" --include="*.py" chat_orchestrator/ | command grep -v test`

`detect_contradictions.py:217` passes `user_org_ids` to an RPC and has the same
mismatch.

- [ ] **Step 2: Repoint it**

Change that call to use `build_search_arguments` from `rag_provider`, so there is
exactly one place that knows the RPC's parameter names.

- [ ] **Step 3: Confirm nothing references the fallback**

Run: `command grep -rn "match_rag_documents" --include="*.py" --include="*.sql" . | command grep -v worktree`
Expected: no results

- [ ] **Step 4: Run the ingestion tests**

Run: `python -m pytest chat_orchestrator/tests/ -k "contradiction or ingestion" -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/experts/handlers/ingestion_expert/detect_contradictions.py
git commit -m "fix(rag): route contradiction detection through the pinned RPC contract"
```

---

### Task 4: Deploy and measure the baseline

**This is the point of Phase 0. Do not skip it.**

- [ ] **Step 1: Deploy**

Merge and deploy Phase 0 alone. Do not bundle it with Phase 1.

- [ ] **Step 2: Confirm retrieval is live**

```bash
doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 300 \
  | command grep "Retrieved.*RAG documents"
```

Expected: `Retrieved N RAG documents` with N > 0. If every line still shows 0, the
signature is still wrong — return to Task 1.

- [ ] **Step 3: Record the baseline**

Ask five questions whose answers are in the corpus (`CET-rules.pdf` is the bulk of
it). For each, record: was a relevant chunk retrieved, and did the answer use it?

Write the five questions and outcomes into the PR. **This is the number every later
phase is compared against.** Without it there is no way to tell whether hybrid
search helped.

- [ ] **Step 4: Decide whether to continue**

If retrieval now works well on the real corpus, Phase 1's value is limited to exact
token matching (part numbers, error codes). That may still be worth it — but decide
deliberately, with the baseline in hand, rather than by default.

---

# Phase 1 — Hybrid search

### Task 5: Full-text column and index

**Files:**
- Create: `db/migrations/00NN_chunks_fulltext.sql` (next free number)

- [ ] **Step 1: Check the table size first**

```sql
SELECT count(*) FROM chunks;
SELECT pg_size_pretty(pg_total_relation_size('chunks'));
```

A generated column addition rewrites the table. At ~1,200 rows this is instant; if
the corpus has grown by orders of magnitude, schedule it.

- [ ] **Step 2: Write the migration**

```sql
-- 00NN_chunks_fulltext.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 1 of docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md.
--
-- Dense vectors are poor at exact token match, and this corpus is full of part
-- numbers, serial codes and error codes (QH611A, E-402, RP1000). A query for
-- "E-402" retrieves chunks *about errors* rather than the chunk containing
-- E-402. A generated tsvector backfills on creation and stays correct without
-- any ingestion change.
--
-- NOTE: this rewrites the chunks table. Check its size before running.

BEGIN;

ALTER TABLE chunks
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('english', coalesce(content, ''))) STORED;

CREATE INDEX IF NOT EXISTS chunks_content_tsv_idx ON chunks USING gin (content_tsv);

COMMIT;
```

- [ ] **Step 3: Mirror into `db/schema/chat_db.sql`**

Add the column to the `CREATE TABLE IF NOT EXISTS chunks` block (line ~354) and the
index below it.

- [ ] **Step 4: Apply and verify exact matching works**

```sql
SELECT count(*) FROM chunks WHERE content_tsv @@ websearch_to_tsquery('english', 'E-402');
```

Expected: a non-zero count if that code appears in the corpus. Try a term you know
is present.

- [ ] **Step 5: Commit**

```bash
git add db/migrations/ db/schema/chat_db.sql
git commit -m "feat(rag): add a full-text index over chunk content"
```

---

### Task 6: The hybrid search RPC

**Files:**
- Create: `db/migrations/00NN_search_chunks_hybrid.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 00NN_search_chunks_hybrid.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Runs a dense (pgvector) and a sparse (tsvector/BM25-ish) ranker over the same
-- permission-filtered candidate set and fuses them with Reciprocal Rank Fusion.
--
-- RRF rather than a weighted score blend: cosine similarity and ts_rank are on
-- incomparable scales, so any weighting would need retuning per corpus. RRF
-- uses only rank position, so it needs no normalisation and no tuning.
--
-- Permission filtering matches search_chunks_with_permissions exactly:
-- p_org_ids NULL means unrestricted (staff only -- the caller is responsible
-- for never passing NULL for a non-staff request; see
-- rag_provider.build_search_arguments).

BEGIN;

CREATE OR REPLACE FUNCTION search_chunks_hybrid(
    query_embedding vector(768),
    query_text      text,
    p_org_ids       uuid[] DEFAULT NULL,
    match_count     int    DEFAULT 10,
    rrf_k           int    DEFAULT 60
)
RETURNS TABLE (
    id          uuid,
    document_id uuid,
    content     text,
    score       float,
    metadata    jsonb
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH permitted AS (
        SELECT c.id, c.document_id, c.content, c.chunk_metadata, c.embedding, c.content_tsv
        FROM chunks c
        JOIN documents d ON d.id = c.document_id
        WHERE p_org_ids IS NULL
           OR d.allowed_organization_ids && p_org_ids
    ),
    dense AS (
        SELECT p.id,
               row_number() OVER (ORDER BY p.embedding <=> query_embedding) AS rank
        FROM permitted p
        WHERE p.embedding IS NOT NULL
        ORDER BY p.embedding <=> query_embedding
        LIMIT match_count * 4
    ),
    sparse AS (
        SELECT p.id,
               row_number() OVER (
                   ORDER BY ts_rank(p.content_tsv,
                                    websearch_to_tsquery('english', query_text)) DESC
               ) AS rank
        FROM permitted p
        WHERE p.content_tsv @@ websearch_to_tsquery('english', query_text)
        ORDER BY ts_rank(p.content_tsv,
                         websearch_to_tsquery('english', query_text)) DESC
        LIMIT match_count * 4
    ),
    fused AS (
        SELECT COALESCE(d.id, s.id) AS id,
               COALESCE(1.0 / (rrf_k + d.rank), 0.0)
             + COALESCE(1.0 / (rrf_k + s.rank), 0.0) AS score
        FROM dense d
        FULL OUTER JOIN sparse s ON s.id = d.id
    )
    SELECT p.id, p.document_id, p.content, f.score, p.chunk_metadata
    FROM fused f
    JOIN permitted p ON p.id = f.id
    ORDER BY f.score DESC
    LIMIT match_count;
END;
$$;

COMMIT;
```

- [ ] **Step 2: Mirror into `db/schema/chat_db.sql`**

- [ ] **Step 3: Apply and verify exact-match ranking**

```sql
-- A code query must rank the chunk containing it first. Substitute a code
-- you confirmed exists in Task 5 Step 4.
SELECT id, left(content, 120) AS preview, score
FROM search_chunks_hybrid(
    (SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1),
    'E-402', NULL, 5
);
```

Expected: the top row's preview contains the literal code.

- [ ] **Step 4: Verify permission filtering**

```sql
-- With a real org id that does not own the CET document, its chunks must not appear.
SELECT count(*) FROM search_chunks_hybrid(
    (SELECT embedding FROM chunks WHERE embedding IS NOT NULL LIMIT 1),
    'regulation', ARRAY['<some-other-org-uuid>']::uuid[], 10
);
```

- [ ] **Step 5: Commit**

```bash
git add db/migrations/ db/schema/chat_db.sql
git commit -m "feat(rag): add hybrid dense+sparse search with RRF fusion"
```

---

### Task 7: Route retrieval through hybrid search

**Files:**
- Modify: `chat_orchestrator/orchestrator/services/rag_provider.py`
- Test: `chat_orchestrator/tests/test_rag_provider_hybrid.py`

- [ ] **Step 1: Write the failing test**

Create `chat_orchestrator/tests/test_rag_provider_hybrid.py`:

```python
"""Hybrid retrieval: argument contract and fallback behaviour."""

import pytest

from orchestrator.services.rag_provider import (
    HYBRID_RPC_ARGUMENTS,
    RAGProvider,
    build_hybrid_arguments,
)


def test_hybrid_argument_names_are_pinned():
    assert HYBRID_RPC_ARGUMENTS == {
        "query_embedding",
        "query_text",
        "p_org_ids",
        "match_count",
        "rrf_k",
    }


def test_build_hybrid_arguments_emits_only_declared_names():
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="E-402", organization_ids=["7"], limit=5
    )
    assert set(args) == HYBRID_RPC_ARGUMENTS


def test_the_raw_query_text_is_passed_for_exact_matching():
    """The whole point: 'E-402' must reach the sparse ranker unmodified."""
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="E-402", organization_ids=["7"], limit=5
    )
    assert args["query_text"] == "E-402"


def test_org_ids_are_passed_as_an_array_not_a_scalar():
    """search_chunks_hybrid takes uuid[], unlike the single-org legacy RPC."""
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="q", organization_ids=["7", "9"], limit=5
    )
    assert args["p_org_ids"] == ["7", "9"]


def test_staff_pass_null_org_ids():
    args = build_hybrid_arguments(
        embedding=[0.1] * 768, query="q", organization_ids=[], limit=5, is_staff=True
    )
    assert args["p_org_ids"] is None


def test_a_non_staff_caller_with_no_orgs_is_refused():
    with pytest.raises(ValueError, match="no organizations"):
        build_hybrid_arguments(
            embedding=[0.1] * 768, query="q", organization_ids=[], limit=5, is_staff=False
        )


@pytest.mark.asyncio
async def test_hybrid_failure_returns_empty_rather_than_widening():
    class _Client:
        def rpc(self, name, _args):
            raise RuntimeError(f"{name} does not exist")

    provider = RAGProvider()
    provider._rag_client = _Client()

    class _Perms:
        organization_ids = ["7"]
        roles = []
        is_staff = False

    assert await provider.retrieve("q", "t@example.com", user_permissions=_Perms()) == []
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest chat_orchestrator/tests/test_rag_provider_hybrid.py -v`
Expected: FAIL with `ImportError: cannot import name 'HYBRID_RPC_ARGUMENTS'`

- [ ] **Step 3: Implement**

Add to `rag_provider.py`:

```python
HYBRID_RPC_ARGUMENTS = {
    "query_embedding",
    "query_text",
    "p_org_ids",
    "match_count",
    "rrf_k",
}

DEFAULT_RRF_K = 60


def build_hybrid_arguments(
    embedding: List[float],
    query: str,
    organization_ids: List[str],
    limit: int,
    is_staff: bool = False,
    rrf_k: int = DEFAULT_RRF_K,
) -> Dict[str, Any]:
    """Arguments for search_chunks_hybrid.

    `query_text` is the user's raw text, deliberately unprocessed: the sparse
    ranker's entire value is matching literal tokens like 'E-402' or
    'QH611A' that the dense ranker cannot.

    Unlike the single-org legacy RPC, this takes an array -- a caller with
    several organizations searches across all of them.
    """
    if not is_staff and not organization_ids:
        raise ValueError(
            "cannot build a permission-filtered search for a non-staff caller "
            "with no organizations"
        )
    return {
        "query_embedding": embedding,
        "query_text": query,
        "p_org_ids": None if is_staff else list(organization_ids),
        "match_count": limit * OVERFETCH_FACTOR,
        "rrf_k": rrf_k,
    }
```

Then replace the RPC call in `retrieve` to use hybrid, keeping the fail-closed
behaviour:

```python
            try:
                arguments = build_hybrid_arguments(
                    embedding=embedding,
                    query=query,
                    organization_ids=user_org_ids,
                    limit=limit,
                    is_staff=bool(getattr(permissions, "is_staff", False)),
                )
            except ValueError as e:
                LOGGER.warning(f"RAG retrieval refused for {user_email}: {e}")
                return []

            try:
                results = client.rpc("search_chunks_hybrid", arguments).execute()
            except Exception as e:
                LOGGER.error(
                    f"Hybrid RAG search failed for {user_email}; returning no results "
                    f"rather than falling back to an unfiltered search: {e}"
                )
                return []
```

Update the row-mapping loop below it to read `row.get("score", 0.0)` in place of
`row.get("similarity", 0.0)`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest chat_orchestrator/tests/ -k rag -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add chat_orchestrator/orchestrator/services/rag_provider.py
git add -f chat_orchestrator/tests/test_rag_provider_hybrid.py
git commit -m "feat(rag): route retrieval through hybrid search"
```

---

### Task 8: Label the prefetch honestly

**Files:**
- Modify: `chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py:294-305`

The automatic prefetch stays — it is one cheap query and gives the model a baseline
without a round trip. But it currently presents as the complete knowledge base,
which discourages the model from searching further once Phase 3 lands.

- [ ] **Step 1: Reword the block**

```python
        rag_formatted = "# Knowledge Base — First-Pass Results\n\n"
        rag_formatted += (
            "An automatic search returned the passages below. They may be "
            "incomplete or off-target. When they do not answer the question, "
            "search again with your knowledge and graph tools rather than "
            "concluding the information is unavailable.\n\n"
        )
        rag_formatted += "\n---\n".join(rag_docs)
```

- [ ] **Step 2: Run the orchestrator suite**

Run: `python -m pytest chat_orchestrator/tests/ -q`
Expected: PASS. Update any test asserting the old header text.

- [ ] **Step 3: Commit**

```bash
git add chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py
git commit -m "feat(rag): present the prefetch as a first pass, not the whole base"
```

---

### Task 9: Re-measure against the baseline

- [ ] **Step 1: Deploy Phase 1**

- [ ] **Step 2: Re-run the five baseline questions from Task 4 Step 3**

Record the same two outcomes per question.

- [ ] **Step 3: Add three exact-match questions**

Ask about a part number, an error code and an acronym you confirmed are in the
corpus. These are what hybrid search exists for; if they do not improve, RRF is not
earning its place and Phase 3 should be reconsidered.

- [ ] **Step 4: Write both tables into the PR**

---

# Phase 2 — Graph query RPCs

### Task 10: Permission-filtered graph RPCs

**Files:**
- Create: `db/migrations/00NN_graph_query_rpcs.sql`

- [ ] **Step 1: Write the migration**

```sql
-- 00NN_graph_query_rpcs.sql
--
-- Apply by hand in the Supabase SQL editor against chat_db. Idempotent.
--
-- Phase 2 of docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md.
--
-- entities/relationships/entity_mentions have NO permission columns. Every
-- function here filters through
--   entities -> entity_mentions -> documents.allowed_organization_ids
-- There is no other path. p_org_ids NULL means unrestricted (staff).
--
-- A relationship is visible only when BOTH endpoints are: an edge whose far
-- end lives in another org's documents must not be traversable.

BEGIN;

CREATE OR REPLACE FUNCTION search_entities_permitted(
    p_query    text,
    p_org_ids  uuid[] DEFAULT NULL,
    p_type     text   DEFAULT NULL,
    p_limit    int    DEFAULT 10
)
RETURNS TABLE (id uuid, name text, type text, description text) 
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT e.id, e.name, e.type, e.description
    FROM entities e
    WHERE (p_type IS NULL OR e.type = p_type)
      AND e.name ILIKE '%' || p_query || '%'
      AND (
          p_org_ids IS NULL
          OR EXISTS (
              SELECT 1 FROM entity_mentions em
              JOIN documents d ON d.id = em.document_id
              WHERE em.entity_id = e.id
                AND d.allowed_organization_ids && p_org_ids
          )
      )
    ORDER BY length(e.name), e.name
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_neighbors_permitted(
    p_entity_id  uuid,
    p_org_ids    uuid[] DEFAULT NULL,
    p_rel_type   text   DEFAULT NULL,
    p_limit      int    DEFAULT 25
)
RETURNS TABLE (
    neighbor_id       uuid,
    neighbor_name     text,
    neighbor_type     text,
    relationship_type text,
    description       text,
    direction         text
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    WITH visible AS (
        SELECT e.id
        FROM entities e
        WHERE p_org_ids IS NULL
           OR EXISTS (
               SELECT 1 FROM entity_mentions em
               JOIN documents d ON d.id = em.document_id
               WHERE em.entity_id = e.id
                 AND d.allowed_organization_ids && p_org_ids
           )
    )
    SELECT t.id, t.name, t.type, r.relationship_type, r.description, 'outgoing'::text
    FROM relationships r
    JOIN entities t ON t.id = r.target_entity_id
    WHERE r.source_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    UNION ALL
    SELECT s.id, s.name, s.type, r.relationship_type, r.description, 'incoming'::text
    FROM relationships r
    JOIN entities s ON s.id = r.source_entity_id
    WHERE r.target_entity_id = p_entity_id
      AND (p_rel_type IS NULL OR r.relationship_type = p_rel_type)
      AND r.source_entity_id IN (SELECT id FROM visible)
      AND r.target_entity_id IN (SELECT id FROM visible)
    LIMIT p_limit;
END;
$$;

CREATE OR REPLACE FUNCTION get_entity_evidence_permitted(
    p_entity_id uuid,
    p_org_ids   uuid[] DEFAULT NULL,
    p_limit     int    DEFAULT 5
)
RETURNS TABLE (chunk_id uuid, document_id uuid, document_title text, excerpt text)
LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY
    SELECT em.chunk_id, em.document_id, d.title, coalesce(em.context, c.content)
    FROM entity_mentions em
    JOIN documents d ON d.id = em.document_id
    LEFT JOIN chunks c ON c.id = em.chunk_id
    WHERE em.entity_id = p_entity_id
      AND (p_org_ids IS NULL OR d.allowed_organization_ids && p_org_ids)
    ORDER BY em.confidence DESC
    LIMIT p_limit;
END;
$$;

COMMIT;
```

- [ ] **Step 2: Mirror into `db/schema/chat_db.sql`**

- [ ] **Step 3: Apply and verify each**

```sql
SELECT * FROM search_entities_permitted('DCU', NULL, NULL, 5);
SELECT * FROM get_entity_neighbors_permitted(
    (SELECT id FROM entities LIMIT 1), NULL, NULL, 5);
SELECT * FROM get_entity_evidence_permitted(
    (SELECT id FROM entities LIMIT 1), NULL, 3);
```

- [ ] **Step 4: Verify a cross-org entity is excluded**

```sql
-- Substitute an org uuid that owns none of the documents mentioning this entity.
SELECT count(*) FROM search_entities_permitted(
    '<entity name>', ARRAY['<unrelated-org-uuid>']::uuid[], NULL, 10);
```
Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add db/migrations/ db/schema/chat_db.sql
git commit -m "feat(rag): add permission-filtered graph query RPCs"
```

---

# Phase 3 — Agentic graph tools

### Task 11: Tool schemas

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/tool_schemas.py`

- [ ] **Step 1: Add four schemas**

Append to `TOOL_SCHEMAS`, matching the existing 5-slot description style (see
`MEMORY.md`'s MCP tool description audit — `[READ-ONLY]` prefix, what it does, when
to use it, what it returns, and which sibling tool to prefer when):

```python
 {'name': 'get_graph_schema',
  'description': '[READ-ONLY] List the entity types and relationship types in the knowledge '
                 'graph, with counts and example entity names. Call this FIRST when a question '
                 'needs structured facts about equipment, sites or their connections — it tells '
                 'you what kinds of things exist before you search for specific ones. Returns a '
                 'compact ontology, filtered to what you may see. For free-text passages rather '
                 'than entities, use summarize_knowledge instead.',
  'inputSchema': {'type': 'object', 'properties': {}},
  'visible_to_customer': False},
 {'name': 'search_entities',
  'description': '[READ-ONLY] Find entities in the knowledge graph by name, optionally narrowed '
                 'to one entity type from get_graph_schema. Use to turn a name a user mentioned '
                 'into a real entity id before traversing. Returns matching entities with their '
                 'ids, types and descriptions; suggests near-matches when nothing matches '
                 'exactly. Follow with get_entity_neighbors to explore what an entity connects '
                 'to.',
  'inputSchema': {'type': 'object',
                  'properties': {'query': {'type': 'string',
                                           'description': 'Name or partial name to search for.'},
                                 'entity_type': {'type': 'string',
                                                 'description': 'Optional type filter — use a '
                                                                'value from get_graph_schema.'},
                                 'limit': {'type': 'integer', 'default': 10}},
                  'required': ['query']},
  'visible_to_customer': False},
 {'name': 'get_entity_neighbors',
  'description': '[READ-ONLY] List what one entity connects to in the knowledge graph, '
                 'optionally narrowed to one relationship type. Use after search_entities to '
                 'follow a connection — which meters sit on a DCU, which site a grid belongs '
                 'to. Returns neighbouring entities with the relationship joining them and its '
                 'direction. For the source passages behind a claim, use get_entity_evidence.',
  'inputSchema': {'type': 'object',
                  'properties': {'entity_id': {'type': 'string',
                                               'description': 'Entity id from search_entities.'},
                                 'relationship_type': {'type': 'string',
                                                       'description': 'Optional filter — use a '
                                                                      'value from '
                                                                      'get_graph_schema.'},
                                 'limit': {'type': 'integer', 'default': 25}},
                  'required': ['entity_id']},
  'visible_to_customer': False},
 {'name': 'get_entity_evidence',
  'description': '[READ-ONLY] Retrieve the source document passages an entity was extracted '
                 'from. Use to ground a claim before stating it, or when a neighbour '
                 'relationship looks surprising and you want to check the underlying text. '
                 'Returns excerpts with their document titles. For a broad topic summary rather '
                 'than one entity\'s sources, use summarize_knowledge.',
  'inputSchema': {'type': 'object',
                  'properties': {'entity_id': {'type': 'string',
                                               'description': 'Entity id from search_entities.'},
                                 'limit': {'type': 'integer', 'default': 5}},
                  'required': ['entity_id']},
  'visible_to_customer': False},
```

- [ ] **Step 2: Commit**

```bash
git add mcp_servers/servers/knowledge_server/tool_schemas.py
git commit -m "feat(rag): declare the four graph query tools"
```

---

### Task 12: Tool handlers with actionable errors

**Files:**
- Modify: `mcp_servers/servers/knowledge_server/knowledge_mcp_server.py`
- Test: `mcp_servers/tests/servers/knowledge_server/test_graph_tools.py`

- [ ] **Step 1: Write the failing test**

Create `mcp_servers/tests/servers/knowledge_server/test_graph_tools.py`:

```python
"""Graph tools: formatting, permission scoping, and error quality.

An agent self-corrects from a good error and gives up on a bad one, so the
error text is part of the interface and is tested as such.
"""

from mcp_servers.servers.knowledge_server.knowledge_mcp_server import (
    format_entity_results,
    format_neighbors,
    org_ids_for_request,
    suggest_near_matches,
)


def test_org_ids_are_null_for_staff():
    assert org_ids_for_request(is_staff=True, organization_ids=["7"]) is None


def test_org_ids_are_the_callers_orgs_otherwise():
    assert org_ids_for_request(is_staff=False, organization_ids=["7", "9"]) == ["7", "9"]


def test_a_non_staff_caller_with_no_orgs_raises_rather_than_widening():
    """NULL means unrestricted; reaching it by accident hands over the graph."""
    import pytest

    with pytest.raises(ValueError, match="no organizations"):
        org_ids_for_request(is_staff=False, organization_ids=[])


def test_entity_results_are_formatted_with_ids():
    rows = [{"id": "abc-123", "name": "DCU-7721", "type": "DCU", "description": "A DCU."}]
    text = format_entity_results(rows, query="DCU")
    assert "DCU-7721" in text
    assert "abc-123" in text


def test_no_matches_suggests_near_names_from_the_permitted_set_only():
    """A suggestion built from unfiltered names leaks entities the caller cannot see."""
    text = suggest_near_matches("DCU-7712", permitted_names=["DCU-7721", "DCU-7112"])
    assert "DCU-7721" in text
    assert "DCU-7112" in text


def test_no_matches_and_no_permitted_names_says_so_without_naming_anything():
    text = suggest_near_matches("DCU-7712", permitted_names=[])
    assert "DCU-7712" in text
    assert "no" in text.lower()


def test_empty_entity_results_never_return_a_bare_empty_list():
    text = format_entity_results([], query="Autor")
    assert text.strip()
    assert "Autor" in text


def test_neighbors_include_the_relationship_and_direction():
    rows = [
        {"neighbor_id": "m-1", "neighbor_name": "M-001", "neighbor_type": "Meter",
         "relationship_type": "connected_to", "description": "on the DCU",
         "direction": "outgoing"},
    ]
    text = format_neighbors(rows, entity_id="d-1")
    assert "M-001" in text
    assert "connected_to" in text
    assert "outgoing" in text


def test_no_neighbors_is_stated_not_returned_empty():
    text = format_neighbors([], entity_id="d-1")
    assert "d-1" in text
    assert text.strip()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest mcp_servers/tests/ -k graph_tools -v`
Expected: FAIL with `ImportError: cannot import name 'org_ids_for_request'`

- [ ] **Step 3: Implement the helpers**

Add to `knowledge_mcp_server.py`:

```python
import difflib


def org_ids_for_request(is_staff: bool, organization_ids: List[str]):
    """Org filter for the graph RPCs. None means unrestricted.

    Raises rather than returning None for a non-staff caller with no
    organizations: every graph RPC reads NULL as "show everything", so
    reaching it by accident hands the whole graph to someone entitled to
    none of it.
    """
    if is_staff:
        return None
    if not organization_ids:
        raise ValueError(
            "cannot scope a graph query for a non-staff caller with no organizations"
        )
    return list(organization_ids)


def suggest_near_matches(query: str, permitted_names: List[str]) -> str:
    """A 'did you mean' line built ONLY from names the caller may see.

    Suggesting from the unfiltered entity table would leak the existence of
    entities in other organizations -- the subtle version of the permission
    bug these tools exist to avoid.
    """
    close = difflib.get_close_matches(query, permitted_names, n=3, cutoff=0.5)
    if close:
        return f"No entity matching '{query}'. Closest by name: {', '.join(close)}."
    return (
        f"No entity matching '{query}', and no close names either. "
        f"Call get_graph_schema to see which entity types exist."
    )


def format_entity_results(rows: List[Dict[str, Any]], query: str) -> str:
    """Entity search results, or an actionable message. Never a bare empty list."""
    if not rows:
        return suggest_near_matches(query, [])
    lines = [f"Found {len(rows)} entit{'y' if len(rows) == 1 else 'ies'} matching '{query}':", ""]
    for row in rows:
        description = f" — {row['description']}" if row.get("description") else ""
        lines.append(f"- {row['name']} ({row['type']}) [id: {row['id']}]{description}")
    lines.append("")
    lines.append("Use get_entity_neighbors with an id above to see what it connects to.")
    return "\n".join(lines)


def format_neighbors(rows: List[Dict[str, Any]], entity_id: str) -> str:
    """Neighbour results, or an actionable message."""
    if not rows:
        return (
            f"Entity {entity_id} has no visible connections. It may genuinely have "
            f"none, or the entities on the other side may be outside your access. "
            f"Try get_entity_evidence to see the source passages instead."
        )
    lines = [f"{len(rows)} connection(s):", ""]
    for row in rows:
        description = f" — {row['description']}" if row.get("description") else ""
        lines.append(
            f"- [{row['direction']}] {row['relationship_type']} → "
            f"{row['neighbor_name']} ({row['neighbor_type']}) "
            f"[id: {row['neighbor_id']}]{description}"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest mcp_servers/tests/ -q`
Expected: PASS

- [ ] **Step 5: Wire up the four handlers**

Add handlers following the existing `@registry.tool(...)` pattern. `get_graph_schema`
calls `summarize_entity_graph` and renders with P1's `render_primer`; the other
three call their RPCs and render with the helpers above. Each catches exceptions
and returns the error text, never a traceback.

For `search_entities`, on zero rows, fetch up to 50 permitted entity names via
`search_entities_permitted` with an empty query and pass those to
`suggest_near_matches` — so suggestions come from the caller's own visible set.

- [ ] **Step 6: Regenerate the manifest**

```bash
python mcp_servers/scripts/export_tools.py
git diff --stat mcp_servers/tool_definitions.json
```

Expected: four new tools under the knowledge server. **If whole servers disappeared
from the JSON, the venv is missing their dependencies — fix that before
committing**, or production loses those servers entirely.

- [ ] **Step 7: Commit**

```bash
git add mcp_servers/servers/knowledge_server/knowledge_mcp_server.py \
        mcp_servers/tool_definitions.json
git add -f mcp_servers/tests/servers/knowledge_server/test_graph_tools.py
git commit -m "feat(rag): add permission-filtered graph tools with actionable errors"
```

---

### Task 13: Attach the ontology primer

- [ ] **Step 1: Confirm the module exists**

Open `/knowledge-modules`. The `entity-graph` module should be present from P1's
seed script. If not:

```bash
python scripts/seed_context_provider_modules.py --apply
```

- [ ] **Step 2: Attach it to `staff.system`**

Pin it in the Context page. Leave `customer.system` unattached initially — the
graph is staff-facing material and the customer prompt is already the tighter
budget.

- [ ] **Step 3: Confirm it renders**

```bash
doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 200 \
  | command grep -i "entity-graph\|Live Context"
```

- [ ] **Step 4: Confirm the model uses the tools**

Ask a staff-mode question needing structured facts ("which meters are on DCU-7721?").
Then:

```bash
doctl apps logs 525c885e-c7e4-4721-b654-b724c1de5553 anansi-bot --type run --tail 200 \
  | command grep "search_entities\|get_entity_neighbors\|get_graph_schema"
```

Expected: a discovery trajectory — schema or search first, then neighbours.

If the model never calls them, the primer is not landing. Check it is actually in
the rendered prompt before changing the tool descriptions.

---

### Task 14: Final verification and PR

- [ ] **Step 1: Run every suite**

Run: `python -m pytest chat_orchestrator/tests/ mcp_servers/tests/ shared/tests/ -q`
Expected: PASS

- [ ] **Step 2: Run pre-commit**

Run: `pre-commit run --all-files`
Expected: all hooks pass. `git add -f` any untracked test files, then re-run.

- [ ] **Step 3: Confirm every migration is applied**

```sql
SELECT proname FROM pg_proc
WHERE proname IN ('search_chunks_hybrid', 'search_entities_permitted',
                  'get_entity_neighbors_permitted', 'get_entity_evidence_permitted');
SELECT column_name FROM information_schema.columns
WHERE table_name = 'chunks' AND column_name = 'content_tsv';
```
Expected: four functions, one column.

- [ ] **Step 4: Confirm no unfiltered fallback survives**

Run: `command grep -rn "match_rag_documents" --include="*.py" --include="*.sql" . | command grep -v worktree`
Expected: no results

- [ ] **Step 5: Open the PR**

```bash
git push -u origin feat/hybrid-agentic-retrieval
gh pr create --title "feat(rag): hybrid retrieval and agentic graph tools" --body "$(cat <<'EOF'
Restores permission-filtered retrieval — which had been returning nothing on every
request — then adds exact-match capability via hybrid BM25+vector search with RRF,
and exposes the entity graph as tools the main LLM drives itself.

Stage 4 of 4 in the context-architecture programme.

Spec: `docs/superpowers/specs/2026-08-19-hybrid-agentic-retrieval-design.md`
Plan: `docs/superpowers/plans/2026-08-23-p4-hybrid-agentic-retrieval.md`

## Phase 0 — the bug

`RAGProvider.retrieve` called `search_chunks_with_permissions` with argument names
the function does not declare. It raised, fell back to `match_rag_documents` (not
defined anywhere in `db/`), which raised too, and `retrieve` returned `[]`. On every
message. Documented in the 2026-08-05 plan doc at line 2579 and never picked up.

The fallback is deleted rather than defined: falling back to an unfiltered search is
a permission bypass waiting to start working.

Live signature found: __

## Measured

| question | baseline (Phase 0) | hybrid (Phase 1) |
|---|---|---|
| | | |

Exact-match questions (the justification for hybrid):

| query | retrieved the literal match? |
|---|---|

## Migrations — apply by hand before merging

- `00NN_chunks_fulltext.sql` (rewrites the chunks table — size checked: __ rows)
- `00NN_search_chunks_hybrid.sql`
- `00NN_graph_query_rpcs.sql`

## Notes for the reviewer

- `entities`/`relationships` have no permission columns; every graph query filters
  through `entity_mentions -> documents.allowed_organization_ids`, and a
  relationship needs BOTH endpoints visible.
- "Did you mean" suggestions are built only from the caller's permitted set —
  suggesting from unfiltered names would leak entity existence across orgs.
- Agency stays with the main LLM: tools plus an ontology primer, no separate
  agentic-RAG orchestration loop.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Programme complete

All four stages are done. Two things deliberately left out, both worth revisiting
with evidence rather than by default:

**Multimodal ingestion.** Layout-aware or vision-language parsing so schematics,
pinout charts and exploded parts diagrams become retrievable. Needs a new parser,
a full re-ingestion pass, image storage and a second embedding path — and it
changes ingestion, not retrieval. Revisit once Phase 1's measurements show whether
text retrieval is the binding constraint.

**Dropping the automatic prefetch.** Task 8 kept it and relabelled it as a first
pass. If telemetry shows the model reliably reaches for the graph and knowledge
tools unprompted, the prefetch becomes redundant and can go — one fewer embedding
call per request. Check tool-call frequency in the logs before deciding.
