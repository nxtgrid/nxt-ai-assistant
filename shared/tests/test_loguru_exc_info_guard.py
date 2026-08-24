"""Tests for the `loguru-exc-info` pre-commit guard.

`exc_info=True` is a *stdlib* logging keyword. Loguru accepts arbitrary kwargs
and hands them to `str.format`, so an unknown one is silently swallowed and the
traceback is never emitted -- the call logs its message and nothing else. That
cost us the cause of a production media-capture failure on 2026-08-24.

The guard classifies each module's loggers by how they were obtained, so stdlib
call sites (where `exc_info=True` is correct) stay untouched.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_PATH = REPO_ROOT / ".github" / "scripts" / "check_loguru_exc_info.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_loguru_exc_info", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _violations(source: str) -> list:
    return guard.find_violations(source, Path("example.py"))


def test_flags_exc_info_on_a_directly_imported_loguru_logger():
    source = """
from loguru import logger

logger.warning("boom {}", value, exc_info=True)
"""
    (violation,) = _violations(source)
    assert violation.lineno == 4
    assert violation.method == "warning"


def test_flags_exc_info_on_a_logger_built_by_shared_get_logger():
    source = """
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)
LOGGER.error("boom", exc_info=True)
"""
    (violation,) = _violations(source)
    assert violation.method == "error"


def test_flags_exc_info_on_a_module_alias_of_the_loguru_logger():
    source = """
from loguru import logger

LOGGER = logger
LOGGER.debug("boom", exc_info=True)
"""
    assert len(_violations(source)) == 1


def test_ignores_exc_info_on_a_stdlib_logger():
    source = """
import logging

logger = logging.getLogger(__name__)
logger.error("boom %s", value, exc_info=True)
"""
    assert _violations(source) == []


def test_ignores_exc_info_on_a_stdlib_logger_held_on_an_instance():
    source = """
import logging


class Registry:
    def __init__(self, name):
        self._logger = logging.getLogger(name)

    def run(self):
        try:
            pass
        except Exception:
            self._logger.error("boom", exc_info=True)
"""
    assert _violations(source) == []


def test_ignores_the_pytest_raises_exc_info_binding():
    source = """
import pytest

with pytest.raises(ValueError) as exc_info:
    pass
assert "x" in str(exc_info.value)
"""
    assert _violations(source) == []


def test_ignores_a_correctly_written_loguru_traceback_call():
    source = """
from loguru import logger

logger.opt(exception=True).warning("boom {}", value)
"""
    assert _violations(source) == []


def test_repo_has_no_loguru_exc_info_call_sites():
    """The regression bar: every loguru site in the tree uses opt(exception=True)."""
    violations = guard.scan_repo(REPO_ROOT)
    assert violations == [], "\n".join(guard.format_violation(v) for v in violations)


@pytest.mark.parametrize(
    "path",
    [
        "shared/layout/road_network.py",
        "mcp_servers/bridge.py",
        "anansi_app/services/bot_status_service.py",
        "mcp_servers/shared_code/tool_registry.py",
        "mcp_servers/server_registry.py",
        "mcp_servers/servers/customer_server/client_grid_status.py",
    ],
)
def test_known_stdlib_call_sites_keep_their_exc_info(path: str):
    """These modules log through stdlib `logging`, where `exc_info=True` works."""
    source = (REPO_ROOT / path).read_text()
    assert "exc_info=True" in source
