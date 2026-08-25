"""episodic_scheduler.py's scheduling decisions and outage handling.

This daemon exists because nothing had ever run the episodic distillation
batch: scripts/distill_episodic_memory.py said "run nightly", but no
scheduler invoked it and repo-root scripts/ is not in any deployed image, so
episodic_distillations had been empty since migration 0019 created it. These
tests cover the pure decision functions and the "is this run trustworthy?"
logic, without ever calling an LLM or a database.
"""

import os
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

_ANANSI_APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (_ANANSI_APP_ROOT, os.path.join(_ANANSI_APP_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import episodic_scheduler  # noqa: E402


class TestSchedulingEnabled:
    def test_enabled_by_default(self, monkeypatch):
        """A deployment that never heard of this flag still gets distillation.

        The whole point of this change is that the batch has never run; an
        opt-in default would leave it not running.
        """
        monkeypatch.delenv("EPISODIC_DISTILL_ENABLED", raising=False)
        assert episodic_scheduler._episodic_scheduling_enabled() is True

    def test_explicit_false_disables(self, monkeypatch):
        monkeypatch.setenv("EPISODIC_DISTILL_ENABLED", "false")
        assert episodic_scheduler._episodic_scheduling_enabled() is False

    def test_case_and_whitespace_tolerant(self, monkeypatch):
        monkeypatch.setenv("EPISODIC_DISTILL_ENABLED", "  FALSE  ")
        assert episodic_scheduler._episodic_scheduling_enabled() is False

    def test_any_other_value_leaves_it_enabled(self, monkeypatch):
        monkeypatch.setenv("EPISODIC_DISTILL_ENABLED", "true")
        assert episodic_scheduler._episodic_scheduling_enabled() is True


class TestDistillHour:
    def test_defaults_to_an_hour_after_the_grafana_indexer(self, monkeypatch):
        """Both are LLM-heavy nightly batches in the same container."""
        monkeypatch.delenv("EPISODIC_DISTILL_HOUR", raising=False)
        assert episodic_scheduler._distill_hour() == 3

    def test_reads_the_env_var(self, monkeypatch):
        monkeypatch.setenv("EPISODIC_DISTILL_HOUR", "5")
        assert episodic_scheduler._distill_hour() == 5

    def test_a_junk_value_falls_back_rather_than_crashing_the_daemon(self, monkeypatch):
        monkeypatch.setenv("EPISODIC_DISTILL_HOUR", "not-a-number")
        assert episodic_scheduler._distill_hour() == 3


class TestIsDue:
    def test_due_in_the_scheduled_hour_when_not_yet_run_today(self):
        now = datetime(2026, 8, 25, 3, 30, tzinfo=ZoneInfo("UTC"))
        assert episodic_scheduler._is_due(now, 3, None) is True

    def test_not_due_outside_the_scheduled_hour(self):
        now = datetime(2026, 8, 25, 4, 0, tzinfo=ZoneInfo("UTC"))
        assert episodic_scheduler._is_due(now, 3, None) is False

    def test_not_due_twice_in_the_same_day(self):
        now = datetime(2026, 8, 25, 3, 45, tzinfo=ZoneInfo("UTC"))
        assert episodic_scheduler._is_due(now, 3, date(2026, 8, 25)) is False

    def test_due_again_the_next_day(self):
        now = datetime(2026, 8, 26, 3, 5, tzinfo=ZoneInfo("UTC"))
        assert episodic_scheduler._is_due(now, 3, date(2026, 8, 25)) is True


def _stub_distill(monkeypatch, results):
    """Replace distill_anchor_type with a canned per-anchor-type result."""
    seen = []

    async def _fake(anchor_type, apply=False, client=None, on_progress=None):
        seen.append(anchor_type)
        outcome = results[anchor_type]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    import shared.episodic_memory

    monkeypatch.setattr(shared.episodic_memory, "distill_anchor_type", _fake)
    return seen


def _ok(written=2, targets=("Alpha", "Beta"), skipped=(), enumerated=2):
    return {
        "anchor_type": "grid",
        "enumerated": enumerated,
        "targets": list(targets),
        "written": written,
        "skipped": list(skipped),
        "error": None,
    }


class TestRunDistillationOnce:
    def test_a_clean_run_of_both_anchor_types_is_trustworthy(self, monkeypatch):
        seen = _stub_distill(monkeypatch, {"grid": _ok(), "organization": _ok()})
        assert episodic_scheduler.run_distillation_once(verbose=False) is True
        assert seen == ["grid", "organization"]

    def test_zero_enumerated_anchors_is_not_trustworthy(self, monkeypatch):
        """[] means "Auth DB may be down" as readily as "no grids".

        Returning False keeps the daemon from consuming the day on what may
        have been an outage -- see shared/entity_eligibility.py.
        """
        _stub_distill(
            monkeypatch,
            {"grid": _ok(enumerated=0, targets=(), written=0), "organization": _ok()},
        )
        assert episodic_scheduler.run_distillation_once(verbose=False) is False

    def test_a_reported_error_is_not_trustworthy(self, monkeypatch):
        bad = _ok()
        bad["error"] = "CHAT_DB_URL / CHAT_DB_SERVICE_KEY are not set"
        _stub_distill(monkeypatch, {"grid": bad, "organization": _ok()})
        assert episodic_scheduler.run_distillation_once(verbose=False) is False

    def test_one_anchor_type_raising_does_not_stop_the_other(self, monkeypatch):
        seen = _stub_distill(
            monkeypatch,
            {"grid": RuntimeError("auth db down"), "organization": _ok()},
        )
        assert episodic_scheduler.run_distillation_once(verbose=False) is False
        assert seen == ["grid", "organization"]

    def test_zero_targets_with_anchors_enumerated_is_still_trustworthy(self, monkeypatch):
        """Every anchor hand-edited is a real, complete outcome -- not an outage."""
        _stub_distill(
            monkeypatch,
            {
                "grid": _ok(enumerated=3, targets=(), written=0),
                "organization": _ok(enumerated=1, targets=(), written=0),
            },
        )
        assert episodic_scheduler.run_distillation_once(verbose=False) is True


class TestBothAnchorTypesAreCovered:
    def test_grid_and_organization_both_run(self):
        """The episodic provider anchors on either, so both must be filled."""
        assert episodic_scheduler.ANCHOR_TYPES == ("grid", "organization")


@pytest.mark.parametrize("anchor_type", ["grid", "organization"])
def test_anchor_types_match_what_the_provider_can_look_up(anchor_type):
    from shared.entity_eligibility import SUPPORTED_ANCHOR_ENTITY_TYPES

    assert anchor_type in SUPPORTED_ANCHOR_ENTITY_TYPES
