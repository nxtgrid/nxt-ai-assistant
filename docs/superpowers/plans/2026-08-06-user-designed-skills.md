# User-Designed Skills (Agent Builder)

Replaces the persistent-agent concept with **user-designed skills**: ordered LLM
steps, authored interactively in the web app, saved as an expert workflow, and
run on a schedule (or an alert trigger) inside a specific Telegram group.

**Status:** complete — all 7 phases (0-6) shipped
**Author:** design worked out 2026-08-06

---

## Read this first

This plan is written to be executed by an implementer who has not seen the
design conversation. Every phase is independently shippable and independently
revertible. Do not start a phase before its stated prerequisites are merged.

**Repo rules that will bite you (see `CLAUDE.md`):**

- Run `pre-commit run --all-files` before claiming anything is committed. Not
  `pytest`, not `ruff check .` — both silently pass while files are missing.
- New test files under any `tests/` directory need `git add -f`. A plain
  `git add` is a silent no-op; the commit succeeds and CI never runs them.
- This plan file lives under `docs/superpowers/plans/`, which is also
  gitignored. It needs `git add -f` too.

---

## The model, in one page

A **skill** is an ordered list of steps. Each step is an LLM call with tools.
Steps pass data through `packet_state` using `{{var}}` placeholders.

A skill is authored in a chat-like builder in the web app: each user message is
one step. The user can rewind to any step, reword it, and continue — everything
after the rewind point is archived. What survives archiving *is* the skill.

A saved skill runs as an expert workflow. It is scheduled with recurrence, or
triggered by an alert arriving on `/chat/notify`. Every run is scoped to exactly
one Telegram group, which supplies its authorization.

### Run-mode output: which steps talk to the user

Builder mode and run mode show different amounts of the same execution.

**Builder mode** (Phase 4, a human is watching live): every step's full
response is shown, one per chat message, as it happens. No summarization —
this is the design surface, the user needs to see everything to iterate.

**Run mode** (Phase 5, a scheduled/triggered execution delivering into a
Telegram group, nobody watching live): by default, only the run's *last*
step delivers its full response (text + attachments + choices — the whole
`ResponseEnvelope` from Phase 1), preceded by a short summary of every step
that ran before it. Nothing else is sent.

