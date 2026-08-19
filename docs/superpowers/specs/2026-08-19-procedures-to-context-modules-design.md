# P2 — Procedures Out of the System Prompt

**Date:** 2026-08-19
**Covers:** c.2 (procedures for fixing meters etc. currently in a Google Doc)
**Depends on:** P1 only if procedures should stay live-linked to their doc; the migration itself does not
**Umbrella:** `2026-08-19-context-architecture-design.md`

---

## The question asked, and the answer

> *"procedures to fix meters etc which are in the google doc should be individual
> skills - correct? Evaluate and tell me"*

**No — they should be on-demand context modules, not skills.**

A skill in this codebase is an ordered list of steps with `output_var` bindings and
per-step write gating, executed by `WorkflowExecutor` (`db/migrations/0011_skills.sql`,
`orchestrator/experts/skill_step_bindings.py`). It is a *program*.

A troubleshooting procedure is reference text a technician reads and adapts. The
situation in front of them never matches the numbered steps exactly. Modelling them
as skills costs three things:

1. **Wrong retrieval trigger.** A skill fires when the model selects it by name from
   the catalog. A procedure is needed mid-diagnosis, when the model has already
   started reasoning and needs detail — which is exactly what the `on_demand`
   knowledge tier and `get_knowledge_module` were built for.
2. **No blending.** Two procedures cannot both be "running". A model reading two
   related procedures and synthesising can.
3. **Wrong authoring surface.** You would be re-authoring 20+ documents through a
   conversational step builder, one message per step, for content that is already
   written prose.

The two-tier knowledge design in `shared/prompts/knowledge.py` was built for exactly
this shape: a catalog line per procedure, body fetched on demand.

## What's actually there — and why this matters more than expected

The procedures are **not** in the `troubleshooting.procedures` prompt. That prompt's
bundled body is a 19-line placeholder saying no procedures have been configured yet.

They are inside **`customer.system`** — the customer-facing system prompt itself —
in its live Google Doc override:

- `ProcedureProvider` (`orchestrator/services/procedure_provider.py:63`) calls
  `PROMPTS.text("customer.system")` and parses `## Procedure N: Title` headers out of
  the result, with `### Purpose` / `### Prerequisites` / `### Procedure Steps`
  subsections.
- The bundled `customer.system.prompt` is 23,789 chars and contains **zero**
  `## Procedure` headers. Every procedure exists only in the Google Doc that
  overrides it.

So in production, every customer conversation carries the full text of every
procedure in its system instructions, whether or not anyone is troubleshooting
anything — on top of a 23.7KB base prompt, against a `MAX_CONTEXT_CHARS` of 30,000
(`instructions_provider.py:105`).

**Before implementing, measure it.** The size of the win is unknown until someone
runs:

```bash
python -c "from shared.prompts import PROMPTS; b=PROMPTS.text('customer.system'); print(len(b))"
```

against production credentials. If the live body exceeds `MAX_CONTEXT_CHARS`, the
prompt is already being silently clipped at `instructions_provider.py:193-199` —
which would mean some procedures are being truncated mid-document today, and the
tail ones never reach the model at all.

## The parser already exists

`ProcedureProvider._parse_procedures` produces exactly the fields a knowledge module
needs. The mapping is nearly direct:

| `Procedure` field | `knowledge_modules` column |
|---|---|
| `title` | `title` |
| `purpose` (from `### Purpose`) | `summary` — the only thing the model sees when choosing |
| `full_text` | `body` |
| `id` (`procedure_1`) | not used — slug is derived from the title |
| `number` | `metadata` / migration log only |

Slugs derive from the title (`procedure-commissioning-failed-troubleshooting`), not
the number. Procedure numbering in the doc is editorial and will change; a slug is a
stable address that prompt pins reference.

