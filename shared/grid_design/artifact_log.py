"""Durable, per-design, versioned history of generated LPP artifacts.

The LPP workflow uploads generated files (distribution maps, site layout
renders, QGIS projects, etc.) to Google Drive during handler execution, but
the resulting Drive file IDs otherwise only ever land in ephemeral
``packet_state`` jsonb. This module gives that data a durable home on
``gd_designs.artifacts`` -- a jsonb object keyed by artifact type, each value
a newest-first list of version entries.

Both functions here are call-and-forget: they must never raise, since a
failure to log/update artifact history must never fail the workflow step
that produced the artifact in the first place. Every exception is caught,
logged as a warning, and swallowed -- callers get ``None`` back and move on.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from shared.grid_design.db import Repository
from shared.utils.logging import get_logger

logger = get_logger(__name__)

_DRIVE_ID_SUFFIX = "_drive_id"


def sweep_state_for_artifacts(design_id: str, state: dict, *, packet_id: str | None = None) -> None:
    """Best-effort: record every ``*_drive_id`` key in ``state`` as a design artifact.

    Earlier LPP steps (map generation, community resolution, site layout, ...)
    stash Drive file IDs into ``packet_state`` under keys like
    ``map_image_drive_id`` or ``site_layout_png_drive_id`` -- long before a
    design row (and thus a ``design_id``) exists. This sweep is how those
    pre-existing uploads retroactively get attached to a design's artifact
    history once the design is created, and how every subsequent step's state
    updates keep getting attached as they happen.

    For each key in ``state`` ending in ``_drive_id`` with a truthy value,
    derives the artifact_type by stripping the suffix (e.g.
    "map_image_drive_id" -> "map_image") and calls ``append_design_artifact``.
    Falsy values (None, "", 0) are skipped -- there's no Drive file to log.

    Call-and-forget: relies entirely on ``append_design_artifact``'s own
    non-fatal contract (it never raises), but each per-key call is also
    wrapped in its own try/except so one unexpected failure can't stop the
    sweep from processing the remaining keys. Never raises itself.
    """
    for key, value in state.items():
        if not key.endswith(_DRIVE_ID_SUFFIX) or not value:
            continue
        artifact_type = key[: -len(_DRIVE_ID_SUFFIX)]
        try:
            append_design_artifact(
                design_id,
                artifact_type,
                drive_file_id=value,
                packet_id=packet_id,
                label=artifact_type,
            )
        except Exception:
            logger.warning(
                "sweep_state_for_artifacts: failed to log artifact for design_id=%s "
                "key=%s; continuing sweep",
                design_id,
                key,
                exc_info=True,
            )


def append_design_artifact(
    design_id: str,
    artifact_type: str,
    *,
    drive_file_id: str,
    web_view_link: str | None = None,
    packet_id: str | None = None,
    label: str | None = None,
    mime_type: str | None = None,
    max_versions: int = 10,
) -> dict | None:
    """Prepend a new artifact-version entry for a design; non-fatal on any failure.

    Reads the design's current ``artifacts`` dict (defaulting to ``{}`` if
    missing/None), prepends the new entry to ``artifacts[artifact_type]``
    (creating the list if absent), truncates to the most recent
    ``max_versions`` entries (newest first, so truncation drops the oldest),
    and writes the whole dict back via ``Repository("designs").update``.

    Returns the updated ``artifacts`` dict on success. Returns ``None`` if the
    design isn't found, or if any step raises (DB error, etc.) -- in both
    cases a warning is logged but nothing is raised.
    """
    try:
        repo = Repository("designs")
        # Known best-effort limitation: this is a plain read-modify-write with no
        # optimistic-concurrency guard, so two near-simultaneous calls for the same
        # design_id can race and the second write can silently clobber the first's
        # changes. Accepted for this phase -- artifact history is non-fatal, best-effort
        # history, not the system of record. Full concurrency-safe merge belongs with the
        # broader packet_state concurrency work already deferred to a later phase.
        design = repo.get(design_id)
        if design is None:
            logger.warning(
                "append_design_artifact: design %s not found; skipping artifact log write "
                "(artifact_type=%s)",
                design_id,
                artifact_type,
            )
            return None

        artifacts: dict[str, Any] = dict(design.get("artifacts") or {})
        entry = {
            "drive_file_id": drive_file_id,
            "web_view_link": web_view_link,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "packet_id": packet_id,
            "label": label,
            "mime_type": mime_type,
            "stale": False,
        }
        existing = list(artifacts.get(artifact_type, []))
        # max_versions <= 0 must still cap the list (not keep it nearly-full): a negative
        # slice bound like existing[:-1] keeps almost everything, the opposite of intent.
        artifacts[artifact_type] = [entry] + existing[: max(max_versions - 1, 0)]

        updated = repo.update(design_id, {"artifacts": artifacts})
        if updated is None:
            logger.warning(
                "append_design_artifact: update returned no row for design %s "
                "(artifact_type=%s); design may have been deleted concurrently",
                design_id,
                artifact_type,
            )
            return None
        return artifacts
    except Exception:
        logger.warning(
            "append_design_artifact failed for design_id=%s artifact_type=%s",
            design_id,
            artifact_type,
            exc_info=True,
        )
        return None


def mark_artifact_stale(design_id: str, artifact_type: str, drive_file_id: str) -> dict | None:
    """Mark a specific artifact-version entry (matched by drive_file_id) as stale=True.

    Non-fatal on any failure (same contract as ``append_design_artifact``).
    If no entry in ``artifacts[artifact_type]`` matches ``drive_file_id``,
    this is a no-op: it returns the unchanged ``artifacts`` dict without
    writing anything back (nothing to persist, so no update call is made).

    ``drive_file_id`` is expected to be unique per version within an
    ``artifact_type``, so only the first matching entry is marked stale --
    matching stops there rather than scanning the rest of the list.

    Returns the updated ``artifacts`` dict on success/no-op, ``None`` on
    failure (design not found, or any exception).
    """
    try:
        repo = Repository("designs")
        # Known best-effort limitation: this is a plain read-modify-write with no
        # optimistic-concurrency guard, so two near-simultaneous calls for the same
        # design_id can race and the second write can silently clobber the first's
        # changes. Accepted for this phase -- artifact history is non-fatal, best-effort
        # history, not the system of record. Full concurrency-safe merge belongs with the
        # broader packet_state concurrency work already deferred to a later phase.
        design = repo.get(design_id)
        if design is None:
            logger.warning(
                "mark_artifact_stale: design %s not found; skipping (artifact_type=%s, "
                "drive_file_id=%s)",
                design_id,
                artifact_type,
                drive_file_id,
            )
            return None

        artifacts: dict[str, Any] = dict(design.get("artifacts") or {})
        entries = list(artifacts.get(artifact_type, []))

        matched = False
        new_entries = []
        for entry in entries:
            if not matched and entry.get("drive_file_id") == drive_file_id:
                new_entries.append({**entry, "stale": True})
                matched = True
            else:
                new_entries.append(entry)
        entries = new_entries

        if not matched:
            logger.warning(
                "mark_artifact_stale: no matching entry for design_id=%s artifact_type=%s "
                "drive_file_id=%s; no-op",
                design_id,
                artifact_type,
                drive_file_id,
            )
            return artifacts

        artifacts[artifact_type] = entries
        updated = repo.update(design_id, {"artifacts": artifacts})
        if updated is None:
            logger.warning(
                "mark_artifact_stale: update returned no row for design %s "
                "(artifact_type=%s, drive_file_id=%s)",
                design_id,
                artifact_type,
                drive_file_id,
            )
            return None
        return artifacts
    except Exception:
        logger.warning(
            "mark_artifact_stale failed for design_id=%s artifact_type=%s drive_file_id=%s",
            design_id,
            artifact_type,
            drive_file_id,
            exc_info=True,
        )
        return None
