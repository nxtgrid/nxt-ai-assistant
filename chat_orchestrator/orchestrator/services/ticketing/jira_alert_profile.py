"""Jira alert-ticket profile -- n8n field parity for /notify-filed tickets.

The n8n "VRM and Grafana Alerts Automation" workflow files alert tickets into
a specific Jira project with a specific field shape (Grid select field with
auto-added options, Alarm Type, a cascading Category field, a date field, and
a no-grid priority fallback). This module reproduces that field shape
exactly so tickets filed by Anansi's ``/notify`` endpoint are indistinguishable
from the ones n8n used to file directly -- the parity gate the rollout plan
requires before n8n can be cut over (see
docs/superpowers/plans/2026-07-27-smart-alert-correlation-notify.md).

Deliberately separate from the OPS/Task shape ``JiraTicketBackend`` already
uses for customer escalations (``_create_jira_ticket``) -- alert tickets are
a different project with a different field set, and escalation tickets must
keep behaving exactly as they do today regardless of whether this profile is
configured.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from shared.config import flag_registry as fr
from shared.utils.grid_matcher import find_best_grid_match
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# In-process TTL cache for a grid field's option list, keyed by
# (base_url, grid_field_id, grid_context_id) -- avoids refetching all options
# (maxResults=10000) on every alert. Short TTL: new grid options are added
# rarely, but should show up within a few minutes without a restart.
_OPTIONS_CACHE: Dict[tuple, tuple[List[Dict[str, Any]], float]] = {}
_GRID_OPTIONS_TTL: float = 300.0


@dataclass(frozen=True)
class AlertTicketProfile:
    """Field/option ids for the alert-ticket Jira project, loaded from the
    ``JIRA_ALERT_*`` flags. All-blank by default -- ``is_configured()`` gates
    whether ``JiraTicketBackend`` uses this profile at all."""

    project_id: str = ""
    project_key: str = ""
    issue_type_id: str = ""
    reporter_account_id: str = ""
    alarm_label: str = ""
    grid_field_id: str = ""
    grid_context_id: str = ""
    grid_ignored_option_id: str = ""
    alarm_type_field_id: str = ""
    alarm_type_option_id: str = ""
    category_field_id: str = ""
    category_parent_option_id: str = ""
    category_child_option_id: str = ""
    date_field_id: str = ""
    no_grid_priority_id: str = ""
    escalated_priority_id: str = ""

    # Fields that must be non-empty for the profile to be usable.
    # grid_ignored_option_id and escalated_priority_id are optional
    # refinements -- blank is a legitimate "not set" for both.
    _REQUIRED_FIELDS = (
        "project_id",
        "project_key",
        "issue_type_id",
        "reporter_account_id",
        "alarm_label",
        "grid_field_id",
        "grid_context_id",
        "alarm_type_field_id",
        "alarm_type_option_id",
        "category_field_id",
        "category_parent_option_id",
        "category_child_option_id",
        "date_field_id",
        "no_grid_priority_id",
    )

    def is_configured(self) -> bool:
        return all(bool(getattr(self, name)) for name in self._REQUIRED_FIELDS)


def load_alert_ticket_profile() -> AlertTicketProfile:
    """Build an ``AlertTicketProfile`` from the ``JIRA_ALERT_*`` flags.

    Explicit field-by-field mapping rather than a name transform -- a couple
    of flag names (``JIRA_ALERT_LABEL``, ``JIRA_ALERT_TYPE_FIELD_ID``/
    ``_TYPE_OPTION_ID``) don't follow the ``JIRA_ALERT_<FIELD_NAME>``
    pattern the other flags do, so a generic transform would silently
    KeyError on those.
    """
    return AlertTicketProfile(
        project_id=fr.get("JIRA_ALERT_PROJECT_ID"),
        project_key=fr.get("JIRA_ALERT_PROJECT_KEY"),
        issue_type_id=fr.get("JIRA_ALERT_ISSUE_TYPE_ID"),
        reporter_account_id=fr.get("JIRA_ALERT_REPORTER_ACCOUNT_ID"),
        alarm_label=fr.get("JIRA_ALERT_LABEL"),
        grid_field_id=fr.get("JIRA_ALERT_GRID_FIELD_ID"),
        grid_context_id=fr.get("JIRA_ALERT_GRID_CONTEXT_ID"),
        grid_ignored_option_id=fr.get("JIRA_ALERT_GRID_IGNORED_OPTION_ID"),
        alarm_type_field_id=fr.get("JIRA_ALERT_TYPE_FIELD_ID"),
        alarm_type_option_id=fr.get("JIRA_ALERT_TYPE_OPTION_ID"),
        category_field_id=fr.get("JIRA_ALERT_CATEGORY_FIELD_ID"),
        category_parent_option_id=fr.get("JIRA_ALERT_CATEGORY_PARENT_OPTION_ID"),
        category_child_option_id=fr.get("JIRA_ALERT_CATEGORY_CHILD_OPTION_ID"),
        date_field_id=fr.get("JIRA_ALERT_DATE_FIELD_ID"),
        no_grid_priority_id=fr.get("JIRA_ALERT_NO_GRID_PRIORITY_ID"),
        escalated_priority_id=fr.get("JIRA_ALERT_ESCALATED_PRIORITY_ID"),
    )


def _slugify_grid(grid_name: str) -> str:
    """Same slug convention as ``jira_backend._slugify_grid`` -- duplicated
    rather than imported to keep this module import-independent of
    ``jira_backend`` (which imports *this* module to build alert tickets)."""
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", grid_name.strip().lower()).strip("-")
    return slug or "unknown"


def _doc(text: str) -> Dict[str, Any]:
    """Wrap plain text in a single-paragraph ADF doc."""
    return {
        "type": "doc",
        "version": 1,
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}],
    }


def build_alert_issue_fields(
    profile: AlertTicketProfile,
    summary: str,
    description: str,
    grid_option_id: Optional[str],
    grid_name: Optional[str],
    extra_labels: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Build the Jira ``{"fields": {...}}`` body for a new alert ticket.

    Mirrors n8n's ``issueBody()`` (Prepare Jira Context / Prepare Added Jira
    Grid Option) exactly, with one deliberate normalization: n8n's two code
    paths disagreed on the Category field (one hardcoded
    ``customfield_10086``/``10082``/``10085``, the other used the configured
    ids) -- this always uses the configured ids, which is what the plan's
    "Category cascade" flags describe and is the only sane single source of
    truth to reproduce.

    ``grid-<slug>`` is always added to labels when ``grid_name`` is known --
    even if ``grid_option_id`` couldn't be resolved -- so
    ``find_open_by_grid``'s label-based fallback works regardless of whether
    the custom grid *field* resolution succeeded.
    """
    labels: List[str] = []
    for label in [profile.alarm_label, *(extra_labels or [])]:
        if label and label not in labels:
            labels.append(label)
    if grid_name:
        grid_label = f"grid-{_slugify_grid(grid_name)}"
        if grid_label not in labels:
            labels.append(grid_label)

    fields_out: Dict[str, Any] = {
        "project": {"id": profile.project_id},
        "summary": summary,
        "issuetype": {"id": profile.issue_type_id},
        "reporter": {"id": profile.reporter_account_id},
        "labels": labels,
        "description": _doc(description),
        profile.alarm_type_field_id: {"id": profile.alarm_type_option_id},
        profile.category_field_id: {
            "id": profile.category_parent_option_id,
            "child": {"id": profile.category_child_option_id},
        },
        profile.date_field_id: datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }

    if grid_option_id:
        fields_out[profile.grid_field_id] = {"id": str(grid_option_id)}
    else:
        fields_out["priority"] = {"id": profile.no_grid_priority_id}

    return {"fields": fields_out}


