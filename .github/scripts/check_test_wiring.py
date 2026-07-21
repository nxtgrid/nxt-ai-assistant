#!/usr/bin/env python3
"""Guard against test files that exist but never run.

`.gitignore` ignores `tests/` on purpose — this is the public OSS mirror of an
internal tree, and tests can carry operator-specific data, so publishing one is
an explicit `git add -f`. The cost of that safety is two silent failure modes:

1. A new test file is never committed. `git add` on an ignored path is a no-op,
   `git commit` then succeeds without it, and the test never reaches CI.
2. A test file *is* committed but sits outside every path CI runs, so it is
   tracked, reviewed, and dead. Every file under `mcp_servers/tests/` except
   `test_grafana_variable_substitution.py` was in this state, including a suite
   that had been failing since the initial commit.

Both fail silently today. This script makes them fail loudly instead. It runs
from pre-commit, so (1) is caught on the machine where the file still exists —
CI checks out a clean tree and cannot see a file that was never committed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
GITIGNORE = REPO_ROOT / ".gitignore"

# Repo path -> the pytest invocation in ci.yml that covers it. Every tracked test
# file must live under one of these prefixes, and every invocation must still
# appear verbatim in ci.yml — so dropping a job fails here rather than quietly
# switching a suite off.
CI_TEST_PATHS = {
    "chat_orchestrator/tests/": "pytest tests/",
    "shared/": "pytest ../shared",
    "mcp_servers/tests/": "pytest mcp_servers/tests",
    "anansi_app/tests/": "pytest anansi_app/tests",
}

TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*|[^/]*_test)\.py$")


def _git(*args: str) -> list[str]:
    out = subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [line for line in out.splitlines() if line]


def check_every_tracked_test_runs() -> list[str]:
    """Tracked test files must sit under a path CI runs."""
    orphans = [
        f
        for f in _git("ls-files")
        if TEST_FILE_RE.search(f) and not f.startswith(tuple(CI_TEST_PATHS))
    ]
    if not orphans:
        return []
    return [
        "Tracked test files that no CI job runs:",
        *(f"  {f}" for f in orphans),
        "",
        "  These are committed but dead. Either add the path to a pytest",
        "  invocation in .github/workflows/ci.yml and to CI_TEST_PATHS in",
        "  this script, or move the file under an existing test root.",
    ]


def check_ci_paths_are_live() -> list[str]:
    """Each declared path must still be run by ci.yml."""
    workflow = CI_WORKFLOW.read_text()
    stale = [f"{path} (expected `{cmd}`)" for path, cmd in CI_TEST_PATHS.items() if cmd not in workflow]
    if not stale:
        return []
    return [
        "Test suites declared here but no longer run by ci.yml:",
        *(f"  {p}" for p in stale),
        "",
        "  A test suite was removed from CI. Restore the job, or drop the path",
        "  here and relocate the tests it covered.",
    ]


def _deliberately_unpublished() -> set[str]:
    """Test files .gitignore names explicitly, rather than via the blanket rule.

    Naming a path is how the internal tree records "this one must not be
    published". The blanket `tests/` rule already hides it, so the entry is
    redundant to git — it exists to state the intent, and this is what reads it.
    """
    return {
        line.strip()
        for line in GITIGNORE.read_text().splitlines()
        if line.strip().endswith(".py") and not line.lstrip().startswith("#")
    }


def check_no_dropped_test_files() -> list[str]:
    """Untracked test files hidden by .gitignore — the silent-drop case."""
    untracked = [
        f
        for f in _git(
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--",
            ":(glob)**/tests/**",
            ":(exclude)**/.venv/**",
        )
        if TEST_FILE_RE.search(f)
    ]
    dropped = sorted(set(untracked) - _deliberately_unpublished())
    if not dropped:
        return []
    return [
        "Test files present on disk but not tracked by git:",
        *(f"  {f}" for f in dropped),
        "",
        "  .gitignore hides `tests/`, so a plain `git add` on these is a no-op and",
        "  the commit will silently omit them. Pick one:",
        "",
        "    git add -f <path>      # publish it (check it carries no operator data)",
        "",
        "  ...or, if it must stay internal, name it in .gitignore next to",
        "  mcp_servers/tests/test_meter_actions.py to record that decision.",
        "",
        "  See 'Adding a new test file' in CONTRIBUTING.md.",
    ]


def main() -> int:
    problems: list[str] = []
    for check in (
        check_no_dropped_test_files,
        check_every_tracked_test_runs,
        check_ci_paths_are_live,
    ):
        if lines := check():
            problems.extend(lines + [""])

    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
