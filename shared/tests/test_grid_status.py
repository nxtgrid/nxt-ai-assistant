import pytest

from shared.grid_status import GridStatus, SiteStatus, classify_grid_status, normalize_site_status


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        (GridStatus.FS_ON, SiteStatus.ON),
        (GridStatus.HPS_ON, SiteStatus.ON),
        (GridStatus.LIKELY_ISOLATED, SiteStatus.ISOLATED),
        (GridStatus.OFF, SiteStatus.OFF),
        (GridStatus.UNKNOWN, SiteStatus.UNKNOWN),
    ],
)
def test_normalizes_grids_status_for_alerts(raw: GridStatus, normalized: SiteStatus) -> None:
    assert normalize_site_status(raw) is normalized


@pytest.mark.parametrize(
    ("vrm_is_on", "vrm_data_stale"),
    [(True, True), (None, False)],
)
def test_stale_or_missing_vrm_is_unknown(vrm_is_on: bool | None, vrm_data_stale: bool) -> None:
    assert (
        classify_grid_status(vrm_is_on=vrm_is_on, vrm_data_stale=vrm_data_stale)
        is GridStatus.UNKNOWN
    )


def test_fresh_vrm_off_is_off() -> None:
    assert classify_grid_status(vrm_is_on=False, vrm_data_stale=False) is GridStatus.OFF


def test_below_hps_threshold_is_isolated_even_when_fs_reports_on() -> None:
    status = classify_grid_status(
        vrm_is_on=True,
        vrm_data_stale=False,
        vrm_power_kw=1.0,
        hps_threshold_kw=2.0,
        fs_on=True,
        hps_on=True,
    )
    assert status is GridStatus.LIKELY_ISOLATED


def test_fs_precedes_hps_when_both_are_on() -> None:
    status = classify_grid_status(
        vrm_is_on=True,
        vrm_data_stale=False,
        fs_on=True,
        hps_on=True,
    )
    assert status is GridStatus.FS_ON


def test_hps_state_is_fallback_when_power_or_threshold_is_missing() -> None:
    status = classify_grid_status(
        vrm_is_on=True,
        vrm_data_stale=False,
        vrm_power_kw=None,
        hps_threshold_kw=2.0,
        fs_on=False,
        hps_on=True,
    )
    assert status is GridStatus.HPS_ON


def test_on_with_no_mode_evidence_is_unknown() -> None:
    status = classify_grid_status(
        vrm_is_on=True,
        vrm_data_stale=False,
        fs_on=None,
        hps_on=None,
    )
    assert status is GridStatus.UNKNOWN
