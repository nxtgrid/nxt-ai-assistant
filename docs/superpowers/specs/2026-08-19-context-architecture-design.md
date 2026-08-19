# Context Architecture — Umbrella Design

**Date:** 2026-08-19
**Status:** Approved for decomposition into four specs
**Goal:** Replace ad-hoc context assembly with four explicitly managed memory types, so that content currently embedded in system instructions via Google Drive objects can be untangled into addressable, individually-attachable units.

---

## Why this exists

Context reaches the model today through eight independent code paths converging in
`chat_orchestrator/orchestrator/graphs/nodes/prepare_context.py`. Three of them
resolve live Google Docs. Two are hardcoded string builders. One (RAG) is dead.
None of them are visible or controllable from the admin UI as a single list.

The consequence is that the only way to teach the bot something has been to add it
to a Google Doc that gets appended to the system prompt — for every request, for
every user, regardless of relevance. This design makes each unit of context an
addressable object with an owner, a lifecycle, and an explicit attachment to the
prompts that need it.

## The four types

| type | question | current home | target |
|---|---|---|---|
| **Working** | What's happening now? | `ConversationState`, `MAX_CONTEXT_CHARS = 30000` | unchanged |
| **Semantic** | What do I know? | `knowledge_modules` + 3 hardcoded injectors | one module list, four body providers |
| **Procedural** | How do I do this? | `skills` table + a 42KB Google Doc | skills with lifecycle + function steps |
| **Episodic** | What happened before? | `user_preferences`, `conversation_summaries` | + per-grid/per-org distillation |

## Current-state map (verified 2026-08-19)

### Already shipped — do not rebuild

- **Knowledge modules.** `knowledge_modules` table with `slug/title/summary/body/tags/scope/mode/source/source_ref`.
  Explicit per-prompt pinning via `prompt_knowledge_overrides`. Two tiers: `pinned`
  (inlined, 20k char budget, `budget_pinned`) and `on_demand` (one catalog line each,
  body fetched via the `get_knowledge_module` MCP tool). Admin page at
  `/knowledge-modules`, already labelled "🧠 Context" in `anansi_app/nicegui_app/layout.py:31`.
  Delivered by `docs/superpowers/plans/2026-08-05-context-knowledge-consolidation.md`.
- **User preferences.** `user_preferences` table, injected at
  `prepare_context.py:337` under a 500-char cap, explicitly scoped to formatting
  and style only.
- **Skill scheduling.** `user_schedules.skill_id` + `anchor_entity_type` fan skills
  out across every eligible grid or organization (`db/migrations/0013_skill_scheduling.sql`,
  `orchestrator/experts/entity_fanout.py`). Per-entity outcomes land in
  `user_schedule_logs`. No UI exposes any of it.

### The structural gap

`KnowledgeModule` is a frozen dataclass whose `body` is a plain `str` read straight
from the DB row (`shared/prompts/knowledge.py:26-38`). `KnowledgeStore.all_modules()`
selects `id, slug, title, summary, body, tags, scope, mode` — it never reads the
`source` or `source_ref` columns that already exist in the table.

Four separate requests — a Google Doc as a module, a GraphRAG entity summary, a
grids/orgs/users directory, and an episodic per-grid distillation — are all the same
shape: **a module whose body is computed at render time under the caller's
permissions.** None of them can be expressed while `body` is a static string.

This is the single highest-leverage change in the whole programme, and it is P1.

### The dead path

`RAGProvider.retrieve` (`chat_orchestrator/orchestrator/services/rag_provider.py:161`)
calls `search_chunks_with_permissions` with `match_threshold` / `user_role_ids` /
`user_org_ids`. The function's committed signature (`db/schema/chat_db.sql:709`) is
`(query_embedding, p_organization_id integer, match_count, similarity_threshold)`.
The call raises. The `except` falls back to `match_rag_documents`, which is not
defined anywhere in `db/`. That raises too, the outer `except` returns `[]`, and
`_fetch_rag_context` logs a warning and continues.

**Vector retrieval has returned nothing on every chat message for an unknown period.**
This was documented in the 2026-08-05 plan doc (line 2579) and never picked up.

Two consequences for this programme:

1. There is no heuristic-RAG migration to manage and no regression risk in P4.
   Nothing is running to preserve.
2. No judgement about current answer quality can be treated as evidence about the
   corpus or the retrieval approach. The corpus has not been reachable.

## Cross-cutting decisions

**D0 — Migration numbers in these specs are indicative, not reserved.** The four
projects are independently shippable and may land out of order. Each spec names a
number (0017-0020) to keep its own SQL readable; assign the real number at
implementation time from whatever `db/migrations/` actually holds. Per `MEMORY.md`,
merging a migration file does not apply it - 0016 is still unapplied in production
and producing live PGRST204 errors. Confirm application before any code depending on
a migration deploys.


**D1 — One module list, many body providers.** Every unit of semantic memory appears
in one admin list and attaches to prompts through the existing
`prompt_knowledge_overrides` mechanism. What varies is only how the body is
produced. Rejected: separate UI surfaces per source type, which would multiply the
attachment mechanism by four.

**D2 — Just-in-time modules are placeholders in the list, resolved at render.** A
`graph`, `directory` or `episodic` module has no stored body. Its row exists so an
operator can attach it to prompts; its content is computed per request under that
request's permissions. Body is read-only in the UI for every non-`manual` source.

**D3 — Permission is resolved inside the provider, never cached across callers.**
JIT provider output is per-request and per-identity. The existing 300s
`KnowledgeStore` cache covers module *metadata* only; resolved bodies are not cached
in it. Any provider-internal caching must be keyed on the permission set.

**D4 — Procedures are context, not skills.** Reference material a model reads and
adapts belongs in semantic memory. Only genuinely ordered, repeatable step
sequences belong in procedural memory. See P2 for the full argument.

**D5 — Every provider fails open.** A prompt must always render. A provider that
raises, times out, or returns nothing contributes an empty string and logs. This
matches the existing `_compose_knowledge` contract in `shared/prompts/core.py:150-175`.

**D6 — Retrieval stays under the main LLM's agency.** No separate agentic-RAG
orchestration loop. Retrieval capability is exposed as tools the main model calls,
plus an ontology primer so it knows what to ask for. This is an explicit constraint,
not an implementation convenience.

## The four projects

| | project | delivers | depends on |
|---|---|---|---|
| **P1** | Resolvable context modules | b.2 gdoc, b.3 graph, b.4 directory, d.1 episodic | — |
| **P2** | Procedures out of the system prompt | c.2 | P1 only for live-linked docs |
| **P3** | Skills lifecycle + function steps | c.1, c.4 | — |
| **P4** | Hybrid + agentic retrieval | RAG rework | shares graph plumbing with P1's b.3 |

Specs:

- `2026-08-19-resolvable-context-modules-design.md`
- `2026-08-19-procedures-to-context-modules-design.md`
- `2026-08-19-skills-lifecycle-and-function-steps-design.md`
- `2026-08-19-hybrid-agentic-retrieval-design.md`

### Recommended sequence

**P1 → P2 → P4 → P3.**

P1 is the spine: it is what actually lets Google Drive content out of the system
prompt, and its graph provider builds the entity-permission plumbing that P4's
agentic tools reuse. P2 is small once P1 exists and delivers the largest immediate
reduction in per-request context. P4 has the highest ceiling on answer quality but
inherits P1's graph work. P3 is fully independent and can move in parallel with any
of them if there is capacity — it shares no code with the other three.

The dead-RPC fix in P4 Phase 0 is a few lines and independent of everything. It
should ship as soon as someone can verify the live function signature against
production, regardless of where P4 sits in the queue.

## Out of scope

- **Multimodal / layout-aware ingestion** (ColPali, vision-language parsers for
  schematics and pinout diagrams). Real value for technical manuals, but it needs a
  new parser dependency, a full re-ingestion pass, image storage, and a second
  embedding path. Its own project, after P4 establishes whether text retrieval is
  the binding constraint.
- **Working memory changes.** `MAX_CONTEXT_CHARS` and the summarization behaviour
  stay as they are.
- **Replacing the expert system.** P3 converts the five prompt-only experts and
  exposes handlers as steps; the four pipeline experts stay as code.
