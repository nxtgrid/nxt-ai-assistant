"""Tests for scripts/backfill_design_artifacts.py -- the one-time backfill of
gd_designs.artifacts from pre-existing light_preliminary_package packet_state.

Mocking follows the style of shared/tests/test_artifact_log.py: a MagicMock
standing in for Repository (design_repo.get), and append_design_artifact
patched at the module level so we can assert on exact call args without
touching a real DB.

`scripts` has no `__init__.py` (first Python script under scripts/), but is
importable as a namespace package given the repo root is on PYTHONPATH (see
CLAUDE.md's PYTHONPATH conventions for this repo).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from scripts import backfill_design_artifacts as backfill


def _design_repo(design_row: dict | None) -> MagicMock:
    repo = MagicMock()
    repo.get.return_value = design_row
    return repo


# ── extract_drive_id_keys ────────────────────────────────────────────────────


def test_extract_drive_id_keys_ignores_non_matching_and_falsy():
    state = {
        "design_id": "design1",  # doesn't end in _drive_id -- ignored
        "map_image_drive_id": "file-map",
        "site_layout_png_drive_id": "",  # falsy -- ignored
        "other_drive_id": None,  # falsy -- ignored
        "site_name": "Commville",  # unrelated -- ignored
    }
    assert backfill.extract_drive_id_keys(state) == {"map_image": "file-map"}


# ── backfill_packet ──────────────────────────────────────────────────────────


def test_backfill_packet_skips_when_no_design_id():
    packet = {"packet_id": "p1", "packet_state": {"site_name": "Commville"}}
    repo = _design_repo(None)

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {"outcome": "skipped_no_design_id"}
    repo.get.assert_not_called()
    mock_append.assert_not_called()


def test_backfill_packet_skips_when_no_drive_id_keys():
    packet = {"packet_id": "p1", "packet_state": {"design_id": "design1", "site_name": "x"}}
    repo = _design_repo({"id": "design1", "artifacts": {}})

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {"outcome": "nothing_to_backfill", "design_id": "design1"}
    repo.get.assert_not_called()  # nothing to look up, no reason to fetch the design
    mock_append.assert_not_called()


def test_backfill_packet_design_not_found():
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "missing-design", "map_image_drive_id": "file-new"},
    }
    repo = _design_repo(None)

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {"outcome": "design_not_found", "design_id": "missing-design"}
    mock_append.assert_not_called()


def test_backfill_packet_appends_new_drive_id():
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "design1", "map_image_drive_id": "file-new"},
    }
    repo = _design_repo({"id": "design1", "artifacts": {}})

    with patch.object(
        backfill,
        "append_design_artifact",
        return_value={"map_image": [{"drive_file_id": "file-new"}]},
    ) as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {
        "outcome": "processed",
        "design_id": "design1",
        "appended": 1,
        "failed": 0,
    }
    mock_append.assert_called_once_with(
        "design1",
        "map_image",
        drive_file_id="file-new",
        packet_id="p1",
        label="map_image",
    )
    repo.get.assert_called_once_with("design1")


def test_backfill_packet_dedupes_existing_drive_id():
    """Re-running against a design that already has that exact drive_file_id
    recorded must NOT re-append -- this is the idempotency guarantee."""
    existing_artifacts = {"map_image": [{"drive_file_id": "file-existing"}]}
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "design1", "map_image_drive_id": "file-existing"},
    }
    repo = _design_repo({"id": "design1", "artifacts": existing_artifacts})

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {
        "outcome": "processed",
        "design_id": "design1",
        "appended": 0,
        "failed": 0,
    }
    mock_append.assert_not_called()


def test_backfill_packet_write_failure_is_not_dedup():
    """A key that passes the local dedup check (not already present in the
    design's artifacts) but for which append_design_artifact returns None is
    a genuine write failure (e.g. concurrent delete, transient DB error) --
    it must be counted as `failed`, not folded into `appended: 0` as if it
    were just an already-backfilled key."""
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "design1", "map_image_drive_id": "file-new"},
    }
    repo = _design_repo({"id": "design1", "artifacts": {}})

    with patch.object(backfill, "append_design_artifact", return_value=None) as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result == {
        "outcome": "processed",
        "design_id": "design1",
        "appended": 0,
        "failed": 1,
    }
    mock_append.assert_called_once_with(
        "design1",
        "map_image",
        drive_file_id="file-new",
        packet_id="p1",
        label="map_image",
    )


def test_backfill_packet_multiple_drive_id_keys_fetches_design_once():
    packet = {
        "packet_id": "p1",
        "packet_state": {
            "design_id": "design1",
            "map_image_drive_id": "file-map",
            "site_layout_png_drive_id": "file-layout",
            "qgis_project_drive_id": "file-qgis",
        },
    }
    repo = _design_repo({"id": "design1", "artifacts": {}})

    with patch.object(
        backfill, "append_design_artifact", side_effect=lambda *a, **k: {}
    ) as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=False)

    assert result["outcome"] == "processed"
    assert result["appended"] == 3
    assert mock_append.call_count == 3
    # The design row must be fetched ONCE per packet, not once per _drive_id key.
    repo.get.assert_called_once_with("design1")


def test_backfill_packet_dry_run_reports_without_writing(capsys):
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "design1", "map_image_drive_id": "file-new"},
    }
    repo = _design_repo({"id": "design1", "artifacts": {}})

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=True)

    assert result == {
        "outcome": "processed",
        "design_id": "design1",
        "appended": 1,
        "failed": 0,
    }
    mock_append.assert_not_called()
    out = capsys.readouterr().out
    assert "[dry-run] would append" in out
    assert "design_id=design1" in out
    assert "artifact_type=map_image" in out
    assert "drive_file_id=file-new" in out


def test_backfill_packet_dry_run_still_dedupes_existing():
    existing_artifacts = {"map_image": [{"drive_file_id": "file-existing"}]}
    packet = {
        "packet_id": "p1",
        "packet_state": {"design_id": "design1", "map_image_drive_id": "file-existing"},
    }
    repo = _design_repo({"id": "design1", "artifacts": existing_artifacts})

    with patch.object(backfill, "append_design_artifact") as mock_append:
        result = backfill.backfill_packet(packet, repo, dry_run=True)

    assert result == {
        "outcome": "processed",
        "design_id": "design1",
        "appended": 0,
        "failed": 0,
    }
    mock_append.assert_not_called()


# ── run() -- batch orchestration ─────────────────────────────────────────────


def test_run_continues_after_one_packet_errors(capsys):
    packets = [
        {
            "packet_id": "bad",
            "packet_state": {"design_id": "design-bad", "map_image_drive_id": "file-bad"},
        },
        {
            "packet_id": "good",
            "packet_state": {"design_id": "design-good", "map_image_drive_id": "file-good"},
        },
    ]

    def fake_get(design_id):
        if design_id == "design-bad":
            raise Exception("boom: connection reset")
        return {"id": design_id, "artifacts": {}}

    repo = MagicMock()
    repo.get.side_effect = fake_get

    with (
        patch.object(backfill, "Repository", return_value=repo),
        patch.object(backfill, "iter_lpp_packets", return_value=iter(packets)),
        patch.object(backfill, "append_design_artifact", return_value={}) as mock_append,
    ):
        backfill.run(dry_run=False, limit=None)

    # The bad packet's exception must not have stopped processing of the good one.
    mock_append.assert_called_once_with(
        "design-good",
        "map_image",
        drive_file_id="file-good",
        packet_id="good",
        label="map_image",
    )
    out = capsys.readouterr().out
    assert "Packets scanned:                          2" in out
    assert "Packets errored:                           1" in out


def test_run_dry_run_writes_nothing(capsys):
    packets = [
        {
            "packet_id": "p1",
            "packet_state": {"design_id": "design1", "map_image_drive_id": "file-new"},
        }
    ]
    repo = MagicMock()
    repo.get.return_value = {"id": "design1", "artifacts": {}}

    with (
        patch.object(backfill, "Repository", return_value=repo),
        patch.object(backfill, "iter_lpp_packets", return_value=iter(packets)),
        patch.object(backfill, "append_design_artifact") as mock_append,
    ):
        backfill.run(dry_run=True, limit=None)

    mock_append.assert_not_called()
    out = capsys.readouterr().out
    assert "[dry-run] would append" in out
    assert "Artifact entries would be appended: 1" in out
    assert "WARNING: Running LIVE" not in out


def test_run_reports_append_failed_separately_from_nothing_to_backfill(capsys):
    """A genuinely-new key whose append_design_artifact call fails must be
    counted in `append_failed`, not folded into `nothing_to_backfill` --
    otherwise an operator reading the summary after a run with real DB
    errors would wrongly conclude the backfill fully succeeded."""
    packets = [
        # Key already present in the design's artifacts -- dedup check skips
        # the append_design_artifact call entirely. Still counts as
        # "nothing new to backfill", unaffected by this fix.
        {
            "packet_id": "already-deduped",
            "packet_state": {
                "design_id": "design-deduped",
                "map_image_drive_id": "file-existing",
            },
        },
        # Key passes the dedup check (not already present) but the write
        # itself fails -- a genuine write failure, must show up separately.
        {
            "packet_id": "write-fails",
            "packet_state": {
                "design_id": "design-write-fails",
                "map_image_drive_id": "file-new",
            },
        },
    ]

    def fake_get(design_id):
        if design_id == "design-deduped":
            return {
                "id": design_id,
                "artifacts": {"map_image": [{"drive_file_id": "file-existing"}]},
            }
        return {"id": design_id, "artifacts": {}}

    repo = MagicMock()
    repo.get.side_effect = fake_get

    with (
        patch.object(backfill, "Repository", return_value=repo),
        patch.object(backfill, "iter_lpp_packets", return_value=iter(packets)),
        patch.object(backfill, "append_design_artifact", return_value=None) as mock_append,
    ):
        backfill.run(dry_run=False, limit=None)

    # Only the write-fails packet's key ever reaches append_design_artifact --
    # the deduped packet's key is skipped before the call.
    mock_append.assert_called_once_with(
        "design-write-fails",
        "map_image",
        drive_file_id="file-new",
        packet_id="write-fails",
        label="map_image",
    )
    out = capsys.readouterr().out
    assert "Packets with nothing new to backfill:      1" in out
    assert "Artifact entries failed to append (write errors): 1" in out


def test_run_prints_live_warning_when_not_dry_run(capsys):
    with (
        patch.object(backfill, "Repository", return_value=MagicMock()),
        patch.object(backfill, "iter_lpp_packets", return_value=iter([])),
    ):
        backfill.run(dry_run=False, limit=None)

    out = capsys.readouterr().out
    assert "WARNING: Running LIVE" in out
    assert "--dry-run" in out


def test_run_no_live_warning_when_dry_run(capsys):
    with (
        patch.object(backfill, "Repository", return_value=MagicMock()),
        patch.object(backfill, "iter_lpp_packets", return_value=iter([])),
    ):
        backfill.run(dry_run=True, limit=None)

    out = capsys.readouterr().out
    assert "WARNING: Running LIVE" not in out


# ── iter_lpp_packets -- pagination ───────────────────────────────────────────


class _FakeResponse:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, pages):
        self._pages = pages
        self._page_index = 0

    def select(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, start, end):
        page_size = end - start + 1
        self._page_index = start // page_size if page_size else 0
        return self

    def execute(self):
        if self._page_index >= len(self._pages):
            return _FakeResponse([])
        return _FakeResponse(self._pages[self._page_index])


class _FakeClient:
    def __init__(self, pages):
        self._pages = pages

    def table(self, _name):
        return _FakeQuery(self._pages)


def test_iter_lpp_packets_paginates_across_pages():
    pages = [
        [{"id": 1, "packet_id": "p1"}, {"id": 2, "packet_id": "p2"}],
        [{"id": 3, "packet_id": "p3"}],
    ]
    fake_client = _FakeClient(pages)

    with (
        patch.object(backfill, "get_client", return_value=fake_client),
        patch.object(backfill, "PAGE_SIZE", 2),
    ):
        result = list(backfill.iter_lpp_packets())

    assert [r["packet_id"] for r in result] == ["p1", "p2", "p3"]


def test_iter_lpp_packets_respects_limit():
    pages = [
        [{"id": 1, "packet_id": "p1"}, {"id": 2, "packet_id": "p2"}],
        [{"id": 3, "packet_id": "p3"}],
    ]
    fake_client = _FakeClient(pages)

    with (
        patch.object(backfill, "get_client", return_value=fake_client),
        patch.object(backfill, "PAGE_SIZE", 2),
    ):
        result = list(backfill.iter_lpp_packets(limit=1))

    assert [r["packet_id"] for r in result] == ["p1"]


def test_iter_lpp_packets_limit_zero_yields_nothing():
    """`--limit 0` must yield zero packets, not one -- previously the loop
    yielded a row before ever checking the limit, so limit=0 still returned
    exactly one packet."""
    pages = [
        [{"id": 1, "packet_id": "p1"}, {"id": 2, "packet_id": "p2"}],
    ]
    fake_client = _FakeClient(pages)

    with patch.object(backfill, "get_client", return_value=fake_client) as mock_get_client:
        result = list(backfill.iter_lpp_packets(limit=0))

    assert result == []
    # Zero packets requested means zero DB work -- the client is never fetched.
    mock_get_client.assert_not_called()
