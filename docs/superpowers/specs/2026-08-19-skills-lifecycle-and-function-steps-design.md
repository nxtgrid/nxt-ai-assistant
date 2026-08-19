# P3 — Skills Lifecycle and Function Steps

**Date:** 2026-08-19
**Covers:** c.1 (expert workflows as skills), c.3 (ingestion as a skill — answered: no), c.4 (skills page as a list with a modal)
**Depends on:** nothing — fully independent of P1/P2/P4
**Umbrella:** `2026-08-19-context-architecture-design.md`

---

## Part A — c.1: which expert workflows can become skills

Expert workflows are already ordered step sequences. They live in the
`experts.definitions` prompt — a ~42KB body resolved from a Google Doc — in the form:

```
1. [llm] understand_request - Parse user intent and identify grid
2. [function:fetch_month_metrics] - Get last 30 days from Grafana
```

Tallying every expert in the bundled definition by step type:

| expert | `[llm]` | `[function:]` | verdict |
|---|---|---|---|
| grid_analyst | 0 | 0 | prompt-only — **convert** |
| grid_monitor | 0 | 0 | prompt-only — **convert** |
| site_visit_tracker | 0 | 0 | prompt-only — **convert** |
| signing | 0 | 0 | prompt-only — **convert** |
| community_sizing | 0 | 0 | prompt-only — **convert** |
| context_expert | 2 | 7 | pipeline — **keep as code** |
| grids_technical_reviewer | 2 | 9 | pipeline — **keep as code** |
| ingestion_expert | 4 | 9 | pipeline — **keep as code** |
| package_generator | 2 | 12 | pipeline — **keep as code** |

The split is clean and it is not the split anyone expected. Five "experts" have no
workflow at all — they are system-instruction blocks with tool access, wearing the
expert machinery for no reason. Four are genuine deterministic pipelines.

**Recommendation: convert the five, keep the four.**

Converting `package_generator` would mean exposing twelve ordered handlers —
`create_site_folder`, `copy_lpp_template`, `generate_qgis_project`,
`update_design_distances` (which sleeps 60s waiting on AppSheet),
`generate_site_bom`, `populate_lpp_cells` — to reordering in a conversational
builder. Their order encodes real external constraints. A skill builder that lets an
operator move `copy_lpp_template` ahead of `create_site_folder` is not a feature.

This corrects an earlier reading that experts could migrate 1:1 given a function step
type. They cannot, and the reason is not technical capability — it is that four of
them should not be user-editable.

### What "convert" means for the five

Each becomes a skill whose steps are its instruction blocks, plus its tool access
and its interactive-button behaviour. Concretely, per expert:

1. Read its section from the live `experts.definitions` body — **not** the bundled
   file. Per `MEMORY.md` and the 2026-08-05 plan doc, production resolves this prompt
   from a DB or Google Doc override; editing the bundled file has no effect. Verify
   first:
   ```bash
   python -c "from shared.prompts import PROMPTS; print(PROMPTS.resolve('experts.definitions')[1:])"
   ```
2. Split the instruction body into steps at its natural boundaries. A prompt-only
   expert whose instructions are one block becomes a one-step skill; that is a
   legitimate outcome and is still an improvement — it becomes nameable, schedulable,
   and editable without a Google Doc.
3. Preserve `staff_only` from the expert's existing access gating.
4. Leave the expert definition in place until the skill is verified, then strike it
   through in the Doc (the existing disable convention) rather than deleting.

Note that `grid_analyst` is already struck through in the live doc as of 2026-08-07
(per `MEMORY.md`), so its conversion is the safest one to do first.

### Function steps — build them, but for composition, not migration

Skill steps today are LLM-only:
`{index, name, instruction, output_var, allow_write, is_response_step}`
(`db/migrations/0011_skills.sql`, `orchestrator/experts/skill_step_bindings.py`).

Adding a `function` step kind is still worth doing — not to migrate the four
pipelines, but so *new* skills can call the deterministic handlers that already exist
in `step_registry`. "Fetch Grafana KPIs, then reason about them" is a skill an
operator should be able to build; today they can only do the reasoning half.

```json
{"index": 2, "kind": "function", "handler": "fetch_grafana_kpis", "output_var": "kpis"}
```

`kind` defaults to `"llm"` when absent, so every existing skill row stays valid with
no backfill.

Two constraints:

- **Only allowlisted handlers.** A new `exposed_to_builder: bool` on the
  `@register_step` decorator, defaulting to `False`. Handlers reach the builder by
  deliberate opt-in, reviewed one at a time. The registry currently holds handlers
  that mutate spreadsheets and trigger BOM generation; none of those should appear in
  a picker by default.
- **`allow_write` still governs.** A function handler that mutates requires
  `allow_write=True` on its step, same gate as tools
  (`skill_step_bindings.filter_tools_for_step`).

`WorkflowExecutor` already dispatches `[function:handler]` steps; the work is
plumbing skill-sourced steps into the same dispatch rather than building a second
execution path.

## Part B — c.3: should ingestion be a skill?

**No.** Thirteen steps, nine of them functions: `fetch_document`,
`classify_document`, `improve_content`, `preprocess_document`, `detect_duplicates`,
`extract_entities`, `detect_contradictions`, `prepare_approval_summary`,
`embed_and_store`. It writes to `documents`, `chunks`, `entities` and
`relationships`, and carries a human approval gate mid-flow.

