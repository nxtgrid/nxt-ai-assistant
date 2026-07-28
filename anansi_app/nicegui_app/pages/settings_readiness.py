"""Deployment Readiness panel.

Answers "what is this deployment still missing" in terms of capabilities rather
than a list of unset variables, and says for each one whether the operator can
fix it on this page or must set it in the host environment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Mapping, Optional

from nicegui import ui

from shared.config import flag_registry as registry

_SEVERITY_RANK = {"required": 0, "recommended": 1}
_SEVERITY_COLOR = {"required": "#ef4444", "recommended": "#f59e0b"}


@dataclass(frozen=True)
class PanelRow:
    title: str
    description: str
    missing: List[str]
    severity: str
    satisfied: bool
    settable_here: bool


def _settable_here(missing: List[str]) -> bool:
    """True when every missing name is an editable flag in the registry.

    Host-owned credentials are registered ``editable=False``, so this is exactly
    the question "can the operator finish this without leaving the app".
    """
    if not missing:
        return False
    for entry in missing:
        # A requirement may be "A or B"; it is settable if any alternative is.
        alternatives = [part.strip() for part in entry.split(" or ")]
        flags = [registry.FLAGS.get(name) for name in alternatives]
        if not any(flag is not None and flag.editable for flag in flags):
            return False
    return True


def build_rows(env: Optional[Mapping[str, str]] = None) -> List[PanelRow]:
    """Readiness rows, unsatisfied first, required before recommended."""
    rows = [
        PanelRow(
            title=status.capability.title,
            description=status.capability.description,
            missing=list(status.missing),
            severity=status.capability.severity,
            satisfied=status.satisfied,
            settable_here=_settable_here(list(status.missing)),
        )
        for status in registry.readiness(env=env)
    ]
    rows.sort(key=lambda row: (row.satisfied, _SEVERITY_RANK.get(row.severity, 9)))
    return rows


def render_panel(rows: List[PanelRow]) -> None:
    """Render the readiness card. Fully-ready deployments collapse to one line."""
    outstanding = [row for row in rows if not row.satisfied]
    with ui.card().classes("w-full q-mb-md"):
        if not outstanding:
            ui.label("✅ Deployment ready — every capability is configured.").classes(
                "text-subtitle1 text-weight-bold"
            )
            return

        ui.label("Deployment readiness").classes("text-subtitle1 text-weight-bold")
        ui.label(
            f"{len(outstanding)} of {len(rows)} capabilities are not configured yet."
        ).classes("text-caption").style("color: #64748b")

        for row in outstanding:
            with ui.row().classes("items-start gap-2 w-full no-wrap q-mt-sm"):
                ui.element("div").style(
                    "width: 8px; height: 8px; border-radius: 9999px; margin-top: 6px;"
                    f" background-color: {_SEVERITY_COLOR.get(row.severity, '#64748b')};"
                    " flex: 0 0 auto;"
                )
                with ui.column().classes("gap-0"):
                    ui.label(row.title).classes("text-weight-medium")
                    ui.label(row.description).classes("text-caption").style("color: #64748b")
                    ui.label("Missing: " + ", ".join(row.missing)).classes("text-caption")
                    ui.label(
                        "Set below on this page."
                        if row.settable_here
                        else "Set in the deployment environment, then reload."
                    ).classes("text-caption").style("color: #64748b")

        with ui.expansion(f"Configured ({len(rows) - len(outstanding)})").classes("w-full"):
            for row in rows:
                if row.satisfied:
                    ui.label(f"✅ {row.title}").classes("text-caption")
