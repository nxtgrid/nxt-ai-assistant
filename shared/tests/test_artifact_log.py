"""Tests for shared/grid_design/artifact_log.py -- per-design artifact history.

Both `append_design_artifact` and `mark_artifact_stale` must never raise --
any DB failure is caught, logged, and swallowed (returns None). Mocking
follows the style of shared/tests/test_grid_design_writer.py:
`patch.object(artifact_log, "Repository", return_value=repo)` with a
MagicMock standing in for the supabase-py-backed Repository.
"""

from unittest.mock import MagicMock, patch

from shared.grid_design import artifact_log


def _repo(design_row: dict | None):
    """Build a MagicMock Repository whose .get() returns design_row and whose
    .update() echoes back {**design_row, **changes} (like a real DB write)."""
    repo = MagicMock()
    repo.get.return_value = design_row

    def fake_update(pk_value, changes):
        if design_row is None:
            return None
        merged = dict(design_row)
        merged.update(changes)
        return merged

    repo.update.side_effect = fake_update
    return repo


# ── append_design_artifact ──────────────────────────────────────────────────


def test_append_creates_list_for_new_artifact_type():
    repo = _repo({"id": "design1", "artifacts": {}})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1",
            "distribution_map",
            drive_file_id="file-abc",
            web_view_link="https://drive.google.com/file-abc",
            packet_id="lpp-1",
            label="distribution_map",
            mime_type="image/png",
        )

    assert result is not None
    entries = result["distribution_map"]
    assert len(entries) == 1
    assert entries[0]["drive_file_id"] == "file-abc"
    assert entries[0]["web_view_link"] == "https://drive.google.com/file-abc"
    assert entries[0]["packet_id"] == "lpp-1"
    assert entries[0]["label"] == "distribution_map"
    assert entries[0]["mime_type"] == "image/png"
    assert entries[0]["stale"] is False
    assert "created_at" in entries[0]

    # Assert on the actual call_args, not just "no exception".
    args, _ = repo.update.call_args
    assert args[0] == "design1"
    assert args[1]["artifacts"] == result


def test_append_second_call_prepends_new_entry_first():
    existing_entry = {
        "drive_file_id": "file-old",
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": "lpp-0",
        "label": "distribution_map",
        "mime_type": "image/png",
        "stale": False,
    }
    repo = _repo({"id": "design1", "artifacts": {"distribution_map": [existing_entry]}})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1",
            "distribution_map",
            drive_file_id="file-new",
            packet_id="lpp-2",
        )

    entries = result["distribution_map"]
    assert len(entries) == 2
    assert entries[0]["drive_file_id"] == "file-new"
    assert entries[1]["drive_file_id"] == "file-old"
    assert entries[1] == existing_entry  # untouched


def test_append_truncates_to_max_versions_dropping_oldest():
    # Seed 10 existing entries (file-0 oldest .. file-9 newest-of-the-existing).
    existing = [
        {
            "drive_file_id": f"file-{i}",
            "web_view_link": None,
            "created_at": f"2026-01-0{i + 1}T00:00:00+00:00",
            "packet_id": None,
            "label": None,
            "mime_type": None,
            "stale": False,
        }
        for i in range(9, -1, -1)  # newest-first: file-9, file-8, ..., file-0
    ]
    repo = _repo({"id": "design1", "artifacts": {"distribution_map": existing}})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1",
            "distribution_map",
            drive_file_id="file-new",
            max_versions=10,
        )

    entries = result["distribution_map"]
    assert len(entries) == 10
    assert entries[0]["drive_file_id"] == "file-new"
    ids = [e["drive_file_id"] for e in entries]
    # The oldest entry (file-0) must have been dropped, not the newest.
    assert "file-0" not in ids
    assert "file-1" in ids
    assert "file-9" in ids


def test_append_max_versions_zero_caps_to_new_entry_only():
    # max_versions <= 0 must still cap the list, not keep it nearly-full. A naive
    # `existing[: max_versions - 1]` slice with max_versions=0 becomes `existing[:-1]`,
    # which keeps all-but-the-last entry -- the opposite of the intended cap.
    existing = [
        {
            "drive_file_id": f"file-{i}",
            "web_view_link": None,
            "created_at": f"2026-01-0{i + 1}T00:00:00+00:00",
            "packet_id": None,
            "label": None,
            "mime_type": None,
            "stale": False,
        }
        for i in range(4, -1, -1)  # newest-first: file-4 .. file-0
    ]
    repo = _repo({"id": "design1", "artifacts": {"distribution_map": existing}})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1",
            "distribution_map",
            drive_file_id="file-new",
            max_versions=0,
        )

    entries = result["distribution_map"]
    # Only the newly-added entry should remain -- no old entries carried over.
    assert len(entries) == 1
    assert entries[0]["drive_file_id"] == "file-new"


