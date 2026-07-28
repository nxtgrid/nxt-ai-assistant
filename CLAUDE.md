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
