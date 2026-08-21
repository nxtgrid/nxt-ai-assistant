# Expert Steps as Skill Tools — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use superpowers:subagent-driven-development
> or superpowers:executing-plans to work this task-by-task. Steps use `- [ ]` checkboxes.

**Spec:** `docs/superpowers/specs/2026-08-20-expert-steps-as-skill-tools-design.md`
**Branch:** `feat/expert-steps-as-skill-tools`
**Goal:** lift `convert_expert_to_skill.py`'s `[function:...]` refusal so LPP
(`package_generator`), GTR (`grids_technical_reviewer`) and the grid analysis flow
(`grid_analyst`) can be defined as skills — and then actually define them.

---

## Operator decisions already made — do not re-litigate

| decision | ruling | consequence for this plan |
|---|---|---|
| Mock fidelity | **Fixed synthetic values** | `MockSpec` is declarative data, not a callable and not a replay lookup. No run-output persistence in scope. Build the seam so replay *could* back it later, but do not build replay. |
| Ordering enforcement | **Contract preconditions only** | No pinned-order mechanism. The model picks order; `unmet_prerequisite` (Phase 3) is the sole guardrail. Do **not** add a second ordering system. |
| Conversion scope | All experts eventually; **three are the proof bar**: GTR, LPP, and the grid analysis flow (`grid_analyst`) | Phases 8–10 convert those three; Phase 11 authors and activates them as real skills. The other six wait. |
| Sequencing | **GTR → LPP → grid_analyst** | GTR is 9 contracts + 1 mutation + no long-running steps. LPP has the deepest state chains, the most mutations and the AppSheet wait. `grid_analyst` is last because `analyze_failures_loop` is a loop construct that may not map onto the step model at all. |

---

## Critical context for the implementer

### Measured ground truth — verify before trusting any older doc

P3 (`2026-08-22-p3-skills-lifecycle-and-function-steps.md`) says these experts
must **not** be converted. This plan supersedes that specific ruling, but P3's
*reasoning* was sound and the machinery below exists to answer it. Re-measure
rather than trusting either doc:

```bash
GOOGLE_API_KEY=test-key CHAT_DB_URL=https://placeholder.supabase.co \
CHAT_DB_SERVICE_KEY=placeholder MODEL_THINKING=gemini-pro-latest \
MODEL_FAST=gemini-flash-latest MODEL_LITE=gemini-2.5-flash-lite \
FALLBACK_MODEL=gemini-2.5-flash-lite PYTHONPATH=.:chat_orchestrator \
chat_orchestrator/.venv/bin/python -c "
import orchestrator.experts.handlers
from orchestrator.experts.step_registry import get_step_registry
r = get_step_registry()
print(len(r.list_handlers()), 'handlers',
      sum(1 for n in r.list_handlers() if r.get_contract(n)), 'contracts')
print('exposed:', r.builder_exposed_handlers())"
```

As of 2026-08-20: **52 handlers, 17 contracts** (all `package_generator`),
**3 builder-exposed** (all GTR read-only fetches).

### Three facts that shape the work

1. **Contracts describe state, not arguments.** 12 of LPP's 17 handlers declare
   `params=()`. `populate_lpp_cells` declares four, and all four are *user
   overrides* — its real inputs are `consumes_state=("document_id",)` plus four
   `consumes_results`. A tool schema built only from `params` would be empty for
   most steps.
2. **Mutation is prose.** `side_effects` is free text. Never branch on it.
   Also never reuse the `READ_ONLY_TOOL_PREFIXES` heuristic in
   `skill_step_bindings.py` for this — it already mislabels: `store_module` and
   `process_doc_edits` write and match no prefix; `check_existing_review` only reads.
3. **`_execute_function_step` halts the workflow on any exception**
   (`StepResult.failure(str(e))`). That is the wrong shape for an LLM tool call
   and is what Phase 3 replaces.

### Repo traps (from CLAUDE.md — read it, these have all bitten before)

- `tests/` is gitignored. **`git add -f` every new test file**, then
  `pre-commit run --all-files` before claiming done. A plain `git add` is a
  silent no-op and CI will never run the suite.
- `docs/superpowers/plans/` is gitignored too — this file needed `-f`.
- Don't import the `shared.prompts.PROMPTS` singleton in tests; construct a bare
  `PromptLibrary()` or monkeypatch `_db_body_for`/`_gdoc_body_for`.
- `experts.definitions` resolves from a DB/Google Doc override, **not** the
  bundled file. Editing `shared/prompts/library/experts.definitions.prompt`
  changes nothing in production.

