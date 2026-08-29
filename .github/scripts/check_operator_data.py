#!/usr/bin/env python3
"""Guard against real grid/customer identifiers reaching a commit.

`AGENTS.md` and `CLAUDE.md` both say this repo is public and real grid/site
names, customer names, org tags, ticket refs and chat/session ids must never
reach a commit message, PR body, code comment, or test fixture. On 2026-08-29
that rule existed and was not consulted: the alert-flooding fix on
`fix/alert-fail-open-noise` (#159) was committed and its PR opened carrying
three real grid names, two real ticket refs and a real DCU serial, pulled from
`doctl` logs during the investigation and left in a commit message, a PR body,
two docstrings and new test fixtures. Nothing scanned any of those surfaces.
This does.

Two scan surfaces, run as two separate pre-commit hooks (see
`.pre-commit-config.yaml`):

- `--commit-msg <path>`: the commit message about to be created.
- (no flag): the *added* lines of the currently staged diff, plus the paths of
  staged files. Only additions are scanned, not a file's full content, so
  this does not block unrelated edits to a file that predates this hook and
  still carries a name from before the scrub -- that is a separate, one-time
  cleanup, not something a commit gate should hold hostage. It does catch the
  AGENTS.md-identified gap of editing an already-tracked test file, since a
  staged modification to a tracked file still shows up as `+` lines here.

What it matches against is a literal, case-insensitive, whole-word list read
from `.github/scripts/operator_names.local.txt` -- gitignored, never
published, because the list of real identifiers *is* the sensitive data this
script exists to keep out. See `operator_names.example.txt` (tracked) for the
format. If the local file is missing or empty, this prints a notice and exits
0 rather than blocking every commit on every fresh clone forever -- the
protection is opt-in until someone populates the list, same tradeoff as any
local secrets baseline.

Known gap: CI runs `pre-commit run --all-files` (see `.github/workflows/ci.yml`),
which does not stage anything and does not pass `--hook-stage commit-msg`, so
both hooks are no-ops there today. This is local-machine protection only,
same as `pre-commit install` itself only helps once it's actually installed.
Provisioning the local list to CI would mean putting real names in a GitHub
Actions secret -- a separate decision, not made by this script.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
NAME_LIST_PATH = REPO_ROOT / ".github" / "scripts" / "operator_names.local.txt"
EXAMPLE_LIST_PATH = REPO_ROOT / ".github" / "scripts" / "operator_names.example.txt"

# +++ b/path, +++ /dev/null (deletion -- no new-file lines to scan)
_DIFF_NEW_FILE_RE = re.compile(r"^\+\+\+ (?:b/(?P<path>.+)|(?P<devnull>/dev/null))$")
_DIFF_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,\d+)? @@")


@dataclass(frozen=True)
class Hit:
    location: str
    name: str
    snippet: str


def load_names(path: Path = NAME_LIST_PATH) -> list[str]:
    """Non-empty, non-comment lines from the local identifier list."""
    if not path.exists():
        return []
    names = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            names.append(stripped)
    return names


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
    ).stdout


def _name_pattern(names: list[str]) -> re.Pattern[str] | None:
    if not names:
        return None
    # Not \b: \b treats "_" as a word character, so a configured name inside
    # "test_foo_fixture.py" or "FOO_GRID_ID" -- exactly how a real name tends
    # to show up in a filename or a Python identifier -- would not be a
    # boundary and the match would silently miss. Alnum-only boundaries catch
    # both that and prose, while still rejecting a longer word like "Foobar"
    # that merely contains one as a substring.
    alternation = "|".join(re.escape(name) for name in names)
    return re.compile(rf"(?<![A-Za-z0-9])(?:{alternation})(?![A-Za-z0-9])", re.IGNORECASE)


def find_hits(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Configured names present in `text`, matched whole-word and case-insensitively."""
    return sorted({match.group(0) for match in pattern.finditer(text)})


def iter_added_lines(diff_text: str):
    """Yield (path, new_lineno, line_content) for every `+` line in a `-U0` diff.

    Deletions and `+++`/`---` file headers are not additions and are skipped.
    """
    path: str | None = None
    lineno = 0
    for line in diff_text.splitlines():
        header = _DIFF_NEW_FILE_RE.match(line)
        if header:
            path = header.group("path")  # None for /dev/null (deleted file)
            continue
        hunk = _DIFF_HUNK_RE.match(line)
        if hunk:
            lineno = int(hunk.group("start"))
            continue
        if path is None:
            continue
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            yield path, lineno, line[1:]
            lineno += 1
        # "-" lines don't consume new-file line numbers; context lines don't
        # appear at all under -U0.


def scan_staged_diff(pattern: re.Pattern[str]) -> list[Hit]:
    diff_text = _git("diff", "--cached", "--unified=0", "--no-color")
    hits = []
    for path, lineno, content in iter_added_lines(diff_text):
        for name in find_hits(content, pattern):
            hits.append(Hit(f"{path}:{lineno}", name, content.strip()))
    return hits


def scan_staged_paths(pattern: re.Pattern[str]) -> list[Hit]:
    paths = [p for p in _git("diff", "--cached", "--name-only").splitlines() if p]
    hits = []
    for path in paths:
        for name in find_hits(path, pattern):
            hits.append(Hit(path, name, path))
    return hits


def scan_commit_message(path: Path, pattern: re.Pattern[str]) -> list[Hit]:
    text = path.read_text(encoding="utf-8")
    hits = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name in find_hits(line, pattern):
            hits.append(Hit(f"commit message:{lineno}", name, line.strip()))
    return hits


def format_hits(hits: list[Hit], *, source: str) -> str:
    lines = [f"Real identifier(s) found in {source} -- this repo is public:", ""]
    for hit in hits:
        lines.append(f'  {hit.location}: "{hit.name}" in: {hit.snippet}')
    lines += [
        "",
        "  Swap in an obviously-fake placeholder (see AGENTS.md, first section):",
        '    grid/site name  -> "Site A" / "the unmanaged grid"',
        '    customer + org  -> "Customer (#OrgTag)"',
        '    ticket/escalation id -> "<ticket-id>"',
        "",
        "  If this is a false positive (the match is coincidental, not the real",
        "  identifier), skip this hook for the commit:",
        "    SKIP=operator-data git commit ...            # staged-diff check",
        "    SKIP=operator-data-commit-msg git commit ...  # commit-message check",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit-msg",
        action="store_true",
        help="Scan the commit message file (path given positionally) instead of the staged diff.",
    )
    parser.add_argument("path", nargs="?", help="Commit message file, for --commit-msg.")
    args = parser.parse_args(argv)

    names = load_names()
    if not names:
        print(
            f"[check_operator_data] {NAME_LIST_PATH.relative_to(REPO_ROOT)} is missing "
            "or empty -- privacy scan skipped for this commit. Copy "
            f"{EXAMPLE_LIST_PATH.name} to {NAME_LIST_PATH.name} and add the real "
            "grid/customer identifiers this repo must never publish.",
            file=sys.stderr,
        )
        return 0
    pattern = _name_pattern(names)
    assert pattern is not None  # names is non-empty here

    if args.commit_msg:
        if not args.path:
            print("[check_operator_data] --commit-msg requires the message file path.", file=sys.stderr)
            return 1
        hits = scan_commit_message(Path(args.path), pattern)
        source = "the commit message"
    else:
        hits = scan_staged_diff(pattern) + scan_staged_paths(pattern)
        source = "staged changes"

    if not hits:
        return 0
    print(format_hits(hits, source=source), file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
