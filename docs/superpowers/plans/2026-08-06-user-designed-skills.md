# User-Designed Skills (Agent Builder)

Replaces the persistent-agent concept with **user-designed skills**: ordered LLM
steps, authored interactively in the web app, saved as an expert workflow, and
run on a schedule (or an alert trigger) inside a specific Telegram group.

**Status:** planned, not started
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