---

## Phase 1 — Contract vocabulary

Everything downstream reads these fields. No user-visible change.

**Files:** `chat_orchestrator/orchestrator/experts/step_contracts.py`,
test `chat_orchestrator/tests/experts/test_step_contracts.py`

- [x] **Task 1.1 — `OutputSpec`.** `name`, `value_type`, `description`,
      `where` (`"state"` | `"data"`). Declares what a step returns so a caller
      can chain onto it; `produces_state` alone gives names with no types and
      `StepResult.data` is undeclared entirely.
- [x] **Task 1.2 — `MUTATION_KINDS`** = `("external_write", "db_write",
      "notification", "control_action")`.
- [x] **Task 1.3 — `MockSpec`.** Fields `state_updates: Dict`, `data: Dict`,
      `message: str`. Deliberately **not** `frozen=True` (it holds dicts, so a
      generated `__hash__` would raise; nothing hashes contracts, and failing
      loudly beats a silently-unhashable frozen type). Values must be
      self-evidently synthetic (`MOCK-` prefixes, obviously fake ids).
- [x] **Task 1.4 — new `StepContract` fields**, all defaulted so the 17 existing
      contracts and 35 uncontracted handlers keep working untouched:
      `mutates: bool = False`, `mutation_kind: str = ""`,
      `outputs: tuple[OutputSpec, ...] = ()`, `mock: Optional[MockSpec] = None`,
      `required_permission: str = ""`, `expected_latency_seconds: float = 0.0`.
- [x] **Task 1.5 — `validate_mock_covers_outputs(contract)`.** A `MockSpec` that
      doesn't populate every `produces_state` key is the failure mode that makes
      mocked runs worthless: mock `copy_lpp_template` into an empty result and
      `populate_lpp_cells` fails its `document_id` precondition, so the run dies
      at the first mutation and proves nothing. Return findings, don't raise.
- [x] **Task 1.6 — tests** for each of the above, plus a regression asserting a
      bare `StepContract()` still constructs with every new field defaulted.

**Done 2026-08-20** (`428df915`). 27 tests.

## Phase 2 — Run-scoped state carrier

The blocker. Without it, Phase 4's tools cannot satisfy their own preconditions.

**Files:** `workflow_executor.py`, `step_context.py`,
test `chat_orchestrator/tests/experts/test_skill_run_state.py`

- [x] **Task 2.1** — Give a skill run one state object playing the role
      `packet_state` plays in a workflow run. Thread it through the
      `_execute_skill_step_tool_call` loop so each call reads prior calls'
      `state_updates` and `data`. **Reuse `StepContext.packet_state` /
      `accumulated_results` and `StepResult.state_updates` / `data`** — do not
      invent a parallel mechanism.
- [x] **Task 2.2** — Verify state survives across rounds of the tool loop in
      `_execute_skill_step_tool_call`, not just within one round.
- [x] **Task 2.3** — Test: two chained handler calls, the second reading a key
      the first produced.

**Done 2026-08-20** (`ba8c3aea`). `StepContext.apply_result()`. 13 tests.

## Phase 3 — Soft failures

**Files:** `step_context.py` (`StepResult`), `workflow_executor.py`
(`_execute_function_step`), test `test_soft_failures.py`

- [x] **Task 3.1 — `StepResult.soft_failure(code, message, remediation)`.**
      Returned to the model as a normal tool result. **Must not halt the run** —
      that is the whole difference from `StepResult.failure()`.
- [x] **Task 3.2 — codes:** `missing_parameter`, `invalid_parameter`,
      `unmet_prerequisite`, `guard_satisfied`, `not_permitted`.
