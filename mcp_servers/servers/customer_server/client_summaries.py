"""FS delivery/schedule daily-summary methods for CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from servers.customer_server.client_base import (
    DEFAULT_TIMEZONE,
    STAFF_ORG_ID,
    TIMESCALE_DATABASE,
    TIMESCALE_HOST,
    TIMESCALE_PASSWORD,
    TIMESCALE_PORT,
    TIMESCALE_USER,
    logger,
)
from servers.customer_server.formatting import _find_closest_grid_name, _format_time_12h


class ClientSummariesMixin:
    async def _get_last_fs_delivery(
        self,
        conn,
        grid_id: int,
    ) -> Optional[Dict[str, Any]]:
        """
        Get the last FS command delivery percentage for a grid.

        Args:
            conn: Auth database connection
            grid_id: Grid ID to get delivery stats for

        Returns:
            Dict with command, delivery_pct, successful, total, executed_at
            or None if no FS commands found for this grid
        """
        try:
            row = await conn.fetchrow(
                """
                SELECT
                    dbe.successful_count,
                    dbe.total_count,
                    db.fs_command,
                    dbe.created_at
                FROM directive_batch_executions dbe
                JOIN directive_batches db ON dbe.directive_batch_id = db.id
                WHERE db.grid_id = $1
                  AND db.fs_command IS NOT NULL
                ORDER BY dbe.id DESC
                LIMIT 1
                """,
                grid_id,
            )

            if not row:
                return None

            total = row["total_count"] or 0
            successful = row["successful_count"] or 0
            delivery_pct = round((successful / total * 100), 1) if total > 0 else 0

            return {
                "command": row["fs_command"],
                "delivery_pct": delivery_pct,
                "successful": successful,
                "total": total,
                "executed_at": row["created_at"].isoformat() if row["created_at"] else None,
            }
        except Exception as e:
            logger.error(f"Error getting last FS delivery for grid {grid_id}: {e}")
            return None

    async def _get_fs_schedule(
        self,
        conn,
        grid_id: int,
        current_fs_on: Optional[bool],
    ) -> Dict[str, Any]:
        """
        Get FS command schedule for the next 24 hours.

        Returns:
        - scheduled_commands: Raw list of scheduled FS on/off commands with times
        - fs_on_periods: Calculated FS On periods (start/end/duration)
        - total_fs_on_hours: Total hours FS will be on in next 24h
        - current_state: Current FS state

        Args:
            conn: Auth database connection
            grid_id: Grid ID to get schedule for
            current_fs_on: Current FS state from TimescaleDB (True/False/None)

        Returns:
            Dict with scheduled_commands, fs_on_periods, total_fs_on_hours
        """
        try:
            # Query directive_batches for FS commands
            fs_commands = await conn.fetch(
                """
                SELECT id, hour, minute, is_repeating, fs_command
                FROM directive_batches
                WHERE grid_id = $1
                  AND fs_command IS NOT NULL
                  AND (is_deleted IS NULL OR is_deleted = false)
                ORDER BY hour, minute
                """,
                grid_id,
            )

            if not fs_commands:
                return {
                    "current_state": (
                        "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                    ),
                    "scheduled_commands": [],
                    "fs_on_periods": [],
                    "total_fs_on_hours": None,
                    "summary": "No FS schedule configured for this grid",
                }

            # Get current time in UTC
            now = datetime.now(timezone.utc)
            current_hour = now.hour
            current_minute = now.minute
            now_minutes = current_hour * 60 + current_minute

            # Filter commands for next 24 hours
            # For repeating commands that have already passed today, shift them to tomorrow
            # by adding 24*60 minutes to their effective time
            commands_next_24h = []
            for cmd in fs_commands:
                cmd_dict = dict(cmd)
                cmd_minutes = cmd_dict["hour"] * 60 + cmd_dict["minute"]

                if cmd_dict["is_repeating"]:
                    # Repeating: if time has passed today, it will run tomorrow
                    if cmd_minutes <= now_minutes:
                        # Already passed today - schedule for tomorrow (add 24h)
                        cmd_dict["effective_minutes"] = cmd_minutes + 24 * 60
                    else:
                        # Still upcoming today
                        cmd_dict["effective_minutes"] = cmd_minutes
                    commands_next_24h.append(cmd_dict)
                else:
                    # Non-repeating: only if time hasn't passed today
                    if cmd_minutes > now_minutes:
                        cmd_dict["effective_minutes"] = cmd_minutes
                        commands_next_24h.append(cmd_dict)

            # Sort by effective time (accounts for tomorrow's repeating commands)
            sorted_cmds = sorted(commands_next_24h, key=lambda x: x["effective_minutes"])

            # Build scheduled_commands list (the raw schedule)
            scheduled_commands = []
            for cmd in sorted_cmds:
                time_12h = _format_time_12h(cmd["hour"], cmd["minute"])
                scheduled_commands.append(
                    {
                        "time": f"{cmd['hour']:02d}:{cmd['minute']:02d}",
                        "time_display": time_12h,
                        "action": cmd["fs_command"],  # 'on' or 'off'
                        "is_repeating": cmd["is_repeating"],
                    }
                )

            if not commands_next_24h:
                return {
                    "current_state": (
                        "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                    ),
                    "scheduled_commands": [],
                    "fs_on_periods": [],
                    "total_fs_on_hours": None,
                    "summary": "No remaining FS commands scheduled today",
                }

            # Calculate FS On periods accounting for current state
            periods = []
            total_minutes = 0.0

            # Track current simulated state (start with actual current state)
            simulated_fs_on = current_fs_on if current_fs_on is not None else False
            current_on_start_minutes: Optional[int] = None

            # If currently ON, track from "now"
            if simulated_fs_on:
                current_on_start_minutes = now_minutes

            for cmd in sorted_cmds:
                # Use effective_minutes for calculation (accounts for tomorrow)
                cmd_minutes = cmd["effective_minutes"]
                # Display time uses original hour/minute
                cmd_time_str = f"{cmd['hour']:02d}:{cmd['minute']:02d}"
                cmd_time_12h = _format_time_12h(cmd["hour"], cmd["minute"])

                # Case-insensitive comparison for fs_command (DB may store ON/OFF or on/off)
                fs_cmd = cmd["fs_command"].lower() if cmd["fs_command"] else ""

                if fs_cmd == "on":
                    if not simulated_fs_on:
                        # Transitioning OFF → ON
                        simulated_fs_on = True
                        current_on_start_minutes = cmd_minutes
                    # else: ON → ON has no effect (already on)

                elif fs_cmd == "off":
                    if simulated_fs_on and current_on_start_minutes is not None:
                        # Transitioning ON → OFF - record the period
                        duration_minutes = cmd_minutes - current_on_start_minutes
                        duration_hours = round(duration_minutes / 60, 1)

                        # Format start time
                        if current_on_start_minutes == now_minutes:
                            start_display = "Now"
                            start_str = "now"
                        else:
                            start_hour = current_on_start_minutes // 60
                            start_min = current_on_start_minutes % 60
                            start_display = _format_time_12h(start_hour, start_min)
                            start_str = f"{start_hour:02d}:{start_min:02d}"

                        periods.append(
                            {
                                "start": start_str,
                                "start_display": start_display,
                                "end": cmd_time_str,
                                "end_display": cmd_time_12h,
                                "duration_hours": duration_hours,
                            }
                        )
                        total_minutes += duration_minutes

                        simulated_fs_on = False
                        current_on_start_minutes = None
                    # else: OFF → OFF has no effect (already off)

            # Handle unclosed period (still ON at end of 24h window)
            if simulated_fs_on and current_on_start_minutes is not None:
                # ON extends to end of 24h window from now
                end_minutes = now_minutes + 24 * 60
                duration_minutes = end_minutes - current_on_start_minutes
                duration_hours = round(duration_minutes / 60, 1)

                # Format start time
                if current_on_start_minutes == now_minutes:
                    start_display = "Now"
                    start_str = "now"
                else:
                    start_hour = current_on_start_minutes // 60
                    start_min = current_on_start_minutes % 60
                    start_display = _format_time_12h(start_hour, start_min)
                    start_str = f"{start_hour:02d}:{start_min:02d}"

                periods.append(
                    {
                        "start": start_str,
                        "start_display": start_display,
                        "end": "24:00",
                        "end_display": "Midnight",
                        "duration_hours": duration_hours,
                    }
                )
                total_minutes += duration_minutes

            # Calculate total hours
            total_hours = round(total_minutes / 60, 1) if total_minutes > 0 else 0

            # Build summary
            if scheduled_commands:
                cmd_summary = ", ".join(
                    f"FS {c['action'].upper()} at {c['time_display']}" for c in scheduled_commands
                )
            else:
                cmd_summary = "No commands scheduled"

            return {
                "current_state": (
                    "on" if current_fs_on else "off" if current_fs_on is False else "unknown"
                ),
                "scheduled_commands": scheduled_commands,
                "fs_on_periods": periods,
                "total_fs_on_hours": total_hours,
                "summary": f"Schedule: {cmd_summary}. Total FS On: {total_hours}h in next 24h",
            }

        except Exception as e:
            logger.error(f"Error getting FS schedule for grid {grid_id}: {e}")
            return {
                "current_state": "unknown",
                "scheduled_commands": [],
                "fs_on_periods": [],
                "total_fs_on_hours": None,
                "summary": f"Error retrieving FS schedule: {str(e)}",
                "error": str(e),
            }

    async def _get_yesterday_on_hours(
        self,
        ts_conn,
        grid_id: int,
    ) -> Dict[str, Any]:
        """
        Calculate how many hours the grid was ON yesterday.

        Uses grid_energy_snapshot_15_min table with TimescaleDB's native
        time_bucket_gapfill() for gap-filling missing periods using LOCF
        (Last Observation Carried Forward).

        ON = is_fs_active=true OR is_hps_on=true.

        Args:
            ts_conn: TimescaleDB connection
            grid_id: Grid ID

        Returns:
            Dict with yesterday_on_hours, total_periods, on_periods, coverage_pct
        """
        try:
            # Calculate yesterday's date range in UTC (naive timestamps for DB compatibility)
            now = datetime.utcnow()
            yesterday_start = (now - timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            yesterday_end = yesterday_start + timedelta(days=1)

            # Use TimescaleDB's time_bucket_gapfill with LOCF for gap filling
            # This fills in missing 15-min slots using last observation carried forward
            rows = await ts_conn.fetch(
                """
                SELECT
                    time_bucket_gapfill('15 minutes', created_at) AS bucket,
                    locf(last(is_fs_active, created_at)) AS is_fs_active,
                    locf(last(is_hps_on, created_at)) AS is_hps_on
                FROM grid_energy_snapshot_15_min
                WHERE grid_id = $1
                  AND created_at >= $2
                  AND created_at < $3
                GROUP BY bucket
                ORDER BY bucket
                """,
                grid_id,
                yesterday_start,
                yesterday_end,
            )

            # Count ON periods (is_fs_active=True OR is_hps_on=True)
            total_slots = 96  # 24 hours * 4 slots per hour
            on_count = 0
            actual_data_points = 0

            for row in rows:
                is_fs = row["is_fs_active"]
                is_hps = row["is_hps_on"]

                # Count actual data points (not gap-filled nulls)
                if is_fs is not None or is_hps is not None:
                    actual_data_points += 1

                # ON if either is True
                if is_fs is True or is_hps is True:
                    on_count += 1

            # Convert to hours (each slot = 0.25 hours)
            on_hours = round(on_count * 0.25, 1)
            coverage_pct = (
                round(actual_data_points / total_slots * 100, 1) if total_slots > 0 else 0
            )

            return {
                "on_hours": on_hours,
                "total_periods": total_slots,
                "on_periods": on_count,
                "data_coverage_pct": coverage_pct,
                "date": yesterday_start.strftime("%Y-%m-%d"),
            }

        except Exception as e:
            logger.error(f"Error calculating yesterday ON hours for grid {grid_id}: {e}")
            return {
                "on_hours": None,
                "error": str(e),
            }

    async def _get_fs_state_transitions(
        self,
        ts_conn,
        grid_id: int,
        start_date: datetime,
        end_date: datetime,
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> Dict[str, Any]:
        """
        Query TimescaleDB for FS state transitions over a date range.

        Detects when is_fs_active changes between true/false, computes daily ON hours.

        Args:
            ts_conn: TimescaleDB connection
            grid_id: Grid ID
            start_date: Start datetime (UTC, inclusive)
            end_date: End datetime (UTC, exclusive)
            grid_tz: Grid timezone for day boundary grouping

        Returns:
            Dict with per-day FS ON hours, data coverage, and state transitions
        """
        try:
            tz = ZoneInfo(grid_tz)
            rows = await ts_conn.fetch(
                """
                SELECT
                    time_bucket_gapfill('15 minutes', created_at) AS bucket,
                    locf(last(is_fs_active, created_at)) AS is_fs_active
                FROM grid_energy_snapshot_15_min
                WHERE grid_id = $1
                  AND created_at >= $2
                  AND created_at < $3
                GROUP BY bucket
                ORDER BY bucket
                """,
                grid_id,
                start_date,
                end_date,
            )

            # Group by local date and detect transitions
            days: Dict[str, Dict[str, Any]] = {}
            prev_fs = None

            for row in rows:
                bucket_utc = row["bucket"]
                is_fs = row["is_fs_active"]

                # Convert to local time for day grouping
                if bucket_utc.tzinfo is None:
                    bucket_utc = bucket_utc.replace(tzinfo=timezone.utc)
                local_dt = bucket_utc.astimezone(tz)
                day_key = local_dt.strftime("%Y-%m-%d")

                if day_key not in days:
                    days[day_key] = {
                        "fs_on_slots": 0,
                        "total_slots": 0,
                        "data_points": 0,
                        "transitions": [],
                    }

                day = days[day_key]
                day["total_slots"] += 1

                if is_fs is not None:
                    day["data_points"] += 1

                if is_fs is True:
                    day["fs_on_slots"] += 1

                # Detect transitions
                if prev_fs is not None and is_fs is not None and prev_fs != is_fs:
                    new_state = "on" if is_fs else "off"
                    day["transitions"].append(
                        {
                            "time": local_dt.strftime("%-I:%M %p"),
                            "time_utc": bucket_utc.strftime("%H:%M"),
                            "new_state": new_state,
                        }
                    )

                prev_fs = is_fs

            # Compute summary per day
            result_days = {}
            for day_key, day in days.items():
                fs_on_hours = round(day["fs_on_slots"] * 0.25, 1)
                expected_slots = 96  # 24h * 4 slots/h
                data_coverage_pct = round(day["data_points"] / expected_slots * 100, 1)
                result_days[day_key] = {
                    "fs_on_hours": fs_on_hours,
                    "data_coverage_pct": data_coverage_pct,
                    "transitions": day["transitions"],
                }

            return {"days": result_days}

        except Exception as e:
            logger.error(f"Error getting FS state transitions for grid {grid_id}: {e}")
            return {"days": {}, "error": str(e)}

    async def _get_fs_command_executions(
        self,
        conn,
        grid_id: int,
        start_date: datetime,
        end_date: datetime,
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> Dict[str, Any]:
        """
        Query Auth DB for FS command executions in a date range.

        Args:
            conn: Auth database connection
            grid_id: Grid ID
            start_date: Start datetime (UTC, inclusive)
            end_date: End datetime (UTC, exclusive)
            grid_tz: Grid timezone for day boundary grouping

        Returns:
            Dict with per-day command executions and delivery percentages
        """
        try:
            tz = ZoneInfo(grid_tz)
            rows = await conn.fetch(
                """
                SELECT
                    dbe.successful_count, dbe.total_count, dbe.created_at,
                    db.fs_command, db.hour, db.minute, db.is_repeating
                FROM directive_batch_executions dbe
                JOIN directive_batches db ON dbe.directive_batch_id = db.id
                WHERE db.grid_id = $1
                  AND db.fs_command IS NOT NULL
                  AND dbe.created_at >= $2
                  AND dbe.created_at < $3
                ORDER BY dbe.created_at
                """,
                grid_id,
                start_date,
                end_date,
            )

            days: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                exec_at = row["created_at"]
                if exec_at and exec_at.tzinfo is None:
                    exec_at = exec_at.replace(tzinfo=timezone.utc)
                local_dt = exec_at.astimezone(tz) if exec_at else None
                day_key = local_dt.strftime("%Y-%m-%d") if local_dt else "unknown"

                if day_key not in days:
                    days[day_key] = {"commands": [], "delivery_pcts": []}

                total = row["total_count"] or 0
                successful = row["successful_count"] or 0
                delivery_pct = round((successful / total * 100), 1) if total > 0 else 0

                days[day_key]["commands"].append(
                    {
                        "time": local_dt.strftime("%-I:%M %p") if local_dt else None,
                        "command": row["fs_command"],
                        "delivery_pct": delivery_pct,
                        "successful": successful,
                        "total": total,
                    }
                )
                days[day_key]["delivery_pcts"].append(delivery_pct)

            # Compute avg delivery per day
            result_days = {}
            for day_key, day in days.items():
                pcts = day["delivery_pcts"]
                avg_pct = round(sum(pcts) / len(pcts), 1) if pcts else None
                result_days[day_key] = {
                    "commands": day["commands"],
                    "avg_delivery_pct": avg_pct,
                }

            return {"days": result_days}

        except Exception as e:
            logger.error(f"Error getting FS command executions for grid {grid_id}: {e}")
            return {"days": {}, "error": str(e)}

    def _correlate_fs_commands_and_state(
        self,
        transitions_data: Dict[str, Any],
        commands_data: Dict[str, Any],
        grid_tz: str = DEFAULT_TIMEZONE,
    ) -> List[Dict[str, Any]]:
        """
        Merge command executions and state transitions into a per-day correlated view.

        For each command, looks for a matching state transition (same direction, within 30 min).
        Unmatched transitions or commands are flagged as discrepancies.

        Returns:
            List of per-day summary dicts
        """
        trans_days = transitions_data.get("days", {})
        cmd_days = commands_data.get("days", {})
        all_dates = sorted(set(list(trans_days.keys()) + list(cmd_days.keys())))

        daily_summary = []
        for date_key in all_dates:
            trans_day = trans_days.get(date_key, {})
            cmd_day = cmd_days.get(date_key, {})

            transitions = list(trans_day.get("transitions", []))
            commands = list(cmd_day.get("commands", []))

            # Track which transitions have been matched
            matched_trans_indices = set()
            enriched_commands = []

            for cmd in commands:
                cmd_direction = (cmd.get("command") or "").lower()  # normalize to "on"/"off"
                matched_transition = None

                # Look for a transition in the same direction within 30 min
                for i, trans in enumerate(transitions):
                    if i in matched_trans_indices:
                        continue
                    if trans.get("new_state") != cmd_direction:
                        continue
                    # Simple time proximity check via string comparison
                    # (both are in local time format like "6:15 AM")
                    matched_transition = trans.get("time")
                    matched_trans_indices.add(i)
                    break

                enriched_commands.append(
                    {
                        **cmd,
                        "matched_transition": matched_transition,
                    }
                )

            # Enriched transitions with matched_command
            enriched_transitions = []
            for i, trans in enumerate(transitions):
                matched_cmd = None
                if i in matched_trans_indices:
                    # Find the command that matched this transition
                    for ecmd in enriched_commands:
                        if ecmd.get("matched_transition") == trans.get("time"):
                            matched_cmd = ecmd.get("time")
                            break

                enriched_transitions.append(
                    {
                        **trans,
                        "matched_command": matched_cmd,
                    }
                )

            # Discrepancies: unmatched commands or transitions
            discrepancies = []
            for ecmd in enriched_commands:
                if ecmd.get("matched_transition") is None:
                    discrepancies.append(
                        f"Command '{ecmd.get('command')}' at {ecmd.get('time')} had no matching state transition"
                    )
            for i, etrans in enumerate(enriched_transitions):
                if i not in matched_trans_indices:
                    discrepancies.append(
                        f"State transition to '{etrans.get('new_state')}' at {etrans.get('time')} had no matching command"
                    )

            daily_summary.append(
                {
                    "date": date_key,
                    "fs_on_hours": trans_day.get("fs_on_hours", 0),
                    "data_coverage_pct": trans_day.get("data_coverage_pct", 0),
                    "commands_executed": enriched_commands,
                    "state_transitions": enriched_transitions,
                    "discrepancies": discrepancies,
                    "avg_delivery_pct": cmd_day.get("avg_delivery_pct"),
                }
            )

        return daily_summary

    async def _get_fs_summary_for_grid(
        self,
        auth_conn,
        ts_conn,
        grid_id: int,
        grid_tz: str,
        start_date: datetime,
        end_date: datetime,
    ) -> Optional[Dict[str, Any]]:
        """
        Get correlated FS command/state summary for a grid. Reuses existing open connections.

        Args:
            auth_conn: Auth database connection
            ts_conn: TimescaleDB connection
            grid_id: Grid ID
            grid_tz: Grid timezone
            start_date: Start datetime (UTC)
            end_date: End datetime (UTC)

        Returns:
            Correlated daily summary or None on failure
        """
        try:
            transitions_data, commands_data = await asyncio.gather(
                self._get_fs_state_transitions(ts_conn, grid_id, start_date, end_date, grid_tz),
                self._get_fs_command_executions(auth_conn, grid_id, start_date, end_date, grid_tz),
            )

            daily_summary = self._correlate_fs_commands_and_state(
                transitions_data, commands_data, grid_tz
            )

            # Compute overall summary
            total_fs_on = sum(d.get("fs_on_hours", 0) for d in daily_summary)
            total_days = len(daily_summary) or 1
            delivery_pcts = [
                d["avg_delivery_pct"]
                for d in daily_summary
                if d.get("avg_delivery_pct") is not None
            ]
            overall_delivery = (
                round(sum(delivery_pcts) / len(delivery_pcts), 1) if delivery_pcts else None
            )
            total_discrepancies = sum(len(d.get("discrepancies", [])) for d in daily_summary)

            return {
                "daily_summary": daily_summary,
                "summary": {
                    "total_days": len(daily_summary),
                    "total_fs_on_hours": round(total_fs_on, 1),
                    "avg_daily_fs_on_hours": round(total_fs_on / total_days, 1),
                    "overall_avg_delivery_pct": overall_delivery,
                    "total_discrepancies": total_discrepancies,
                },
            }
        except Exception as e:
            logger.error(f"Error getting FS summary for grid {grid_id}: {e}")
            return None

    async def get_fs_daily_summary(
        self,
        organization_id: int,
        grid_name: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get daily FS summary showing command executions vs actual state transitions.

        Args:
            organization_id: Organization ID (2 = staff)
            grid_name: Grid name (fuzzy matched)
            start_date: Start date YYYY-MM-DD (inclusive, defaults to yesterday)
            end_date: End date YYYY-MM-DD (inclusive, defaults to today)

        Returns:
            Correlated daily FS summary with discrepancy detection
        """
        try:
            import asyncpg

            conn = await asyncpg.connect(
                host=os.getenv("AUTH_DB_HOST"),
                port=int(os.getenv("AUTH_DB_PORT", "6543")),
                user=os.getenv("AUTH_DB_USER"),
                password=os.getenv("AUTH_DB_PASSWORD"),
                database=os.getenv("AUTH_DB_NAME", "postgres"),
                ssl="require",
                statement_cache_size=0,
            )

            try:
                # Resolve grid name via fuzzy matching (same pattern as get_grid_status)
                if organization_id == STAFF_ORG_ID:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    available_rows = await conn.fetch(
                        """
                        SELECT name FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                available_names = [row["name"] for row in available_rows]
                if grid_name:
                    matched = _find_closest_grid_name(grid_name, available_names)
                    if matched:
                        grid_name = matched

                if not grid_name:
                    return {"error": "Grid name is required"}

                # Get grid_id and timezone
                if organization_id == STAFF_ORG_ID:
                    grid_row = await conn.fetchrow(
                        """
                        SELECT id, name, timezone FROM grids
                        WHERE LOWER(name) = LOWER($1)
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_name,
                    )
                else:
                    grid_row = await conn.fetchrow(
                        """
                        SELECT id, name, timezone FROM grids
                        WHERE LOWER(name) = LOWER($1) AND organization_id = $2
                          AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_name,
                        organization_id,
                    )

                if not grid_row:
                    return {"error": f"Grid '{grid_name}' not found"}

                grid_id = grid_row["id"]
                resolved_name = grid_row["name"]
                grid_tz = grid_row["timezone"] or DEFAULT_TIMEZONE

                # Parse dates (defaults: yesterday to today)
                now_utc = datetime.utcnow()
                if start_date:
                    try:
                        sd = datetime.strptime(start_date, "%Y-%m-%d")
                    except ValueError:
                        return {
                            "error": f"Invalid start_date format: {start_date}. Use YYYY-MM-DD."
                        }
                else:
                    sd = (now_utc - timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )

                if end_date:
                    try:
                        ed = datetime.strptime(end_date, "%Y-%m-%d")
                    except ValueError:
                        return {"error": f"Invalid end_date format: {end_date}. Use YYYY-MM-DD."}
                else:
                    ed = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

                # End date is inclusive, so add 1 day for the query range
                ed_exclusive = ed + timedelta(days=1)

                # Cap at 30 days
                if (ed_exclusive - sd).days > 31:
                    return {"error": "Date range cannot exceed 30 days"}

                # Reject future start dates
                if sd > now_utc:
                    return {"error": "Start date cannot be in the future"}

                # Open TimescaleDB connection
                ts_conn = None
                try:
                    import asyncpg as asyncpg_ts

                    if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD:
                        ts_conn = await asyncpg_ts.connect(
                            host=TIMESCALE_HOST,
                            port=TIMESCALE_PORT,
                            user=TIMESCALE_USER,
                            password=TIMESCALE_PASSWORD,
                            database=TIMESCALE_DATABASE,
                            ssl="require",
                        )

                        fs_result = await self._get_fs_summary_for_grid(
                            auth_conn=conn,
                            ts_conn=ts_conn,
                            grid_id=grid_id,
                            grid_tz=grid_tz,
                            start_date=sd,
                            end_date=ed_exclusive,
                        )
                    else:
                        return {"error": "TimescaleDB not configured"}
                finally:
                    if ts_conn:
                        await ts_conn.close()

                if not fs_result:
                    return {"error": "Failed to get FS summary data"}

                return {
                    "grid_name": resolved_name,
                    "grid_id": grid_id,
                    "date_range": {
                        "start": sd.strftime("%Y-%m-%d"),
                        "end": ed.strftime("%Y-%m-%d"),
                    },
                    "timezone": grid_tz,
                    **fs_result,
                }

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error in get_fs_daily_summary: {e}")
            return {"error": f"Failed to get FS daily summary: {str(e)}"}

