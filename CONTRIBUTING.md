# Contributing to Anansi

Thank you for your interest in contributing!

## Dev Environment Setup

See [README.md](README.md) for full setup instructions. Quick summary:

```bash
git clone <repository-url>
cd anansi
./setup_shared.sh

# Chat orchestrator
cd chat_orchestrator && python3 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
cp .env.example .env  # fill in credentials

# MCP servers
cd ../mcp_servers && python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Running Tests

There are four suites, all run by CI:

```bash
# chat_orchestrator + shared (shared has no venv of its own)
cd chat_orchestrator && source .venv/bin/activate
pytest tests/
pytest ../shared

# mcp_servers + anansi_app (from the repo root)
PYTHONPATH="$PWD:$PWD/mcp_servers" pytest mcp_servers/tests
PYTHONPATH="$PWD:$PWD/anansi_app" pytest anansi_app/tests

# Specific test file
pytest -v tests/experts/test_workflow_executor.py

# Code quality (run before pushing)
pre-commit run --all-files
```

### Adding a new test file

`.gitignore` ignores `tests/` on purpose. This is the public OSS mirror of an
internal tree, and tests there can carry operator-specific data (grid names,
meter numbers, org IDs), so the default is to keep them out. Individual test
files are published only after they have been checked for that, by force-adding
them:

```bash
git add -f chat_orchestrator/tests/experts/test_my_feature.py
```

**A plain `git add` will silently do nothing.** Git prints a hint, but `git
commit` afterwards succeeds without the file, so the test never reaches CI and
nobody finds out.

The `test-wiring` pre-commit hook (`.github/scripts/check_test_wiring.py`) fails
the commit rather than letting that happen. It runs on every commit, not just on
staged files, because the thing it looks for is a file that is *missing* from the
commit. If a test must stay internal, name it in `.gitignore` next to
`mcp_servers/tests/test_meter_actions.py` — that list is the hook's record of
which tests are unpublished on purpose.

The same hook checks that every tracked test file sits under a path CI actually
runs, and that each of those paths is still present in `.github/workflows/ci.yml`.
A test that is committed but that no job runs is as invisible as one that was
never committed: until this was added, everything under `mcp_servers/tests/`
except `test_grafana_variable_substitution.py` was in exactly that state, and one
of those suites had been failing since the initial commit.

Two related traps:

- `ruff check .` skips ignored files, so a brand-new test file is not linted
  locally. `pre-commit run --all-files` operates on tracked files and is what CI
  runs — use it before pushing.
- The same applies to `scripts/*.py` and `scripts/*.sh`, which are ignored for
  the same reason.

Do not "fix" this by broadening `.gitignore`. Files arrive in these directories
from upstream syncs, and the deny-by-default is what keeps internal ones out of
a public repository.

### Adding or editing a prompt

Every prompt Anansi sends to a model lives in `shared/prompts/library/` as a
`.prompt` file — YAML frontmatter, then a markdown body. Wording changes to an
*existing* prompt don't need a PR at all: edit it from the Prompts admin page
(`/prompts`) if you have edit access. Add a `.prompt` file here only for a
genuinely new prompt.

```yaml
---
id: my_feature.summarize        # dotted namespace matching the calling module
description: One sentence a reviewer can act on without reading the body.
owner: ops                       # ops | eng — who this prompt belongs to
overridable: true                # false if a bad edit could break parsing
                                  # (JSON-emitting prompts) or bypass a safety
                                  # policy (see ticketing.correlation for why)
output: text                     # or json, which requires a `schema` field
sections: []                     # [] = whole body is the system channel;
                                  # ["system_instructions"] etc. splits by H1
variables: [doc_type, content]   # every {{placeholder}} used below
access:
  view: [ops, eng]
  edit: [ops]                    # empty list = nobody but admins
  publish: [ops]
---
Your prompt text, with {{doc_type}} and {{content}} substituted at render time.
```

Then:

- Call it with `PROMPTS.text("my_feature.summarize", doc_type=..., content=...)`
  (or `PROMPTS.render(...)` if you need the system/context split, or
  provenance for logging).
- If the prompt is JSON-emitting or drives routing/parsing logic, set
  `overridable: false` — it should ship reviewed with the app, not be
  editable live. `ticketing.correlation`'s frontmatter explains the reasoning
  if you want the fuller argument.
- Regenerate the parity snapshot so future drift is caught: delete
  `chat_orchestrator/tests/prompt_checksums.json` and re-run
  `pytest tests/test_prompt_parity.py` from `chat_orchestrator/` — it
  recreates the file on a missing-snapshot run. Review the diff before
  committing; a change here changes what the model sees.

See `docs/superpowers/specs/2026-07-30-prompt-library-design.md` for the full
design (resolution order, knowledge modules, access control).

## Code Style

- **Python 3.11+**, formatted with `ruff` (100-char line length)
- Run `pre-commit install` once to enforce style on every commit — today that means `ruff check` plus the `test-wiring` hook (see `.pre-commit-config.yaml`); `ruff format` and `mypy` are configured (root `pyproject.toml`) but not currently run automatically anywhere, so run them manually
- Type hints encouraged
- No `TODO`/`FIXME` comments — complete the work or open an issue

## Branching & PR Workflow

1. Fork the repo and create a branch from `main`
2. Make your changes with focused commits
3. Run `pre-commit run --all-files` before pushing
4. Open a PR against `main` — fill out the PR template
5. Address review feedback; a maintainer will merge

## Licensing

By submitting a contribution you agree that your changes are licensed under the Mozilla Public License 2.0 (inbound = outbound). The project uses DCO rather than a CLA.

## Commit Sign-off (DCO)

By contributing you certify that you have the right to submit the work under this project's license. Add a sign-off to your commits:

```bash
git commit -s -m "feat: add new tool"
```

This adds `Signed-off-by: Your Name <your@email.com>` to the commit message.

## Guides

Step-by-step walkthroughs for common contribution scenarios:

- **[Adding an MCP Server](guides/mcp-servers.md)** — create a new tool server, register it, define tools in JSON, and test locally
- **[Expert Workflows](guides/expert-workflows.md)** — build multi-step LLM workflows with function handlers and mid-run user input

See `CLAUDE.md` for detailed architecture notes used during day-to-day development.

## Secret Management

- **Never commit `.env` files** — they contain credentials and are gitignored by default
- Always use `.env.example` as your template (`cp .env.example .env`), then fill in your own values
- If you accidentally commit a secret, rotate it immediately and rewrite history with `git filter-repo`
- The `detect-secrets` pre-commit hook will catch most accidental secret inclusions before they land in a commit
- For security vulnerabilities related to exposed credentials, follow the process in [SECURITY.md](SECURITY.md)

## Reporting Bugs

Open a [GitHub Issue](../../issues/new?template=bug_report.md) with steps to reproduce.

## Security Issues

See [SECURITY.md](SECURITY.md) — do not open public issues for vulnerabilities.
