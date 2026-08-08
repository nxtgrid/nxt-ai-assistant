# Prompt permissions: unlock ticketing, default-admin the rest

**Date:** 2026-08-08
**Status:** Approved, pending implementation

## Problem

Opening `ticketing.jira_issue_types` in the Prompts admin page (`anansi_app/nicegui_app/pages/prompts.py`) showed no Save button after editing — only "Reload cache" and "Revert to default."

Root cause: `overridable: false` in the prompt's frontmatter is the single upstream gate for *all* live editing of a prompt — body edits **and** Google Doc binding alike (`shared/prompts/access.py`'s `_allows()`: *"`overridable: false` beats every grant, admin included"*). It is not Google-Doc-specific, despite that being a reasonable first guess from the UI layout. `ticketing.jira_issue_types` ships with `overridable: false` and empty `access.edit`/`access.publish` — nobody can edit or publish it from the UI, by design, and the only way to change it is a PR.

Broader ask, from there: make the two ticketing prompts editable by ops/eng, default the other currently-locked prompts to admin-only, and (originally) let admins manage per-prompt permissions without a PR. The last part was explicitly descoped — see Non-goals.

## Decisions

### 1. Mechanism: frontmatter-only, no new table or admin UI

Three approaches were discussed:

- **A — frontmatter only (chosen).** Every permission/lock change ships via PR, same as today. Zero new code.
- B — a new `prompt_access_overrides` DB table, mirroring how `prompt_doc_bindings` already layers over frontmatter, with an admin-facing "Permissions" section in the detail dialog. Would have let admins add/remove grants without a PR.
- C — bolt the same columns onto the existing `prompt_doc_bindings` table instead of a new one. Rejected even before A vs. B was settled: conflates "which Google Doc feeds this prompt" with "who may touch it."

**Chosen: A.** After seeing B's shape and cost, the self-service admin UI was explicitly deferred (see Non-goals) rather than built now. This means `access.py` and `core.py` need **zero** code changes — the existing frontmatter `access.edit`/`access.publish`/`access.view` lists, and the `is_prompt_admin()` bypass that already grants any admin edit/publish/view on any `overridable: true` prompt regardless of its own access list, already express everything below.

### 2. Scope of "the other locked prompts"

Inventory of all 27 prompts (see `shared/prompts/library/*.prompt`) showed a clean existing pattern: every `overridable: true` prompt already grants edit/publish to exactly its own `owner` group (ops→`[ops]`, eng→`[eng]`), and every `overridable: false` prompt has `owner: eng` with empty access lists. 11 prompts are already overridable and actively owner-scoped (`customer.system`, `staff.system`, `troubleshooting.procedures`, etc.) — **these are untouched**. 16 are locked; all 16 are in scope here.

### 3. `ticketing.correlation` gets a split gate, not a full unlock

Both ticketing prompts were locked, but for different reasons:

- `ticketing.jira_issue_types` — no code anywhere asserts it must stay locked. Unlocked fully: `edit`/`publish`: `[ops, eng]`.
- `ticketing.correlation` — `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`'s module docstring explicitly documents `overridable: false` as the enforcement mechanism for *"deployments... cannot silently substitute different grouping rules, confidence bounds, or prompt limits"* without review. Two separate tests assert this (`shared/tests/test_prompt_library_contents.py`'s `NON_OVERRIDABLE`, and `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`'s `test_the_ticketing_correlation_prompt_is_not_overridable`). This is a documented safety guarantee, not just a cautious default.

  Resolution: unlock it, but split the gate — `edit: [ops, eng]` (anyone can draft a change) and **`publish: [eng]`** (only eng can promote a draft to live). `publish: [eng]` rather than admin-only because it matches the prompt's own `owner: eng` and keeps "editable by ops/eng" true for the team that asked; admin-only would mean eng couldn't publish its own owned prompt without an admin's help.

  **Important limitation, stated plainly rather than implied:** this is a role gate, not code review. It only produces real second-party review when ops drafts and eng promotes. An eng account can draft and publish its own change solo, with nobody else looking at it. The rewritten docstrings (see Companion changes) must say this accurately, not imply a review guarantee that no longer exists.

### 4. Admin-only default = leave `access.edit`/`access.publish` empty, not `[admin]`

No prompt in the library lists `admin` explicitly anywhere — `is_prompt_admin()` already bypasses the whitelist for any `overridable: true` prompt. The 14 non-ticketing locked prompts follow that same convention: flip `overridable: true`, leave `edit`/`publish` empty. Admins get full access automatically; ops/eng do not, without a separate explicit-deny mechanism needed.

## Target state

