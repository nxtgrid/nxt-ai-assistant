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

```bash
# All orchestrator tests
cd chat_orchestrator && source .venv/bin/activate
pytest tests/

# Specific test file
pytest -v tests/experts/test_workflow_executor.py

# Code quality (run before pushing)
pre-commit run --all-files
```

## Code Style

- **Python 3.11+**, formatted with `ruff` (100-char line length)
- Run `pre-commit install` once to enforce style on every commit
- Type hints encouraged; `mypy` runs in CI
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
