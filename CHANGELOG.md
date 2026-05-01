# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [Unreleased]

## [0.1.0] - 2026-04-22

Initial public release.

### Added
- Mozilla Public License 2.0
- OSS paperwork: NOTICE, AUTHORS, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, CHANGELOG
- GitHub issue and PR templates, CI workflow (lint + tests), secret-scan workflow, Dependabot config
- `ORGANIZATION_NAME` env var for operator branding in chart watermarks and error messages
- Generic `org_logo_white.svg` asset slot (deploy your own logo)
- `app.example.yaml` template for DigitalOcean App Platform deployment
- `.python-version` pinned to 3.11
- `DOC_CODE_PREFIX` env var to parameterize document-code pattern (default: `DOC`)

### Changed
- Removed company-specific branding from all Python source files and test fixtures
- Generalized Grafana folder name default from hardcoded value to `GRAFANA_FOLDER_NAME` env var (default: empty = all folders)
- `JIRA_PROJECT_KEY` default changed from hardcoded value to empty string
- Worktree scripts now auto-detect repo root instead of using hardcoded paths
- Chat DB migration directories un-ignored so OSS users can track schema history

### Removed
- Internal planning documents (`todos/`, `plans/`, `docs/deploy-checklists/`, `docs/superpowers/`, `docs/solutions/`)
- One-off scripts with hardcoded production state (`clear_stuck_conversation.py`, `extract_tagged_telegram_messages.py`, `check_orgs.py`, `extract_tags.sh`)
- Operator logo assets removed from git index (still gitignored)
