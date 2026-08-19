"""grafana_scheduler.py's scheduling-decision and subprocess-invocation logic.

This is the daemon that replaces the nightly Grafana-indexing job that used
to live in chat_orchestrator's own APScheduler instance -- that job could
never actually run there (it imported grafana_indexer_incremental from a
rag_pipeline/ingestion path that never contained it, in an image that never
copied anansi_app/ in at all), so it silently failed once a night,
indefinitely. These tests cover the pure decision functions (should
scheduling run at all, is now the due hour) and the subprocess wrapper's
success/failure handling, without ever invoking the real indexer.
"""

import os
import subprocess
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

_ANANSI_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ANANSI_APP_ROOT, os.path.join(_ANANSI_APP_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import grafana_scheduler  # noqa: E402


class TestGrafanaSchedulingEnabled:
    def test_enabled_by_default(self, monkeypatch):
        monkeypatch.delenv("GRAFANA_ENABLED", raising=False)
        monkeypatch.delenv("GRAFANA_ACTIONS_ENABLED", raising=False)

        assert grafana_scheduler._grafana_scheduling_enabled() is True

    def test_disabled_when_grafana_enabled_is_false(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_ENABLED", "false")
        monkeypatch.delenv("GRAFANA_ACTIONS_ENABLED", raising=False)

        assert grafana_scheduler._grafana_scheduling_enabled() is False

    def test_disabled_when_legacy_flag_is_false(self, monkeypatch):
        """GRAFANA_ACTIONS_ENABLED=false disables even if the new-style
        GRAFANA_ENABLED flag is unset or true -- mirrors
        grafana_mcp_server.py's own dual-flag disable-wins check."""
        monkeypatch.delenv("GRAFANA_ENABLED", raising=False)
        monkeypatch.setenv("GRAFANA_ACTIONS_ENABLED", "false")

        assert grafana_scheduler._grafana_scheduling_enabled() is False

    def test_enabled_when_explicitly_true(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_ENABLED", "true")
        monkeypatch.delenv("GRAFANA_ACTIONS_ENABLED", raising=False)

        assert grafana_scheduler._grafana_scheduling_enabled() is True


class TestSyncHour:
    def test_default_is_2am(self, monkeypatch):
        monkeypatch.delenv("GRAFANA_SYNC_HOUR", raising=False)

        assert grafana_scheduler._sync_hour() == 2

    def test_reads_configured_hour(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_SYNC_HOUR", "14")

        assert grafana_scheduler._sync_hour() == 14

    def test_falls_back_to_default_on_invalid_value(self, monkeypatch):
        monkeypatch.setenv("GRAFANA_SYNC_HOUR", "not-a-number")

        assert grafana_scheduler._sync_hour() == 2


class TestScheduleTimezone:
    def test_default_is_utc(self, monkeypatch):
        monkeypatch.delenv("METRICS_TIMEZONE", raising=False)

        assert grafana_scheduler._schedule_timezone() == ZoneInfo("UTC")

    def test_reads_configured_timezone(self, monkeypatch):
        monkeypatch.setenv("METRICS_TIMEZONE", "Africa/Lagos")

        assert grafana_scheduler._schedule_timezone() == ZoneInfo("Africa/Lagos")

    def test_falls_back_to_utc_on_invalid_timezone(self, monkeypatch):
        monkeypatch.setenv("METRICS_TIMEZONE", "Not/AZone")

        assert grafana_scheduler._schedule_timezone() == ZoneInfo("UTC")


class TestIsDue:
    def test_due_when_hour_matches_and_not_yet_run_today(self):
        now = datetime(2026, 8, 19, 2, 5, tzinfo=ZoneInfo("UTC"))

        assert grafana_scheduler._is_due(now, sync_hour=2, last_run_date=None) is True

    def test_not_due_when_already_run_today(self):
        now = datetime(2026, 8, 19, 2, 5, tzinfo=ZoneInfo("UTC"))

        assert (
            grafana_scheduler._is_due(now, sync_hour=2, last_run_date=date(2026, 8, 19)) is False
        )

    def test_due_again_the_next_day(self):
        now = datetime(2026, 8, 20, 2, 5, tzinfo=ZoneInfo("UTC"))

        assert (
            grafana_scheduler._is_due(now, sync_hour=2, last_run_date=date(2026, 8, 19)) is True
        )

    def test_not_due_outside_the_scheduled_hour(self):
        now = datetime(2026, 8, 19, 14, 0, tzinfo=ZoneInfo("UTC"))

        assert grafana_scheduler._is_due(now, sync_hour=2, last_run_date=None) is False


class _FakeCompletedProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestRunIndexerOnce:
    def test_clean_exit_reports_success(self, monkeypatch):
        monkeypatch.setattr(
            grafana_scheduler.subprocess,
            "run",
            lambda *a, **kw: _FakeCompletedProcess(
                returncode=0, stdout="\n✅ Grafana indexing completed: 123 panels indexed"
            ),
        )

        assert grafana_scheduler.run_indexer_once(verbose=False) is True

    def test_non_zero_exit_reports_failure(self, monkeypatch):
        monkeypatch.setattr(
            grafana_scheduler.subprocess,
            "run",
            lambda *a, **kw: _FakeCompletedProcess(
                returncode=1,
                stderr="\n⚠️  Grafana indexing completed with warnings: "
                "16/16 panel description(s) failed to generate",
            ),
        )

        assert grafana_scheduler.run_indexer_once(verbose=False) is False

    def test_timeout_propagates_to_the_caller(self, monkeypatch):
        """run_daemon's loop is what's responsible for catching this --
        run_indexer_once itself should not swallow it, so a hang doesn't
        silently look like a failure with no signal at all."""

        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="grafana_indexer_incremental.py", timeout=600)

        monkeypatch.setattr(grafana_scheduler.subprocess, "run", _raise_timeout)

        try:
            grafana_scheduler.run_indexer_once(verbose=False)
            assert False, "expected TimeoutExpired to propagate"
        except subprocess.TimeoutExpired:
            pass
