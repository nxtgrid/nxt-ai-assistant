# Notes for Claude

## Before pushing: always run `pre-commit run --all-files`

`git status`/`pytest` passing locally is not enough — both can silently hide
problems that only `pre-commit run --all-files` catches:

- **New test files under any `tests/` directory need `git add -f`.** The repo's
  `.gitignore` denies `tests/` by default (operator data leak prevention — see
  `CONTRIBUTING.md` "Adding a new test file"). A plain `git add` on a new test
  file is a silent no-op: `git commit` succeeds, the file never reaches the
  remote, and CI simply never runs those tests. `pytest` locally still finds
  the file on disk, so the suite looks green even though it never got
  committed — the gap only shows up in `pre-commit run --all-files`'s
  `test-wiring` hook, or by diffing `git show --stat HEAD` against what you
  expected to commit.
- **`ruff check .` skips ignored files**, so a new (not-yet-force-added) test
  file gets zero linting locally, even though CI's `pre-commit run --all-files`
  lints it once it's tracked. Fixes surface only after force-adding.

Hit this exactly on the `feature/alert-correlation-notify` branch (PR #25):
6 new test files (`test_alert_facts.py`, `test_correlation_render.py`,
`test_correlation_rules.py`, `test_correlation_store.py`, `test_correlator.py`,
`shared/tests/test_telegram_send.py`) were
written, passed locally, and were reported as committed/pushed — but a plain
`git add <path>` had silently dropped all of them from the commit. The PR's
initial CI run showed all-green because the missing suites just weren't part
of what CI executed. Caught it by running `pre-commit run --all-files` after
the fact, which failed on `test-wiring`.

**Checklist before telling the user a task is committed/pushed/CI-clean:**
1. `pre-commit run --all-files` (not just `ruff check .` or `pytest`)
2. If it reports untracked files under `tests/`, vet them for operator data
   then `git add -f` each one explicitly
3. Re-run the hook to confirm clean, re-run the relevant test suites

## Debugging a "bot is failing" / production incident report

Production runs on DigitalOcean App Platform (built via `.github/workflows/build-images.yml`
→ GHCR → App Platform). Skip codebase spelunking and pull real logs first:

```bash
doctl apps list                                   # find the app id (name: anansi)
doctl apps list-deployments <app-id> | head        # confirm current deploy is healthy
doctl apps logs <app-id> anansi-bot --type run --tail 500
```

- `anansi` app id as of 2026-08: `525c885e-c7e4-4721-b654-b724c1de5553`; the
  Telegram bot / chat orchestrator component is `anansi-bot`.
- `doctl account get` can 403 on this token even when `doctl apps ...` works fine —
  don't take that 403 as "doctl has no access," just try `apps list`/`apps logs` directly.
- Log timestamps are UTC; convert the failure time the user reports (usually local,
  UTC+2) before grepping. Then `grep -n "ERROR\|Traceback\|Exception"` and match the
  session/chat_id and `user_input` lines around that timestamp to confirm you're
  looking at the right request (see `handler.py`'s `_normalize_telegram_webhook` /
  `_handle_webhook_async` log lines — they print `chat_id`, `session=`, and the raw
  message text).

**If the failure text is exactly "Something went wrong on our end. Please try
again or contact support."** — this is the single catch-all `SYSTEM/internal_error`
message from `shared/utils/error_messages.py`'s `categorize_error()`, returned for
*any* unhandled exception that doesn't match its known patterns (rate-limit, timeout,
503, connection, parse, db). It fires from the top-level `except Exception` in
`chat_orchestrator/handler.py` (`_process_telegram_async`) and in
`orchestrator/services/webhook_processor.py:161-165`. Because it's a catch-all with
no detail, **seeing this text tells you nothing about the specific cause** — go
straight to the doctl logs for the real exception/traceback rather than guessing
from the message. Also check whether it hit more than one message/session in the
same window: if a scheduled task and an unrelated live message both got this exact
text back-to-back, that's a signal of a systemic failure (env/config breakage,
upstream outage, context-window overflow) rather than a one-off, and — per the
2026-08-02 incident below — a failed scheduled run can leave contaminated history
that makes the *next* unrelated message fail identically, so don't assume two
failures are two separate bugs.