def test_append_max_versions_negative_caps_to_new_entry_only():
    existing = [
        {
            "drive_file_id": "file-old",
            "web_view_link": None,
            "created_at": "2026-01-01T00:00:00+00:00",
            "packet_id": None,
            "label": None,
            "mime_type": None,
            "stale": False,
        }
    ]
    repo = _repo({"id": "design1", "artifacts": {"distribution_map": existing}})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1",
            "distribution_map",
            drive_file_id="file-new",
            max_versions=-5,
        )

    entries = result["distribution_map"]
    assert len(entries) == 1
    assert entries[0]["drive_file_id"] == "file-new"


def test_append_handles_artifacts_none():
    repo = _repo({"id": "design1", "artifacts": None})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1", "distribution_map", drive_file_id="file-abc"
        )

    assert result is not None
    assert len(result["distribution_map"]) == 1
    assert result["distribution_map"][0]["drive_file_id"] == "file-abc"


def test_append_handles_artifacts_key_absent():
    repo = _repo({"id": "design1"})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1", "distribution_map", drive_file_id="file-abc"
        )

    assert result is not None
    assert len(result["distribution_map"]) == 1


def test_append_returns_none_when_design_not_found():
    repo = _repo(None)
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "missing-design", "distribution_map", drive_file_id="file-abc"
        )

    assert result is None
    repo.update.assert_not_called()


def test_append_swallows_update_exception_and_returns_none():
    repo = _repo({"id": "design1", "artifacts": {}})
    repo.update.side_effect = Exception("boom: connection reset")
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1", "distribution_map", drive_file_id="file-abc"
        )

    assert result is None


def test_append_swallows_get_exception_and_returns_none():
    repo = MagicMock()
    repo.get.side_effect = Exception("boom: db unreachable")
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.append_design_artifact(
            "design1", "distribution_map", drive_file_id="file-abc"
        )

    assert result is None


# ── mark_artifact_stale ──────────────────────────────────────────────────────


def test_mark_stale_sets_flag_on_matching_entry_only():
    entry_a = {
        "drive_file_id": "file-a",
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": None,
        "label": None,
        "mime_type": None,
        "stale": False,
    }
    entry_b = {
        "drive_file_id": "file-b",
        "web_view_link": None,
        "created_at": "2026-01-02T00:00:00+00:00",
        "packet_id": None,
        "label": None,
        "mime_type": None,
        "stale": False,
    }
    other_type_entry = {
        "drive_file_id": "file-a",  # same id, different artifact_type -- must stay untouched
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": None,
        "label": None,
        "mime_type": None,
        "stale": False,
    }
    artifacts = {
        "distribution_map": [entry_b, entry_a],
        "site_layout_png": [other_type_entry],
    }
    repo = _repo({"id": "design1", "artifacts": artifacts})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-a")

    assert result is not None
    dm = {e["drive_file_id"]: e for e in result["distribution_map"]}
    assert dm["file-a"]["stale"] is True
    assert dm["file-b"]["stale"] is False
    # Different artifact_type untouched even though drive_file_id matches.
    assert result["site_layout_png"][0]["stale"] is False

    args, _ = repo.update.call_args
    assert args[0] == "design1"
    assert args[1]["artifacts"] == result


def test_mark_stale_no_matching_entry_is_noop():
    entry = {
        "drive_file_id": "file-a",
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": None,
        "label": None,
        "mime_type": None,
        "stale": False,
    }
    artifacts = {"distribution_map": [entry]}
    repo = _repo({"id": "design1", "artifacts": artifacts})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale(
            "design1", "distribution_map", "file-does-not-exist"
        )

    # Graceful no-op: unchanged artifacts returned, no write performed.
    assert result == artifacts
    repo.update.assert_not_called()


def test_mark_stale_handles_artifacts_none():
    repo = _repo({"id": "design1", "artifacts": None})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-a")

    # No artifacts at all -> no matching entry -> graceful no-op.
    assert result == {}
    repo.update.assert_not_called()


def test_mark_stale_handles_artifacts_key_absent():
    repo = _repo({"id": "design1"})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-a")

    assert result == {}
    repo.update.assert_not_called()