A step can be flagged `is_response_step: true` (a checkbox next to that
step's message in the builder). Each flagged step, when reached during a
run, delivers its own full response immediately — preceded by a short
summary of only the steps *since the previous response step* (or since the
run started, for the first one). The final step is always treated as an
implicit response step even if not flagged, so a skill with zero flagged
steps still delivers exactly one message: a summary of steps 1..N-1 plus
step N's full response.

Concretely, for a 5-step skill with steps 2 and 5 flagged: a run sends two
messages — (short summary of step 1) + (step 2's full response), then later
(short summary of steps 3–4) + (step 5's full response, since 5 is both
flagged and the last step).

**Building the short summary: prefer free, not another LLM call.** Every
step already produces a `StepExecutionRecord.result_summary`
(`workflow_executor.py`'s `ExecutionSummary`/`StepStatus` machinery). Template
the "steps since the last response step" summary from the already-collected
`result_summary` strings first (join with a bullet or arrow) — this costs
nothing and Phase 0 is specifically making LLM cost visible, so don't
quietly reintroduce it here by summarizing via a fresh model call unless the
templated version reads too roughly in practice. If a real LLM summarization
step turns out to be necessary, it is itself a `[llm]` step and its tokens
get accounted for by Phase 0's instrumentation like any other.

This applies to Phase 5's delivery logic and to Phase 3's storage shape
(`is_response_step` per step) and Phase 4's builder UI (the checkbox). It does
not change Phase 1's envelope shape — a "short summary" message and a "full
response" message are both just envelopes, one with a templated text body,
one with the step's real output.

### Why this shape

Almost all of the machinery already exists:

| Need | Already in the codebase |
|---|---|
| LLM steps in a workflow | `ParsedStep.step_type == "llm"`, `_execute_llm_step` (`workflow_executor.py:2325`) |
| `{{var}}` substitution | `render_body` (`shared/prompts/render.py:38`) — strict, supports `{{> partials.x}}` |
| Inter-step state | `packet_state` + `StepContract.consumes_state/produces_state` |
| Versioned DB-first storage | `prompt_versions` / `prompt_labels` + propose/publish |
| Fan-out across entities | `agent_worker._get_eligible_entities` / `_build_anchor_metadata` |
| Grid → Telegram chat mapping | `auth_service.get_eligible_grids_for_agents()` (eligibility = has a chat) |
| Chat-scoped authorization | `auth_service.resolve_permissions_from_chat()` |
| Recurrence parsing | `shared/scheduling/recurrence.py` |

### Facts established during design — do not re-derive

1. **The main conversation graph has no checkpointer.** `full_conversation_graph.py:260`
   is a bare `builder.compile()`. History reloads from `chat_messages` every
   turn. Rewind is therefore just archiving rows — there is no checkpoint state
   to unwind. (The only checkpointer in the codebase belongs to the persistent
   agent worker, which Phase 6 deletes.)

2. **Authorization is org-scoped, not grid-scoped.** `grid_ids` is
   *every grid in the resolved org* (`auth_service.py:234`). Per-grid partial
   access does not exist. `is_staff` is derived from the **chat's** org, not the
   user's own (`auth_service.py:231`) — so a staff-authored skill running in a
   customer group automatically receives customer-level tools and instructions.
   This is exactly the desired "as if the user asked in that chat" behavior; do
   not build a parallel permission path.

3. **User liveness** = a row in `public.accounts` with `deleted_at IS NULL`.

4. **~~LLM steps cannot call tools today.~~ Fixed by Phase 2.** Was:
   `_execute_llm_step` called `generate_messages(..., tools_payload=None)`.
   Now: gated entirely behind a new `ParsedStep.is_skill_step` flag (default
   `False` for every step parsed from a Google Doc, so this changed zero
   production expert-workflow behavior) — see Phase 2's implementation note
   for why that gate exists and wasn't in the original plan text.

5. **~~LLM step output only reaches state if the step name contains
   "parse".~~ Fixed by Phase 2** for skill steps specifically (the `"parse"`-
   name convention is untouched for everything else). A skill step's
   `-> {{var}}` write clause is extracted and persisted to `packet_state` --
   see `skill_step_bindings.py`.

6. **~~The API response envelope discards everything but text.~~ Fixed by
   Phase 1.** Was: `_handle_webhook_async` returned `{success, message,
   session_id}`, dropping `tool_results` and `reply_markup`. Now: the direct
   API response (both the async path and a legacy sync entrypoint) also
   includes `attachments`/`choices`/`tool_calls`/`tokens`, built via
   `orchestrator/models/envelope.py`. See Phase 1's "implementation note" for
   why this is a *derived view* over the original tuple rather than a
   replacement for it — that distinction matters for anyone building on this.

---

## Decisions already made (do not relitigate)

- **No streaming.** Design runs are blocking HTTP requests with a spinner.
- **No branching.** Rewind discards the tail; it does not replay it. After
  rewinding to step 3, steps 4+ are gone and the user re-does them by hand.
- **Side effects of discarded steps stand.** A ticket filed by a rewound step
  stays filed. The UI says so; nothing is rolled back.
- **`{{var}}` writes are explicit only.** A step writes a variable only when the
  author declares it. Never infer variables from free-text LLM output.
- **Variables are flat in v1.** No `{{a.b}}` — `_VARIABLE` is `[A-Za-z0-9_]+`
  and extending it is out of scope.
- **Every run is scoped to one Telegram group.** That group supplies auth and
  receives output. Grid-anchored (O&M groups), org-anchored (customer groups),
  or a fixed internal group (engineering, sales).
- **Mini app is out of scope.** Web app only.
- **Skills are staff-flagged** like MCP tools and slash commands.

---

## Phase 0 — Token and cost instrumentation

Ships alone. No dependencies on anything else in this plan. Do this first: it is
useful even if the rest is never built, and it tells you what a skill run costs
before one exists.

### Context

Token accounting today is partial and inconsistent:

- Interactive turns write totals into `chat_messages.metadata.{input_tokens,
  output_tokens}` (`full_conversation_graph.py:534-545`). This works.
- `TokenUsageModel` / `save_token_usage()` / the `token_usage` table are **dead
  code** — never called, and `token_usage` is not in `db/schema/chat_db.sql`.
  Do not build on them. Delete them in this phase.
- Workflow steps make their own LLM calls that land in no total anywhere.
- There is **no pricing table** in the repo.

### Work

1. **Delete dead code.** Remove `save_token_usage()` from
   `chat_orchestrator/orchestrator/services/supabase_client.py:844` and
   `TokenUsageModel` from `orchestrator/models/database.py:80`. Confirm with
   grep that nothing references them first.

2. **Add a pricing table.** New module
   `shared/llm/pricing.py`:
   - `PRICES: dict[str, tuple[Decimal, Decimal]]` mapping model id →
     (usd_per_1m_input, usd_per_1m_output).
   - Cover every model in `orchestrator/config/settings.py`: the values of
     `GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL`, `GEMINI_AGENT_PRO_MODEL`,
     `GEMINI_DEEP_THINKING_MODEL`, `VERIFICATION_MODEL`.
   - `estimate_cost_usd(model, input_tokens, output_tokens) -> Decimal | None`.
     Returns `None` for an unknown model — never guess, never fall back to a
     default price.
   - Docstring must state this is an estimate and when the table was last
     checked.

3. **Instrument workflow steps.** In
   `chat_orchestrator/orchestrator/experts/workflow_executor.py`:
   - `_execute_llm_step` (line 2325) already receives a response object with
     usage. Accumulate `input_tokens` / `output_tokens` onto `ExecutionSummary`
     (line 130).
   - Persist the per-run totals to `agent_work_packets` — add a
     `token_usage jsonb` column (migration below) written at the end of
     `_execute_workflow_inner`.

4. **Migration** `db/migrations/0010_run_token_usage.sql`:
   ```sql
   ALTER TABLE agent_work_packets
     ADD COLUMN IF NOT EXISTS token_usage jsonb NOT NULL DEFAULT '{}';
   ```
   Shape: `{"input_tokens": N, "output_tokens": N, "model": "...",
   "cost_usd": "0.0123", "rounds": N}`. `cost_usd` is a *string* (Decimal), not
   a float. Omit the key entirely when the model is unpriced — do not write
   `null` and do not write `0`.
   Also add the column to `db/schema/chat_db.sql` so a fresh install matches.

5. **Aggregation.** In `anansi_app/services/supabase_reader.py`, add
   `get_run_usage_by_skill(days_back: int = 7) -> dict[str, dict]` returning
   per-skill `{runs, input_tokens, output_tokens, cost_usd, failures}`.
   Until Phase 3 exists there are no skills — key it by `packet_type` for now
   and switch the key in Phase 3.

6. **UI.** Add a "Last 7 days" column to the agents page
   (`anansi_app/nicegui_app/pages/agents.py`). Render `—` for unpriced models.
   Label the header "Est. cost (7d)" — the word *Est.* is required.

### Acceptance criteria

- `grep -rn "save_token_usage\|TokenUsageModel"` returns nothing outside tests.
- Running any expert workflow writes a non-empty `token_usage` on its packet row.
- A model absent from `PRICES` produces a row with tokens but no `cost_usd`,
  and the UI shows `—` rather than `$0.00`.
- New tests in `chat_orchestrator/tests/` (remember `git add -f`):
  `test_pricing.py` — known model, unknown model, zero tokens.

---

## Phase 1 — Transport-neutral response envelope

**Prerequisite:** none. Can run parallel to Phase 0.

### Context

`_handle_webhook_async` computes `response_text, tool_results, reply_markup`
and returns only the text (`handler.py:2658-2678`). Telegram callers get images
via separate `sendPhoto` calls before the text (`handler.py:2783`, `3005`) and
interactive controls via `reply_markup`. API callers get neither.

The builder needs to render what a step produced. This also defines what a
"step result" is for Phase 2's `{{var}}` extraction, so it must land first.

### Work

1. **Define the envelope.** New `orchestrator/models/envelope.py`:
   ```python
   @dataclass
   class Attachment:
       kind: str          # "image" | "document"
       url: str | None    # Drive URL or proxy URL
       data_b64: str | None
       mime_type: str
       caption: str = ""

   @dataclass
   class Choice:
       label: str
       value: str         # callback_data equivalent

   @dataclass
   class ResponseEnvelope:
       text: str
       attachments: list[Attachment]
       choices: list[Choice]          # from reply_markup
       tool_calls: list[str]          # tool names invoked, for the builder UI
       tokens: dict[str, int]
       session_id: str
   ```

2. **Build it once, adapt per transport.**

   **Implementation note (added during Phase 1, supersedes this item's
   original wording):** `process_webhook_with_graph` does **not** return a
   `ResponseEnvelope` directly — it still returns its original
   `(text, tool_results, reply_markup)` tuple, widened to a 4-tuple with
   `tokens` added. `build_response_envelope()` in `envelope.py` builds the
   envelope as a *derived view* over that same tuple, called only where the
   transport-neutral shape is actually needed.

   Reason for the deviation: `handler.py`'s Telegram-sending code
   (`_process_telegram_async`, `_process_and_respond_async`) turned out to
   depend on the FULL `ToolCallResult` objects for more than image
   extraction — `.output`/`.error`/`.success` feed escalation-message
   formatting, `.name` drives a tool-triggered button special-case
   (`schedule_create_user_agent` → "View Agent State" button), and LLM-authored
   `[BUTTONS]` blocks get parsed out of the response text itself
   (`parse_procedure_buttons`) *after* the graph call, mutating `reply_markup`
   in place. None of that survives a lossy `ToolCallResult` → `Attachment`
   projection. Replacing the tuple with an envelope-only return would force
   rewriting all of that in the same change — a materially bigger, riskier
   refactor than "byte-identical, this is a refactor not a redesign" calls
   for, and bigger than what was understood when this plan was written.

   So: 7 call sites across `handler.py` and `callback_handlers.py` were
   widened to unpack 4 values instead of 3 (the 5 Telegram-bound ones ignore
   the new `tokens` element; the 2 direct-API ones — one async, one a legacy
   sync entrypoint — use it to build the envelope). **The production Telegram
   send path does not go through the envelope or through
   `choices_to_reply_markup`.** That function exists so the envelope's shape
   is provably round-trippable (tested), not because anything wired sends
   through it yet. A future phase that wants Telegram sending to actually run
   through the envelope needs to fold in that business logic deliberately —
   don't assume it's already unified because the envelope type exists.

   - The API path (both the async and legacy-sync direct-response branches in
     `handler.py`) builds the envelope and serializes it into the JSON
     response.
   - The Telegram path is otherwise untouched. Existing Telegram tests pass
     unmodified — verified, not just intended.

3. **Backwards compatibility.** The API response keeps its existing top-level
   `{success, message, session_id}` keys (other callers depend on them —
   `anansi_app`, the scheduler, n8n). Add the rest alongside. `message` stays
   the plain text.

### Acceptance criteria

- Every existing Telegram test passes with no modification.
- `POST /chat` with `X-Api-Key` on a message that produces a chart returns a
  non-empty `attachments` array.
- A message that would produce Telegram buttons returns a non-empty `choices`.
- New test `chat_orchestrator/tests/test_response_envelope.py` covering:
  text-only, text+image, text+choices, and the Telegram adapter round-trip.

---

## Phase 2 — LLM steps get tools and `{{var}}` output binding

**Prerequisite:** Phase 1 (the envelope defines what a step result is).

This is the load-bearing phase. Everything after it is UI.

**Implementation note (added during Phase 2, read before touching this
code):**

- **`ParsedStep` gained two new fields** the original plan text didn't
  anticipate: `is_skill_step: bool = False` and `allow_write: bool = False`.
  Nothing in this plan said how the executor should tell a skill step
  apart from an ordinary Google-Doc-authored `[llm]` step -- and giving
  *every* `[llm]` step tool access unconditionally would have been a real
  production-behavior change (an existing expert's reasoning step gaining
  the ability to call tools mid-step, changing its output), not the
  additive, opt-in capability this phase is supposed to be. `is_skill_step`
  defaults `False` everywhere a step is parsed from a Google Doc today, so
  none of this phase's new code paths (tool resolution, `{{var}}` binding)
  execute for any currently-live workflow. Phase 3 sets `is_skill_step=True`
  (and `allow_write` per-step) when it constructs `ParsedStep` from a stored
  skill's steps.
- **Pause-on-missing-value does not go through `StepLoopSignal`.** The plan
  named `action="return"` as the mechanism, but that vocabulary belongs to
  `_execute_one_step`'s while-loop, a different scope than `_execute_llm_step`
  (which has always returned a plain `str`, never a `StepLoopSignal`).
  Reworking that contract would have touched `_execute_one_step`'s dispatch
  logic broadly. Instead: a new `SkillStepVariableError` (a `RuntimeError`
  subclass) is raised for both failure modes -- an unresolvable `{{read}}`
  and a declared write that produced nothing -- and deliberately left
  uncaught by `_execute_llm_step`'s own blanket `except Exception`, so it
  propagates to `_execute_one_step`'s *existing* except-block, which already
  calls `packet_service.fail_packet(...)` with a clear reason. Same
  "terminal failure over a new resumable state" reasoning as Phase 1's
  envelope deviation -- see that phase's note for the pattern.
- **The tool-round bound reuses `settings.max_tool_rounds`** (the same
  setting the main chat graph's tool loop already uses), not a new
  skill-specific setting -- consistent with Phase 5 item 7 describing that
  *existing* setting as what "a 'find tickets, evaluate each' step will
  exhaust," implying this phase wires to it and Phase 5 raises/separates it
  later, not that Phase 2 invents the separate setting itself.
- New `orchestrator/experts/skill_step_bindings.py` holds the shared,
  regex-driven pieces (write-clause parsing, `RESULT:` extraction, read-only
  tool-name filtering) so `workflow_executor.py` (runtime) and
  `skill_validation.py` (static, save-time) can't drift on what counts as a
  write, a read, or a read-only tool.

### Work

1. **Tools on LLM steps.** In `_execute_llm_step`
   (`workflow_executor.py:2325`), replace `tools_payload=None` with a real
   payload resolved from the step's allowlist.

   **Default is read-only.** A step gets only tools whose name matches a
   read-only prefix (`get_`, `list_`, `search_`, `check_`, `fetch_`) unless the
   step explicitly declares `allow_write: true`. This is what makes the
   "rewound steps already took effect" trade-off survivable during design.
   Put the prefix list in one named constant with a comment explaining why.

   Resolve the candidate set through `permissions_service.get_available_tools(user_context)`
   — the same call `prepare_tools.py:487` makes. Do not build a second tool path.

2. **Explicit output binding.** A step declares at most one output variable.

   Syntax in the step instruction, parsed out *before* rendering reads:
   ```
   List all open tickets for {{grid}} → {{open_tickets}}
   ```
   - `→ {{name}}` (or `-> {{name}}`) at the end of the instruction declares a
     write. Everything else in `{{...}}` is a read.
   - Strip the write clause from the text sent to the LLM; instead append a
     short extraction instruction telling the model to end its reply with the
     value on its own line.
   - Store the extracted value at `packet_state[name]` via
     `packet_service.update_state`.
   - If the step declares a write and nothing extractable comes back, **pause
     the run** with a clear message ("step 3 declared `{{open_tickets}}` but
     produced no value"). Do not write empty. Do not continue silently.
     `StepLoopSignal` (`workflow_executor.py:236`) already has the vocabulary —
     use `action="return"`.

3. **Reads resolve against `packet_state`.** Use the existing `render_body`
   (`shared/prompts/render.py:38`) with `declared` = union of
   (skill inputs, every prior step's output var). This gives free interop with
   function steps: a skill can consume a `{{design_id}}` written by real code.

4. **Static validation, exposed as a function.**
   `validate_skill_steps(steps) -> list[ValidationError]` in a new
   `orchestrator/experts/skill_validation.py`. Rules:
   - Every read resolves to an earlier step's write or a declared skill input.
   - No duplicate output var names.
   - Write clause is well-formed and names a valid identifier.
   - Warn (not error) on a write nothing downstream reads.

   This is called by the builder UI at save time so the author sees errors
   inline. Runtime keeps `render_body`'s strictness as the backstop.

5. **Stable step identity.** `run_single_step` currently refuses LLM steps
   ("no stable standalone identity for an LLM step outside a parsed workflow
   sequence", `workflow_executor.py:~2860`). Skill steps get identity from
   their index + name in the stored step list. You do **not** need
   `run_single_step` for rewind — rewind re-runs *forward from step N* using
   the normal sequential loop. Leave `run_single_step` alone.

### Acceptance criteria

- A skill step instructed to "list open tickets" actually calls the tool and
  returns real data.
- A step with `allow_write: false` (the default) cannot invoke a write tool
  even when the LLM asks for it — assert the tool is absent from the payload,
  not merely that it wasn't called.
- `A → {{x}}` followed by a step reading `{{x}}` passes the value through.
- A step reading `{{y}}` that no earlier step writes fails
  `validate_skill_steps` with a specific message naming `y` and the step index.
- Tests: `chat_orchestrator/tests/experts/test_skill_steps.py`,
  `test_skill_validation.py`. `git add -f` both.

---

## Phase 3 — Skill storage and catalog registration

**Prerequisite:** Phase 2.

**Implementation note (added during Phase 3):**

- **The catalog integration point is `instructions_provider.py`, not
  `shared/prompts/core.py`.** The plan named `PromptLibrary`'s
  `_compose_knowledge` (`core.py:167-170`) as the mechanism to reuse, which
  is right -- but `PromptLibrary.render()` itself is called from 15+
  unrelated places (ticketing, ingestion, verification, doc-editing), most
  of which have nothing to do with an interactive chat turn. The actual
  once-per-turn call site for the customer/staff system prompt -- the one
  that runs alongside `prepare_tools.py`'s tool-list assembly, which is what
  "presented alongside MCP tools" requires -- is
  `InstructionsProvider.get_customer_instructions` /
  `._get_staff_instructions_from_doc` in
  `orchestrator/services/instructions_provider.py`. That's where
  `_append_skill_catalog` hooks in, as a new helper appended after
  `_cap_context`, not inside `core.py`.
- **New `shared/prompts/skills.py`, not an extension of
  `shared/prompts/knowledge.py`.** Mirrors `KnowledgeStore`'s shape
  deliberately (same cache lifecycle, same "degrade to empty, never raise"
  contract) but is a separate module: skills have no per-prompt pinning or
  geographic/org scope selection the way knowledge modules do (every active
  skill is potentially relevant to every conversation), so the selection
  logic is a plain `staff_only` gate, not `select_for_prompt`'s pin+scope
  matching. `SKILL_CATALOG` is a module-level singleton (mirrors `PROMPTS`)
  so its 5-minute TTL cache is actually shared across calls, not rebuilt
  fresh per turn.
- **The catalog line shows a skill's `title`, not its `slug`** -- this
  deviates from knowledge modules' catalog (which shows `slug`). A skill's
  title is the author-chosen, editable name (the original request's
  "editable title"); slug is a stable identifier with no user-facing
  purpose yet. Sort order follows the same choice: by title, not slug.
- **`generate_skill_summary()` (`orchestrator/experts/skill_summary.py`)
  has no caller yet.** Phase 3 built the generation function per item 4, but
  not a `create_skill`/`save_skill` service -- there's no "save a skill" flow
  until Phase 4's builder exists to call it. Building that service now,
  without a real caller to prove it integrates correctly, would be
  speculative.

### Work

1. **Migration** `db/migrations/0011_skills.sql`:
   ```sql
   CREATE TABLE IF NOT EXISTS skills (
       id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
       slug           text NOT NULL UNIQUE,
       title          text NOT NULL,
       summary        text NOT NULL,
       steps          jsonb NOT NULL DEFAULT '[]',
       inputs         jsonb NOT NULL DEFAULT '[]',
       staff_only     boolean NOT NULL DEFAULT true,
       status         text NOT NULL DEFAULT 'active',
       created_by     text NOT NULL,
       created_at     timestamptz NOT NULL DEFAULT now(),
       updated_at     timestamptz NOT NULL DEFAULT now(),
       CONSTRAINT skills_status_chk CHECK (status IN ('active', 'disabled', 'unusable'))
   );
   ```
   Mirror into `db/schema/chat_db.sql`.

   `steps` element shape:
   ```json
   {"index": 0, "name": "find_tickets", "instruction": "...",
    "output_var": "open_tickets", "allow_write": false,
    "is_response_step": false}
   ```

   `is_response_step` drives run-mode output selection — see "Run-mode
   output: which steps talk to the user" below and Phase 5's delivery logic.
   Defaults false; the *last* step in the list is always treated as a
   response step regardless of this flag (see below), so a skill with no
   steps marked still delivers something.

   `status = 'unusable'` is set automatically when a run finds the creating
   account deleted (Phase 5). Nothing auto-deletes; an admin removes it later.

2. **Storage: JSONB, not a prompt body.** Steps need per-step metadata
   (`output_var`, `allow_write`) that has no natural home in prose. Reuse the
   *renderer* (`render_body` per step instruction), not the prompt-library
   storage shape. Step instructions may still include `{{> partials.x}}` for
   reuse.

3. **Catalog registration.** Register skills through the existing on-demand
   knowledge-module mechanism (`shared/prompts/core.py:167-170`), so
   `prepare_tools` and prompt composition need one integration rather than two:
   - Render skills as their **own labeled block**, not merged into the context
     module list. The model must not be choosing between a document and a
     procedure in one flat list.
   - Filter by `staff_only` against `user_context.is_staff` — the same gate
     `command_registry` uses (`staff_only`, e.g. line 395).
   - The **Prompts page's Context picker must not list skills.** Pinning a
     procedure into a prompt is a category error; if it is offered, someone
     will do it.

4. **Auto-summary.** On save, generate `summary` with a single LLM call from
   the step list. Author can edit it. Keep it under 200 chars — it goes into
   every request's context.

### Acceptance criteria

- A staff-only skill is absent from the catalog for a customer-org context.
- The Prompts page Context picker shows context modules only.
- A skill's title+summary appears in the rendered prompt exactly once.

---

## Phase 4 — Web builder

**Prerequisite:** Phases 1–3.

### Work

1. **Archive columns.** Migration `db/migrations/0012_message_archive.sql`:
   ```sql
   ALTER TABLE chat_messages
     ADD COLUMN IF NOT EXISTS archived_at timestamptz;
   CREATE INDEX IF NOT EXISTS chat_messages_archived_idx
     ON chat_messages (session_id) WHERE archived_at IS NULL;
   ```

2. **Filter archived messages at every load site.** There are **three** in
   `init_services.py` alone — the main load (line 79), the reply-era window
   (line 118), and cross-session context (line 166). Add the filter inside
   `get_messages_filtered` itself so it cannot be missed, rather than at each
   call site.

   > This is the exact shape of the 2026-08-02 incident in `CLAUDE.md`: a
   > history filter that didn't cover one path. Grep for every
   > `get_messages_filtered` caller before declaring this done.

3. **Builder page** `anansi_app/nicegui_app/pages/skill_builder.py`:
   - Chat transcript, one user message = one step.
   - Each step renders its envelope: text, attachments, tool names invoked,
     token count.
   - Per-message **Rewind** button: archives that message and everything after
     it, and repopulates the input box with the message text for editing.
   - Per-message **"Also return this response"** checkbox, setting that
     step's `is_response_step`. See "Run-mode output" above for what this
     controls — it has no effect in the builder itself (every step is always
     shown live there), only on what a scheduled/triggered run later sends.
     The last step's checkbox is disabled and shown checked (always true,
     per the "final step is always an implicit response step" rule) rather
     than silently ignored, so the author isn't left wondering why toggling
     it does nothing.
   - Inline validation errors from `validate_skill_steps`.
   - When rewinding past a step that ran a write tool, show what it did. For
     function steps, `StepContract.side_effects` is already populated with
     strings like "uploads to Google Drive" — surface it verbatim.
   - Save panel: title, editable auto-summary, staff-only toggle.

4. **Identity over the API channel.** The builder calls `POST /chat` with
   `source="api"` and the logged-in user's email.

   **Security requirement, do not skip:** `handler.py:2551` trusts a
   caller-supplied `user_email` when lookup misses, which makes the shared
   `API_KEY` an impersonation oracle. Before the builder sends its first
   message, gate the caller-supplied-email fallback behind a new
   `TRUSTED_IDENTITY_CALLERS` check, or require a separate header the web app
   alone holds. Do not ship the builder without closing this.

### Acceptance criteria

- Rewinding to step 2 of a 5-step session leaves 2 visible steps, and a fresh
  turn does not see steps 3–5 in its history — verified by asserting on the
  loaded `conversation_history`, not by reading the UI.
- Archived messages do not return through the reply-era window.
- A run whose email is not in `accounts` is rejected, not silently accepted.

### Implementation notes

1. **"Filter at `get_messages_filtered`" undersold the fix.** Grepping every
   caller (as item 2's own text demanded) found `get_messages_filtered` has
   exactly one caller — `init_services.py`'s main load. The other two named
   call sites ("reply-era window", "cross-session context") both call a
   *different* method, `get_messages_around_timestamp`, which builds its own
   two raw queries and shares no helper with `get_messages_filtered`. Patching
   only the named function would have left both reply-context paths
   unfiltered — the exact shape of the 2026-08-02 incident this item warns
   about, reintroduced by following the item's literal text instead of its
   intent. Fixed by adding `archived_at IS NULL` at the query level to all
   three raw query-builder chains across `get_messages`, `get_messages_filtered`,
   and `get_messages_around_timestamp` (`supabase_client.py`) — `get_messages`
   included, since `get_messages_filtered` itself delegates to it when
   `exclude_types` is empty, and `handler.py`/`escalation_service.py` also
   call it directly.

2. **Rewind semantics: the clicked step's own message is archived too, not
   just what follows.** "Archives that message and everything after it" (this
   section's own text) is what's implemented — `archive_from_message_index`
   cuts at `message_index >= target`, target included. Read against the
   acceptance criterion literally ("rewinding to step 2... leaves 2 visible
   steps"), this looks contradictory at first: archiving step 2 onward should
   leave 1 step, not 2. It isn't — the criterion describes the state *after*
   the resend that follows a rewind, not the archive click alone. Click
   Rewind on step 2 of 5 → steps 2–5 archived, input repopulated with step
   2's text, transcript now shows 1 step. Edit and resend → the new message
   becomes the new step 2 → transcript shows 2 steps (1 + the resend), and
   that resend's own conversation history load — the "fresh turn" the
   criterion means — correctly never saw archived steps 2–5. Tested directly
   (`test_rewinding_to_step_2_of_5_leaves_2_visible_steps`) against the
   archive call itself rather than a full resend round-trip, since the
   resend path is exactly Phase 4's existing POST /chat mechanism with
   nothing new to verify.

3. **Added a per-step "allow this step to make changes" toggle — not in this
   section's original Work list.** Phase 2 built `ParsedStep.allow_write`
   specifically to gate write-tool access per step, defaulting to `False`
   with no other way to set it `True`. Without a builder control, no saved
   skill could ever call a write tool, which silently breaks the "Skill
   steps DO need tools" requirement for anything beyond read-only lookups.
   Added as a switch next to "Also return this response" on every step.

4. **Two new chat_orchestrator endpoints the plan didn't name:
   `POST /skills/validate` and `POST /skills/summarize`.** `validate_skill_steps`
   (Phase 2) and `generate_skill_summary` (Phase 3) both live in
   `chat_orchestrator`, which the builder (`anansi_app`) cannot import
   directly — separately deployed services, no shared import path outside
   `shared/`. Both are thin, auth-gated (`X-Api-Key` only) wrappers with no
   side effects; `/skills/validate` runs after every message to show inline
   errors and again before Save, `/skills/summarize` runs once when the Save
   dialog opens to prefill the editable summary.

5. **`IDENTITY_ASSERTION_KEY`: the concrete shape of "gate the caller-supplied-email
   fallback."** A new secret, distinct from `API_KEY`, sent as
   `X-Identity-Assertion-Key`. `app.py`'s `is_identity_trusted_caller` checks
   it (fails closed — unset or mismatched both mean "not trusted"); the flag
   flows through `_auth_method`'s existing path into both `_handle_webhook`
   and `_handle_webhook_async`, which now call a shared
   `_resolve_email_lookup_fallback` helper instead of trusting
   `webhook_req.user_email` unconditionally. Applied to *both* webhook
   handlers (the plan named only the async one, `handler.py:2551`) — the
   sync twin had the identical pattern and no separate caller in production,
   but shipping a fixed and an unfixed copy of the same bug felt like
   exactly the kind of gap this whole item exists to close.

6. **Session identity: synthetic per-draft `user_id`, not per-user.**
   `generate_session_id` derives a session deterministically from
   `(source, chat_id, topic_id, user_id)` with no explicit session parameter
   on the wire — so the builder sends `f"{email}:{draft_id}"` as `user_id`
   (a fresh UUID per page load) rather than the email alone, or every
   "New skill" attempt for one staff member would collapse into a single
   ever-growing session. This is also *why* the identity-trust fallback is
   load-bearing for the builder specifically: a synthetic `user_id` never
   matches a real Auth DB account, so `get_user_email`'s lookup always
   misses and the fallback always fires. No "resume a draft after closing
   the tab" or drafts-list feature was built — out of scope for this phase;
   losing an unsaved draft on reload is an accepted limitation of a first
   cut, not a bug.

7. **Scoped out of this pass: per-step token counts and attachments.**
   The Work section's step envelope describes both. Token usage is tracked
   per *workflow run* (Phase 0, `agent_work_packets.token_usage`), not per
   `chat_messages` row — builder-mode turns go through the general `/chat`
   path, not `WorkflowExecutor`, so there is nothing at message granularity
   to read back after a reload. Attachments come from the API response's
   `ResponseEnvelope` (Phase 1), which is not itself persisted verbatim into
   `chat_messages`. Both are addressable later without a schema change by
   widening what gets written to `metadata` at save-message time; deferred
   rather than done half-right under this phase's time budget.

8. **Deliberately did not filter `archived_at` in anansi_app's separate admin
   chat viewer** (`nicegui_app/pages/chat.py` / `services/supabase_reader.py`).
   That page is a full historical audit tool across every conversation, not
   the builder's own live view — and "side effects of discarded steps stand"
   (this doc's "Decisions already made") reads as "rewind hides from the
   *author*," not "rewind redacts the record." An ops/support user
   auditing what actually happened should still see it. Revisit if that
   reading turns out to be wrong.

9. **`SkillBuilderService` (anansi_app) talks to `chat_db` directly**, the
   same pattern `SupabaseReader` already uses (including its own direct
   writes, e.g. `delete_bot_message`) — not a new chat_orchestrator endpoint
   for reads/writes that don't need chat_orchestrator's involvement at all.
   Sending a message is the one thing that must go through chat_orchestrator
   (that's where the LLM/expert routing lives); loading the transcript back
   and archiving on Rewind are pure `chat_messages` operations with no
   reason to round-trip through an HTTP hop that adds latency and a second
   point of auth failure for no benefit.

---

## Phase 5 — Scheduling, entity fan-out, triggers

**Prerequisite:** Phase 4.

### Work

1. **The scheduler starts the run and hands off.** Do not add a fifth scheduler.
   Extend `user_schedules` with `skill_id uuid`, `anchor_entity_type text`, and
   `skill_inputs jsonb`; when `skill_id` is set, the executor resolves the
   target chat, builds a webhook request with
   `metadata.scheduled_execution = true`, and lets the existing expert router
   dispatch to the workflow — the same path scheduled commands already take.

   The scheduler's job ends once the run is queued. It does not execute steps.

2. **Per-run authorization.** For each (skill, target chat):
   - Look up the creating account by email; `deleted_at IS NOT NULL` or no row
     → set `skills.status = 'unusable'`, abort **all** runs of that skill, and
     record the reason.
   - Resolve the chat's org via `get_organization_from_chat`.
   - Proceed only if the creator's org matches, or the creator is staff.
     Otherwise skip this chat.
   - Then call `resolve_permissions_from_chat(chat_id, topic_id, user_id,
     telegram_id)` and run with **those** permissions. Do not merge in the
     creator's permissions — the chat is authoritative, and it is what makes a
     staff-authored skill behave correctly in a customer group.

   Note: grid access is org-wide, so there is no per-grid check to write.

3. **Skips are silent in the group, loud in the UI.** A skipped chat sends
   nothing to Telegram but **must** appear in the web run history with its
   reason. A skill fanned across 6 grids that runs on 2 has to show 4 skips.

4. **Failure delivery.** Failures surface in the target group **only when that
   group is staff-facing** (org == `STAFF_ORG_ID`). Customer-group failures
   route to the existing debug channel (`handler.py:3106`) and the web run
   history — never to customers. Failures occurring before a group is resolved
   (unusable skill, render error) go to the run history and the debug channel.

5. **Entity fan-out.** Reuse `_get_eligible_entities` and
   `_build_anchor_metadata` from `agent_worker.py:355-390` — lift them into a
   shared module before Phase 6 deletes their current home. Add the
   `"organization"` branch (`grid` already exists); anything else stays
   unsupported.

   Keep the safety property from `_reconcile_expert`: **if the eligibility
   query returns 0 rows, skip the tick** rather than acting on an empty set.
   The Auth DB may simply be down.

6. **Alert trigger.** In `handle_notify` (`app.py:2134`), after grid resolution
   and **after** the alert-correlation decision, wake skills whose trigger is
   `notify` and whose anchor matches the resolved grid.

   Firing before the correlation decision would re-run skills on duplicate
   re-fires of the same alert — precisely the noise this trigger exists to
   avoid. Rate-limit per (skill, grid) the way the old user-agent path did:
   one run per 5 minutes minimum.

7. **Raise the tool-round ceiling for skill runs.** `max_tool_rounds` defaults
   to 3 (`config/settings.py:246`), which a "find tickets, evaluate each"
   step will exhaust. Add a separate setting for skill steps (suggested default
   8) rather than raising the global.

8. **Run-mode delivery selection.** See "Run-mode output: which steps talk to
   the user" earlier in this doc for the full rule; this is where it's
   implemented. As the workflow executor advances through a skill's steps
   during a scheduled/triggered run (`metadata.scheduled_execution = true`,
   distinguishing this from a builder-mode run):
   - Buffer each step's `result_summary` as it completes.
   - When a step has `is_response_step = true`, OR it's the last step, send
     one message to the target chat: a templated join of the buffered
     summaries since the last send, followed by that step's full
     `ResponseEnvelope`. Clear the buffer.
   - A run that never reaches a response step (aborted, failed early) sends
     nothing via this path — its outcome still lands in the debug
     channel/run history per item 4 above.

### Acceptance criteria

- A skill scheduled "daily at 6am, in each grid" produces one run per eligible
  grid, each authenticated from that grid's chat.
- A skill whose creator's account is soft-deleted flips to `unusable` and runs
  nowhere.
- A customer-group run failure appears in the debug channel and run history,
  and produces no message in the customer group.
- A duplicate alert re-fire does not produce a second run.
- A 5-step skill run with no steps flagged sends exactly one message: a short
  summary of steps 1–4 plus step 5's full response.
- A 5-step skill run with step 2 flagged sends two messages: (summary of step
  1 + step 2's full response), then (summary of steps 3–4 + step 5's full
  response).

### Implementation notes

1. **The load-bearing gap this section's Work items assumed away: nothing
   turned a saved skill into a runnable workflow.** "Lets the existing
   expert router dispatch to the workflow" (item 1) reads as if that
   dispatch already existed. It didn't: `execute_workflow` only ever
   derived its `ParsedStep` list from `expert_config.get_workflow()` +
   `parse_workflow()`'s plain-doc-text parser, which has no notion of
   `is_skill_step`/`allow_write`/`is_response_step` — going through it
   would have silently dropped every per-step flag Phases 2–4 built. Built
   the missing bridge first, as groundwork the rest of this phase sits on:
   - `ParsedStep` gained `is_response_step: bool = False`.
   - `execute_workflow`/`_execute_workflow_inner` gained two additive,
     opt-in params: `pre_parsed_steps` (bypasses `get_workflow()` +
     `parse_workflow()` entirely when given) and `on_step_complete`
     (fires once per step reaching a terminal outcome, with that step's
     full response text — not just `StepExecutionRecord.result_summary`,
     which is a short label like "Generated 340 chars", never the text
     itself). Neither changes behavior for any existing caller.
   - New `orchestrator/experts/skill_runner.py`: builds `ParsedStep`s
     directly from a skill's stored `steps` (preserving every flag), a
     minimal synthetic `ExpertConfig` stand-in, creates the packet, and
     executes. Deliberately does **not** reuse `expert_handler.py`'s
     `_create_new_packet` (LPP-specific input building, auto-cancel-
     superseded-packets, slash-command parsing — none of it applies to an
     unattended, linear skill run) or its resume/confirmation/cancellation
     paths (nobody is present to resume anything mid-run). It *does* reuse
     `_build_step_context` directly (generic enough to need no changes).
   - `expert_router.py` gained an early branch: `metadata.skill_id` +
     `metadata.scheduled_execution` routes straight to `expert_handler.py`
     with a synthetic `matched_expert_id="skill:<uuid>"`, skipping all
     NL/command-matching (a scheduled/triggered run already knows exactly
     which skill to run — matching it against human free text makes no
     sense and was never going to work, since a skill's UUID is nothing a
     person would type). `expert_handler.py` checks for that prefix
     immediately after its `matched_expert_id` null-check and delegates
     to `skill_runner.run_skill_packet`, before it would otherwise call
     `ExpertInstructionsProvider.get_expert_config()` (which has no notion
     of skills at all).

2. **`resolve_auth.py` needed a *third* auth branch, not the existing
   `is_scheduled_execution` one.** The generic scheduled-command path
   trusts a permissions snapshot captured when the schedule was created
   (`metadata.scheduled_organization_id`/`scheduled_is_staff`). This
   section's own item 2 explicitly rejects that for skills: "the chat is
   authoritative... do not merge in the creator's permissions." Added
   `elif is_scheduled_execution and metadata.get("skill_id"):`, ahead of
   the generic branch, that calls `resolve_permissions_from_chat` fresh
   every time — the same resolution a live Telegram message in that chat
   would get. Unlike the live-chat branch, an unresolvable org here must
   **not** raise `PermissionError` (there's no live user to see an error
   response) — it returns normally with empty `organization_ids`, and
   `skill_schedule_dispatch.py` treats that as a per-chat skip.

3. **Entity fan-out gained an `"organization"` branch backed by a new query.**
   `AuthService` had no `get_eligible_organizations_for_agents` — added it,
   mirroring `get_eligible_grids_for_agents`'s shape (not deleted, has a
   `developer_group_telegram_chat_id`). Lifted into
   `orchestrator/experts/entity_fanout.py` per item 5; `agent_worker.py`'s
   `_get_eligible_entities`/`_build_anchor_metadata` are now thin
   delegating wrappers, not deleted (their own many internal call sites are
   untouched) — Phase 6 deletes the whole file, so nothing further to do
   there. Caught two identical bugs by testing before wiring in: both this
   module's `get_eligible_entities` and, later,
   `skill_schedule_dispatch.dispatch_skill_alert_trigger`, eagerly
   constructed a real `AuthService` (a live DB connection) before checking
   whether the call site actually needed one — an unsupported
   `entity_type`/a rate-limited or inactive skill would still pay for a
   connection attempt it never used. Both fixed to defer construction
   until the first line that actually calls it.

4. **The "debug channel" is `ESCALATION_TELEGRAM_CHAT_ID`, not `tele_debug`.**
   Item 4 pointed at `handler.py:3106` — by the time this phase started
   that line was `tele_debug(...)`, which is gated behind `DEBUG=true` and
   therefore inert in production (`.env.example` documents `DEBUG=false` as
   the expected value; `DEBUG=true` would also loosen `source` validation,
   a security-relevant flag, so it isn't something production would ever
   set for this reason). `ESCALATION_TELEGRAM_CHAT_ID` — already used
   elsewhere in `handler.py` to auto-escalate customer-facing errors to
   staff — is the mechanism that's actually live in production. Used that
   instead; the run history (`user_schedule_logs`) is the primary,
   always-reliable record regardless of which Telegram channel a failure
   also reaches.

5. **The dispatcher lives entirely inside `chat_orchestrator`, not
   `anansi_app`.** Entity fan-out and per-run authorization both need
   direct Auth DB access (`AUTH_DB_HOST`/`USER`/`PASSWORD`) — `anansi_app`'s
   own `.env.example` has never had those, so `broadcast_scheduler.py`
   could not do this work even if it tried. New
   `orchestrator/experts/skill_schedule_dispatch.py` (chat_orchestrator)
   does the fan-out, authorization, run-history logging, and failure
   routing; a new `POST /skills/dispatch-schedule` endpoint is what
   `broadcast_scheduler.py` calls once it recognizes a due skill row —
   matching item 1's "the scheduler starts the run and hands off... it
   does not execute steps" precisely: recognizing "due" and advancing
   `next_run_at` is *all* `process_due_skill_schedules` does.
   `user_schedules` rows with `skill_id` set don't fit the
   `scheduled_messages` queue's one-row-one-chat model the existing
   command path uses (a skill fans out to N entities per tick, not one),
   so this queries `user_schedules` directly on a timer instead of going
   through `claim_user_command_messages`.

6. **Per-entity dispatch calls the conversation graph directly, in-process
   — not `process_webhook_with_graph`, and not a second HTTP hop.** The
   dispatcher already runs inside chat_orchestrator, so there's no reason
   to loop back through its own `/chat` endpoint. It also needs
   `expert_error`/`expert_executed` off the final graph state to decide
   success vs. failure and where to route a failure — information
   `process_webhook_with_graph`'s public contract narrows away (it returns
   only `(text, tool_results, reply_markup, tokens)`). Calling
   `build_full_conversation_graph` + `invoke_full_graph` directly, the same
   two calls `process_webhook_with_graph` itself makes, gives the full
   state dict without touching that function's signature (a wide-
   blast-radius change — every existing caller unpacks its narrow tuple).

7. **Ownership split between `skill_runner.py` and `skill_schedule_dispatch.py`.**
   `skill_runner.py` executes the skill and delivers *success* messages
   progressively as flagged steps complete (`_ResponseBuffer`); it has no
   notion of run history or staff-vs-customer routing. The dispatcher owns
   everything that happens once the graph call returns: logging the
   outcome (item 3) and routing a *failure*'s notification (item 4),
   reusing the org resolution already done for authorization rather than a
   second lookup. A successful run's messages are already delivered by the
   time the dispatcher sees the result; it only ever needs to log.

8. **The alert trigger (item 6) is not a fan-out.** A cron-scheduled skill
   fans one schedule out across every eligible entity of its
   `anchor_entity_type`. An alert-triggered skill does the opposite: it
   targets *exactly the one grid the alert concerns*, never every grid —
   firing a grid's triggered skill into every *other* grid's chat would
   obviously be wrong. Modeled as `user_schedules` rows with a new
   `schedule_type='notify_trigger'` value (`cron_expression`/`next_run_at`
   simply unused on these rows — `handle_notify` wakes them directly, no
   poll loop involved) rather than a schema fork, so the same
   `skill_id`/`anchor_entity_type`/authorization/dispatch machinery
   applies unchanged; only *which* entity gets targeted differs
   (`entity_fanout`'s eligibility query has no part in this path — the one
   "entity" is already resolved by `handle_notify` via
   `resolve_grid_notification_target`). Wired in as a backgrounded task
   after `_resolve_notify_ticket_full` (the correlation decision) returns,
   never before — firing earlier would re-run triggered skills on every
   duplicate re-fire of the same alert, exactly the noise
   `ALERT_CORRELATION_ENABLED` exists to prevent. Rate limit
   (`ALERT_TRIGGER_MIN_INTERVAL_SECONDS = 300`) keyed on
   `(schedule_id, grid_name)`, read from `user_schedule_logs`' own
   `anchor_entity_id` column — no new table. A malformed/unparseable last-
   run timestamp fails **open** (not rate-limited) rather than silently
   blocking a grid's alerts forever on bad data.

9. **`skills.created_by` liveness, not the schedule's own creator.** Item 2's
   "look up the creating account" is about the *skill's* creator
   (`skills.created_by`), checked once per skill dispatch — not
   `user_schedules.created_by_email`, since one staff-authored skill can be
   scheduled by several different `user_schedules` rows (different orgs
   each scheduling the same skill), and "abort all runs of that skill" only
   makes sense as a property of the skill itself. New
   `AuthService.is_account_email_live` returns a tri-state
   `Optional[bool]` (`True`/`False`/`None`-for-"couldn't check") rather
   than a plain bool: a DB error must skip the tick, exactly like
   `_reconcile_expert`'s pre-existing "0 eligible entities, skip — don't
   mass-terminate" safety property (preserved unchanged in
   `entity_fanout`'s callers) — a transient Auth DB outage must never flip
   a skill to `unusable`.

10. **`user_schedules.command` became nullable, with two new CHECK
    constraints** (`db/migrations/0013_skill_scheduling.sql`): exactly one
    of `command`/`skill_id` per row, and `anchor_entity_type` set if and
    only if `skill_id` is. A skill row has no single command text — the
    skill's own steps are what runs.

11. **`skill_max_tool_rounds` (item 7) is read via `tools_payload is not None`,
    not a new parameter.** `_call_llm_step_with_tools` already receives
    `tools_payload=None` for every non-skill step (Phase 2) — that's
    already the exact `is_skill_step` signal, so no signature change was
    needed to pick the right ceiling.

---

## Phase 6 — Remove persistent agents

**Prerequisite:** Phase 5 in production and carrying real load. This phase is
last for a reason — do not start it early.

### Before you write any code

**Find out what is actually running.** Persistent expert definitions live in a
Google Doc that is not in this repo. `reconcile_instances` auto-provisions an
instance for every expert with `type: persistent` + an `anchor_entity`, across
every eligible entity. Query production:

```sql
SELECT expert_id, status, count(*)
FROM persistent_agent_instances
GROUP BY 1, 2 ORDER BY 3 DESC;
```

If there is a live `grid_monitor` watching every grid, deleting it is a
functional deletion, not cleanup. Confirm with the operator that nothing
depends on it before proceeding.

### Work

1. **Terminate auto-provisioned instances.** Everything auto-created carries
   `created_by = 'auto:reconciliation'` (`agent_worker.py:418`). Terminate
   those; leave UI-created ones for manual review. Note that terminated rows
   still occupy `UNIQUE (expert_id, anchor_entity_id)` — delete rather than
   terminate if the slot needs freeing.

2. **Delete, in this order:**
   - `orchestrator/services/agent_worker.py` (after Phase 5 lifted
     `_get_eligible_entities` / `_build_anchor_metadata` out)
   - `orchestrator/graphs/persistent_agent_graph.py`,
     `persistent_agent_state.py`
   - `orchestrator/services/user_agent_service.py`
   - The instance-matching block in `handler.py:1042-1093`
   - `schedule_create_user_agent` / `schedule_list_user_agents` /
     `schedule_cancel_user_agent` from the schedule MCP server and
     `command_registry.py:440-460`
   - Persistent-agent branches in `anansi_app/services/agent_management_service.py`
     and `nicegui_app/pages/agents.py`
   - `agent-state` handling in `orchestrator/mini_app/router.py:1141`

3. **Drop tables** in a final migration: `persistent_agent_instances`,
   `agent_events`. Keep `agent_work_packets` — skills use it.

4. **Remove the checkpointer.** It has no remaining users; drop
   `_init_checkpointer` and its dependency.

5. `expert_type` in `expert_instructions_provider.py` keeps `stateless` and
   `user_startable`; remove `persistent`.

### Acceptance criteria

- `grep -rn "persistent_agent"` returns only migration files and CHANGELOG.
- The full test suite passes.
- `pre-commit run --all-files` is clean.

### Implementation notes

1. **Prerequisite gate re-checked, not waived.** "Phase 5 merged" and "Phase 5
   in production carrying real load" are different claims; the operator
   confirmed only the former. Ran the production census anyway — zero
   `active`/`executing` rows existed (worst case: 9 `grid_monitor` rows
   `paused`, which `agent_management_service.py`'s "Only allow pausing active
   or resuming paused" comment and `agents.py`'s Pause/Resume buttons confirm
   is a deliberate, resumable staff action, not an abandoned state — so this
   wasn't a `created_by`-based auto-churn call, it was the operator's own
   sign-off that nothing depended on resuming them). With nothing live, the
   "load-bearing" risk the prerequisite exists to catch didn't apply, and the
   operator explicitly cleared it.

2. **The plan's file list undersold the footprint by roughly 2x.** Reading
   actual call graphs (not grepping `persistent_agent` alone — see #3) turned
   up a second full module the plan never named:
   `orchestrator/services/expert_tool_runner.py`, "the ONLY bridge between
   persistent agents and expert workflows" per its own docstring, plus its
   test file `test_headless_expert.py`. Confirmed dead by tracing that its
   `start_expert_workflow`/`check_workflow_result` MCP tools had exactly one
   real caller (`agent_worker.py`'s `think_and_act`) — a same-named
   `start_expert_workflow` *virtual* tool in `prepare_tools.py` is a
   live-chat-only NL-routing feature that `full_conversation_graph.py`
   intercepts unconditionally before any real MCP dispatch, so it never
   reaches this one. Also newly found and deleted: `agent_event_filter.py`
   (imported only by the `handler.py` block item 2 already named), the
   entire `mcp_servers/servers/messaging_server/` (one tool, `send_to_group`,
   documented in its own header as "Intended for persistent agents only"),
   and 2 more schedule-MCP tools beyond the plan's named 3 —
   `start_expert_workflow`/`check_workflow_result` were implemented in
   `schedule_mcp_server.py` alongside `create_user_agent`/`list_user_agents`/
   `cancel_user_agent`, not colocated with `expert_tool_runner.py`.

3. **The repo's `grep` is shimmed to `ugrep --ignore-files`, which silently
   skips everything under `tests/`** (gitignored per this repo's operator-data
   policy) **and, separately, a 3-alternative pattern
   (`persistent_agent\|PersistentAgent\|persistent agent`) produced false
   negatives that a single-term search didn't** — every sweep in this phase
   used `command grep` (bypassing the shim) with one term at a time, cross-
   checked against a broader symbol sweep (`AgentWorker`, `agent_worker`,
   `UserAgentService`, etc.) to catch files that reference the subsystem
   without the literal string `persistent_agent` — `expert_tool_runner.py`,
   `messaging_mcp_server.py`, and the flag/JSON manifest files below were all
   found this way, not the first way.

4. **Two more `handler.py` call sites existed beyond the one instance-
   matching block the plan named** (whose line numbers had drifted, as
   expected): the "attach View State button to agent creation responses"
   block (checked `tr.name == "schedule_create_user_agent"`) and
   `build_agent_state_url` in `mini_app/schemas.py`, which had exactly two
   callers total — that block and `schedule_mcp_server.py`'s
   `create_user_agent` tool. Deleted the function once both callers were gone.

5. **"Remove the checkpointer" required no separate work.**
   `_init_checkpointer` was a method on `AgentWorker` itself
   (`agent_worker.py:792`), not a free function with its own callers —
   deleting the file already removed it. The "avoid checkpointer
   serialization errors" comments scattered across `prepare_tools.py`,
   `expert_handler.py`, `resolve_auth.py`, etc. describe a defensive pattern
   (service objects don't belong in LangGraph state) that's still good
   advice even with zero checkpointers left anywhere in the codebase; left
   them as-is rather than rewriting ~8 unrelated files' comments.

6. **`nicegui_app/pages/agents.py` was not uniformly persistent-agent
   content** the way the plan's "persistent-agent branches" phrasing implied
   — `_render_run_cost_section` (LLM cost across *all* workflow/skill runs)
   and `_render_scheduled_jobs_section` (system jobs + `user_schedules`,
   which Phase 5 skills also flow through) have nothing to do with agent
   instances and are the only admin visibility into either. Stripped the
   file to just those two sections rather than deleting it; kept the same
   route/file path (`/agents`) to avoid bookmark/wiring churn, but relabeled
   the nav entry from "🤖 Agents" to "📊 System Ops" since a page with that
   name and zero agents on it would be its own confusion. Its backing
   service, `agent_management_service.py`, had no such split — 100% agent
   CRUD — so it was deleted outright, along with its `get_agent_service()`
   accessor.

7. **Two fields were left in place as deliberately-unenforced vestiges**
   rather than chased through every construction site: `StepContext.
   call_depth` (only `expert_tool_runner.py` ever set it to a non-zero
   "agent-invoked" value; `expert_meta_tools.py`'s own headless-style call
   always passes 0) and `ExpertConfig.anchor_entity_type`/`wake_schedule`
   (parsed from the experts-definitions doc's `## Anchor Entity`/
   `## Wake Schedule` sections; their only 2 readers were the 2 files
   deleted in this phase). Both are harmless — a permanently-zero int and
   two permanently-unread optional strings — and touching the ~15 test
   files that construct `StepContext` for a comment-only concern was
   disproportionate. Fixed the one stale docstring claim (`call_depth`'s)
   that was actively wrong once nothing sets it; left the parsing logic,
   since the live experts-definitions Google Doc (external to this repo)
   almost certainly still has `## Type: persistent` blocks for
   `grid_monitor`/`site_visit_tracker`/`user_agent` — those now parse as
   `stateless` (the existing unknown-type fallback) rather than erroring,
   but cleaning the doc itself is the operator's action, not this PR's.

8. **Went beyond the plan's 2-table drop.** `checkpoints`/
   `checkpoint_writes`/`checkpoint_blobs` are never declared in
   `db/schema/chat_db.sql` — `AsyncPostgresSaver.setup()` created them at
   runtime, only when `PERSISTENT_AGENTS_ENABLED=true` — but
   `agent_worker.py`'s checkpointer was their only reader or writer
   anywhere in this codebase, and the 2026-07-11 retention audit found them
   to be ~640 MB, the single largest chunk of Chat DB bloat at the time.
   Added them to the same migration (`0014_drop_persistent_agents.sql`)
   since leaving them was leaving the majority of the cleanup on the table;
   also dropped `claim_agent_events` (an RPC operating on `agent_events`)
   and removed `persistent_agent_instances` from `chat_db.sql`'s
   auto-`updated_at`-trigger table list, which would otherwise fail a fresh
   bootstrap by trying to attach a trigger to a table that no longer exists.

9. **Manifest/flag files needed regeneration, not just hand-editing.**
   `mcp_servers/tool_definitions.json` is what `server_registry.list_tools`
   actually serves in production (preferred over the code manifest when
   present) — edited it directly (JSON, not the export script, to avoid
   pulling in unrelated servers' live-credential-dependent output in this
   sandbox) to drop the `messaging` key and the 5 removed `schedule` tools,
   verified against `mcp_servers/tests/test_tool_manifest_sync.py`'s
   AST-based 3-layer sync checks. `shared/config/flags.env.example` is
   generated from `flag_registry.py` (`test_generated_env_example_is_current`
   caught the drift) — regenerated with `python -m shared.config.
   flag_registry`, which also dropped `MESSAGING_ENABLED`/
   `MESSAGING_ACTIONS_ENABLED` automatically once `messaging` came out of
   `MCP_SERVER_NAMES`. `chat_orchestrator/.env.example` is a separate,
   hand-maintained reference file with its own values/comment style; edited
   by hand. `STARTUP_RECOVERY_ENABLED`'s "recover orphaned agent packets"
   comment refers to `agent_work_packets` (kept — skills use it), not
   persistent agents; left untouched despite the naming coincidence.

---

## Deliberately out of scope

- Streaming / progressive display.
- Branching conversation trees.
- Nested variables (`{{a.b}}`).
- Rolling back side effects of rewound steps.
- Mini-app authoring.
- Per-grid (sub-org) permissions — the schema does not support them.

## Open items

- Exact pricing values in `shared/llm/pricing.py` need checking against current
  published rates at implementation time.
- Whether skills should be invocable from Telegram by name (`/skillname`).
  Cheap once a skill is an expert workflow — `command_registry` and the NL
  `start_expert_workflow` router already handle workflow dispatch. Defer until
  a skill exists and someone asks.
