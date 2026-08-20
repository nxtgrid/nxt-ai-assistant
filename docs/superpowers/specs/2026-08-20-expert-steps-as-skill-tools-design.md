# Expert Steps as Skill Tools — Design

**Status:** design, pre-implementation
**Branch:** `feat/expert-steps-as-skill-tools`
**Goal:** make every expert convertible to a skill — specifically LPP
(`package_generator`) and GTR (`grids_technical_reviewer`), the two the
existing converter refuses.

---

## Why this exists

`scripts/convert_expert_to_skill.py` refuses any expert whose doc section
contains a `[function:...]` marker:

```
Cannot convert: 'package_generator' has function steps (...14 markers...)
and stays as code
```

That refusal is correct *today* and should stay until the gaps below are
closed. A plain text split cannot represent a registered-handler call, so
converting LPP right now would produce a prose wrapper with none of the
site-design/BOM/map generation work attached.

P3 (`2026-08-22-p3-skills-lifecycle-and-function-steps.md`) deliberately
scoped these out ("**Do not convert them**"). This design reverses that
decision by building the machinery P3 assumed away.

---

## Ground truth (measured, not assumed)

Taken from the live registry, not from the plan docs:

```
52 registered handlers, 17 with a StepContract
```

| expert family | handlers | contracts | builder-exposed |
|---|---:|---:|---:|
| package_generator (**LPP**) | 17 | **17** | 0 |
| grids_technical_reviewer (**GTR**) | 9 | **0** | 3 |
| ingestion_expert | 9 | 0 | 0 |
| grid_analyst | 7 | 0 | 0 |
| context_expert | 6 | 0 | 0 |
| community_detector / community_sizing / doc_editor / signing | 4 | 0 | 0 |

Three facts that shape everything below:

1. **Only LPP has contracts.** GTR has nine handlers and zero. Contract
   coverage is the prerequisite for every other requirement.
2. **Contracts describe state, not arguments.** 12 of LPP's 17 handlers
   declare `params=()`. `populate_lpp_cells` declares four params — and all
   four are *user overrides*, not its real inputs. Its actual inputs are
   `consumes_state=("document_id",)` plus four `consumes_results` naming
   prior steps. As a tool, it takes almost no LLM-supplied arguments.
