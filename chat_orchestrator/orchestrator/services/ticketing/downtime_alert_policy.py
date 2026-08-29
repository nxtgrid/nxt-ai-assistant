"""Pure downtime-delivery floor for /notify alerts.

The correlation ladder's job is to silence noise: a re-fire of an alert
already recorded on an open ticket adds nothing an operator can act on, so
``_duplicate_delivery``/``_amend_delivery`` keep Telegram quiet. That is the
right default for equipment chatter and the wrong one for downtime -- an
open, never-closed ticket on an unrelated component (a stale MPPT ticket, a
battery-equalization warning from last week) will otherwise absorb a grid's
alerts indefinitely while the grid itself is dark.

This module is the floor under that: whenever live telemetry says the grid is
down -- the inverter is in fault and/or the phases read 0 V -- an alert goes
out even if correlation wanted silence, and it goes out again every day the
grid stays down. One per day is both the cap and the guarantee: a stale
ticket may hold a downtime alert for at most ``DOWNTIME_ALERT_INTERVAL``.

Pure functions only -- no I/O, no flags, no LLM calls. The caller supplies
the live telemetry and the timestamp of the last downtime alert actually
delivered for the grid (``notify_alert_deliveries``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from shared.grid_status import SiteStatus

from .alert_judgment_context import AlertTelemetry

DOWNTIME_ALERT_INTERVAL = timedelta(hours=24)


@dataclass(frozen=True)
class DowntimeState:
    """What live telemetry says about the grid being down right now.

    ``known`` is the honesty flag: stale or absent telemetry cannot assert
    "down" *or* "up", and this override only ever adds a send, so an unknown
    state leaves the correlation decision exactly as it found it.
    """

    down: bool
    known: bool
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class DowntimeOverride:
    send: bool
    reason: str


def _all_phases_zero(telemetry: AlertTelemetry) -> bool:
    phases = (telemetry.l1_voltage_v, telemetry.l2_voltage_v, telemetry.l3_voltage_v)
    return all(value is not None for value in phases) and all(value == 0.0 for value in phases)


def assess_downtime(telemetry: AlertTelemetry) -> DowntimeState:
    """Classify one live telemetry reading as down / up / unknowable.

    Downtime is "inverter is in fault and/or voltage output is 0", so either
    signal alone is sufficient -- a site still reporting ``on`` while all
    three phases read 0 V is down whatever the status field says.

    ``output_kw`` is deliberately *not* a downtime signal: a solar site at
    night legitimately produces 0 kW with the grid up on battery.

    A plant whose gateway has stopped reporting is the third signal, and the
    only one that arrives on a *stale* reading. This reverses an earlier call
    here ("stale telemetry stays silent: unknowable is not down") -- correctly,
    because at the time every unreadable telemetry looked alike. Now that
    ``unavailable_reason`` distinguishes them, "stale" specifically means VRM
    served us a reading whose gateway timestamp is over 30 minutes old: not an
    absence of evidence but evidence of absence. The plant is dark, and device
    alerts derived from the same dark feed ("MPPT X performs lower than other
    MPPTs" for every MPPT that stopped reporting) are artefacts of it. Saying
    so once a day beats force-sending each artefact.
    """
    if telemetry.unavailable_reason == "stale":
        return DowntimeState(down=True, known=True, reasons=("plant_comms_down",))

    if not telemetry.fresh:
        return DowntimeState(down=False, known=False)

    status_known = telemetry.site_status != SiteStatus.UNKNOWN
    phases_known = all(
        value is not None
        for value in (telemetry.l1_voltage_v, telemetry.l2_voltage_v, telemetry.l3_voltage_v)
    )
    if not (status_known or phases_known):
        return DowntimeState(down=False, known=False)

    reasons: list[str] = []
    if telemetry.site_status == SiteStatus.OFF:
        reasons.append("inverter_fault")
    if _all_phases_zero(telemetry):
        reasons.append("zero_output_voltage")

    return DowntimeState(down=bool(reasons), known=True, reasons=tuple(reasons))


def _parse_sent_at(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed


def decide_downtime_override(
    state: DowntimeState,
    *,
    last_downtime_alert_at: datetime | str | None,
    now: datetime,
) -> DowntimeOverride:
    """Should a suppressed alert be sent anyway because the grid is down?

    Fail-open on an unreadable prior timestamp: a ledger value we cannot
    parse must not be the reason a dark grid stays silent.
    """
    if not state.known:
        return DowntimeOverride(send=False, reason="downtime_unknown")
    if not state.down:
        return DowntimeOverride(send=False, reason="not_down")
    if last_downtime_alert_at is None:
        return DowntimeOverride(send=True, reason="newly_down")

    sent_at = _parse_sent_at(last_downtime_alert_at)
    if sent_at is None:
        return DowntimeOverride(send=True, reason="downtime_timestamp_invalid")
    if now - sent_at > DOWNTIME_ALERT_INTERVAL:
        return DowntimeOverride(send=True, reason="still_down_daily_reminder")
    return DowntimeOverride(send=False, reason="already_alerted_today")