def test_mark_stale_does_not_mutate_original_entry_dict():
    # Regression: mark_artifact_stale must build fresh dicts on write, not mutate the
    # nested dict objects returned by Repository.get() in place.
    entry = {
        "drive_file_id": "file-a",
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": None,
        "label": None,
        "mime_type": None,
        "stale": False,
    }
    artifacts = {"distribution_map": [entry]}
    repo = _repo({"id": "design1", "artifacts": artifacts})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-a")

    assert result["distribution_map"][0]["stale"] is True
    # The original entry object (as returned by repo.get()) must be untouched.
    assert entry["stale"] is False
    assert result["distribution_map"][0] is not entry


def test_mark_stale_two_entries_same_drive_file_id_marks_only_first_match():
    # drive_file_id is expected to be unique per version within an artifact_type, but if
    # it's ever duplicated, only the first match (list order, newest-first) gets marked --
    # no break-less "mark everything that matches" behavior.
    entry_newest = {
        "drive_file_id": "file-dup",
        "web_view_link": None,
        "created_at": "2026-01-02T00:00:00+00:00",
        "packet_id": None,
        "label": "newest",
        "mime_type": None,
        "stale": False,
    }
    entry_oldest = {
        "drive_file_id": "file-dup",
        "web_view_link": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "packet_id": None,
        "label": "oldest",
        "mime_type": None,
        "stale": False,
    }
    artifacts = {"distribution_map": [entry_newest, entry_oldest]}
    repo = _repo({"id": "design1", "artifacts": artifacts})
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-dup")

    entries = result["distribution_map"]
    assert entries[0]["label"] == "newest"
    assert entries[0]["stale"] is True
    assert entries[1]["label"] == "oldest"
    assert entries[1]["stale"] is False


def test_mark_stale_returns_none_when_design_not_found():
    repo = _repo(None)
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("missing-design", "distribution_map", "file-a")

    assert result is None
    repo.update.assert_not_called()


def test_mark_stale_swallows_exception_and_returns_none():
    repo = _repo(
        {"id": "design1", "artifacts": {"distribution_map": [{"drive_file_id": "file-a"}]}}
    )
    repo.update.side_effect = Exception("boom")
    with patch.object(artifact_log, "Repository", return_value=repo):
        result = artifact_log.mark_artifact_stale("design1", "distribution_map", "file-a")

    assert result is None


# ── sweep_state_for_artifacts ────────────────────────────────────────────────


def test_sweep_appends_only_matching_drive_id_keys():
    state = {
        "map_image_drive_id": "file-map",
        "site_layout_png_drive_id": "file-layout",
        "site_name": "Commville",  # unrelated key, must be ignored
    }
    with patch.object(artifact_log, "append_design_artifact") as mock_append:
        artifact_log.sweep_state_for_artifacts("design1", state, packet_id="lpp-1")

    assert mock_append.call_count == 2
    calls = {c.args[1]: c for c in mock_append.call_args_list}
    assert set(calls.keys()) == {"map_image", "site_layout_png"}
    assert calls["map_image"].args[0] == "design1"
    assert calls["map_image"].kwargs["drive_file_id"] == "file-map"
    assert calls["map_image"].kwargs["packet_id"] == "lpp-1"
    assert calls["site_layout_png"].kwargs["drive_file_id"] == "file-layout"


def test_sweep_skips_falsy_drive_id_values():
    state = {
        "map_image_drive_id": "",
        "other_drive_id": None,
        "real_drive_id": "file-real",
    }
    with patch.object(artifact_log, "append_design_artifact") as mock_append:
        artifact_log.sweep_state_for_artifacts("design1", state)

    mock_append.assert_called_once()
    assert mock_append.call_args.args[1] == "real"
    assert mock_append.call_args.kwargs["drive_file_id"] == "file-real"


def test_sweep_continues_after_one_key_raises():
    state = {
        "bad_drive_id": "file-bad",
        "good_drive_id": "file-good",
    }

    def fake_append(design_id, artifact_type, **kwargs):
        if artifact_type == "bad":
            raise Exception("boom")
        return {}

    with patch.object(
        artifact_log, "append_design_artifact", side_effect=fake_append
    ) as mock_append:
        # Must not raise despite "bad" key's call raising internally.
        artifact_log.sweep_state_for_artifacts("design1", state)

    assert mock_append.call_count == 2
    called_types = {c.args[1] for c in mock_append.call_args_list}
    assert called_types == {"bad", "good"}