| Prompt | `overridable` | `access.edit` | `access.publish` |
|---|---|---|---|
| `ticketing.jira_issue_types` | false → **true** | [] → **[ops, eng]** | [] → **[ops, eng]** |
| `ticketing.correlation` | false → **true** | [] → **[ops, eng]** | [] → **[eng]** |
| `context_filter.relevance` | false → **true** | unchanged ([]) | unchanged ([]) |
| `doc_editing.edit_highlighted` | false → **true** | unchanged | unchanged |
| `doc_editor.locate_edits` | false → **true** | unchanged | unchanged |
| `ingestion.classify_document` | false → **true** | unchanged | unchanged |
| `ingestion.detect_contradictions` | false → **true** | unchanged | unchanged |
| `ingestion.extract_entities` | false → **true** | unchanged | unchanged |
| `ingestion.improve_content.modification` | false → **true** | unchanged | unchanged |
| `ingestion.improve_content.naming` | false → **true** | unchanged | unchanged |
| `ingestion.improve_content.quality_eval` | false → **true** | unchanged | unchanged |
| `intent_router.route` | false → **true** | unchanged | unchanged |
| `procedure.match` | false → **true** | unchanged | unchanged |
| `thread_assignment.classify` | false → **true** | unchanged | unchanged |
| `verification.sanitize` | false → **true** | unchanged | unchanged |
| `verification.sanitize_system` | false → **true** | unchanged | unchanged |

`access.view` is unchanged everywhere (`[ops, eng]` already on every row in scope).

## Companion changes (required, not optional cleanup)

Discovered by grepping for every `overridable is False`/`is True` assertion in the repo — 13 of the 16 target prompts turned out to have a deliberately-authored regression test, not just an unset default:

- `ticketing.correlation.prompt` description: drop "Versioned with the application; not editable at runtime" (no longer true), describe the draft/publish split instead.
- `shared/tests/test_prompt_library_contents.py`: replace the `NON_OVERRIDABLE` entry for `ticketing.correlation` with an assertion of the new policy (overridable, `edit` ⊇ {ops, eng}, `publish` == {eng}) instead of deleting coverage outright.
- `shared/tests/test_prompt_services.py`: its `LOCKED` set names 6 of the 14 (`context_filter.relevance`, `thread_assignment.classify`, `intent_router.route`, `procedure.match`, `verification.sanitize`, `verification.sanitize_system`). Move them to a new admin-only-asserting category rather than just dropping coverage.
- `shared/tests/test_prompt_ingestion.py`: same treatment for its `LOCKED_IDS` (the other 6: `ingestion.classify_document`, `ingestion.detect_contradictions`, `ingestion.extract_entities`, `ingestion.improve_content.{quality_eval,modification,naming}`).
- `chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`: rewrite `test_the_ticketing_correlation_prompt_is_not_overridable` (name, docstring, and assertion) to test the new split-gate policy instead of "always locked."
- `chat_orchestrator/orchestrator/services/ticketing/correlation_rules.py`: rewrite the module docstring and `get_correlation_instructions()`'s docstring so they describe the draft/publish split honestly (a permission gate, not code review) instead of the old PR-reviewed guarantee.

`doc_editing.edit_highlighted` and `doc_editor.locate_edits` are the only 2 of the 14 with no dedicated test either way.

## Explicitly out of scope

- **No code changes** to `access.py`, `core.py`, or `prompts.py`'s UI — the existing frontmatter + access-list machinery, unmodified, already expresses everything above.
- **Admin-manageable permissions without a PR** (Approach B/C above) — explicitly declined for now. Today's PR-only mechanism stays authoritative for every prompt, including these 16 after this change. Revisit as its own spec if manual PRs for permission tweaks become a real bottleneck.
- **Per-prompt model selection** (thinking/fast/lite tiers replacing today's hardcoded per-service `_MODEL` env vars) — raised in the same conversation, confirmed architecturally tenable (there's already a `default_model` seam in `shared/llm/factory.py`, and `PromptSpec.model` already exists in the schema but has zero consumers today), and explicitly deferred to its own follow-up spec since it shares essentially no code with this change.

## Testing / verification

1. `pytest shared/tests/test_prompt_library_contents.py shared/tests/test_prompt_services.py shared/tests/test_prompt_ingestion.py`
2. `pytest chat_orchestrator/tests/services/ticketing/test_correlation_rules.py`
3. Full suite + `pre-commit run --all-files` before calling this done (per this repo's CLAUDE.md — new/changed files under a `tests/` directory need `git add -f` or they silently never reach CI).
4. Spot-check in the admin UI: `ticketing.jira_issue_types` shows Save draft + Save & Publish for an ops or eng account; `ticketing.correlation` shows Save draft but not Save & Publish for an ops-only account.