**2026-08-02 incident (reference case):** A scheduled Jira-ticket query returned
an oversized `search_issues_with_comments` result (full comment threads, no cap)
that blew past Gemini's 1,048,576-token context limit → generic error. Because
`save_user_message.py`'s early-save (which persists the user/trigger message before
the graph runs, to survive crashes) didn't tag `message_type`, the crashed scheduled
run's trigger text was saved as an untyped message that defaults to `"interactive"`
— so `init_services`' `exclude_types=["scheduled", "scheduled_user"]` filter didn't
exclude it. The very next live message ("Hi what can you do") loaded that history,
Gemini continued the primed Jira search instead of answering the greeting, and hit
the identical context-overflow failure. Fixed by: capping/truncating comments in
`search_issues_with_comments` (`mcp_servers/servers/jira_server/jira_mcp_server.py`),
tagging the early-save with `message_type="scheduled"` when
`metadata.scheduled_execution` is set (`save_user_message.py`), and adding a
`context_length_exceeded` category to `error_messages.py` so this fails with an
actionable message instead of the generic one next time.

## A CI failure with no relevant diff is probably the CVE audit, not your PR

Before assuming a red CI check means something in your change is wrong, run
`gh pr checks <PR#>` and look at *which* job failed. `Dependency CVE audit
(chat_orchestrator)` and `Dependency CVE audit` (the `mcp_servers` /
`rag_pipeline` / `anansi_app` matrix) run `pip-audit` against whatever's
currently pinned — they check the vulnerability database's state *at CI run
time*, not at commit time. A PR that touched zero dependency files can go
from green to red days later purely because a new CVE got disclosed against
an already-pinned (often transitive) package. If `Tests`/`Lint`/`Validate`
are all green and only an audit job is red, it's this — don't go hunting in
your own diff first.

**Fix, for the `uv`-based jobs (`chat_orchestrator`, and any future `uv`
project — check `.github/workflows/ci.yml` for which projects use `uv sync`
vs. plain `requirements.txt`):**

```bash
cd chat_orchestrator
uv lock --upgrade-package <name>   # bumps ONLY that package; minimal lockfile diff
```

Then verify with the *exact* command CI runs (see `ci.yml`'s
`audit-orchestrator` job — the `--ignore-vuln` list changes over time, copy
it from there rather than the line below):

```bash
uv sync --extra dev && uv pip install pip-audit
uv run pip-audit --progress-spinner off --ignore-vuln PYSEC-2026-1845
```

If no fixed version exists yet, or the bump is a breaking major version,
follow the pattern already in `ci.yml` above the `pip-audit` invocation:
add `--ignore-vuln <ID>` with a comment explaining *why* the finding doesn't
apply here (see the existing `PYSEC-2026-1845` entry) — never ignore a
CVE silently.

For the `requirements.txt`-based matrix job (`mcp_servers` / `rag_pipeline`
/ `anansi_app`), there's no `uv.lock` to target — edit the affected
package's pin directly in that project's `requirements.txt`.

**2026-08-07 incident (reference case):** PR #74 (feat/skill-step-tools, a
`workflow_executor.py`/test-only change with zero dependency edits) failed
only `Dependency CVE audit (chat_orchestrator)`; every other job was green.
The log showed `h2 4.4.0 CVE-2026-71554, fix: 4.4.1` — `h2` is a transitive
dependency of `httpx[http2]`, not anything this PR touched. Fixed with
`uv lock --upgrade-package h2` (a 3-line lockfile diff), verified locally
with the command above before pushing.

## A local `.env` with real credentials makes some tests silently non-hermetic

`shared.prompts.PROMPTS` (`shared/prompts/core.py`) is a **process-wide
singleton**, built once at import time (`_build_default_library`) with its
DB-override and Google-Doc lookups wired up automatically whenever
`CHAT_DB_URL`/`CHAT_DB_SERVICE_KEY`/`GOOGLE_SERVICE_ACCOUNT_JSON` happen to be
set — it has no "test mode." Any test that touches `PROMPTS` (directly, or
indirectly through something like `ExpertInstructionsProvider`, which reads
`experts.definitions` through it) inherits *whatever's live* in the real
`chat_db` prompts table or Google Doc at that exact moment, the instant real
credentials are present in the process's environment — including a
`chat_orchestrator/.env` copied in from a working deployment for unrelated
local-dev reasons (e.g. to test a DB-reading feature in a git worktree). CI
never hits this (`ci.yml`'s Tests job sets only a placeholder
`CHAT_DB_URL`/`CHAT_DB_SERVICE_KEY` and no Google credentials at all, so it
always resolves to the bundled files) — this is purely a local-environment
trap.

**If a test whose name says "bundled" or a checksum/snapshot test
(`test_prompt_parity.py`) fails locally, check what's in your `chat_orchestrator/.env`
before suspecting the codebase.** A quick way to check without printing
secrets: `awk -F= '{print $1"="length($2)}' chat_orchestrator/.env` — real
values are tens to thousands of characters; template/placeholder values are
usually empty or a handful of chars.

