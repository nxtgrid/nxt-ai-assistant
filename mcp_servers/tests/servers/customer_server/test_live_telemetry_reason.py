"""Coverage for _output_unavailable_reason, the diagnostic that says *why* an
urgent alert rendered "Site status: Unknown".

The four reasons are deliberately distinct because they need different
responses, and the two that ``_inverter_voltage_is_stale`` collapses into one
boolean are the ones that matter most: ``no_report_time`` means VRM served a
reading with no OV1 secondsAgo, so there was no staleness evidence either way
and the reading was discarded; ``stale`` means there was evidence, and it said
the gateway has not reported in over 30 minutes.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../../"))
for _p in (os.path.join(_REPO_ROOT, "mcp_servers"), _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from servers.customer_server.client_grid_status import (  # noqa: E402
    _output_unavailable_reason,
)


@dataclass
class _Voltage:
    """The subset of InverterVoltage the reason helper reads."""

    data_timestamp: Optional[datetime] = None
    total_power_kw: Optional[float] = None
    error: Optional[str] = None


def test_reading_with_an_error_is_reported_as_unavailable():
    assert _output_unavailable_reason(_Voltage(error="vrm 503")) == "reading_unavailable"


def test_missing_reading_is_reported_as_unavailable():
    assert _output_unavailable_reason(None) == "reading_unavailable"


def test_absent_report_time_is_distinguished_from_a_stale_one():
    """No OV1 secondsAgo: the reading is dropped for want of evidence, not
    because the gateway is known to be behind."""
    assert _output_unavailable_reason(_Voltage(data_timestamp=None)) == "no_report_time"


def test_old_report_time_is_reported_as_stale():
    old = datetime.utcnow() - timedelta(hours=3)
    assert _output_unavailable_reason(_Voltage(data_timestamp=old)) == "stale"


def test_fresh_reading_with_no_power_value_is_neither_stale_nor_missing():
    recent = datetime.utcnow() - timedelta(minutes=2)
    assert _output_unavailable_reason(_Voltage(data_timestamp=recent)) == "no_power_value"


def test_every_reason_is_a_distinct_string():
    """The whole point is that a single grep separates the causes."""
    reasons = {
        _output_unavailable_reason(_Voltage(error="boom")),
        _output_unavailable_reason(_Voltage(data_timestamp=None)),
        _output_unavailable_reason(_Voltage(data_timestamp=datetime.utcnow() - timedelta(hours=3))),
        _output_unavailable_reason(
            _Voltage(data_timestamp=datetime.utcnow() - timedelta(minutes=2))
        ),
    }
    assert len(reasons) == 4