- [x] **Task 3.3 — precondition checking from contracts.** Before running a
      handler, check `consumes_state` against run state. On a miss, return
      `unmet_prerequisite` naming **which step produces the key** (derive by
      scanning other contracts' `produces_state`).

      This is how ordering survives — it is the direct answer to P3's objection
      that these pipelines encode ordering that free reordering destroys.
      Ordering stops being implicit in recipe line-order and becomes an enforced
      precondition. Per the operator ruling, this is the *only* ordering
      guardrail; do not add a pinned-order mechanism.
- [x] **Task 3.4 — `guard_satisfied`** from `guard_keys`, so a re-called step
      reports "already done" instead of redoing external work.
- [x] **Task 3.5** — `_execute_function_step` keeps `StepResult.failure` for
      genuine crashes; only contract-detectable misuse becomes a soft failure.

**Done 2026-08-21** (`d44551c1`). Task 3.3 reuses the pre-existing (unrelated
"Phase C/D") `validate_step_prerequisites`/`PrereqReport` rather than
re-deriving producer-chain scanning — see
`WorkflowExecutor._soft_failure_before_running_step`'s own docstring for the
full reasoning. Only `unmet_prerequisite`/`guard_satisfied` get automatic
detection here; `missing_parameter`/`invalid_parameter`/`not_permitted` are
vocabulary a handler or Phase 6 can use, not auto-fired by this phase. 20
tests.

## Phase 4 — Steps as callable tools

**Files:** new `step_tool_schema.py`, `workflow_executor.py`,
tests `test_step_tool_schema.py`

- [x] **Task 4.1 — derive a JSON-schema tool declaration from `StepContract`.**
      Inputs come from `params` **and** caller-suppliable `consumes_state` keys
      (see fact 1 — `params` alone is empty for most steps). A key only a prior
      step can produce is documented as a precondition, not an argument.
      Outputs come from `outputs`.
- [x] **Task 4.2 — route by name in `_execute_skill_step_tool_call`:** a call
      matching a registered handler goes to `_execute_function_step`; everything
      else keeps going to `context.mcp_executor`. Preserve the existing
      never-raise contract — failures come back as `ToolCallResult(success=False)`.
- [x] **Task 4.3** — only contract-bearing, permission-cleared handlers get
      declared. No contract ⇒ not offered.
- [x] **Task 4.4** — tests: schema shape, handler-vs-MCP routing, unknown name.

**Done 2026-08-21** (`3b546172`). One addition beyond the literal task text,
made for a concrete reason: mutating steps also need `allow_write=True`
(mirroring `filter_tools_for_step`'s existing MCP read-only gate, via
`contract.mutates` instead of a name prefix) — without it, "permission-
cleared" alone would let a non-allow_write skill step reach real handlers
like `write_review_section`/`send_lpp_map_to_telegram` the moment Phase 6
adds real permission checking, since `required_permission` and `mutates`
answer different questions. `allow_write` threaded through
`_execute_llm_step` → `_call_llm_step_with_tools` →
`_execute_skill_step_tool_call` to reach the routing check.

## Phase 5 — Mock mode

**Files:** `step_context.py`, `workflow_executor.py`, `skill_validation.py`,
`anansi_app/nicegui_app/pages/skill_builder.py`, tests

- [x] **Task 5.1 — `dry_run: bool` on `StepContext`**, threaded through
      execution.
- [x] **Task 5.2** — when `dry_run` and `contract.mutates`, return the
      `MockSpec`'s result instead of calling the handler. Non-mutating steps run
      for real in both modes — that is the point of the feature.
- [x] **Task 5.3 — per-step toggle in `skills.steps`** (jsonb — **no migration
      needed**) plus a skill-level default; builder surfaces a switch on each
      mutating step.
- [x] **Task 5.4 — save-time validation** in `skill_validation.py`: a mutating
      step with no `MockSpec` cannot be saved mock-enabled.
- [x] **Task 5.5 — R6 marking.** A mocked run must be labelled mocked in the run
      log, the chat response, and anything persisted. A mocked BOM or signature
      request reading as real is the worst failure this feature can produce.

**Done 2026-08-21** (`b0fab503`). "Skill-level default" turned out not to
need new persisted state at all: `StepContext.dry_run` is set from
`metadata.dry_run` at whatever point a run gets triggered (nothing sets
that key yet -- Phase 11 will be the first). `ParsedStep.mock` is the
per-step override, read from `skills.steps[].mock` by
`skill_runner.build_parsed_steps`, mirroring `allow_write` exactly. The
builder's switch lives in `_render_pending_step` (not `_render_step` --
this chat-driven widget can't create or re-run a `kind:"function"` step at
all, only preserve one from a reopened/converted skill), gated on a
`mutates` key only Phase 7's converter will ever stamp — dormant until then
by construction, not by an oversight.

## Phase 6 — Permission gating

- [x] **Task 6.1** — check `required_permission` at call time; failure returns
      `not_permitted` (Phase 3). MCP tools filter through
      `permissions_service.get_available_tools(user_context)`; function handlers
      have **no** permission model today — `exposed_to_builder` is a hand-set
      boolean true for 3 of 52. Exposing all 52 without this is privilege
      escalation (`send_lpp_map_to_telegram`, `embed_and_store`, `store_module`).
- [x] **Task 6.2** — decide `exposed_to_builder`'s fate: keep as a second gate,
      or fold into the permission model. Do not leave two overlapping gates
      undocumented.