**Fix for a test that needs deterministic bundled content:** don't import the
`shared.prompts.PROMPTS` singleton. Construct a bare `PromptLibrary()`
instead (`from shared.prompts import PromptLibrary`) — leaving
`db_body_for`/`gdoc_body_for` unset always resolves from the bundled file,
regardless of environment (see `test_prompt_parity.py`). For code with no
injection seam (`ExpertInstructionsProvider` imports the module-level
`PROMPTS` directly), monkeypatch the singleton's `_db_body_for`/
`_gdoc_body_for` attributes to `None` for the test's duration instead (see
`test_expert_instructions_provider_library.py`'s `_force_bundled_prompts`
fixture) — pytest's `monkeypatch` reverts it automatically.

**2026-08-07 incident (reference case):** During Phase 4 of
`docs/superpowers/plans/2026-08-06-user-designed-skills.md`, a `chat_orchestrator/.env`
copied into a git worktree (for the skill builder's own DB-reading work) had
real `CHAT_DB_URL`/`GOOGLE_SERVICE_ACCOUNT_JSON`/`EXPERT_INSTRUCTIONS_DOC_ID`
values. `test_get_all_expert_ids_returns_the_bundled_experts`,
`test_bundled_experts_still_parse_despite_pre_existing_format_quirks`, and
`test_prompt_text_has_not_drifted` all failed — the live experts-definitions
source had `grid_analyst` struck through at that moment, and the live
`customer.system`/`staff.system`/`experts.definitions` text didn't match the
committed `prompt_checksums.json` snapshot, neither of which reflected
anything actually wrong in the repo. A first investigation pass
(`git stash --include-untracked` to check against a "clean" tree) missed
this: `.env` is gitignored, and `--include-untracked` does not stash
gitignored files, so the live-credentials `.env` stayed in place throughout
that check and the failures reproduced regardless of any code diff — for
the wrong reason. Root-caused on a second pass by testing in a worktree with
no `.env` copied in at all (failures vanished) and confirming the mechanism
directly (monkeypatching `PROMPTS._gdoc_body_for` to a fake live body made a
freshly-checked-out `main` reproduce the exact same "missing expert"
symptom). Fixed by making the three tests construct/force bundled-only
resolution instead of touching `prompt_checksums.json` or any bundled
content — there was no actual drift to reconcile.

## Nav menu items: the URL route must match the displayed label

`anansi_app/nicegui_app/layout.py`'s `OPERATIONS_NAV`/`BOT_ADMIN_NAV` (the
sidebar) and `anansi_app/nicegui_app/main.py`'s `@ui.page(...)` routes are
two separate lists that have to move together. It's easy to relabel a nav
entry (e.g. "Agents" → "Runs") without renaming its route, which leaves the
URL bar showing the *old* name every time someone clicks that item — and
the mismatch compounds the next time someone relabels again without
checking. **Whenever you change a nav label — in this app or any other UI
in this repo — rename its route/URL stub to match in the same change**
(kebab-case of the label, dropping the emoji):

1. Rename the route (`@ui.page("/old-path")` → `/new-path`, or the
   equivalent in whatever router the surface uses), and leave a tiny
   redirect stub at the old path so existing bookmarks/links don't 404 —
   see `main.py`'s `/agents`, `/skills`, `/knowledge-modules`, `/documents`
   for the pattern (each just does `ui.navigate.to("/new-path")`).
2. Update the nav config (`layout.py`'s tuples/`NavItem`s) to the new
   target.
3. Update `anansi_app/tests/test_layout.py`'s target assertions.
4. Grep the repo for the old path literal before finishing — a hardcoded
   link elsewhere (e.g. `pages/tickets.py`'s "View in Chats" link) needs to
   move too if it points at the route you just renamed.

**One standing exception: `/conversations` ("💬 Chats") can never become
`/chats` or `/chat`.** The DO App Platform ingress spec
(`.do/app.example.yaml`) prefix-routes *any* path starting with `/chat`
straight to the `chat-orchestrator` service (the Telegram webhook) — a
NiceGUI page at `/chats` would never be reached, ingress swallows it first.
Leave that one route/label mismatch in place; don't "fix" it again. (See
also `AGENTS.md`, which carries this same note for non-Claude coding
agents.)

**2026-08-20 incident (reference case):** `/agents`, `/skills`, and
`/knowledge-modules`/`/documents` had all been relabeled ("🎰 Runs", "🎬
Workflows", "🧠 Context", "📚 RAG Knowledgebase") in earlier work without
their routes moving, so every one of those menu clicks landed on a stale
URL. Fixed on `fix/nav-url-stubs` by renaming the routes to `/runs`,
`/workflows`, `/context`, `/rag-knowledgebase`, adding a redirect stub at
each old path, and updating `test_layout.py` to assert the new targets
(plus a new test pinning the `/conversations` exception so it isn't
"fixed" by mistake later).
