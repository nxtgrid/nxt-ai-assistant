# MCP Tool Description Audit & Rewrite — Review and Plan

> **Status:** APPROVED 2026-08-19 — decisions recorded in "Decisions" below.
> **Branch:** to be created off `main` @ `76fb6eb1`.

**Goal:** Make all ~105 MCP tool descriptions accurate against their implementations,
consistent in structure, and carry the operational metadata (side effects, latency,
cost, prerequisites) an LLM needs to pick and sequence tools well.

---

## Part 1 — Review: what is actually there

### 1.1 Where descriptions live

| Source | Servers | Tools | Notes |
|---|---|---|---|
| `servers/<name>/tool_schemas.py` | 12 | 101 | `TOOL_SCHEMAS` list of dicts |
| `grafana_mcp_server.py` (runtime) | 1 | variable | Built from the DB `panels` table's `tool_description` column + a hardcoded wrapper template |
| `chat_orchestrator/orchestrator_mcp_server.py` | 1 | 3 | Internal; not in the manifest |
| `mcp_servers/tool_definitions.json` | 12 | 101 | **Generated** by `scripts/export_tools.py` — and what prod actually serves |

**Critical mechanic:** `server_registry.list_tools()` (`mcp_servers/server_registry.py:181-187`)
returns the JSON manifest **wholesale** if the server has any entry there, with no
per-tool merge back to code. `tool_definitions.json` is therefore the live
source of truth for every LLM-facing description, and `tool_schemas.py` is only
the input to the generator.

**Token weight:** the served manifest is ~31.7k chars of description + ~40.2k chars
of `inputSchema` ≈ **18k tokens** in every staff system prompt. Descriptions alone
are ~8k tokens. Any metadata added multiplies by 105.

### 1.2 Two live bugs found by the audit

Both are pre-existing, unrelated to style, and worth fixing regardless of the rest.

1. **`knowledge.get_knowledge_module` is invisible in production.** It is registered
   in code (`knowledge_mcp_server.py:261`, added in `1291e85c`) and present in
   `tool_schemas.py`, but absent from `tool_definitions.json`. Because the registry
   returns the JSON list wholesale, the tool is never advertised — the on-demand
   knowledge-module tier described in its own text cannot be reached by the model.
2. **`jira.change_status` serves a stale description.** The manifest says
   *"Internal tickets support Done only."*; the code says *"Use transition names like
   'Done', 'In Progress', 'To Do'. If the ticket is unassigned, it will be
   auto-assigned to the requesting user."* The manifest text wins, so the
   auto-assign behaviour is undocumented to the model.

**Why the test suite missed both:** `tests/test_tool_manifest_sync.py` checks
manifest ⊆ code, code → dispatchable, and dynamic servers not frozen. It has no
check for **code ⊆ manifest**, and no check that **descriptions match**.

### 1.3 Accuracy defects (description contradicts implementation)

| Tool | Description claims | Code does | Verdict |
|---|---|---|---|
| `equipment_diagnostics.schedule_equipment_check` | "Default: 5 min for inverter, **12 min for comms**" | `delay_minutes = arguments.get("delay_minutes", 5)` — flat 5, no per-check-type branch (`equipment_diagnostics_mcp_server.py:976`) | **False.** Model will schedule comms checks 7 min too early. |
| `knowledge.web_search` | "Supports country targeting (**default: Nigeria**)" | `country = arguments.get("country", "")` — no default, param marked "Optional." (`knowledge_mcp_server.py:445`) | **False.** No Nigeria bias unless the model passes `country` explicitly. |
| `meters.create_meter_reading_task` | Lists 10 reading types | `enum` has **11** — `maximum_power_threshold` (protocol ID 46) is in neither the tool nor the param description | **Incomplete.** A valid reading type is undiscoverable. Also `power_limit` is glossed as "maximum power threshold setting", colliding with the undocumented sibling. |
| `equipment_diagnostics.get_batch_downtime_summary` | "Get **24-hour** downtime summary" | `hours` param, default 24, fully honoured (`:1033`) | **Misleading.** Hardcodes a value that is a parameter; model won't ask for other windows. |