**Done 2026-08-21** (`9255858c`). `required_permission` names a role
checked against `UserContext.roles`, with `is_staff=True` always clearing
(the one boundary this codebase already enforces elsewhere). Declared and
routable deliberately stopped being the exact same predicate:
`is_declared_function_step` (routing) stays structural-only, so a
permission-gated call still reaches `_execute_declared_function_step_call`
and gets an explicit `not_permitted` there — declaring it hidden but
routing it rejectable, on purpose, gives a clearer failure than an MCP
fallthrough would. Task 6.2: kept as two gates, documented on
`StepHandlerRegistry.expose_to_builder` — design-time authoring curation
vs. runtime call authorization, answering genuinely different questions.

## Phase 7 — Lift the converter's refusal

Machinery, and a prerequisite for every conversion below.

**Files:** `scripts/convert_expert_to_skill.py` (gitignored — `git add -f`)

- [x] **Task 7.1** — replace `has_function_steps`' blanket refusal with a check
      that every named `[function:...]` handler is contract-bearing and
      permission-cleared. Refuse only handlers that are not, naming them.
- [x] **Task 7.2** — carry `[function:...]` markers through
      `split_instructions_into_steps` as `kind: "function"` steps instead of
      flattening them into prose. **This is the bug the whole plan exists to
      fix**: today a conversion would silently drop the orchestration and leave
      a prose wrapper with none of the real work attached.
- [x] **Task 7.3** — keep the dry-run default and the "read the step text before
      `--apply`" warning. The script's own docstring is right that a green exit
      code is necessary but not sufficient.

**Done 2026-08-21** (`a26482f7`). "Permission-cleared" landed as "does not
block conversion at all" rather than a hard gate: `required_permission` is
a runtime, per-caller check (Phase 6), and this script has no caller to
check it against — refusing on it here would be pre-judging something only
meaningful at run time. Surfaced in the printed output instead. Function
steps whose contract mutates are stamped `mutates`/`mock: True` on
conversion, closing the loop with Phase 5's builder switch and
`unmockable_handlers` check.

## Phase 8 — GTR (`grids_technical_reviewer`) — first proof

9 handlers, **0 contracts**, 3 already builder-exposed. The easiest of the three.

- [x] **Task 8.1** — contracts for all 9 handlers.
- [x] **Task 8.2** — `write_review_section` is the only real mutation: mark
      `mutates=True` and give it a `MockSpec`.
- [x] **Task 8.3** — `gtr_analysis_conversation` is already an LLM tool-loop —
      map it onto an `llm` step, not a `function` step.
