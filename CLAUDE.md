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