**Summary quality is load-bearing.** An `on_demand` module is chosen from its summary
alone. A `### Purpose` written as prose for a human reader ("This procedure covers
what to do when commissioning does not complete") is weaker than a symptom-first
line ("Meter commissioning fails or hangs — no completion callback, meter stays in
pending"). The migration must print proposed summaries for review before writing, the
same dry-run discipline the 2026-08-05 migration used.

## Design

### Migration script

`scripts/migrate_procedures_to_modules.py`, dry-run by default, mirroring
`scripts/migrate_rag_docs_to_modules.py`:

1. Resolve the live `customer.system` body via `PROMPTS.text` and report its
   provenance, so the operator knows whether they are reading the Doc, a DB override,
   or the bundled file.
2. Parse with the existing `ProcedureProvider._parse_procedures` — reused, not
   reimplemented, so the two can never drift on what counts as a procedure.
3. For each, propose `slug` / `title` / `summary` / `body`, generating a symptom-first
   summary via LLM and printing it for review.
4. Report the total character count being removed from the system prompt.
5. On `--apply`, write modules with `mode='on_demand'`, `scope='sector'`,
   `source='manual'`, and pin them to no prompts.

### The doc edit — the part that actually delivers the win

Writing the modules changes nothing on its own. The procedures keep flowing into
every request until they are removed from the Google Doc.

This is a **manual, operator-performed step** and the spec should not pretend
otherwise: an operator deletes the procedures section from the customer support Doc
and replaces it with a short pointer explaining that procedures are now context
modules. Automating an edit to a live production Google Doc that is the customer
system prompt is not worth the blast radius.

Sequencing that makes it safe:

1. Migrate the modules (additive, no behaviour change — procedures now exist in two
   places).
2. Attach the on-demand catalog to `customer.system` and `staff.system` via the
   existing Knowledge tab. Verify in production that the model calls
   `get_knowledge_module` and gets the right procedure.
3. *Then* remove the section from the Doc.
4. Re-measure the rendered prompt size.

Between steps 1 and 3 the procedures are duplicated. That is deliberate: it is the
only window in which both paths can be compared on live traffic.

### `ProcedureProvider` after the migration

`ProcedureProvider` is not deleted. Its consumer is the *ingestion* flow — per-chunk
matching of support examples to procedures during `embed_and_store`
(`embed_and_store.py:820-839`), which is unrelated to serving procedures to a
conversation.

Once procedures live in `knowledge_modules`, it should read them from there instead
of re-parsing the system prompt. That is a small follow-up, and it removes the last
reason for the doc to contain procedures at all.

### Optionally: live-linked instead of copied

If procedures should keep being authored in Google Docs rather than the admin UI,
create one `source='gdoc'` module per procedure with `source_ref` set to the doc, once
P1's `GDocProvider` exists. This trades a one-time copy for a permanent live link.

Recommendation: **copy first, live-link later if authoring friction proves real.** A
copied module is editable in the admin UI by the ops group that owns this content,
diffable, and has no runtime dependency on Drive availability. Live-linking is the
mechanism that created the current tangle; reintroduce it only against a demonstrated
need.

## Failure modes

| failure | behaviour |
|---|---|
| doc unreachable during migration | script aborts before writing anything; dry-run by default |
| a procedure has no `### Purpose` | flagged in dry run, requires a hand-written summary before `--apply` |
| duplicate slug from two similar titles | migration refuses and reports the collision |
| model doesn't call `get_knowledge_module` | caught in step 2 verification, before the doc is edited |

That last one is the real risk of this project, and the reason for the duplicated
window. If the model reliably ignores the catalog, the answer is better summaries or
promoting the most-used procedures to `pinned` — not reverting.

## Testing

- Parser reuse: given a fixture doc with three procedures, assert the migration
  produces three modules whose bodies round-trip.
- Slug derivation: stability across renumbering, collision detection.
- Missing `### Purpose` is flagged rather than silently producing an empty summary —
  an on-demand module with a blank summary is invisible to the model, which is a
  silent failure and the worst outcome here.
- `validate_module` already enforces "an on_demand module needs a summary"
  (`knowledge_modules.py:85-88`); assert the migration path goes through it.

Per `CLAUDE.md`: `git add -f` new test files, `pre-commit run --all-files` before
claiming done.

## Success criteria

1. Rendered `customer.system` shrinks by the measured procedure payload.
2. No procedure text appears in a request that isn't about that procedure.
3. A tech asking about a specific symptom still gets the full procedure, fetched
   on demand.
4. Ops can edit a procedure in the admin UI without touching a Google Doc.