- [x] **Task 8.4** — extend the contract lint beyond `package_generator`
      (`test_contract_lint.py`'s `_PACKAGE_GENERATOR_MODULE_PREFIX`) to cover GTR.
- [x] **Task 8.5** — the 3 exposed fetches (`fetch_chat_chronology`,
      `fetch_grafana_kpis`, `fetch_pending_actions`) are read-only and need no
      mocks — confirm they stay real in mock mode.

**Done 2026-08-21** (`3a180691`). Real finding, left as-is (not silently
"fixed"): `fetch_chat_chronology` reads `resolved_grids`, but
`resolve_grid_sheets` produces `grids_to_review` — no producer for
`resolved_grids` among GTR's 9 handlers, so that step's grid-chronology
fetch looks like dead code today. `gtr_analysis_conversation` still got a
contract (so it isn't invisible to the lint), with its `side_effects` text
itself carrying the "represent as `[llm]`, not `[function:...]`" note for
whoever reviews Phase 11's converted draft. Contract lint generalized to a
per-expert `_CONTRACTED_EXPERTS` dict; GTR's `consumes_state` keys were all
reachable within GTR itself — no allowlist entries needed.

## Phase 9 — LPP (`package_generator`) — second proof

17 handlers, **17 contracts** (best coverage in the repo), but the deepest state
chains, the most mutations, and the AppSheet wait.

- [x] **Task 9.1 — R5 long-running steps.** `update_design_distances` waits on
      the design system and the pipeline sleeps ~60s on AppSheet — exactly the
      behaviour P3 cited as disqualifying. Blocking inside a tool call burns a
      round of `settings.skill_max_tool_rounds`. Declare
      `expected_latency_seconds`; steps above a threshold get a poll/resume path
      instead of sleeping in-call.
- [x] **Task 9.2** — audit all 17 existing contracts for the Phase 1 fields
      (they predate `mutates` / `outputs` / `mock`).
- [x] **Task 9.3** — `MockSpec`s for every mutating step (Drive, Sheets,
      Telegram, BOM triggers). `copy_lpp_template`'s mock must populate
      `document_id` or the whole chain collapses — see Task 1.5.
- [x] **Task 9.4** — `generate_powerplant_design` declares 22 params; check the
      derived schema is coherent at that size.

**Done 2026-08-21** (`670837c1`). Task 9.1 only partly built as originally
worded: `expected_latency_seconds` is declared on all three steps that
actually sleep in-call (180s/120s/60s), and the derived tool description
now warns above a threshold — but no real poll/resume EXECUTION path
exists. Building one would need a durable background-task/scheduling
subsystem this codebase doesn't have; a process-local `asyncio.Task`
wouldn't survive past the request that started it in this deployment, and
scoping a new one to a single handler risked being fragile for real
production LPP runs. Flagged explicitly in `update_design_distances.py`'s
own contract as deferred, not silently dropped. Every one of the 14
mutating contracts' `MockSpec`s verified via `validate_mock_covers_outputs`
to cover 100% of its own `produces_state` — zero findings.

## Phase 10 — Grid analysis flow (`grid_analyst`) — third conversion

7 handlers, **0 contracts**: `analyze_failures_loop`, `calculate_kpi_values`,
`categorize_issues`, `create_analysis_doc`, `create_kpi_doc`,
`fetch_month_metrics`, `fetch_multi_grid_metrics`.

> **Disambiguation:** this is `grid_analyst` (grid *analysis*), a different
> expert from Phase 8's `grids_technical_reviewer` (grid *technical review*).
> If the operator's "grid analysis flow" meant GTR, Phase 8 already covers it
> and this phase is redundant — confirm before starting.

- [ ] **Task 10.1** — **check whether `grid_analyst` is still struck through
      (disabled) in the live `experts.definitions` override** before doing any
      work. P3 recorded it as struck through. Converting a disabled expert is
      fine — it is the safest possible first conversion — but the operator
      should know that is what they are getting.
- [ ] **Task 10.2** — contracts for all 7 handlers.
- [ ] **Task 10.3** — `create_analysis_doc` and `create_kpi_doc` create Google
      Docs: `mutates=True`, `mutation_kind="external_write"`, each with a
      `MockSpec` returning a synthetic doc id. The two `fetch_*` steps are reads.
- [ ] **Task 10.4** — `analyze_failures_loop` is a loop construct — confirm it
      maps onto the step model at all before assuming it converts cleanly. This
      is the one structural unknown in this phase.
- [ ] **Task 10.5** — extend the contract lint to cover `grid_analyst`.

## Phase 11 — Author, verify and activate the three skills

The actual deliverable: working skills, not just the machinery that permits them.
**Starts only once Phases 1–10 have landed and `pre-commit run --all-files` is clean.**

- [ ] **Task 11.1** — run the converter (dry run first) for each of
      `grids_technical_reviewer`, `package_generator`, `grid_analyst`. Read every
      step's text before `--apply`. Each lands as `status='draft'`.
- [ ] **Task 11.2** — review each draft in `/workflows`: step order, declared
      inputs, which steps are marked mutating, which have mock toggles.
- [ ] **Task 11.3 — mock run each skill end-to-end.** Every mutation mocked,
      every read real. This is the acceptance test for the whole plan: if a
      mocked run collapses at the first mutation, a `MockSpec` is not populating
      its `produces_state` keys (Task 1.5).
- [ ] **Task 11.4 — real run each skill end-to-end**, and diff the output against
      what the expert produces today. A converted LPP must produce the same
      site design / BOM / map artefacts as the expert it replaces.
- [ ] **Task 11.5** — promote `draft` → `active` only after 11.4 passes for that
      skill. Promote independently; do not gate all three on the slowest.
- [ ] **Task 11.6** — strike each converted expert through in the live
      `experts.definitions` override (manual, and **not** the bundled file —
      editing `shared/prompts/library/experts.definitions.prompt` changes
      nothing in production). Do this per-skill, after that skill is active and
      verified, so there is never a window with neither expert nor skill live.

---

## Verification

```bash
pre-commit run --all-files          # not just ruff/pytest — see CLAUDE.md
```

Per-phase: force-add new tests (`git add -f`), re-run the hook, re-run the suites.

**Done means:** GTR, LPP and the grid analysis flow all exist as `active` skills;
each ran end-to-end once fully mocked and once for real; the real runs match what
the experts produce today; each mocked run was visibly marked as mocked at every
surface; and each converted expert is struck through in the live definitions doc.