Verified-correct claims (spot-checked, no change needed): `find_payment` ±2h /
±5%, `customer_get_fs_daily_summary` max-30-days + yesterday/today defaults,
`set_meter_power_limit` 200/600W enum, `get_historical_power_data` 90 days,
`get_ticket_statistics` default 30 (documented on the param, not the tool — the
pattern we should standardise on).

### 1.4 Consistency defects

**Side-effect marking is applied to 41% of tools.**

| Prefix | Count |
|---|---|
| `(none)` | 62 |
| `[READ-ONLY]` | 27 |
| `[ACTION …]` | 16 |

`customer`, `meters`, `reference`, `solar`, `payment_processor` and
`equipment_control` mark consistently. `equipment_diagnostics`, `grid_design`,
`meta` and `schedule` never mark. `jira` and `knowledge` mark some tools and not
others *within the same file*.

**20 mutating tools carry no `[ACTION]` marker**, including several the text
itself calls dangerous:

- `knowledge.edit_doc_section` — its own body says *"SAFETY: This is a destructive write operation."*
- `grid_design.add_subassembly_component`, `remove_subassembly_component`,
  `set_subassembly_component_qty`, `duplicate_subassembly` — *"GLOBAL catalogue
  edit — affects EVERY design"*
- `grid_design.gd_upsert_row`, `gd_delete_row`, `run_auto_design`, `update_design`,
  `design_and_bom`, `create_design`, `trigger_bom`, `change_design_technology`,
  `duplicate_design`, `add_subassembly`, `remove_subassembly`, `set_subassembly_qty`
- `schedule.schedule_user_command`, `cancel_user_schedule`, `pause_user_schedule`,
  `resume_user_schedule` — **4 of these are customer-visible**
- `equipment_diagnostics.schedule_equipment_check`

No prompt or code path reads these markers (`grep` over `shared/prompts/` and
`chat_orchestrator/orchestrator/services/` finds nothing) — they are purely a
signal to the model, which is exactly why the 62 missing ones matter.

**Length varies 50×** — 25 chars (`resume_user_schedule`) to 1289
(`create_meter_reading_task`); median 249, mean 314. The short tail is genuinely
under-specified:

```
 25  schedule.resume_user_schedule          72  jira.add_comment
 29  grid_design.find_grid                  72  knowledge.list_document_types
 48  grid_design.get_design_bom             75  meta.escalation_types_chart
 49  schedule.pause_user_schedule           75  meta.response_distribution_chart
 60  grid_design.list_design_subassemblies  84  schedule.cancel_user_schedule
```

**No shared structure.** Every description is a single unbroken prose paragraph.
The good ones happen to cover what/when/returns/caveats; the weak ones cover
only "what". There is no house order, so the model cannot skim.

**Naming is inconsistent inside `customer_server`**: 8 tools use the
`customer_*` prefix, 14 do not (`meter_information`, `find_payment`,
`get_my_open_issues`, all 9 action tools). *Out of scope for this plan —
renaming is a breaking change — but worth recording.*

**8 params across 5 tools have no description at all:**
`customer.meter_information` (`meter_number`, `organization_id`),
`jira.search_issues_with_comments` (`grid`, `organization`),
`jira.add_comment` (`issue_key`, `comment_text`),
`knowledge.summarize_knowledge` (`topic`),
`schedule.schedule_user_command` (`time_expression`).
(273/281 params are documented — the baseline is good.)

### 1.5 The gap you named: no latency or cost metadata

Only **2 of 105** tools tell the model how long they take:
`meters.create_meter_reading_task` ("approximately 15-20 seconds") and
`equipment_control.restart_comms_chain` ("up to 10 minutes"). Both are the
right idea and should be the template.

Everything else is silent, including the slowest tools in the fleet. Evidence
gathered from the code:

| Tool / group | Real cost | Source |
|---|---|---|
| `grid_design.design_and_bom` (defaults) | **110 s wall-clock for ~10 s of work** — two *blind* `asyncio.sleep`s (30 s auto-populate + 80 s BOM), neither of which exits early. See §1.6. | `grid_design_mcp_server.py:117-118, 741, 818` |
| `grid_design.trigger_bom` | up to **80 s**, but **polls** at 10 s and exits on completion — so ~10-20 s in practice | `:1369-1376` |
| grafana panel render tools | up to **150 s** (`GRAFANA_QUERY_TIMEOUT` 180 s) | `grafana_mcp_server.py:2656, 2669` |
| `meters.create_meter_reading_task` | 15–20 s (documented ✓) | `asyncio.sleep(15)` ×3 |
| `reference.*`, `solar.*` | 20–30 s external HTTP | `timeout=30.0`, `ClientTimeout(total=20)` |
| `jira.*` | ≤15 s | `ClientTimeout(total=15)` |
| `equipment_control.*` | ≤10 s call; **10 min** to reconnect (documented ✓) | `timeout=10` |
| `customer.*`, `equipment_diagnostics.*` grid reads | ~3 s (parallel fan-out, 3 s/grid) | `timeout_per_grid=3.0` |

So "grid power lookup" is ~3 s, while "create a design and BOM" is up to ~110 s,
and the model has no way to tell them apart or to warn the user before making
them wait ~2 minutes.

**On measuring real latency instead of inferring it:** `tool_executor.py:381-382`
logs `Tool {server}.{tool}: {duration_ms}ms` on every call. However
`supabase_client.save_tool_call()` (`:972`) — which would persist
`execution_time_ms` to the `tool_calls` table — **has zero call sites**, so no
latency history is being stored. The only real-world source is the DO App
Platform run log. I did not pull it: dumping prod logs locally exposes chat_ids,
session ids and raw user message text, which is beyond what this task needs.
**Decision needed from you** (see Open Questions).

### 1.6 `design_and_bom`'s ~100 s of blind sleep is real code, but dead code

**Correction (implementation pass, 2026-08-19):** this section originally
diagnosed the 30 s + 80 s blind `asyncio.sleep()`s as live in production. That
was wrong — caught before it was fixed the wrong way, worth recording so the
mistake doesn't get repeated.

`grid_design_mcp_server.py` has **two independent implementations** behind a
`GRID_DESIGN_BACKEND` flag (module docstring, `:11-14`):

- `"internal"` **(the code default, and confirmed live** — `doctl apps spec get`
  on the production app sets six other `GRID_DESIGN_*` keys but not
  `GRID_DESIGN_BACKEND`, so prod runs the default): routes through
  `internal_engine.design_and_bom()` — synchronous work against the Chat DB
  `gd_*` tables, ported from AppSheet's old Apps Script. **Zero `sleep` calls
  anywhere in `internal_engine.py`.** This is what actually runs, and its
  latency really is the ~10 s you described.
- `"appsheet"` — explicitly commented **"legacy AppSheet REST API v2 workflow,
  kept for rollback"** (`:13`). *This* is where the 110 s of blind sleep
  (`DESIGN_AUTOPOPULATE_WAIT_SECONDS`, `BOM_GENERATION_WAIT_SECONDS`,
  `design_and_bom_workflow()`) actually lives — a real defect, but in a
  break-glass path that isn't exercised in normal operation.

