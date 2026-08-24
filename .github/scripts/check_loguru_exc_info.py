#!/usr/bin/env python3
"""Guard against `exc_info=True` on a loguru logger.

`exc_info` is a *stdlib* `logging` keyword. Loguru's methods take arbitrary
kwargs and pass them to `str.format`, so an unknown one is accepted, unused and
silently dropped -- the call logs its message and no traceback at all:

    logger.warning("msg {}", x, exc_info=True)        # message only
    logger.opt(exception=True).warning("msg {}", x)   # message + traceback

Nothing fails, so the mistake survives review and only surfaces when someone
needs the traceback. On 2026-08-24 `capture_escalation_media` lost a customer's
photos three times running and left nothing behind but the file_id.

The repo logs through both libraries, so this cannot be a plain grep: at the
stdlib call sites `exc_info=True` is correct and must stay. Every module here is
classified by how each of its loggers was obtained -- `from loguru import
logger` and `get_logger()` are loguru, `logging.getLogger()` is stdlib -- and
only the loguru ones are reported.
"""

from __future__ import annotations

import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Never worth walking: vendored code, build output, and the deprecated tree that
# ruff already excludes.
SKIP_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "anansi_deprecated",
    "build",
    "dist",
    "node_modules",
    "venv",
}

# Loguru's logger is not a class we can name, so treat these as its factories.
_LOGURU_FACTORIES = {"get_logger", "setup_logging"}
_STDLIB_FACTORIES = {"getLogger"}
# Loguru methods that would swallow the kwarg. `exception()` is included even
# though it already prints a traceback -- the kwarg is still dead weight there.
_LOG_METHODS = {
    "trace",
    "debug",
    "info",
    "success",
    "warning",
    "error",
    "critical",
    "exception",
    "log",
}


@dataclass(frozen=True)
class Violation:
    path: Path
    lineno: int
    col: int
    receiver: str
    method: str


def _dotted(node: ast.expr) -> str | None:
    """Render `logger` / `self._logger` as a name; anything else is unknown."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _classify_loggers(tree: ast.AST) -> tuple[set[str], set[str], bool]:
    """Return (loguru names, stdlib names, whether the module imports loguru)."""
    loguru: set[str] = set()
    stdlib: set[str] = set()
    loguru_factories: set[str] = set()
    imports_loguru = False

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "loguru":
                imports_loguru = True
                for alias in node.names:
                    if alias.name == "logger":
                        loguru.add(alias.asname or alias.name)
            elif module.endswith("utils.logging"):
                imports_loguru = True
                for alias in node.names:
                    if alias.name in _LOGURU_FACTORIES:
                        loguru_factories.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "loguru":
                    imports_loguru = True

    # Assignments can chain (LOGGER = get_logger(...); _LOG = LOGGER) and walk
    # order does not follow the source, so keep resolving until nothing new
    # binds.
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            targets = [name for name in map(_dotted, node.targets) if name]
            if not targets:
                continue
            value = node.value
            bucket: set[str] | None = None
            if isinstance(value, ast.Call):
                factory = _dotted(value.func) or ""
                leaf = factory.rsplit(".", 1)[-1]
                if leaf in _STDLIB_FACTORIES:
                    bucket = stdlib
                elif leaf in loguru_factories:
                    bucket = loguru
                elif leaf == "bind" and factory.rsplit(".", 1)[0] in loguru:
                    # logger.bind(...) returns a loguru logger.
                    bucket = loguru
            elif isinstance(value, ast.Name):
                if value.id in loguru:
                    bucket = loguru
                elif value.id in stdlib:
                    bucket = stdlib
            if bucket is None:
                continue
            for target in targets:
                if target not in bucket:
                    bucket.add(target)
                    changed = True

    return loguru, stdlib, imports_loguru


def find_violations(source: str, path: Path) -> list[Violation]:
    """Every `exc_info=` kwarg in `source` that lands on a loguru logger."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    loguru, stdlib, imports_loguru = _classify_loggers(tree)
    violations: list[Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not any(keyword.arg == "exc_info" for keyword in node.keywords):
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr not in _LOG_METHODS:
            continue
        receiver = _dotted(node.func.value)
        if receiver is None:
            # e.g. logger.opt(...).warning(..., exc_info=True) -- the receiver is
            # itself a call, so only a loguru module can produce it.
            flagged = imports_loguru
            receiver = "<expression>"
        elif receiver in loguru:
            flagged = True
        elif receiver in stdlib:
            flagged = False
        else:
            # A logger this file never binds. Only a module that talks to loguru
            # and never touches stdlib logging can be confident about it, which
            # keeps mixed-logger modules from producing false positives.
            flagged = imports_loguru and not stdlib
        if flagged:
            violations.append(
                Violation(path, node.lineno, node.col_offset, receiver, node.func.attr)
            )

    return sorted(violations, key=lambda v: (str(v.path), v.lineno))


def iter_python_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath) / filename


def scan_repo(root: Path = REPO_ROOT) -> list[Violation]:
    violations: list[Violation] = []
    for path in iter_python_files(root):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "exc_info" not in source:
            continue
        violations.extend(find_violations(source, path.relative_to(root)))
    return violations


def format_violation(violation: Violation) -> str:
    return (
        f"{violation.path}:{violation.lineno}: "
        f"{violation.receiver}.{violation.method}(..., exc_info=True) "
        f"-- loguru drops this; use "
        f"{violation.receiver}.opt(exception=True).{violation.method}(...)"
    )


def main() -> int:
    violations = scan_repo()
    if not violations:
        return 0
    print("exc_info=True on a loguru logger logs no traceback at all.\n")
    for violation in violations:
        print(format_violation(violation))
    print(
        f"\n{len(violations)} call site(s). Loguru accepts unknown kwargs as "
        "str.format arguments and drops them; opt(exception=True) is the loguru "
        "spelling. Stdlib logging call sites are exempt and left alone."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
