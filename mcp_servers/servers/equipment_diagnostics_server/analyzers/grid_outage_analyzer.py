"""Grid outage detection and analysis.

Analyzes historical power data to detect grid outages, identify affected phases,
and calculate outage statistics.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from ..platforms.base_platform import HistoricalDataPoint


def _format_duration(seconds: int) -> str:
    """Format duration in seconds to human-readable hours-minutes string."""
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    if remaining_minutes == 0:
        return f"{hours}h"
    return f"{hours}h {remaining_minutes}m"


@dataclass
class PhaseOutage:
    """Details of a phase-specific outage."""

    phase: str  # L1, L2, L3
    power_before_w: float
    power_after_w: float


@dataclass
class GridOutageEvent:
    """Represents a detected grid outage event."""

    start_time: datetime
    end_time: datetime
    duration_seconds: int
    affected_phases: List[str]  # ["L1"], ["L2", "L3"], ["L1", "L2", "L3"]
    is_full_outage: bool  # True if all phases affected
    pre_outage_load: Dict[str, float]  # Power before outage by phase
    peak_load_before_w: float
    recovery_time_seconds: Optional[int] = None  # Time for power to stabilize
    is_ongoing: bool = False  # True if outage is still in progress at time of analysis
    phase_details: List[PhaseOutage] = field(default_factory=list)
    # Phase(s) that tripped first and opened the outage (the fault trigger).
    # In mini-grid topology where phases track together, an L2 overload trips L2 first
    # and the others cascade — triggering_phases captures the root-cause phase(s).
    triggering_phases: List[str] = field(default_factory=list)
    # Cause classification (populated by classify_outage_cause)
    cause_category: Optional[str] = (
        None  # "battery_depletion", "grid_fault", "vebus_error", "unknown"
    )
    cause_details: Optional[str] = None  # Human-readable cause description
    battery_soc_at_outage: Optional[float] = None  # SOC % when outage started
    vebus_error: Optional[str] = None  # VE.Bus error description if present
    related_alarms: List[Dict[str, Any]] = field(default_factory=list)  # Alarms around outage time

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        result = {
            "start_time": self.start_time.isoformat(),
            "end_time": self.end_time.isoformat(),
            "duration_seconds": self.duration_seconds,
            "duration": _format_duration(self.duration_seconds),
            "affected_phases": self.affected_phases,
            "is_full_outage": self.is_full_outage,
            "pre_outage_load": self.pre_outage_load,
            "peak_load_before_w": self.peak_load_before_w,
            "recovery_time_seconds": self.recovery_time_seconds,
            "is_ongoing": self.is_ongoing,
        }
        if self.triggering_phases:
            result["triggering_phases"] = self.triggering_phases
        if self.phase_details:
            result["phase_details"] = [
                {
                    "phase": pd.phase,
                    "power_before_w": pd.power_before_w,
                    "power_after_w": pd.power_after_w,
                }
                for pd in self.phase_details
            ]
        # Add cause classification if available
        if self.cause_category:
            result["cause"] = {
                "category": self.cause_category,
                "details": self.cause_details,
                "is_expected": self.cause_category == "battery_depletion",
            }
        if self.battery_soc_at_outage is not None:
            result["battery_soc_at_outage"] = self.battery_soc_at_outage
        if self.vebus_error:
            result["vebus_error"] = self.vebus_error
        if self.related_alarms:
            result["related_alarms"] = self.related_alarms
        return result


@dataclass
class OutageAnalysisResult:
    """Result of outage analysis over a time period."""

    time_range_start: datetime
    time_range_end: datetime
    outages: List[GridOutageEvent]
    total_outages: int
    total_downtime_seconds: int
    longest_outage_seconds: int
    phases_most_affected: List[str]  # Phases with most outages

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "time_range": {
                "start": self.time_range_start.isoformat(),
                "end": self.time_range_end.isoformat(),
            },
            "outages": [o.to_dict() for o in self.outages],
            "total_outages": self.total_outages,
            "total_downtime_seconds": self.total_downtime_seconds,
            "total_downtime": _format_duration(self.total_downtime_seconds),
            "longest_outage_seconds": self.longest_outage_seconds,
            "longest_outage": _format_duration(self.longest_outage_seconds),
            "phases_most_affected": self.phases_most_affected,
        }


class GridOutageAnalyzer:
    """Analyzes time series data to detect and characterize grid outages."""

    # Voltage thresholds for outage detection
    # If voltage is above this, the inverter is considered ON (phase may be isolated)
    VOLTAGE_ON_THRESHOLD_V = 180.0  # Below ~200V AC indicates inverter is OFF
    # If voltage is below this, the inverter is definitely OFF
    VOLTAGE_OFF_THRESHOLD_V = 50.0

    def __init__(
        self,
        outage_threshold_w: float = 100.0,
        min_outage_duration_seconds: int = 30,
        recovery_window_seconds: int = 60,
    ):
        """Initialize the analyzer.

        Args:
            outage_threshold_w: Power below this threshold is considered "down"
            min_outage_duration_seconds: Minimum duration to count as an outage
            recovery_window_seconds: Window to measure recovery time after outage
        """
        self.outage_threshold = outage_threshold_w
        self.min_duration = min_outage_duration_seconds
        self.recovery_window = recovery_window_seconds

    def detect_outages(
        self,
        data_points: List[HistoricalDataPoint],
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
        voltage_points: Optional[List[HistoricalDataPoint]] = None,
    ) -> OutageAnalysisResult:
        """Detect grid outages from historical data points.

        Uses both power AND voltage for smarter outage detection:
        - Low power + Low voltage = Real outage (inverter OFF)
        - Low power + High voltage = Phase isolated (NOT an outage, maintenance)
        - High power + High voltage = Normal operation

        Args:
            data_points: List of historical data points (grid power)
            start_time: Optional start of analysis window
            end_time: Optional end of analysis window
            voltage_points: Optional list of voltage data points for smarter detection

        Returns:
            OutageAnalysisResult with detected outages
        """
        if not data_points:
            now = datetime.utcnow()
            return OutageAnalysisResult(
                time_range_start=start_time or now,
                time_range_end=end_time or now,
                outages=[],
                total_outages=0,
                total_downtime_seconds=0,
                longest_outage_seconds=0,
                phases_most_affected=[],
            )

        # Sort by timestamp
        sorted_points = sorted(data_points, key=lambda p: p.timestamp)

        # Determine time range
        if start_time is None:
            start_time = sorted_points[0].timestamp
        if end_time is None:
            end_time = sorted_points[-1].timestamp

        # Separate power data by phase
        phase_data: Dict[str, List[Tuple[datetime, float]]] = {
            "L1": [],
            "L2": [],
            "L3": [],
            "total": [],
        }

        for point in sorted_points:
            if point.phase:
                phase_data[point.phase].append((point.timestamp, point.value))
            else:
                phase_data["total"].append((point.timestamp, point.value))

        # Separate voltage data by phase (if available)
        phase_voltage: Dict[str, List[Tuple[datetime, float]]] = {
            "L1": [],
            "L2": [],
            "L3": [],
        }
        if voltage_points:
            for point in voltage_points:
                if point.phase and point.phase in phase_voltage:
                    phase_voltage[point.phase].append((point.timestamp, point.value))

        # Detect outages per phase (using voltage for smarter detection)
        phase_outages: Dict[str, List[Tuple[datetime, datetime, float]]] = {}

        for phase, readings in phase_data.items():
            if phase == "total" and not readings:
                # Use total from sum of phases if available
                continue

            # Get voltage readings for this phase
            voltage_readings = phase_voltage.get(phase, [])
            phase_outages[phase] = self._detect_phase_outages(readings, voltage_readings)

        # Merge phase outages into grid outage events
        outages = self._merge_phase_outages(phase_outages, phase_data)

        # Calculate statistics
        total_downtime = sum(o.duration_seconds for o in outages)
        longest = max((o.duration_seconds for o in outages), default=0)

        # Find most affected phases
        phase_counts: Dict[str, int] = {"L1": 0, "L2": 0, "L3": 0}
        for outage in outages:
            for phase in outage.affected_phases:
                if phase in phase_counts:
                    phase_counts[phase] += 1

        most_affected = sorted(phase_counts.keys(), key=lambda p: phase_counts[p], reverse=True)

        return OutageAnalysisResult(
            time_range_start=start_time,
            time_range_end=end_time,
            outages=outages,
            total_outages=len(outages),
            total_downtime_seconds=total_downtime,
            longest_outage_seconds=longest,
            phases_most_affected=most_affected,
        )

    def _detect_phase_outages(
        self,
        readings: List[Tuple[datetime, float]],
        voltage_readings: Optional[List[Tuple[datetime, float]]] = None,
    ) -> List[Tuple[datetime, datetime, float]]:
        """Detect outages for a single phase using power and optionally voltage.

        Voltage-based detection logic:
        - Low power + Low voltage (<50V) = Real outage (inverter OFF)
        - Low power + High voltage (>180V) = Phase isolated (NOT an outage)
        - No voltage data = Fall back to power-only detection

        Returns list of (start_time, end_time, power_before) tuples.
        """
        if not readings:
            return []

        # Build voltage lookup dict for quick access
        # Key: timestamp rounded to nearest minute for matching
        voltage_by_time: Dict[datetime, float] = {}
        if voltage_readings:
            for ts, voltage in voltage_readings:
                # Round to nearest minute for fuzzy matching with power timestamps
                rounded_ts = ts.replace(second=0, microsecond=0)
                voltage_by_time[rounded_ts] = voltage

        def get_voltage_at_time(ts: datetime) -> Optional[float]:
            """Get voltage at a timestamp, with 1-minute tolerance."""
            rounded_ts = ts.replace(second=0, microsecond=0)
            if rounded_ts in voltage_by_time:
                return voltage_by_time[rounded_ts]
            # Try adjacent minutes
            prev_min = rounded_ts - timedelta(minutes=1)
            next_min = rounded_ts + timedelta(minutes=1)
            if prev_min in voltage_by_time:
                return voltage_by_time[prev_min]
            if next_min in voltage_by_time:
                return voltage_by_time[next_min]
            return None

        def is_real_outage(power: float, timestamp: datetime) -> bool:
            """Determine if low power indicates a real outage.

            If voltage data is available:
            - Low voltage = inverter is OFF = real outage
            - High voltage = inverter is ON but phase isolated = NOT an outage

            If no voltage data, fall back to power-only detection.
            """
            if power >= self.outage_threshold:
                return False  # Power is fine, not an outage

            # Power is low - check voltage to determine if real outage
            voltage = get_voltage_at_time(timestamp)

            if voltage is None:
                # No voltage data available - fall back to power-only
                return True  # Assume low power = outage

            if voltage < self.VOLTAGE_OFF_THRESHOLD_V:
                # Low voltage confirms inverter is OFF = real outage
                return True

            if voltage > self.VOLTAGE_ON_THRESHOLD_V:
                # High voltage = inverter is ON but phase is isolated
                # This is NOT an outage (maintenance, load disconnected, etc.)
                return False

            # Voltage in uncertain range (50-180V) - assume outage
            return True

        outages = []
        in_outage = False
        outage_start: Optional[datetime] = None
        power_before = 0.0

        for i, (timestamp, power) in enumerate(readings):
            is_down = is_real_outage(power, timestamp)

            if is_down and not in_outage:
                # Outage starting
                in_outage = True
                outage_start = timestamp
                # Get power before outage
                if i > 0:
                    power_before = readings[i - 1][1]
                else:
                    power_before = 0.0

            elif not is_down and in_outage:
                # Outage ending
                if outage_start:
                    duration = (timestamp - outage_start).total_seconds()
                    if duration >= self.min_duration:
                        outages.append((outage_start, timestamp, power_before))

                in_outage = False
                outage_start = None

        # Handle ongoing outage at end of data
        if in_outage and outage_start:
            last_timestamp = readings[-1][0]
            duration = (last_timestamp - outage_start).total_seconds()
            if duration >= self.min_duration:
                outages.append((outage_start, last_timestamp, power_before))

        return outages

    def _merge_phase_outages(
        self,
        phase_outages: Dict[str, List[Tuple[datetime, datetime, float]]],
        phase_data: Dict[str, List[Tuple[datetime, float]]],
    ) -> List[GridOutageEvent]:
        """Merge per-phase outages into grid outage events.

        In mini-grid topology, all three phases track together: a fault on any
        one phase (e.g. L2 overload) trips the whole inverter cluster. So the
        grid outage opens the moment the FIRST phase drops and closes when ALL
        phases have recovered. This keeps the outage start anchored to the
        triggering fault, so alarm-to-outage matching can find the root cause.

        Logic:
        - Track which phases are currently down
        - Start outage when ANY phase goes down (record it as the trigger)
        - End outage when ALL phases have recovered
        - Record max concurrent down phases → is_full_outage
        - Record every phase that went down during the outage → affected_phases
        """
        # Collect all outage start/end times
        all_events: List[Tuple[datetime, str, str, float]] = []  # (time, phase, type, power)

        # Determine which phases have data
        phases_with_data: set = set()
        for phase, outages in phase_outages.items():
            if phase == "total":
                continue
            if outages:  # Phase has at least one outage period
                phases_with_data.add(phase)
            elif phase in phase_data and phase_data[phase]:  # Phase has readings
                phases_with_data.add(phase)

        for phase, outages in phase_outages.items():
            if phase == "total":
                continue
            for start, end, power_before in outages:
                all_events.append((start, phase, "start", power_before))
                all_events.append((end, phase, "end", 0.0))

        if not all_events:
            return []

        # Sort by timestamp, then by event type ("end" before "start" for same timestamp)
        # This ensures if a phase recovers and another goes down at the same instant,
        # we process the recovery first
        all_events.sort(key=lambda e: (e[0], 0 if e[2] == "end" else 1))

        # Process events to build grid outages
        # Phases track in mini-grid topology: any phase going down opens the outage
        grid_outages: List[GridOutageEvent] = []
        down_phases: Dict[str, float] = {}  # phase -> power_before when it went down
        outage_start: Optional[datetime] = None
        triggering_phases: List[str] = []  # phase(s) that opened the current outage
        outage_affected: set = set()  # all phases that went down during the current outage
        outage_phase_powers: Dict[str, float] = {}  # power-before snapshot per phase
        max_concurrent_down = 0  # peak number of phases simultaneously down
        total_phases = len(phases_with_data) if phases_with_data else 3

        for timestamp, phase, event_type, power in all_events:
            if event_type == "start":
                # Phase is going down
                down_phases[phase] = power

                if outage_start is None:
                    # First phase to trip — open the outage and record the trigger
                    outage_start = timestamp
                    triggering_phases = [phase]
                    outage_affected = {phase}
                    outage_phase_powers = {phase: power}
                    max_concurrent_down = 1
                else:
                    # Additional phase tripping during an in-progress outage
                    outage_affected.add(phase)
                    outage_phase_powers.setdefault(phase, power)
                    # Phases that drop in the same sample as the trigger are co-triggers
                    if timestamp == outage_start and phase not in triggering_phases:
                        triggering_phases.append(phase)
                    if len(down_phases) > max_concurrent_down:
                        max_concurrent_down = len(down_phases)

            elif event_type == "end":
                # Phase is recovering
                if phase in down_phases:
                    del down_phases[phase]

                # Outage closes only when ALL phases have recovered
                if not down_phases and outage_start is not None:
                    duration = int((timestamp - outage_start).total_seconds())

                    # Calculate MAX load in 30 minutes before outage (per phase)
                    pre_load: Dict[str, float] = {}
                    window_start = outage_start - timedelta(minutes=30)

                    for p, readings in phase_data.items():
                        if p == "total":
                            continue
                        max_val = 0.0
                        for ts, val in readings:
                            if window_start <= ts < outage_start:
                                max_val = max(max_val, val)
                        if max_val > 0:
                            pre_load[p] = max_val

                    total_pre = sum(pre_load.values())

                    affected_sorted = sorted(outage_affected)
                    phase_details_list = [
                        PhaseOutage(
                            phase=p,
                            power_before_w=outage_phase_powers.get(p, 0.0),
                            power_after_w=0.0,
                        )
                        for p in affected_sorted
                    ]

                    grid_outages.append(
                        GridOutageEvent(
                            start_time=outage_start,
                            end_time=timestamp,
                            duration_seconds=duration,
                            affected_phases=affected_sorted,
                            is_full_outage=max_concurrent_down >= total_phases,
                            pre_outage_load=pre_load,
                            peak_load_before_w=total_pre,
                            phase_details=phase_details_list,
                            triggering_phases=list(triggering_phases),
                        )
                    )

                    # Reset per-outage tracking
                    outage_start = None
                    triggering_phases = []
                    outage_affected = set()
                    outage_phase_powers = {}
                    max_concurrent_down = 0

        # Handle ongoing outage at end of data
        if down_phases and outage_start is not None:
            # At least one phase is still down — outage is ongoing
            # Use latest event timestamp as provisional end_time
            last_event_time = all_events[-1][0] if all_events else outage_start
            duration = int((last_event_time - outage_start).total_seconds())

            # Calculate pre-outage load
            ongoing_pre_load: Dict[str, float] = {}
            window_start = outage_start - timedelta(minutes=30)
            for p, readings in phase_data.items():
                if p == "total":
                    continue
                max_val = 0.0
                for ts, val in readings:
                    if window_start <= ts < outage_start:
                        max_val = max(max_val, val)
                if max_val > 0:
                    ongoing_pre_load[p] = max_val

            total_pre = sum(ongoing_pre_load.values())

            affected_sorted = sorted(outage_affected)
            phase_details_list = [
                PhaseOutage(
                    phase=p,
                    power_before_w=outage_phase_powers.get(p, 0.0),
                    power_after_w=0.0,
                )
                for p in affected_sorted
            ]

            grid_outages.append(
                GridOutageEvent(
                    start_time=outage_start,
                    end_time=last_event_time,
                    duration_seconds=duration,
                    affected_phases=affected_sorted,
                    is_full_outage=max_concurrent_down >= total_phases,
                    pre_outage_load=ongoing_pre_load,
                    peak_load_before_w=total_pre,
                    is_ongoing=True,
                    phase_details=phase_details_list,
                    triggering_phases=list(triggering_phases),
                )
            )

        return grid_outages

    def find_last_outage(
        self,
        data_points: List[HistoricalDataPoint],
    ) -> Optional[GridOutageEvent]:
        """Find the most recent outage in the data.

        Args:
            data_points: Historical data points

        Returns:
            Most recent GridOutageEvent or None if no outages found
        """
        result = self.detect_outages(data_points)

        if result.outages:
            # Return the last one (most recent)
            return result.outages[-1]

        return None

    def calculate_peak_load(
        self,
        data_points: List[HistoricalDataPoint],
        before_time: Optional[datetime] = None,
        window_minutes: int = 60,
    ) -> Dict[str, Any]:
        """Calculate peak load, optionally before a specific time.

        Args:
            data_points: Historical data points
            before_time: Only consider data before this time
            window_minutes: Window to search for peak (default 60 min before)

        Returns:
            Dict with peak load details
        """
        if not data_points:
            return {"peak_power_w": 0, "timestamp": None}

        # Filter by time if specified
        if before_time:
            window_start = before_time - timedelta(minutes=window_minutes)
            filtered = [p for p in data_points if window_start <= p.timestamp <= before_time]
        else:
            filtered = data_points

        if not filtered:
            return {"peak_power_w": 0, "timestamp": None}

        # Group by timestamp, keeping last value per phase (no double-counting)
        timestamp_phases: Dict[datetime, Dict[str, float]] = {}

        for point in filtered:
            ts = point.timestamp
            if ts not in timestamp_phases:
                timestamp_phases[ts] = {"L1": 0, "L2": 0, "L3": 0, "_scalar": 0}

            if point.phase:
                timestamp_phases[ts][point.phase] = point.value
            else:
                timestamp_phases[ts]["_scalar"] = max(timestamp_phases[ts]["_scalar"], point.value)

        # Compute totals from per-phase values (prefer phase sum over scalar)
        timestamp_totals: Dict[datetime, Dict[str, float]] = {}
        for ts, data in timestamp_phases.items():
            phase_sum = data["L1"] + data["L2"] + data["L3"]
            total = phase_sum if phase_sum > 0 else data["_scalar"]
            timestamp_totals[ts] = {
                "total": total,
                "L1": data["L1"],
                "L2": data["L2"],
                "L3": data["L3"],
            }

        # Find peak
        peak_ts = max(timestamp_totals.keys(), key=lambda t: timestamp_totals[t]["total"])
        peak_data = timestamp_totals[peak_ts]

        return {
            "timestamp": peak_ts.isoformat(),
            "total_power_w": peak_data["total"],
            "l1_power_w": peak_data.get("L1", 0),
            "l2_power_w": peak_data.get("L2", 0),
            "l3_power_w": peak_data.get("L3", 0),
        }

    def calculate_summary_stats(
        self,
        data_points: List[HistoricalDataPoint],
    ) -> Dict[str, Any]:
        """Calculate summary statistics for power data.

        Args:
            data_points: Historical data points

        Returns:
            Dict with avg, max, min power statistics
        """
        if not data_points:
            return {
                "avg_power_w": 0,
                "max_power_w": 0,
                "min_power_w": 0,
                "data_points": 0,
            }

        # Group by timestamp, keeping last value per phase (no double-counting)
        timestamp_phases: Dict[datetime, Dict[str, float]] = {}

        for point in data_points:
            ts = point.timestamp
            if ts not in timestamp_phases:
                timestamp_phases[ts] = {"L1": 0, "L2": 0, "L3": 0, "_scalar": 0}
            if point.phase:
                timestamp_phases[ts][point.phase] = point.value
            else:
                timestamp_phases[ts]["_scalar"] = max(timestamp_phases[ts]["_scalar"], point.value)

        # Compute totals from per-phase values (prefer phase sum over scalar)
        timestamp_totals: Dict[datetime, float] = {}
        for ts, data in timestamp_phases.items():
            phase_sum = data["L1"] + data["L2"] + data["L3"]
            timestamp_totals[ts] = phase_sum if phase_sum > 0 else data["_scalar"]

        if not timestamp_totals:
            return {
                "avg_power_w": 0,
                "max_power_w": 0,
                "min_power_w": 0,
                "data_points": 0,
            }

        values = list(timestamp_totals.values())

        return {
            "avg_power_w": round(sum(values) / len(values), 2),
            "max_power_w": round(max(values), 2),
            "min_power_w": round(min(values), 2),
            "data_points": len(values),
        }

    def classify_outage_cause(
        self,
        outage: GridOutageEvent,
        battery_soc: Optional[float] = None,
        alarms: Optional[List[Dict[str, Any]]] = None,
        low_soc_threshold: float = 10.0,
    ) -> GridOutageEvent:
        """Classify the likely cause of an outage.

        Categories:
        - battery_depletion: SOC < threshold (expected behavior by design)
        - low_battery: Low battery alarm triggered (not necessarily depletion)
        - high_temperature: Temperature alarm triggered
        - overload: Overload alarm detected
        - vebus_error: VE.Bus alarm/error occurred around outage time
        - grid_fault: Partial phase failure (not all phases affected)
        - unknown: No clear cause identified

        Args:
            outage: The outage event to classify
            battery_soc: Battery SOC % at time of outage
            alarms: List of alarms around the outage time
            low_soc_threshold: SOC below which battery depletion is expected (default 10%)

        Returns:
            Updated GridOutageEvent with cause classification
        """
        outage.battery_soc_at_outage = battery_soc
        outage.related_alarms = alarms or []

        # Filter alarms to those within 5 minutes of outage start
        relevant_alarms = []
        if alarms:
            for alarm in alarms:
                try:
                    alarm_time = datetime.fromisoformat(alarm.get("timestamp", ""))
                    delta = abs((alarm_time - outage.start_time).total_seconds())
                    if delta <= 300:  # Within 5 minutes
                        relevant_alarms.append(alarm)
                except (ValueError, TypeError):
                    pass

        outage.related_alarms = relevant_alarms

        # Categorize alarms by type
        overload_alarms = []
        temperature_alarms = []
        low_battery_alarms = []
        vebus_errors = []

        for alarm in relevant_alarms:
            desc = alarm.get("description", "").lower()
            code = alarm.get("code", "").lower()

            # Check for overload alarms
            if "overload" in desc or "overload" in code:
                overload_alarms.append(alarm)
            # Check for high temperature alarms
            elif any(
                kw in desc or kw in code
                for kw in ["temperature", "temp", "high temp", "overheat", "thermal"]
            ):
                temperature_alarms.append(alarm)
            # Check for low battery alarms
            elif any(
                kw in desc or kw in code
                for kw in ["low battery", "battery low", "low voltage", "undervoltage"]
            ):
                low_battery_alarms.append(alarm)
            # Check for VE.Bus errors (description = "VE.Bus Error", detail in severity)
            elif "ve.bus error" in desc:
                vebus_errors.append(alarm)

        # Always annotate VE.Bus error when present (independent of cause priority)
        if vebus_errors:
            # severity holds the detailed error (e.g. "VE.Bus Error 10: ...")
            outage.vebus_error = vebus_errors[0].get("severity") or vebus_errors[0].get(
                "description", "VE.Bus error"
            )

        # Priority order for cause classification:
        # 1. Overload (dangerous, needs immediate attention)
        # 2. High temperature (dangerous, needs attention)
        # 3. Low battery alarm
        # 4. VE.Bus errors
        # 5. Battery depletion (expected behavior)
        # 6. Grid fault
        # 7. Unknown

        if overload_alarms:
            outage.cause_category = "overload"
            alarm_desc = overload_alarms[0].get("description", "Overload detected")
            outage.cause_details = f"Overload alarm triggered: {alarm_desc}"
            return outage

        if temperature_alarms:
            outage.cause_category = "high_temperature"
            alarm_desc = temperature_alarms[0].get("description", "High temperature")
            outage.cause_details = f"Temperature alarm triggered: {alarm_desc}"
            return outage

        if low_battery_alarms:
            outage.cause_category = "low_battery"
            alarm_desc = low_battery_alarms[0].get("description", "Low battery")
            outage.cause_details = f"Low battery alarm triggered: {alarm_desc}"
            return outage

        if vebus_errors:
            outage.cause_category = "vebus_error"
            error_desc = vebus_errors[0].get("severity") or vebus_errors[0].get(
                "description", "VE.Bus error"
            )
            outage.cause_details = f"VE.Bus error detected: {error_desc}"
            return outage

        # Check for battery depletion (expected behavior based on SOC)
        if battery_soc is not None and battery_soc < low_soc_threshold:
            # Check if this is nighttime (no solar production expected)
            hour = outage.start_time.hour
            is_night = hour < 6 or hour >= 19  # Roughly 7pm-6am

            if is_night:
                outage.cause_category = "battery_depletion"
                outage.cause_details = (
                    f"Battery SOC at {battery_soc:.1f}% (below {low_soc_threshold}% threshold). "
                    "This is expected behavior - grids are designed to optimize battery longevity, "
                    "not maximize service autonomy."
                )
                return outage

        # Check for partial phase failure (grid fault)
        if not outage.is_full_outage and len(outage.affected_phases) < 3:
            outage.cause_category = "grid_fault"
            phases = ", ".join(outage.affected_phases)
            outage.cause_details = f"Partial grid failure affecting {phases} only"
            return outage

        # Unknown cause
        outage.cause_category = "unknown"
        outage.cause_details = "No clear cause identified from available data"
        return outage