3. **Mutation is prose.** `side_effects` is free text ("Writes to the Main
   Input sheet and replaces the map image…"). Nothing can decide "this needs
   mocking" by reading that programmatically.

---

## Requirements

The three from the brief, plus four the investigation surfaced. R0 is the
one that blocks everything else.

### R0 — A run-scoped state carrier *(new — the real blocker)*

**The pipeline experts are state machines, not stateless tools.** LPP steps
communicate through `packet_state` and `accumulated_results`, populated by
whichever step ran before them. `populate_lpp_cells` reads `document_id`
(written by `copy_lpp_template`) and the results of four named prior steps.

Exposing it as a tool without carrying that state means the LLM calls it and
it immediately fails its own precondition — every time. There is nothing for
it to act on.

So a skill run needs a **run-scoped state object** that tool calls read and
write, playing the role `packet_state` plays in a workflow run. This is
requirement zero: without it R1 produces tools that cannot succeed.

The good news is that `StepContext` already carries `packet_state` and
`accumulated_results`, and `StepResult` already returns `state_updates` and
`data`. The work is to give a *skill run* one of these and thread it through
the tool-call loop in `_execute_skill_step_tool_call`, rather than inventing
a new mechanism.

### R1 — Every step callable as a tool, with real I/O

Derive a JSON-schema tool declaration from `StepContract` and route calls
whose name matches a registered handler to `_execute_function_step` instead
of `context.mcp_executor`. Today `_execute_skill_step_tool_call` only knows
about MCP tools, so the 52 handlers are unreachable to the model.

Two contract gaps to close first:

- **Inputs.** Fold `consumes_state` into the input schema, not just `params`
  — that is where the real inputs live (see ground-truth fact 2). A key the
  caller can supply becomes a tool argument; a key only a prior step can
  produce becomes a documented precondition (see R3).
- **Outputs.** `produces_state` names keys with no types or descriptions,
  and `StepResult.data`'s shape is undeclared entirely. The model cannot
  chain call B onto call A's output without a declared return shape. Add an
  output spec to `StepContract`.

### R2 — Mock run for mutating steps

Two halves, and the second is the one that is easy to get wrong.

**Classify mutation machine-readably.** Add an explicit `mutates` flag (and
a category — `external_write`, `db_write`, `notification`, `control_action`)
to `StepContract`. Do not infer it from `side_effects` prose, and do not
infer it from the name-prefix heuristic in `skill_step_bindings.py`
(`get_`/`list_`/`search_`/`check_`/`fetch_`) — that heuristic already
mislabels: `check_existing_review` reads, but `process_doc_edits` and
`store_module` write and match no prefix.

**A mock must return a plausible result, not nothing.** If a mocked
`copy_lpp_template` returns empty, then `populate_lpp_cells` fails its
`document_id` precondition and the rest of the run collapses — the mocked
run would test nothing. So each mutating handler declares a **mock
producer** that returns a `StepResult` populating the same
`produces_state` keys with clearly-marked synthetic values. That is what
makes "run all the immutable steps and mimic the mutable ones" work end to
end rather than stopping at the first mutation.

Toggling lives per-step in `skills.steps` (jsonb — no migration needed) with
a skill-level default; the builder surfaces it as a switch on each mutating
step.

### R3 — Soft failures the model can act on

`_execute_function_step` currently collapses every exception into
`StepResult.failure(str(e))`, which **halts the workflow**. For an
LLM-driven tool call that is the wrong shape twice over: it aborts a run
that a corrected retry would complete, and `str(e)` gives the model nothing
to correct *with*.

Add `StepResult.soft_failure(code, message, remediation)` — returned to the
model as a normal tool result, never halting the run — with codes:

| code | raised when | remediation the model receives |
|---|---|---|
| `missing_parameter` | required arg absent | which arg, its type, where it comes from |
| `invalid_parameter` | wrong type/shape | what was passed vs. expected |
| `unmet_prerequisite` | `consumes_state` key absent | **which step produces it** |
| `guard_satisfied` | `guard_keys` already set | already done; skip it |
| `not_permitted` | caller lacks rights | no retry; ask the user |

**`unmet_prerequisite` is how ordering survives.** This is the direct answer
to P3's objection that pipelines "encode real external ordering constraints"
that free reordering would destroy. Ordering stops being implicit in the
recipe's line order and becomes an enforced precondition carried by the
contract: call `populate_lpp_cells` too early and you get "needs
`document_id`, produced by `copy_lpp_template` — run that first," not a
crash and not silent garbage.

### R4 — Per-handler permission gating *(new)*

MCP tools are permission-filtered through
`permissions_service.get_available_tools(user_context)`. Function-step
handlers have **no permission model at all** — the only gate is
`exposed_to_builder`, a hand-set boolean currently true for 3 of 52.

Exposing all 52 as callable tools without an equivalent check is a
privilege-escalation surface: `send_lpp_map_to_telegram` sends Telegram
messages, `store_module` writes context modules, `embed_and_store` writes
the RAG corpus. Each handler needs a declared permission requirement checked
at call time, failing as `not_permitted` (R3).

### R5 — Long-running steps *(new)*

`update_design_distances` waits on the external design system, and
`package_generator` sleeps ~60s waiting on AppSheet mid-pipeline — the exact
behaviour P3 cited as disqualifying. As a synchronous call inside the tool
loop that blocks the turn and burns a round of
`settings.skill_max_tool_rounds`.

Contracts should declare expected latency, and steps above a threshold need
a non-blocking path (poll/resume) rather than sleeping inside a tool call.

### R6 — Mocked runs must be visibly marked *(new)*

A run with mocked mutations must be labelled as such everywhere it surfaces
— run log, chat response, and anything persisted. A mocked BOM or a mocked
signature request that reads as real is the worst failure mode this feature
can have, and it is a reporting problem, not an execution one.

---

## Feasibility against the two target experts

**GTR (9 handlers)** is the realistic first conversion:

- Its 3 already-exposed handlers (`fetch_chat_chronology`,
  `fetch_grafana_kpis`, `fetch_pending_actions`) are read-only fetches —
  they need R1 only, no mocking.
- `resolve_grid_sheets`, `fetch_cuf_sub_values`, `fetch_existing_review`,
  `check_existing_review` are reads needing contracts.
- Only `write_review_section` genuinely mutates and needs R2.
- `gtr_analysis_conversation` is an LLM tool-loop already — it maps onto an
  `llm` step rather than a `function` step, which is a simplification.

So GTR is roughly: 9 contracts, 1 mock, no long-running steps. It is
convertible once R0–R3 land.

**LPP (17 handlers)** is the harder one and should follow GTR:

- Contracts already exist for all 17 — the best-covered expert in the repo.
- But it has the deepest `consumes_state` chains (R0 pressure), the most
  mutations (Drive, Sheets, Telegram, BOM triggers — R2), the AppSheet sleep
  (R5), and `generate_powerplant_design` alone declares 22 params.

LPP is the right *proof* that the design holds, and the wrong thing to
attempt first.

---

## Proposed phasing

| phase | work | unblocks |
|---|---|---|
| 1 | `StepContract`: `mutates`, category, output spec, permission, latency. Contract lint extended beyond package_generator | everything |
| 2 | R0 run-scoped state carrier threaded through the skill tool loop | R1 |
| 3 | R3 soft failures + prerequisite checking from contracts | ordering safety |
| 4 | R1 tool-schema derivation + routing handler calls in `_execute_skill_step_tool_call` | steps callable |
| 5 | R2 mock producers + per-step toggle + R6 marking | safe authoring |
| 6 | R4 permission gating | safe exposure |
| 7 | GTR: 9 contracts + 1 mock → convert, verify | proof |
| 8 | LPP: R5 async handling → convert, verify | proof |

Phases 1–3 are prerequisites with no user-visible change. The converter's
`has_function_steps` refusal stays in place until phase 7.

---

## Open questions for the operator

1. **Scope.** Convert all 9 experts, or land GTR + LPP and leave the other
   seven as code until they're actually needed?
2. **Mock fidelity.** Should mock producers return *fixed* synthetic values,
   or replay the last real run's outputs for that step? Replay is far more
   useful for authoring and considerably more work.
3. **Ordering.** Is contract-derived `unmet_prerequisite` enough, or should
   a skill be able to pin a hard step order that the model cannot deviate
   from?
4. **The live doc.** `experts.definitions` resolves from a DB/Google Doc
   override, not the bundled file. Converting an expert means striking it
   through there by hand — confirm who does that and when.
