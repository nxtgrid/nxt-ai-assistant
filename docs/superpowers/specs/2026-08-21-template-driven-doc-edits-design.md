# Template-Driven Doc & Sheet Edits — Design

**Status:** design, pre-implementation
**Branch:** `feat/template-driven-doc-edits` (worktree `../wt-template-driven-doc-edits`)
**Base:** `80e3e5f3` — *fix(skills): make LPP's template_id a real chat/tool-callable parameter*

---

## Why this exists

The `@anansi-chatbot` comment loop on Google Docs works: highlight a section,
leave a comment, the bot rewrites it and resolves the thread. That loop is
already chat-interactive through four knowledge-server MCP tools.

Nothing equivalent exists for Sheets, and template filling is hardwired to
LPP. `populate_lpp_cells` is an 853-line handler that fills cells from a
`## Cell Mapping` markdown table living in the expert's Google Doc, swaps a
map image, and builds a BOM tab — three jobs, one function, all
`package_generator`-specific.

Now that experts convert to skills (PR #128), that hardwiring is the thing
blocking a user from supplying *any* Doc or Sheet as a template. This design
makes the comment loop the single UX contract for both file types, replaces
the Cell Mapping table with a catalogue derived from step contracts, and
splits the LPP monolith — without cutting LPP over.

**Goal:** make retiring `populate_lpp_cells` a configuration change.
**Non-goal:** retiring it in this change.

---

## Ground truth (measured, not assumed)

### Spike 0 — run against live production data

A read-only probe of the Drive comments API across 25 accessible
spreadsheets (46 comments), plus a locator validation against
*NXT-3235 - Okpokunou Technical Review*. Findings are load-bearing and
several contradict what the design would otherwise have assumed.

**1. `quotedFileContent` IS populated for cell-anchored Sheets comments.**
This is the assumption the whole design rests on, and it holds:

```json
{"id": "AAAB0j_fatQ", "resolved": true,
 "quotedFileContent": {"mimeType": "text/html",
   "value": "Financial CUF at 27.3% is severely below the 55% target…"}}
```

Note `mimeType: "text/html"` — real values contained `&#39;` for apostrophe.
**The locator must HTML-unescape before matching.**

**2. An empty cell quotes nothing.** Comment `AAABy4DCQMw` returned
`"quotedFileContent": null`. This confirms the *comment on a non-empty cell*
rule is a hard requirement, not a stylistic preference.

**3. The `anchor` field cannot address a cell.** Every Sheets comment carried
an anchor of the shape:

```json
{"type":"workbook-range","uid":0,"range":"361007030"}
```

`range` is an opaque numeric object ID, not A1 notation, and `uid` was `0`
for comments spanning five different tabs. **There is no path from a comment
to a cell address via the anchor.** This settles two questions permanently:
cell targeting must go through quoted text, and a comment's *span* cannot
define an image boundary.

**4. Locating a cell by searching all tabs for the quoted text works.**
5 of 7 comments resolved; 4 to a unique cell. Both failures are instructive:

| comment | outcome | cause |
|---|---|---|
| `AAAB0jIG6Kc` | no match | **stale quote** — cell edited after commenting (73% similar; `18.6h` → `19.2h`) |
| `AAABnuBYGB4` | **14 matches** | quoted a repeated value; ambiguous by nature |

Both failure modes come from quoting *free-text labels*. A `{{token}}` in a
freshly-copied template is unique by construction and cannot drift between
annotation and fill. **Tokens are what make the locator deterministic;
labels are an explicitly-degraded fallback.**

### Spike 1 — round trip (live, on a template-shaped scratch sheet)

Spike 0 was read-only, against files with real prose already in them. This
spike round-tripped a real `@anansi-chatbot` comment on a fresh two-tab
scratch spreadsheet with `{{token}}`-style placeholders — read, locate,
write, reply, resolve — confirming the write side Spike 0 never touched.
Nothing here contradicts the design; every finding confirms an assumption
already load-bearing above.

**1. The multi-tab locator behaves exactly as Spike 0's `AAABnuBYGB4` case
implied.** `{{total_kwp}}` was placed in two tabs on purpose; the locator
returned both:

```
comment (quoting "{{total_kwp}}"): matches = ["'Main Input'!B1", "'Second'!A1"]
comment (quoting "{{site_name}}"): matches = ["'Main Input'!B2"]
```

**2. `anchor` is exactly as useless on a template as it was on real prose.**
Both comments carried `{"type":"workbook-range","uid":0,"range":"<opaque
numeric id>"}` — same shape as Spike 0, confirms this isn't an artifact of
commenting on already-populated cells.

**3. The write and the resolve are two different Google APIs with two
different OAuth scopes, and only one of them is forgiving.** A cell value
write via `spreadsheets().values().update()` (the `spreadsheets` scope —
what `get_sheets_write_credentials()` in `shared/utils/google_auth.py`
already grants) landed cleanly on the first try. Resolving the comment
(`drive.replies().create(..., body={"action": "resolve"})`) 403'd on the
first attempt — `"Request had insufficient authentication scopes"` — because
the probe script (following this plan's own literal Step 2) built the Drive
client from `get_drive_credentials()`, which is deliberately **read-only**
(`drive.readonly` + `drive.metadata.readonly`). Retrying with
`get_drive_write_credentials()` (full `drive` scope) succeeded immediately
and stayed succeeded on a follow-up read:

```
reply created: {'id': 'AAACF1CPfcA', 'action': 'resolve'}
comment now:   {'id': 'AAACF1CPfb8', 'resolved': True}
```

This was a bug in the plan's own probe script, not in the shipped
implementation: `shared/utils/file_annotations.py`'s `_get_drive_service()`
already calls `get_drive_write_credentials()`, not the read-only function —
confirmed by grep before concluding anything, then confirmed live by rerunning
just the resolve step with the correct credential. **The lesson worth
keeping for any future ad-hoc script against this API: `get_drive_credentials()`
can read comments but cannot reply to or resolve them — reaching for it for
any comment-mutating call is a silent scope trap that only surfaces at
request time, not at import or credential-construction time.**

**4. The value lands in the cell the locator named, and the reply is visible
on the thread.** Follow-up reads confirmed both independently of the write
call's own success flag: `'Main Input'!B2'` contained the written value, and
the comment's `replies` array carried the resolve reply's content and author
(the service account).

### Contract coverage in the registry

```
52 registered handlers — 33 with a StepContract, 10 with ≥1 OutputSpec
```

| expert family | handlers | contracts | ≥1 OutputSpec |
|---|---:|---:|---:|
| package_generator (LPP) | 17 | 17 | **1** |
| grids_technical_reviewer | 9 | 9 | 8 |
| grid_analyst | 7 | 7 | **0** |
| ingestion_expert | 9 | 0 | 0 |
| context_expert | 6 | 0 | 0 |
| community_* / doc_editor / signing | 4 | 0 | 0 |

The four LPP steps that produce the ~40 values `_collect_all_available_values()`
flattens — `generate_distribution_map`, `generate_powerplant_design`,
`generate_site_bom`, `fetch_solar_potential` — declare **zero** OutputSpecs
between them. That backfill is a prerequisite, not an optional polish.

### What already exists and gets reused

- `shared/utils/doc_editing.py` — `scan_comments`, `generate_replacement_markdown`,
  `edit_section`, `pin_revision`, `_resolve_comment`, `_build_thread_instruction`.
  The Drive call in `scan_comments` is file-type agnostic already.
- `shared/utils/gdrive_template_creator.py` — copies Docs *or* Sheets, substitutes
  `[placeholder]` in the **title**, registers a `DOC-NNNN` code.
- `scripts/anansi_helper.gs` — `getSheetImages` already returns `anchor_cell`
  (A1) and `alt_text` per over-grid image. `replaceSheetImage` already reads an
  existing image's height and re-centers the replacement.
- `populate_bom_tab` is already a separately registered step, and
  `create_bom_sheet` is already an importable plain function.
- `StepContract` / `OutputSpec` / `MockSpec` / `soft_failure` from PR #128.

---

## The UX contract

One gesture, two file types: **leave a comment mentioning `@anansi-chatbot`
on something that already has content.**

### Google Docs (unchanged)

Highlight a section, comment an instruction. The bot rewrites the highlighted
text and resolves the thread. Already built.

### Google Sheets (new)

Put a token in the cell you want filled, comment on it:

| in the cell | in the comment | result |
|---|---|---|
| `{{total_kwp}}` | `@anansi-chatbot the total peak capacity` | value written into that cell |
| `Total kWp` (label, col A) | `@anansi-chatbot fill the value beside this` | value written one column to the **right** of the matched cell |
| `{{site_map}}` (under an image) | `@anansi-chatbot the distribution map` | image replaced in place |

The label form writes to the cell immediately right of the match, mirroring
the column-A-label / column-B-value convention `populate_lpp_cells` already
assumes. It is the degraded path: it inherits both Spike 0 failure modes
(stale quotes, ambiguous repeats) that tokens are immune to.

Comments are scanned **per workbook, not per tab** — Drive's `comments.list`
is keyed on the file and returns every comment across every tab in one call.
No list of sub-sheets is needed; this answers an open question from the brief
directly.

### Resolution and the audit trail

Comment text is free prose. An LLM matches it against the run's data
catalogue, and the bot **replies on the thread naming the field it chose**
before resolving:

```
Done: energy.total_kwp = 42.5
```

This reverses the current explicit-mapping-only stance in `populate_cells.py`
("Labels not in the Cell Mapping section are skipped entirely to prevent
incorrect data from being written"). That stance was correct when matching was
silent. The reply thread makes every guess visible, attributable, and
correctable in the same surface the request was made — which is what makes
free-text matching safe enough to adopt.

---

## Architecture

### Layer 1 — one annotation spine, two file types

`scan_comments`'s Drive call is already file-type agnostic; only *locate* and
*apply* differ. Extract the spine:

```
shared/utils/file_annotations.py        (new — the shared half)
    scan_annotations(file_id) -> [Annotation]
    Annotation(comment_id, quoted_text, instruction, author_email, created_time)
    reply_and_resolve(file_id, comment_id, message)
    pin_revision(file_id)                    (moved from doc_editing)

shared/utils/doc_editing.py             (keeps Docs locate+apply, re-exports
                                          scan_comments for compatibility)
shared/utils/sheet_editing.py           (new — Sheets locate+apply)
    locate_cell(sheet_id, quoted_text) -> CellMatch | Ambiguous | NotFound
    write_cell(sheet_id, tab, a1, value)
    resolve_image_slot(sheet_id, token) -> ImageSlot | NotFound
```

Dispatch is on Drive `mimeType`: `…google-apps.document` → doc path,
`…google-apps.spreadsheet` → sheet path. `_build_thread_instruction`
(reply-thread folding with author attribution) and `_strip_bot_mention` are
shared verbatim — they already work and are already tested.

`locate_cell` reads every tab once via `values.get`, HTML-unescapes each cell,
and compares exact-match after stripping. It returns a three-state result,
never a guess:

- exactly one match → write
- **zero matches** → reply on the thread that the quoted text no longer
  appears (Spike 0's stale-quote case), leave unresolved. A similarity score
  may be *reported* to help the human; it is never used to write.
- **multiple matches** → for a `{{token}}`, fill every occurrence (intended
  for repeated headers); for free-text, soft-fail that one comment and say
  how many cells matched (Spike 0's 14-match case).

### Layer 2 — the value catalogue from step contracts

`_collect_all_available_values()` — a hand-written dict of ~40 curated keys —
is replaced by a registry walk:

```python
# chat_orchestrator/orchestrator/experts/output_catalogue.py
build_catalogue(context) -> list[CatalogueEntry]
    for step in accumulated_results:                 # steps that actually ran
        for spec in registry[step].contract.outputs: # declared OutputSpecs
            value = packet_state[spec.name]              if spec.where == "state"
                    accumulated_results[step][spec.name] if spec.where == "data"
            yield CatalogueEntry(
                path        = spec.name,
                value       = value,
                value_type  = spec.value_type,
                description = spec.description,      # ← what the LLM matches on
                produced_by = step,
            )
```

The LLM matches free text against `description` — real prose, not bare key
names — which is what makes "the total peak capacity" resolvable without the
author knowing the field catalogue.

**All comments resolve in a single LLM call.** Forty instructions plus the
catalogue in, a mapping out. This is a structural difference from Docs section
rewriting, which needs one generation per comment, and it has three
consequences: value filling scales to a full template cheaply, the existing
`MAX_EDITS_PER_RUN = 10` cap applies only to generative rewrites, and a
dry-run preview of the entire mapping comes for free.

**Correction (2026-08-21, found during Phase 1 implementation):** the lookup
above, `accumulated_results[step][spec.name]`, is a *literal* key lookup —
confirmed as the established convention by `grids_technical_reviewer`'s
existing `OutputSpec` usage, where `OutputSpec(name="pending_actions", ...)`
matches a real top-level `StepResult.data["pending_actions"]` key. But the
four LPP producer steps this section's `_collect_all_available_values()`
reads from don't return flat data — `generate_distribution_map` nests
`meta.*`/`location.*` inside `statistics`/`center` sub-dicts,
`generate_site_bom` nests `bom.*`/`energy.*` inside `cost_summary`/
`energy_specs`, and `energy.Wp_per_conn` is computed on the fly from *two*
steps' results and exists under no key at all. A direct lookup would have
silently returned nothing for nearly every entry — invisibly, since every
planned test (Phase 1's included) either checks declared names only or hand
-constructs pre-shaped fixtures, never real handler output.

Fixed by having each of the four steps additively publish the exact dotted
`OutputSpec` names alongside their existing keys (zero existing keys
renamed or removed, so LPP's own `_collect_all_available_values` and any
other reader is unaffected). `site.state`'s `OutputSpec` moved off
`resolve_sites` (which never produces it) onto `generate_distribution_map`
(which does, as `site_state`). This section's design — the lookup mechanism
itself, `CatalogueEntry`, single-call resolution — is otherwise unchanged;
only where the four LPP steps' data comes from needed correcting. See
`fix(experts): publish flat catalogue-path keys the OutputSpecs actually
need` for the full diff.

### Layer 3 — three generic, contract-carrying steps

All three declare `StepContract`s, so they are tool-callable from chat via the
function-step routing that landed in #128.

| step | replaces | key params |
|---|---|---|
| `create_from_template` | `copy_lpp_template`'s generic half | `template_id` (URL or bare ID), `output_folder_id`, naming variables |
| `fill_annotations` | `populate_lpp_cells`' cell-filling half | `file_id`, `dry_run` |
| `replace_file_image` | the map-image half | `file_id`, `target` token, image source |

`fill_annotations`' `dry_run` resolves every comment and returns the proposed
mapping — cell, matched field, value — **without writing anything and without
resolving any thread**. It is the propose-and-confirm surface for chat, and it
costs one LLM call because all comments resolve together (Layer 2).

`create_from_template` keeps the title-placeholder substitution
`GoogleTemplateCreator` already performs — so **the naming convention is the
template's own filename**. Name a template `[site_name] Site Package - [date]`
and every copy names itself. It drops the `site_submissions` validation that
makes `copy_lpp_template` LPP-only, and inherits `_extract_drive_file_id` from
`80e3e5f3` so a pasted browser URL works as well as a Drive ID.

### Layer 4 — chat reach

Two paths, both needed:

- **Inside a skill run**: the three steps above are function-step tools with
  access to run state. This is the only way `fill_annotations` can work, since
  the catalogue is built from run state.
- **In plain chat**: generalize the existing `scan_doc_comments` and
  `edit_doc_section` MCP tools to accept a spreadsheet `file_id` and dispatch
  on mimeType. Their descriptions currently say "Google Doc file ID" and must
  be rewritten per the 5-slot standard from PR #99.

---

## Image slots and sizing

### Targeting

`getSheetImages` already returns `anchor_cell` and `alt_text` for every
over-grid image, so no new read plumbing is needed. A comment quoting
`{{site_map}}` matches either:

- an **anchor cell** containing that token — the token sits under the image,
  hidden from print, and is commented on by selecting the cell via the Name
  Box; or
- an image whose **alt text** is that token — set via right-click → Alt text,
  survives the image being moved or resized, and lets the comment live
  anywhere in the sheet.

Both are the same resolver with two ways to name a target.

### Sizing — four layers by precedence

Since the comment's span cannot define a boundary (Spike 0 finding 3), size
comes from the sheet itself. Apps Script's `getColumnWidth(col)` and
`getRowHeight(row)` are both documented and return pixels, so any range
converts to an exact box by summation. One shared helper serves all four
layers:

1. **Merged range** containing the token — the merge *is* the boundary. One
   object carries both the token and the geometry.
2. **Explicit range in the comment** — `@anansi-chatbot {{site_map}} fit B6:F20`.
   The escape hatch when merging would disturb layout.
3. **Existing placeholder image's dimensions** — today's LPP behaviour,
   preserved.
4. **`min_height` heuristic** — today's default, so the existing LPP call site
   keeps working with no argument changes.

In every layer the image fits *inside* the box preserving aspect ratio and
centers horizontally — an extension of what `replaceSheetImage` already does
on the height axis.

`replaceSheetImage` grows an optional `target` parameter (anchor cell or alt
text) and an optional `fit_range`. With neither supplied it behaves exactly as
it does today, which is what keeps the LPP call site untouched.

---

## The LPP split

`populate_lpp_cells` does three jobs. It splits into three functions **behind
the same registered step name**:

```
populate_lpp_cells (registered name unchanged)
    ├── _fill_main_input_cells()   ← Cell Mapping path, untouched
    ├── _replace_map_image()       ← delegates to replace_file_image
    └── _build_bom_tab()           ← already delegates to create_bom_sheet
```

The Google Doc workflow definition does not change. Production behaviour does
not change. `populate_bom_tab` already exists as a separate registered step,
so two of the three seams are already there.

Retiring the step later becomes: annotate the LPP template with tokens, then
change which step names the workflow lists. No code change under time
pressure, and the Cell Mapping path stays available as a rollback for as long
as it's wanted.

---

## Migration tiers

Answering *which docs-related steps need migration*, in dependency order.

### Build order

Tiers describe **what** changes; this is the order it has to happen in, since
Tier 2 gates the most important part of Tier 1:

```
Spike 1  round-trip a bot comment on a scratch template end to end
   ↓
Tier 2   OutputSpec backfill        → without it there is no catalogue
   ↓
Tier 1a  shared spine + sheet_editing + Apps Script targeting
   ↓
Tier 1b  the three generic steps + MCP tool generalization
   ↓
Tier 1c  LPP split (behind the existing step name)
```

### Tier 1 — this change

| step | expert | action |
|---|---|---|
| `create_from_template` | *new* | generic template copy; naming from the template's filename |
| `fill_annotations` | *new* | comment-driven cell/section filling from the catalogue |
| `replace_file_image` | *new* | token- or alt-text-targeted image replacement |
| `process_doc_edits` | doc_editor | **add StepContract** (it has none, so the one handler already doing this work is unreachable as a skill tool) + Sheets dispatch |
| `copy_lpp_template` | LPP | generic half extracted to `create_from_template`; LPP-specific validation stays |
| `populate_lpp_cells` | LPP | split into three internal functions, registered name preserved |
| `replaceSheetImage` | Apps Script | range-aware targeting and sizing, defaults unchanged |
| `scan_doc_comments`, `edit_doc_section` | knowledge MCP | accept spreadsheet IDs; descriptions rewritten to the PR #99 standard |

### Tier 2 — catalogue backfill (gates `fill_annotations`)

OutputSpecs on `generate_distribution_map`, `generate_powerplant_design`,
`generate_site_bom`, `fetch_solar_potential`, `resolve_sites`. Transcribed
directly from `_collect_all_available_values()`, which is already a precise,
production-proven specification of every path and its source.

### Tier 3 — unblocked, not in this change

| step | expert | why it's a candidate |
|---|---|---|
| `write_review_section` | GTR | structured Sheets writes that could become annotation-driven |
| `create_analysis_doc`, `create_kpi_doc` | grid_analyst | build Docs from scratch; natural `create_from_template` consumers; zero OutputSpecs today |
| `fetch_document` | ingestion | no contract |
| `choose_doc_link_mode` | context_expert | no contract |
| `check_existing_review`, `fetch_pending_actions`, `resolve_grid_sheets` | GTR | Sheets readers that could share `sheet_editing`'s tab-reading helpers |

---

## Error handling

Per-comment isolation throughout — one bad comment never halts a run, matching
`process_doc_edits`' existing per-comment `try/except`. Failures use the
`soft_failure(code, message, remediation)` machinery from #128 so the model
receives something it can act on rather than an aborted workflow.

| condition | response |
|---|---|
| quoted text not found (stale) | reply on thread, leave unresolved, report closest match for the human — never write on a fuzzy match |
| multiple matches, `{{token}}` | fill every occurrence |
| multiple matches, free text | soft-fail that comment, report the count |
| no catalogue entry matches | reply asking for clarification, leave unresolved |
| empty `quotedFileContent` | skip with an explanatory reply — the cell was empty, which the contract forbids |
| image target not found | soft-fail that comment only |

`pin_revision` runs **once before any batch**, not per edit — it already works
on any Drive file. Comments are processed in reverse creation order so earlier
edits don't shift later targets, as `process_doc_edits` already does.

---

## Testing

**Unit** — cross-tab token locator including the HTML-unescape path and all
three match states; catalogue builder against a fake registry; image-slot
resolver for anchor-cell and alt-text forms; range-to-pixel conversion; the
comment→instruction thread folding (already covered for Docs, extended).

**Contract tests** — the Apps Script side cannot be exercised from Python, so
`replaceSheetImage`'s new parameters are covered by asserting the request
payload the client builds. The `.gs` changes are reviewed against the existing
`testPing` / `testReplaceSheetImage` harness already in the file.

**Fixtures** — Spike 0's real API responses (anchor shape, HTML-escaped quoted
content, the null-quote case, the 14-match case) become recorded fixtures, so
the failure modes that were discovered from production data are the ones the
tests actually pin.

**Repo rules that bite here** (from `CLAUDE.md`): new test files under any
`tests/` directory need explicit `git add -f` — a plain `git add` is a silent
no-op and CI simply won't run them. `pre-commit run --all-files` is the only
check that catches it. Nothing is reported as committed or CI-clean until that
has run green.

---

## Risks and open items

**Spike 0's findings are from one workbook's comment history.** The
`quotedFileContent` and `anchor` shapes were consistent across 46 comments in
25 files, which is decent evidence, but a *bot-authored* comment on an
*empty-then-tokenised* template cell has not been round-tripped end to end.
The first implementation task is to do exactly that on a scratch template.

**Google may change the anchor format.** The design deliberately does not
depend on it, which is the mitigation.

**Free-text matching is a reversal of a deliberate safety decision.** The
audit-trail reply is what makes it acceptable. If the reply-and-resolve step
fails, the write must not be treated as complete — `edit_section` already has
this ordering right (resolve only after confirming `elements_written > 0`) and
the Sheets path must copy it.

**LPP template annotation is manual.** ~40 comments on a live production
template. Out of scope here by design; the split is what makes it schedulable
separately.

**`.env` non-hermeticity.** Per `CLAUDE.md`, a local `.env` with real
credentials makes `PROMPTS`-touching tests read live DB/Doc content. Spike
work in this worktree uses credentials from the main repo folder; any test
added here must construct a bare `PromptLibrary()` or monkeypatch the
singleton rather than inheriting live state.