def _values_from(response_json: Any) -> List[Dict[str, Any]]:
    if not isinstance(response_json, dict):
        return []
    return response_json.get("values") or response_json.get("options") or []


async def resolve_or_create_grid_option(
    *,
    base_url: str,
    headers: Dict[str, str],
    profile: AlertTicketProfile,
    grid_name: str,
    get_session: Callable[[], Any],
) -> Optional[str]:
    """Resolve ``grid_name`` to a Grid-field option id, auto-creating it if missing.

    Mirrors n8n's ``Get Jira Grid Options`` -> ``Filter Jira Missing Grid
    Option`` -> ``Add Jira Grid Option`` chain: exact match (excluding
    ``grid_ignored_option_id``) first, then fuzzy match, then create.

    Never raises -- returns ``None`` on any HTTP/transport failure, which
    the caller (``JiraTicketBackend.create_ticket``) treats the same as "no
    grid resolved" (files without the grid field, using the no-grid
    priority).
    """
    if not grid_name or not grid_name.strip():
        return None

    cache_key = (base_url, profile.grid_field_id, profile.grid_context_id)
    now = time.monotonic()
    cached = _OPTIONS_CACHE.get(cache_key)
    if cached is not None and (now - cached[1]) < _GRID_OPTIONS_TTL:
        options = cached[0]
    else:
        options_url = (
            f"{base_url}/rest/api/3/field/{profile.grid_field_id}"
            f"/context/{profile.grid_context_id}/option"
        )
        try:
            session = get_session()
            async with session.get(
                options_url, headers=headers, params={"maxResults": "10000"}
            ) as resp:
                if resp.status != 200:
                    LOGGER.warning(
                        "Failed to fetch Jira grid field options: HTTP %s", resp.status
                    )
                    return None
                data = await resp.json()
        except Exception:
            LOGGER.warning("Error fetching Jira grid field options", exc_info=True)
            return None
        options = _values_from(data)
        _OPTIONS_CACHE[cache_key] = (options, now)

    candidates = [
        opt for opt in options if str(opt.get("id", "")) != profile.grid_ignored_option_id
    ]

    grid_lower = grid_name.strip().lower()
    for opt in candidates:
        if str(opt.get("value", "")).strip().lower() == grid_lower:
            return str(opt["id"])

    option_names = [str(opt.get("value", "")) for opt in candidates if opt.get("value")]
    matched, _was_fuzzy, _score = find_best_grid_match(grid_name, option_names)
    if matched:
        for opt in candidates:
            if str(opt.get("value", "")) == matched:
                return str(opt["id"])

    # No match -- create the option, mirroring n8n's "Add Jira Grid Option".
    create_url = (
        f"{base_url}/rest/api/3/field/{profile.grid_field_id}"
        f"/context/{profile.grid_context_id}/option"
    )
    try:
        session = get_session()
        async with session.post(
            create_url,
            json={"options": [{"value": grid_name, "disabled": False}]},
            headers=headers,
        ) as resp:
            if resp.status not in (200, 201):
                LOGGER.warning("Failed to create Jira grid option for %r: HTTP %s", grid_name, resp.status)
                return None
            data = await resp.json()
    except Exception:
        LOGGER.warning("Error creating Jira grid option for %r", grid_name, exc_info=True)
        return None

    created = _values_from(data)
    if not created or not created[0].get("id"):
        return None

    new_option = created[0]
    # Invalidate the cache entry so the next lookup sees the new option too.
    if cache_key in _OPTIONS_CACHE:
        stale_options, ts = _OPTIONS_CACHE[cache_key]
        _OPTIONS_CACHE[cache_key] = ([*stale_options, new_option], ts)
    return str(new_option["id"])
