# Knowledge Modules for Skills

## Problem

Skills (the Workflows feature — `skills` table, editor at `/workflows`) have
**no knowledge-module attachment mechanism at all** today, confirmed by
reading the code rather than inferring it:

- `skill_runner.py`'s `_SyntheticExpertConfig.system_instructions` defaults
  to `""` by design — its own docstring: "skill steps carry their own
  instructions inline via is_skill_step, so this stays empty." A skill
  step's stored instruction becomes the **user**-turn prompt
  (`_build_llm_step_prompt`); nothing is ever passed as a system
  instruction.
- The `skills` table (`db/migrations/0011_skills.sql`) has no
  module/knowledge column, and no per-step field exists either
  (`skill_validation.py`, `skill_step_bindings.py`, `step_context.py` never
  mention knowledge/context modules).
- `shared/prompts/skills.py`'s own docstring confirms this is deliberate for
  its catalog: "Skills have no per-prompt pinning or geographic/org scope
  selection the way knowledge modules do."

Separately, the existing attachment UI (Knowledge Modules page, the "Used by
these prompts" picker in `knowledge_modules.py`) is hard to read: it lists
all 29 prompts in the library as a native multi-select with chips, no
search/filter, each option a single run-on `"prompt_id — description"`
string.

**Goal:** let an operator attach knowledge modules — including the
live/JIT ones (directory, graph, episodic, and gdoc-backed modules) — to a
skill, reusing `prompt_knowledge_overrides` as-is (no schema migration),
with UI that makes "this is a prompt" vs "this is a skill" unambiguous, and
fix the unreadable prompt picker along the way.

## Approach

`prompt_knowledge_overrides.prompt_id` (`db/migrations/0006_prompt_library.sql`)
is a plain `text` column with no FK to any "prompts" table — prompts are
`.prompt` files, not DB rows, so this column was never more than a
free-text key. A skill's pins live in the **same table**, keyed as
`f"skill:{skill_id}"` instead of a real prompt id. Every existing
`KnowledgeStore` method (`overrides_for`, `set_prompt_modules`,
`prompts_pinning`, `set_prompt_pins`) already takes this key as an opaque
string — **none of them change**.

A skill run composes its knowledge **once, at the start of the run**, into
one string, and hands that to the exact slot that already exists and is
already empty: `_SyntheticExpertConfig.system_instructions`. Composition
covers both halves of the existing knowledge-module system, exactly as a
prompt render does:

- **Inline modules** (manual/gdoc/ingested, non-JIT): resolved
  synchronously, budgeted, rendered in full — the same
  select → budget → render pipeline `PromptLibrary._compose_knowledge`
  already runs for prompts.
- **JIT modules** (directory, graph, episodic, and any gdoc module):
  resolved asynchronously and permission-checked per caller, via the exact
  same `JitContextResolver` a live conversation turn already uses —
  `ResolutionContext.from_user_context` exists specifically so a caller
  outside a live chat can build one.

Scope matching (org/grid) reuses `RequestScope`/`ResolutionContext`
unchanged. Grid resolution prefers an explicit grid on the skill's own
inputs (`skill_inputs["grid"]["grid_name"]`) and falls back to the same
chat-channel/permission-based resolver a conversation uses
(`shared/grid_scope.py`) — never guessed, matching that module's existing
"resolve unambiguously or leave `None`" rule.

### The one real design constraint: a skill step has no "context message" channel

In a live conversation, inline-module text and JIT-module text travel on
two different channels (`system_instructions` vs `context_message`).
`_call_llm_step_with_tools` (`workflow_executor.py:2930-2934`) has only one
channel — `system_instructions` — and each step is its own independent
`generate_messages` call with a fresh message list (no shared history
across steps). So both halves are folded into **one** composed string,
built once per run and reused, unchanged, on every step's call — exactly
how `expert_config.system_instructions` already behaves for the legacy
Google-Doc "expert" path, just populated with real content instead of `""`
for a skill run specifically.

Rejected alternative: re-resolving/re-injecting knowledge into every
individual step's own user-turn prompt. Rejected because it would repeat a
JIT network round-trip per step instead of once per run, and would blur the
line skill authors already rely on — a step's own text is *its*
instruction, not a place other content gets spliced in.

## Data model

No migration. The reused shape:

```
prompt_knowledge_overrides (
    prompt_id  text NOT NULL,   -- "customer.system" | "skill:<uuid>" | ...
    module_id  uuid NOT NULL REFERENCES knowledge_modules(id) ON DELETE CASCADE,
    pinned     boolean NOT NULL,
    ...
)
```

New helper, `shared/prompts/skills.py`:

```python
SKILL_PIN_PREFIX = "skill:"

def skill_prompt_id(skill_id: str) -> str:
    return f"{SKILL_PIN_PREFIX}{skill_id}"
```

**Naming note, worth a code comment at both ends:** `skill_runner.py`
already defines its own `SKILL_EXPERT_PREFIX = "skill:"`, used to prefix
`matched_expert_id` for routing — a completely different table and
purpose. The two constants happen to share a literal string but must stay
independently defined (this one lives in `shared/`, since `anansi_app` has
no `orchestrator` package to import `skill_runner` from at all). Cross-
reference both docstrings so a future reader doesn't try to unify them.

## Component design

**`shared/prompts/skills.py`** — add `skill_prompt_id()` (above).

**`shared/prompts/knowledge.py`**:
- Extract the body of `PromptLibrary._compose_knowledge` (select_for_prompt
  → drop JIT → drop empty-body → `budget_inlined` → `render_inlined`) into
  a new free function, `compose_knowledge_text(store, prompt_id, scope) ->
  Tuple[Optional[str], List[str]]`. `PromptLibrary._compose_knowledge`
  becomes a thin wrapper calling it — behavior-identical, pure extraction.
- Add a module-level singleton, `KNOWLEDGE_STORE = KnowledgeStore.from_env()`
  (mirrors `shared/prompts/skills.py`'s existing `SKILL_CATALOG` pattern).
  `shared/prompts/core.py::_build_default_library()` uses this singleton
  instead of constructing its own `KnowledgeStore` — one Supabase client
  and one 300s cache shared by prompt rendering and skill runs, not two.

**`orchestrator/services/jit_context_resolver.py`**:
- Add a public `resolve_jit_context_for(prompt_id, user_context, grid) ->
  Tuple[str, List[str]]`, lifting the try/except/logging wrapper currently
  private to `prepare_context.py::_fetch_jit_context` so it has one home.
  `prepare_context.py` switches to calling it (pure dedup, no behavior
  change there).

**`shared/grid_scope.py`**:
- Add `resolve_scope_grid_from_user_context(user_context) ->
  Optional[str]`, lifting the attribute-unpacking currently private to
  `prepare_context.py::_resolve_scope_grid`. Both `prepare_context.py` and
  `skill_runner.py` call it.

**`orchestrator/experts/skill_runner.py`** (`run_skill_packet`) — at the
point `_SyntheticExpertConfig` is built, where `user_context` and
`skill_inputs` are already in hand:

```python
grid = (skill_inputs.get("grid") or {}).get("grid_name")
if not grid:
    grid = await resolve_scope_grid_from_user_context(user_context)

org_id_str = user_context.organization_ids[0] if user_context and user_context.organization_ids else None
scope = RequestScope(organization_id=org_id_str, grid=grid)
prompt_id = skill_prompt_id(skill_id)

inline_text, inline_used = compose_knowledge_text(KNOWLEDGE_STORE, prompt_id, scope)
jit_text, jit_used = await resolve_jit_context_for(prompt_id, user_context, grid)

system_instructions = "\n\n".join(t for t in (inline_text, jit_text) if t)

expert_config = _SyntheticExpertConfig(
    expert_id=expert_id,
    display_name=skill.get("title") or skill_id,
    system_instructions=system_instructions,
)
```

Note `org_id_str` is the **string** form (`RequestScope.organization_id:
Optional[str]`) — kept independent of the existing `org_id = int(...)`
variable a few lines below, which is a separate value for the packet row's
own `organization_id` column. Reusing the int-cast one directly into
`RequestScope` would silently break `org:`-scoped module matching (a
string-equality check).

Fails open throughout: `compose_knowledge_text`/`resolve_jit_context_for`
already never raise (matching every other knowledge/JIT call site) — a
knowledge-store or provider outage degrades to `system_instructions=""`,
today's exact behavior, not a failed run.

**`anansi_app/nicegui_app/pages/knowledge_picker.py`** (new) — the shared,
entity-agnostic widget. `KnowledgeTabRow`, `build_knowledge_tab`, and
`filter_module_rows` move here wholesale from `prompts.py` (not
duplicated) and are renamed/generalized in the move:
- `PickerRow` dataclass (was `KnowledgeTabRow`: id, title, checked,
  caption, chars, is_jit).
- `filter_picker_rows(rows, needle)` (was `filter_module_rows`).
- `render_module_picker(pinning_id, store, user_email, *, show_budget:
  bool)` — the full search box + scrollable checkbox list + optional
  budget counter + independent "Save context" button, lifted from
  `prompts.py`'s existing Context tab body almost verbatim (was
  `build_knowledge_tab` plus its `ui.tab_panel` rendering code).
- `render_entity_picker(rows: List[PickerRow], on_save, *, label: str)` —
  the reverse direction (module → entities), used for both new pickers in
  `knowledge_modules.py`. Same checkbox-list-with-search look, no budget
  footer; rows are built by the caller (once from prompts, once from
  skills) rather than fetched by the widget itself, since "all prompts" and
  "all skills" come from two different sources.

**`anansi_app/nicegui_app/pages/prompts.py`** — Context tab panel body
replaced with a call to `render_module_picker(row.prompt_id, k_store,
user_email, show_budget=True)`. Pure refactor; behavior unchanged.

**`anansi_app/nicegui_app/pages/knowledge_modules.py`**:
- Replace the current `ui.select(prompt_options, multiple=True,
  use-chips)` with the shared reverse-direction picker over prompts
  (search + checklist instead of a chip-select — this is the readability
  fix; still all 29 prompts, nothing curated away).
- Add a second, identically-styled picker over skills, clearly labeled
  "Used by these skills".
- Needs an "all skills regardless of status" fetch (a small helper, or a
  direct query mirroring `KnowledgeStore`'s own client pattern) —
  `SkillCatalogStore.all_skills()` filters to `status='active'` only, which
  would hide drafts from the picker.
- Save handler: union the two pickers' selected ids
  (`prompt_ids + [skill_prompt_id(id) for id in chosen_skill_ids]`) and
  call `store.set_prompt_pins(module_id, union, actor)` **exactly once**.
  Calling it twice (once per picker) would have the second call's
  `current - selected` diff delete the first call's pins, since both
  pickers' ids live in the same `prompts_pinning(module_id)` result.

**`anansi_app/nicegui_app/pages/skills.py`** — in `_open_editor`, a third
stacked card ("Context") after "Identity & schedule", shown only when
`row is not None` (needs a real `skill_id` to key pins on — a brand-new,
unsaved workflow has none, same rule already applied to slug/title above
it). Calls `render_module_picker(skill_prompt_id(row["id"]), k_store,
user_email, show_budget=True)` — same call the Prompts page now makes,
just a different pinning id.

## Admin UI

- **Editing a skill** (`/workflows` → Edit): a new "Context" card, same
  search-and-tick list an operator already knows from the Prompts page,
  every module available including live ones (labeled "live" instead of a
  char count, same as today). Its own "Save context" button, independent
  of the workflow/identity save actions above it.
- **Editing a knowledge module** (Knowledge Modules page): the existing
  "Used by these prompts" section becomes a searchable checklist instead
  of a chip-select (same 29 options, easier to scan), and a new "Used by
  these skills" section sits next to it in the same clearly-labeled shape.
- Label stays **"Context"**, not "Knowledge," everywhere new — matches the
  existing nav item and the Prompts page's tab, even though the table is
  `knowledge_modules`.

## Failure modes

- Knowledge store unreachable (no `CHAT_DB_URL`/`CHAT_DB_SERVICE_KEY`):
  skill run gets `system_instructions=""`, identical to today — not a
  failed run.
- A JIT provider times out or raises for one module: that module
  contributes nothing; the run continues (already true today via
  `_resolve_all`'s per-module exception handling in
  `jit_context_resolver.py`).
- A knowledge module gets deleted: `ON DELETE CASCADE` on `module_id`
  already removes its `prompt_knowledge_overrides` rows — no new behavior.
- A **skill** gets deleted: nothing cleans up its
  `prompt_knowledge_overrides` rows, because `prompt_id` carries no FK at
  all (true for retired prompt ids today too — this table has never
  self-cleaned on that side). The orphaned row is inert clutter, not a
  correctness bug; not fixing this here matches the table's existing,
  accepted behavior rather than introducing special-case cleanup for
  skills only.
- Operator ticks both pickers and saves: covered by the union-before-save
  rule above; this needs an explicit regression test (below), since the
  failure mode is silent data loss, not an error.

## Testing

- `shared/tests/test_prompt_knowledge.py` (or wherever `_compose_knowledge`
  is covered today): confirm the extraction to `compose_knowledge_text`
  changes nothing about `PromptLibrary.render()`'s existing behavior.
- New unit tests: `skill_prompt_id`; `compose_knowledge_text` called
  directly with a `"skill:..."` id; `resolve_jit_context_for` called with a
  synthetic `user_context`; grid-resolution preference order (explicit
  `skill_inputs` grid → fallback resolver → `None`, never guessed).
- `skill_runner.py`: a run with modules pinned to its `skill:<id>` produces
  a non-empty `system_instructions`; a run with none pinned still produces
  `""` (regression guard, matches existing tests asserting the empty
  default); a knowledge-store outage doesn't fail the run.
- `knowledge_modules.py`: the union-before-save regression test — pin a
  module to a prompt, then to a skill in a separate render/save cycle,
  confirm the prompt pin survives.
- `knowledge_picker.py`: `filter_picker_rows` against title/caption text,
  mirroring the existing `filter_module_rows` tests.
- Admin pages here are generally light on browser-level tests in this
  repo; match whatever coverage `prompts.py`/`knowledge_modules.py`
  already have for their pure helpers (row-building, filtering) rather
  than introducing a new testing style for just this feature.

## Sequencing

1. **Backend plumbing**: `skill_prompt_id`, `compose_knowledge_text`
   extraction + `KNOWLEDGE_STORE` singleton, `resolve_jit_context_for`,
   `resolve_scope_grid_from_user_context`, `skill_runner.py` wiring, tests.
   Independently verifiable end-to-end (an operator can hand-insert a
   `prompt_knowledge_overrides` row via SQL and run a skill) before any UI
   exists.
2. **Shared widget extraction**: `knowledge_picker.py`, refactor
   `prompts.py`'s Context tab onto it. No behavior change — easy to verify
   by diffing before/after screenshots or just re-running its existing
   tests.
3. **Knowledge Modules modal**: fix the prompts picker, add the skills
   picker, union-before-save.
4. **Skill editor**: new "Context" card.
5. Update `docs/` (this repo documents this exact feature area on every
   change — see PR #144) and do a final pass with `pre-commit run
   --all-files`.
