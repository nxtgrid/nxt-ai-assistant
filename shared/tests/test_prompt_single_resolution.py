"""Guardrail: shared/prompts/ is the only package allowed to read a legacy
prompt doc-id env var or fetch a Google Doc for a prompt-backed document.

One prompt = one resolved body. Every consumer of customer.system,
staff.system, experts.definitions, troubleshooting.procedures, or
verification.criteria must go through shared.prompts.PROMPTS, so that
whatever the Prompts admin page resolves a prompt to (bundled MD, a DB
version, or a Google Doc) is what every consumer receives, from the one
cache in shared/prompts/gdoc.py's GDocStore.

If either test here fails, something added a direct os.getenv(<doc-id env
var>) read or a direct Google Doc fetch outside the library instead of
calling PROMPTS.text()/PROMPTS.resolve() -- which reopens the split-brain
this file exists to prevent: two code paths resolving the "same" prompt
to two different bodies.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# The 5 doc-id env vars shared/prompts/gdoc.py's legacy_doc_id_for() maps to
# a prompt id. A direct os.getenv() read of one of these outside the library
# is always a bypass -- there is no legitimate reason for a second call site
# to know these names.
LEGACY_DOC_ENV_VARS = [
    "CUSTOMER_SUPPORT_DOC_ID",
    "STAFF_SUPPORT_DOC_ID",
    "VERIFICATION_DOC_ID",
    "EXPERT_INSTRUCTIONS_DOC_ID",
    "TROUBLESHOOTING_PROCEDURES_DOC_ID",
]

# Fetchers for documents that are NOT prompt-backed -- an arbitrary
# user-supplied doc, the doc editor, and the generic fetcher other tools
# build on. These legitimately call the raw fetch functions directly.
ALLOWED_FETCH_FILES = {
    "mcp_servers/servers/knowledge_server/knowledge_mcp_server.py",
    "chat_orchestrator/orchestrator/experts/handlers/doc_editor/process_doc_edits.py",
    "chat_orchestrator/orchestrator/services/artifacts_provider.py",
    # The fetch function's own definition + docstring example, not a caller.
    "shared/utils/gdrive_doc_fetcher.py",
    # One-time, offline ops tool: seeds the DB override layer FROM a live
    # Google Doc's content. Its entire purpose is reading docs directly, not
    # a second live resolution path competing with PROMPTS at request time.
    "scripts/migrate_docs_to_db.py",
}

_FETCH_MARKERS = ("fetch_google_doc_markdown(", "GoogleDriveDocFetcher()")


def _iter_py_files():
    """Every tracked-looking .py file outside tests, venvs, and worktrees."""
    for path in REPO_ROOT.rglob("*.py"):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if "/tests/" in rel or rel.startswith("tests/"):
            continue
        if any(
            marker in rel
            for marker in ("/.venv/", "/venv/", "/node_modules/", "/.worktrees/", "/.claude/")
        ):
            continue
        if rel.startswith("shared/prompts/"):
            continue
        # Config *declaration* (env var aliases), not a resolution path.
        if rel.endswith("config/settings.py") or rel.endswith("flag_registry.py"):
            continue
        yield path, rel


def test_no_direct_legacy_doc_env_var_reads_outside_the_prompt_library():
    pattern = re.compile(r'os\.getenv\(\s*["\'](' + "|".join(LEGACY_DOC_ENV_VARS) + r')["\']')
    offenders = []
    for path, rel in _iter_py_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(text):
            line_no = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{rel}:{line_no}: {match.group(0)}")

    assert not offenders, (
        "Direct os.getenv() read of a legacy prompt doc-id env var found outside "
        "shared/prompts/. Route through shared.prompts.PROMPTS instead (see "
        "shared/prompts/gdoc.py's legacy_doc_id_for), so this prompt keeps resolving "
        "to one body for every consumer:\n" + "\n".join(offenders)
    )


def test_no_direct_google_doc_fetch_outside_the_prompt_library_or_allowlist():
    offenders = []
    for path, rel in _iter_py_files():
        if rel in ALLOWED_FETCH_FILES:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(marker in text for marker in _FETCH_MARKERS):
            offenders.append(rel)

    assert not offenders, (
        "Direct Google Doc fetch (fetch_google_doc_markdown / GoogleDriveDocFetcher) "
        "found outside shared/prompts/ and ALLOWED_FETCH_FILES. If this is a genuinely "
        "non-prompt document, add it to ALLOWED_FETCH_FILES with a comment saying why; "
        "if it's a prompt body, use PROMPTS.text()/PROMPTS.resolve() instead:\n"
        + "\n".join(offenders)
    )


def test_allowed_fetch_files_still_exist():
    """Catches a silently-stale allowlist entry (e.g. after a rename)."""
    missing = [f for f in ALLOWED_FETCH_FILES if not (REPO_ROOT / f).is_file()]
    assert not missing, f"ALLOWED_FETCH_FILES references files that no longer exist: {missing}"