Net effect: **no code fix was needed for the latency claim** — the live path
was already ~10 s; only the *description* was wrong. Confirmed and fixed as a
documentation change (Phase 1, "Fix the description" — see below), not a
behaviour change. The legacy `appsheet` branch's blind sleeps are left as-is:
touching an untested emergency-rollback path for a description-accuracy task
is not a trade worth making, and per your later instruction ("AppSheet is no
longer used as primary... any AppSheet API will hence become vestiges") that
whole branch is heading for removal on its own timeline anyway, not a timing
tune-up.

**What this did surface, and what got fixed instead:** several tool/param
descriptions still framed AppSheet as the *current* mechanism, which was
actively wrong now that `internal` is both the default and the confirmed live
backend:
- `SERVER_METADATA["grid_design"]["description"]` said "via AppSheet" —
  this string is LLM-facing (`server_registry.list_servers()` →
  `handler.py`'s `list_servers` action), not just an internal comment.
- `design_and_bom`'s own description said "every parameter the old AppSheet
  design form offered" — reworded to drop the AppSheet framing entirely and
  state the real ~10 s latency.
- `target_kwp`/`target_kwh` param descriptions said "AppSheet calculates
  freely if not provided" — AppSheet doesn't calculate anything anymore; the
  auto-design engine does. Fixed both.

A full case-insensitive sweep of every `tool_schemas.py` and
`server_registry.py` confirms these were the only three LLM-facing AppSheet
mentions repo-wide; none remain. The module docstring's own backend list
(`:11-14`) was already accurate and needed no change — it already labels
`appsheet` as legacy/rollback, which is exactly right.

---

## Part 2 — Proposed standard

A fixed 5-slot order. Slots 1–2 mandatory, 3–5 only when they carry information.
Target 150–400 chars for simple tools, up to ~700 for genuinely complex ones.

```
[SIDE-EFFECT] <What it does, one sentence.> <When to use / when not to.>
Returns: <shape of the result.>
Takes ~<latency>. <Prerequisites and hard caveats.>
```

**Slot 1 — side-effect tag.** Exactly one, on every tool, no exceptions:

| Tag | Meaning |
|---|---|
| `[READ-ONLY]` | No state change anywhere. |
| `[ACTION - <VERB PHRASE>]` | Mutates state. Verb phrase names the effect, as `customer_server` already does (`[ACTION - TURNS METER OFF]`). |
| `[ACTION - GLOBAL CATALOGUE EDIT]` | Mutates shared reference data affecting every grid — `grid_design`'s catalogue tools. |

**Slot 2 — what + when.** Present tense, names the concrete domain nouns. Must
include a "use when" or a disambiguating "not for X, use Y instead" whenever a
sibling tool is confusable (`get_issue` vs `search_issues_with_comments`,
`list_design_subassemblies` vs `list_subassembly_components`).

**Slot 3 — returns.** The shape, so the model knows whether a follow-up call is
needed. Required for anything returning an image, an id used by another tool, or
a paginated/truncated list.

**Slot 4 — latency band.** One of five fixed phrases, so it is skimmable and
cheap (~8 tokens):

| Band | Phrase | Applies to |
|---|---|---|
| <2 s | *(omit — the default assumption)* | local/DB reads |
| 2–10 s | `Takes a few seconds.` | grid fan-out reads |
| 10–30 s | `Takes ~15-30s.` | meter commands, external HTTP |
| 30–120 s | `Slow: takes up to ~2 min — tell the user before calling.` | design/BOM, grafana renders |
| async | `Returns immediately; the physical effect takes ~10 min.` | equipment restarts |

**Slot 5 — prerequisites and caveats.** Ordering dependencies ("call
`gd_describe_tables` first"), destructive warnings, and confirmation
requirements. Defaults and enums live in the **param** description, never here —
`jira.get_ticket_statistics` already does this correctly and it stops the two
copies drifting.

**Anti-goals:**

- No restating the parameter list in prose.
- No `This tool ONLY retrieves … it does NOT …` boilerplate once `[READ-ONLY]`
  says it — 4 tools currently do both (`meters` ×3, `payment_processor` ×1).
- Emphasis (`IMPORTANT:`/`CRITICAL:`/`WARNING:`) reserved for genuine safety
  gates. 15 tools use it today; 9 of those are the *identical* sentence
  *"IMPORTANT: Use this tool only if explicitly requested by a user or if its use
  is directly a step in a predefined procedure in your instructions."* repeated
  verbatim across `customer_server`'s action tools (~250 tokens of duplication).
  The policy is right; it belongs in the system prompt or a shared constant, not
  copy-pasted 9×.

---

## Part 3 — Implementation plan

### Phase 0 — Close the pipeline holes *(do first; these are bugs)*

- [ ] Add `test_code_manifest_is_subset_of_json` to `mcp_servers/tests/test_tool_manifest_sync.py` — every code-registered tool must appear in `tool_definitions.json`. Confirm it fails on `get_knowledge_module` today.
- [ ] Add `test_manifest_descriptions_match_code` — description strings must be byte-identical between `tool_schemas.py` and the manifest. Confirm it fails on `jira.change_status` today.
- [ ] Regenerate via `python mcp_servers/scripts/export_tools.py`; both tests go green. `get_knowledge_module` becomes reachable in prod.

**Value even if you stop here:** one dead tool revived, one stale description
fixed, and the regeneration step becomes self-enforcing so this class of drift
cannot recur.

### Phase 1 — Fix the four accuracy defects (done 2026-08-19)

- [x] `schedule_equipment_check` — **fixed the code.** The default delay now
  depends on `check_type`: 12 min for `site_online` (the check that verifies a
  comms-chain restart — matches `restart_comms_chain`'s own "~10 min to
  reconnect" plus a buffer), 5 min otherwise. (The enum has no literal
  `"inverter"`/`"comms"` values, so the description's original "5 min for
  inverter, 12 min for comms" framing was translated to the closest real
  parameter — `site_online` — rather than implemented literally.) Explicit
  `delay_minutes` (including `0`) always overrides. Regression tests in
  `test_schedule_equipment_check.py` (6 cases).
- [x] `web_search` — **fixed the code.** `country` now defaults to `"ng"`
  (was `""`, no bias). An explicit empty string still opts out. Regression
  tests in `test_web_search_country_default.py` (4 cases, covering the
  `topic="news"` query-append path too).
- [x] `create_meter_reading_task` — turned out to be a materially bigger issue
  than "document one missing enum value": reading-type support genuinely
  **differs by meter type** and calling an unsupported combination raises
  immediately. Verified directly from the three dispatch tables in
  `meters_mcp_server.py`: Calin V2's `protocol_mapping` (`:1883`) has no entry
  for `current`/`energy`/`power_limit`; LoRaWAN's `reading_map` (`:1360`) has
  no entry for `power_limit`/`power_down_count`/`maximum_power_threshold`/
  `special_status`/`meter_version`; **`power_limit` — the type most likely to
  be confused with `maximum_power_threshold` — works on NEITHER V2 nor
  LoRaWAN.** Worse, the resulting exception (`"Unknown reading type for V2:
  ..."`) doesn't match any pattern in `error_sanitizer.py`'s `ERROR_PATTERNS`,
  so it reaches the model as the generic fallback message — no signal to
  retry with a different type. Fixed by documenting the real per-meter-type
  support matrix in both the tool and `reading_type` param descriptions, so
  the model picks a working type up front instead of learning the hard way.
  Did not touch the dispatch tables themselves — filling in real protocol
  IDs/byte codes for the missing combinations needs actual vendor/hardware
  documentation this session doesn't have; guessing one risks sending a wrong
  command to live meter hardware. **Flagged, not fixed: `meters_server` has
  zero test coverage in `mcp_servers/tests/` — nothing would catch a
  regression in this routing logic.**
- [x] `get_batch_downtime_summary` — replaced "24-hour" with "a downtime
  summary (default 24h window, see hours)".
- [x] **`design_and_bom` — no code fix needed; see §1.6.** What looked like a
  live 110 s blind-sleep bug turned out to be dead code: the sleeps live only
  in the legacy `GRID_DESIGN_BACKEND=appsheet` rollback branch, which prod
  never touches (confirmed via `doctl apps spec get` — `GRID_DESIGN_BACKEND`
  isn't set, so it's on the `"internal"` default). The live path
  (`internal_engine.design_and_bom()`) has zero sleep calls and already runs
  in ~10 s, matching what you described. Fixed the *description* to state
  that, and — prompted by your follow-up that AppSheet is no longer primary —
  swept and fixed the three remaining LLM-facing strings that still framed
  AppSheet as the current mechanism (`SERVER_METADATA`, `design_and_bom`,
  `target_kwp`/`target_kwh`). Left the legacy branch's own timing alone: an
  untested emergency-rollback path, and per your instruction it's headed for
  removal on its own timeline rather than a tune-up.

Each fix that changed behaviour got a regression test asserting description
and behaviour agree; the two pure-documentation fixes (`create_meter_reading_task`,
`get_batch_downtime_summary`, `design_and_bom`) didn't need new ones since no
behaviour changed.

### Phase 2 — Rewrite descriptions, server by server (done 2026-08-19)

One commit for the whole phase, per your instruction. Order followed the
table below; every server verified with `pytest mcp_servers/tests/` before
moving to the next, one `export_tools.py` regen + full suite + pre-commit at
the end of the phase.

| Order | Server | Tools | What actually happened |
|---|---|---|---|
| 1 | `schedule` | 5 | Tagged (4 `[ACTION …]`, 1 `[READ-ONLY]`); expanded all 5. Documented that `cancel` is permanent but `pause` is resumable — the two read as synonyms before. |
| 2 | `meta` | 7 | Tagged all 7 `[READ-ONLY]`. The 4 pie-chart tools group by 4 *different* populations (bot-response split, escalation reason, action-required subset, new-thread issue type) with near-identical names — added a one-line disambiguator to each pointing at its siblings. Caught myself inventing plausible-sounding escalation-reason examples not in the code (`ESCALATION_REASONS`) — replaced with the real enum values before this went in. |
| 3 | `equipment_diagnostics` | 9 | Tagged all 9. Found `schedule_equipment_check` doesn't itself persist anything — no DB write happens; it only returns a computed command/timing that the caller must separately pass to `schedule_user_command` to actually schedule it. Retagged it `[READ-ONLY]` (it mutates nothing) and made that explicit — a model could otherwise reasonably assume calling it once was sufficient. |
| 4 | `knowledge` | 10 | Tagged the 5 untagged (`edit_doc_section` → `[ACTION - DESTRUCTIVE GOOGLE DOC WRITE]`). `summarize_knowledge`'s handler reads a `max_words` argument that wasn't in the schema at all — the model had no way to request a shorter/longer summary. Added it. |
| 5 | `jira` | 9 | Tagged the 4 untagged reads; filled the 4 empty param descriptions. Found the `issue_key` param description had regressed to plain "Jira issue key" on several tools, silently dropping the "or internal TKT reference" caveat their own tool descriptions promise — verified all 5 issue_key-taking tools actually call `get_internal_ticket()` and support it uniformly, fixed all 5 consistently. |
| 6 | `grid_design` | 28 | Largest job — tagged all 28 (`[ACTION - GLOBAL CATALOGUE EDIT]` for the 5 shared-template tools). Added `Takes ~10s` to `design_and_bom`/`trigger_bom`/`run_auto_design` after confirming (§1.6) `internal_engine.py` has zero sleep calls for any of the three. |
| 7 | `customer` | 22 | Already the best-written server — filled the 2 missing param descriptions on `meter_information`. Left the 9 repeated "IMPORTANT: only if explicitly requested…" sentences alone (see Anti-goals) — that's a real duplication finding, not something to silently delete from 9 safety-relevant tools without a replacement mechanism. |
| 8 | `meters`, `solar`, `equipment_control`, `payment_processor` | 9 | Already fully compliant from Phase 1 (`meters`) or written well from the start — zero changes needed. |
| 8b | `reference` | 3 | One fix: `get_import_prohibition_list`'s description claimed a "live scrape" — checked the handler and found a 1-hour TTL cache; description would have been a *second* inaccuracy this audit introduced. Fixed to state the cache honestly. |
| 9 | `grafana` | dynamic | **Code + generator prompt**, no DB data pass, as planned. (a) Wrapper (`grafana_mcp_server.py:2418-2437`) now emits `[READ-ONLY]` and a "few seconds; up to 150s under heavy load" line. (b) Found the suspected double-period bug was real but *inconsistent* — the README's own sample `tool_description` ends without a period, so some panels would double up and others wouldn't depending on what the LLM happened to generate. Fixed deterministically: `base_description.rstrip(". ")` before every join, regardless of what's stored. (c) Rewrote `grafana.panel_description.prompt` per Appendix A. (d) Trimmed the *user*-prompt template (`grafana_indexer_v2.py`) — it was still asking the model to "list required variables" and "mention time range," directly contradicting the new system prompt's "don't duplicate the wrapper" instruction. (e) Regenerated `chat_orchestrator/tests/prompt_checksums.json` per its own documented workflow (delete, rerun twice); diffed against a backup first to confirm the change was scoped to exactly one entry. Existing DB-stored panel descriptions are untouched until the next indexer run, as designed. |
| 10 | `chat_orchestrator` | 3 | Confirmed this is a **local Claude-Desktop dev/testing harness**, not part of the production `SERVER_METADATA`/`tool_definitions.json` pipeline at all (its own docstring says so; production calls the underlying providers directly). Tagged `[READ-ONLY]`, said so explicitly, and cross-referenced the 3 tools against each other. |
| 9 | `grafana` | dynamic | **Code + generator prompt; no DB data pass.** (a) Rewrite the wrapper at `grafana_mcp_server.py:2425-2440` to emit `[READ-ONLY]` and the up-to-150 s warning — deterministic text stays deterministic. (b) Rewrite `shared/prompts/library/grafana.panel_description.prompt` so regenerated panels conform (draft below); bump `chat_orchestrator/tests/prompt_checksums.json`. (c) Fix the `f"{base_description}. "` seam — the generator is never told to omit a trailing period, so wrapped descriptions very likely read `…grid.. Returns a chart image…`. Confirm against one live `panels` row, then fix in the prompt (below) and/or `.rstrip(".")` in the wrapper. Existing DB rows are left alone and converge on the next `grafana_indexer_v2.py` run. |
| 10 | `chat_orchestrator` | 3 | Internal, 3 one-liners; lowest value. |

### Phase 3 — Make the standard enforceable

- [ ] Write `mcp_servers/guides/tool-descriptions.md` with the 5-slot standard and worked before/after examples. (Note: `README.md:177` links `guides/mcp-servers.md`, but `mcp_servers/guides/` **does not exist at all** — that link is dead today. Flagging separately.)
- [ ] Update `mcp_servers/README.md:191` — its example still shows `"description": "Description of my tool"`.
- [ ] Add `test_tool_descriptions_follow_house_style`: every description starts with a recognised tag; length ≥120 chars; no tool named with a mutating verb carries `[READ-ONLY]`.
- [ ] Re-measure the manifest token budget and record it in the guide (currently ~18k; expect ~+1.5k from latency bands and expanded stubs).

---

## Decisions (2026-08-19)

1. **Latency: inferred bands are fine.** No prod-log pass, no `save_tool_call()`
   wiring in this plan.
2. **`design_and_bom` is ~10 s of real work, not ~110 s** — the design backend
   moved in-house (`GRID_DESIGN_BACKEND=internal`, the default and the confirmed
   live setting). ~~This is now a code fix~~ **Correction, made during
   implementation: it turned out to already be a pure description fix, not a
   code fix — see §1.6.** The 110 s of blind sleep is real but lives entirely
   in the legacy `appsheet` rollback branch, which the live path never
   touches; the live `internal` path has no sleeps at all and already runs in
   ~10 s. Fixed the description (and the "AppSheet" framing in three other
   LLM-facing strings — SERVER_METADATA, `design_and_bom`, `target_kwp`/
   `target_kwh`) to match; left the legacy branch's timing alone.
3. **Fix the code, not the text**, for `schedule_equipment_check` and `web_search`.
4. **All 10 servers** in Phase 2.
5. **Grafana panel descriptions stay as generated.** No DB data pass. Instead the
   *generator prompt* (`shared/prompts/library/grafana.panel_description.prompt`)
   is rewritten — see Phase 2 step 9, revised.

## Superseded — original open questions

1. **Latency numbers — measure or infer?** Inferring from timeouts (done above) is
   free and safe but gives ceilings, not typicals. Measuring means either a
   scoped `doctl apps logs … | grep -oE 'Tool [a-z_.]+: [0-9]+ms'` pass over prod
   logs (needs your approval — the logs carry user message text), or wiring up
   the already-written-but-never-called `save_tool_call()` so we get p50/p95
   properly from then on. **Recommend: ship Phase 2 with inferred bands now, and
   wire up `save_tool_call()` as a separate small PR** so the next revision uses
   real data.
2. **Phase 1 fixes — description or behaviour?** For `schedule_equipment_check`
   and `web_search` I recommend changing the *code* to match the description.
   That is a behaviour change, so it is your call.
3. **Scope of Phase 2** — all 10 servers, or stop after the worst 6
   (`schedule`, `meta`, `equipment_diagnostics`, `knowledge`, `jira`, `grid_design`
   = 68 of 105 tools, and every untagged write)?
4. **Grafana panel descriptions** live in the DB, not the repo. In scope, or a
   separate data task?

---

## Appendix A — Rewritten `grafana.panel_description` prompt

**File:** `shared/prompts/library/grafana.panel_description.prompt` (body only;
frontmatter unchanged). Consumed by `anansi_app/scripts/grafana_indexer_v2.py`
via `PROMPTS.text("grafana.panel_description")` at `:768`, with
`temperature=0.2`, `max_output_tokens=500`.

### What was wrong with the current one

The current body is three sentences and asks for the wrong artifact:

> You are a system that generates tool descriptions for Grafana dashboard panels.
> Given a panel with title, description, query, and dashboard variables, create a
> concise tool description that explains what data this panel shows and what
> variables it requires. Format: A tool description suitable for an LLM to
> understand when to use this panel.

1. **It asks for a whole tool description, but the output is a fragment.** The
   server appends a return-type sentence and an "ask the user which grid and
   time period" instruction (`grafana_mcp_server.py:2425-2440`). The prompt
   doesn't mention this, so the model writes them too — duplicated in every panel.
2. **The caller's own instructions contradict it.** `grafana_indexer_v2.py:445-449`
   tells the model to "List required variables with their valid options" and
   "Mention that time range can be customized" — both already carried by the
   JSON schema and the wrapper.
3. **"what variables it requires" is the least useful thing to spend words on.**
   The schema is authoritative and machine-readable. What the schema *cannot*
   say is which of thirty similar panels answers this question.
4. **No length or format contract** — hence 25-char and 1289-char descriptions
   elsewhere in the fleet, and a trailing period that collides with the
   wrapper's `f"{base_description}. "`.
5. **No grounding rule.** Nothing tells it to prefer the query over the title, so
   a stale or terse title propagates into the description unchallenged.

### Proposed body

```
You write the panel-specific half of an MCP tool description for a Grafana
dashboard panel. A downstream LLM reads it to decide which panel answers a
user's question, so its job is to make this panel distinguishable from the
dozens of others loaded alongside it.

Your output is a FRAGMENT, not a finished tool description. The server wraps it
automatically with:
  - a read-only tag and a warning that the render can take up to 150 seconds
  - a return-type sentence ("Returns a chart image or metric data." for graphs,
    "Returns a calculated metric value." for stat/gauge panels)
  - an instruction to ask the user which grid and time period they want
  - the panel's parameters and their valid options, as a JSON schema
Never write any of that yourself. Duplicating the wrapper wastes context on
every request and produces contradictions when the two disagree.

Write these, in order:

1. WHAT IT MEASURES, in domain terms, read off the DATA QUERY rather than the
   title. Titles are terse and go stale; the query is ground truth. Name the
   unit (kWh, kW, %, count, hours) and the aggregation (instantaneous, daily
   total, mean across the range, last value) whenever the query settles them.

2. WHEN TO REACH FOR IT — and, if another panel could plausibly answer the same
   question, the one distinction that separates them ("per-phase inverter
   output, not site total"; "billed energy, not generated"). This is the
   highest-value sentence you write.

3. ONLY IF IT CHANGES WHETHER THE CALL IS USEFUL: a hard constraint. A metric
   that is meaningless below a multi-day range, a data source that covers only
   some sites, a variable that must be set to something specific.

Rules:
  - One or two sentences, 40 words maximum. This text sits in every staff system
    prompt; you are spending tokens on every request forever.
  - Do NOT end with a period. The server appends one.
  - Open with the subject. Never "This panel…", "This tool…", "Use this to…".
  - Ground every claim in the title, description, or query you were given. If the
    query is opaque, describe only what you can support and stop — never guess a
    unit, a source, or a refresh interval.
  - Do not enumerate variable options; the schema already carries them.
  - No Grafana vocabulary: panel type, datasource, uid, transformation, legend
    format, refId.

Return the fragment alone — no preamble, no quotes, no trailing whitespace.
```

### Rollout

- [ ] Confirm the double-period against one live `panels` row before/after.
- [ ] Update `chat_orchestrator/tests/prompt_checksums.json`
      (`grafana.panel_description` → new sha256).
- [ ] Re-run `grafana_indexer_v2.py` against a single dashboard first; diff old
      vs. new `tool_description` values before a full regen. Note the script
      already tracks `system_prompt_hash` per panel (`:635`) and re-generates on
      change, so a full run will rewrite every panel — do it deliberately.
- [ ] Trim the overlapping instructions in the *user* prompt
      (`grafana_indexer_v2.py:445-449`) in the same change, or they will fight
      the system prompt.
