"""Shared operating-status classification for `/grids` and alert delivery."""

from __future__ import annotations

from enum import Enum


class GridStatus(str, Enum):
    """Raw status categories exposed by the `/grids` command."""

    FS_ON = "fs_on"
    HPS_ON = "hps_on"
    LIKELY_ISOLATED = "likely_isolated"
    OFF = "off"
    UNKNOWN = "unknown"


class SiteStatus(str, Enum):
    """Alert-level site status that intentionally combines FS and HPS."""

    ON = "on"
    ISOLATED = "isolated"
    OFF = "off"
    UNKNOWN = "unknown"


def classify_grid_status(
    *,
    vrm_is_on: bool | None,
    vrm_data_stale: bool,
    vrm_power_kw: float | None = None,
    hps_threshold_kw: float | None = None,
    fs_on: bool | None = None,
    hps_on: bool | None = None,
) -> GridStatus:
    """Return the fleet-status category using the `/grids` precedence rules."""
    if vrm_is_on is None or vrm_data_stale:
        return GridStatus.UNKNOWN
    if vrm_is_on is False:
        return GridStatus.OFF

    effective_hps = hps_on
    if vrm_power_kw is not None and hps_threshold_kw is not None:
        effective_hps = vrm_power_kw >= float(hps_threshold_kw)

    if effective_hps is False:
        return GridStatus.LIKELY_ISOLATED
    if fs_on is True:
        return GridStatus.FS_ON
    if effective_hps is True:
        return GridStatus.HPS_ON
    return GridStatus.UNKNOWN


def normalize_site_status(status: GridStatus) -> SiteStatus:
    """Map raw `/grids` categories to the alert status vocabulary."""
    return {
        GridStatus.FS_ON: SiteStatus.ON,
        GridStatus.HPS_ON: SiteStatus.ON,
        GridStatus.LIKELY_ISOLATED: SiteStatus.ISOLATED,
        GridStatus.OFF: SiteStatus.OFF,
        GridStatus.UNKNOWN: SiteStatus.UNKNOWN,
    }[status]


def service_label(status: GridStatus) -> str:
    """Human label for a processed grid status, in the vocabulary the `/grids`
    command shows an operator: FS / HPS / Isolated / Off / Unknown.

    This is the only grid-status string that should reach a chat model. The raw
    ``is_hps_on`` / ``is_fs_active`` snapshot booleans must never be handed over
    beside it: they lag physical state, read as "off" on any data gap, and a
    model that picks one out of a status payload over the processed verdict will
    tell a customer a live grid is "not active".
    """
    return {
        GridStatus.FS_ON: "FS",
        GridStatus.HPS_ON: "HPS",
        GridStatus.LIKELY_ISOLATED: "Isolated",
        GridStatus.OFF: "Off",
        GridStatus.UNKNOWN: "Unknown",
    }[status]