It is a data pipeline with a review step, not a procedure someone should re-author
in a chat builder. Leave it.

## Part C — c.4: the skills page

### What's there now

`/skill-builder` is a single page that *is* the builder. There is no list. The page
holds one in-progress conversation; navigating away loses it. Saving opens a dialog
with exactly three fields — Title, Summary, Staff only
(`anansi_app/nicegui_app/pages/skill_builder.py:381-386`). Nothing sets status.
Nothing schedules.

Meanwhile the backend already supports far more than the UI exposes:

- `skills.status` accepts `active | disabled | unusable`
- `user_schedules.skill_id` + `anchor_entity_type` fan a skill out across every
  eligible grid or organization, with per-entity outcomes logged to
  `user_schedule_logs` (`db/migrations/0013_skill_scheduling.sql`)
- `skills.inputs` describes accepted inputs; `user_schedules.skill_inputs` holds the
  values bound to one particular schedule

None of it is reachable from the UI. **c.4 is almost entirely a front-end project.**

### Target

`/skills` — a list page:

| column | source |
|---|---|
| Title | `skills.title` |
| Summary | `skills.summary` |
| Status | `skills.status` (badge) |
| Steps | `len(skills.steps)` |
| Schedule | derived from `user_schedules` where `skill_id` matches |
| Audience | `staff_only` |
| Updated | `updated_at`, `created_by` |

**New** and **Edit** open the same modal, which contains:

1. **Identity** — title, summary, staff-only, status.
2. **Steps** — the existing builder, moved into the modal unchanged. Same
   send/transcript/rewind behaviour, same per-step "Allow this step to make changes"
   and "Also return this response" toggles. This is the one part that should not be
   redesigned; it works, and it is the risky part to touch.
3. **Schedule** — reuse `_build_recurrence(dt_utc, frequency)` from
   `anansi_app/nicegui_app/pages/broadcast.py:41`, which already derives
   `{schedule_type, cron_expression, timezone}` from a first-send datetime plus a
   frequency. Plus an `anchor_entity_type` selector (grid / organization / none),
   because a scheduled skill fans out and the author must choose across what.

### Status: add `draft`

The request asks for "active or draft". `skills.status` has three values, none of
them `draft`. Add it:

```sql
-- 0019_skill_draft_status.sql
ALTER TABLE skills DROP CONSTRAINT IF EXISTS skills_status_chk;
ALTER TABLE skills ADD CONSTRAINT skills_status_chk
    CHECK (status IN ('draft', 'active', 'disabled', 'unusable'));
```

`SkillCatalogStore.all_skills()` filters `.eq("status", "active")` already
(`shared/prompts/skills.py:118`), so a draft is invisible to the model with no code
change. That is the whole point of the status: save an unfinished skill without it
entering anyone's context.

This makes the save flow safer than today's, where the only options are "don't save"
or "save it live".

### Persistence while building

Losing an in-progress build on navigation is the current behaviour and the reason
the page can't be a modal today. Saving as `draft` on modal close — including
partial, invalid step lists — is what makes the modal viable. `_validate_steps`
blocks the transition to `active`, not the save itself.

### What the screenshots show

The submitted builder session ended with `customer_get_my_open_issues` failing and
the model falling through to `escalate_to_support`, producing "I'm sorry, I'm unable
to retrieve the list of open tickets… #NXTAction" as the saved step output — twice,
on two different phrasings.

Two things follow, and they matter for this design:

1. **A step that captured a failure is indistinguishable from one that captured a
   result.** Both are just response text. Saving that skill bakes in an apology.
   The modal should surface when a step's tool calls returned errors and refuse to
   promote to `active` until those steps are re-run — the rewind affordance already
   exists to make that cheap.
2. **`customer_get_my_open_issues` failing for a staff user building a staff skill is
   its own bug**, unrelated to this project. It should be investigated separately
   against production logs rather than absorbed into the builder redesign.

## Failure modes

| failure | behaviour |
|---|---|
| draft with invalid steps | saves; cannot be promoted to `active`; not in catalog |
| function handler removed from registry | skill marked `unusable` at run, matching the existing convention for a deleted creator account |
| scheduled skill's creator deleted | already handled — `status='unusable'` (`0011_skills.sql`) |
| step captured a tool error | flagged in the modal; blocks promotion to `active` |
| fan-out finds no eligible entities | logged per existing `user_schedule_logs` `skipped` status |

## Testing

- Status transitions: `draft → active` requires validation to pass; `active → draft`
  removes it from the catalog.
- `SkillCatalogStore.all_skills()` excludes drafts — assert directly, since this is
  the security-relevant behaviour.
- Function-step validation: unknown handler rejected at save; non-exposed handler
  rejected at save; mutating handler without `allow_write` rejected.
- `kind` absent defaults to `"llm"` — existing rows keep working.
- Recurrence derivation reused from broadcast, not reimplemented.

Per `CLAUDE.md`: `git add -f` new test files under `tests/`, and
`pre-commit run --all-files` before claiming done.

## Sequencing

1. `0019` draft status + catalog exclusion test. Ships alone, no UI.
2. `/skills` list page, read-only, alongside the existing builder page.
3. Move the builder into a modal; add identity and status fields; save-as-draft.
4. Schedule section reusing `_build_recurrence` + `anchor_entity_type`.
5. `kind: "function"` steps + `exposed_to_builder` allowlist, initially empty.
6. Convert the five prompt-only experts, one at a time, verifying each before
   striking through its Doc definition.
