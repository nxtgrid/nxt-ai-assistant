"""Meter listing / consumption query methods for CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

from typing import Any, Dict, Optional

from servers.customer_server.client_base import (
    STAFF_ORG_ID,
    TIMESCALE_DATABASE,
    TIMESCALE_HOST,
    TIMESCALE_PASSWORD,
    TIMESCALE_PORT,
    TIMESCALE_USER,
    logger,
)
from servers.customer_server.formatting import _find_closest_grid_name

from shared.auth import get_auth_service


class ClientMetersMixin:
    async def meter_information(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Get comprehensive information about a meter.

        Args:
            meter_number: Meter number to look up
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with meter information including customer details, connection info,
            grid status, meter power status, credit balance, recent directives, and error history
        """
        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            # Use direct database connection instead of Supabase API to bypass RLS
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Query meters table - schema uses external_reference for meter number
                # Apply organization filter unless staff
                if organization_id != STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, rls_organization_id, rls_grid_id
                        FROM meters
                        WHERE external_reference = $1
                          AND rls_organization_id = $2
                        LIMIT 1
                        """,
                        meter_number,
                        organization_id,
                    )
                else:
                    # Staff (org 2) - no organization filter
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, rls_organization_id, rls_grid_id
                        FROM meters
                        WHERE external_reference = $1
                        LIMIT 1
                        """,
                        meter_number,
                    )

                if not meter_row:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter_row["id"]
                grid_id = meter_row.get("rls_grid_id")

                # Query directives table for latest 5 directives (any type)
                # Cast directive_status and directive_type to text to handle invalid enum values
                directive_rows = await conn.fetch(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                    ORDER BY created_at DESC
                    LIMIT 5
                    """,
                    meter_id,
                )

                directives = []
                for directive in directive_rows:
                    directives.append(
                        {
                            "directive_id": directive["id"],
                            "type": directive.get("directive_type", "unknown"),
                            "status": directive.get("directive_status", "unknown"),
                            "created_at": directive.get("created_at"),
                            "updated_at": directive.get("updated_at"),
                        }
                    )

                # Query for last directive with error status
                # Cast directive_status and directive_type to text to handle invalid enum values
                error_directive_row = await conn.fetchrow(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, directive_error, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                      AND directive_status::text = 'FAILED'
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                last_error_directive = None
                if error_directive_row:
                    last_error_directive = {
                        "directive_id": error_directive_row["id"],
                        "type": error_directive_row.get("directive_type", "unknown"),
                        "status": error_directive_row.get("directive_status", "unknown"),
                        "error": error_directive_row.get("directive_error"),
                        "created_at": error_directive_row.get("created_at"),
                        "updated_at": error_directive_row.get("updated_at"),
                    }

                # Query for last successful token directive
                # Cast directive_status and directive_type to text to handle invalid enum values
                # Note: 'SUCCESSFUL' is the correct enum value, not 'COMPLETED'
                successful_token_row = await conn.fetchrow(
                    """
                    SELECT id, directive_type::text as directive_type, directive_status::text as directive_status, token, created_at, updated_at
                    FROM directives
                    WHERE meter_id = $1
                      AND directive_status::text IN ('COMPLETED', 'SUCCESSFUL')
                      AND directive_type::text = 'TOKEN'
                      AND token IS NOT NULL
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                last_successful_token = None
                if successful_token_row:
                    last_successful_token = {
                        "directive_id": successful_token_row["id"],
                        "token": successful_token_row.get("token"),
                        "token_type": successful_token_row.get("directive_type", "TOKEN"),
                        "created_at": successful_token_row.get("created_at"),
                        "updated_at": successful_token_row.get("updated_at"),
                    }

                try:
                    commissioning_row = await conn.fetchrow(
                        """
                        SELECT mc.created_at, mc.meter_commissioning_status::text AS meter_commissioning_status
                        FROM meter_commissionings mc
                        JOIN metering_hardware_install_sessions mhis
                          ON mc.metering_hardware_install_session_id = mhis.id
                        WHERE mhis.meter_id = $1
                        ORDER BY mc.created_at DESC
                        LIMIT 1
                        """,
                        meter_id,
                    )
                except Exception as commissioning_err:
                    logger.warning(
                        f"Commissioning query failed for meter {meter_number}: {commissioning_err}"
                    )
                    commissioning_row = None

            # Get enriched meter information (customer, connection, grid)
            # Note: This method still uses Supabase client - will need refactoring if it queries auth db
            enriched_meter = await self._get_meter_enriched_info(meter_id, None, organization_id)

            # Get comprehensive grid status if meter has a grid
            full_grid_status = None
            if grid_id:
                full_grid_status = await self.get_grid_status(
                    organization_id=organization_id,
                    grid_id=grid_id,
                )

            # Build response with enriched data
            response = {
                "meter_found": True,
                "meter_number": meter_number,
                "commissioning_date": commissioning_row["created_at"]
                if commissioning_row
                else None,
                "commissioning_status": commissioning_row["meter_commissioning_status"].upper()
                if commissioning_row and commissioning_row["meter_commissioning_status"]
                else None,
                "directives_count": len(directives),
                "directives": directives,
                "last_error_directive": last_error_directive,
                "last_successful_token": last_successful_token,
                "message": (
                    f"Found {len(directives)} directive(s)"
                    if directives
                    else "No directives found for this meter"
                ),
            }

            # Add enriched fields if available (customer, connection, grid, dcu, meter status)
            if "customer_name" in enriched_meter:
                response["customer_name"] = enriched_meter["customer_name"]
            if "connection_type" in enriched_meter:
                response["connection_type"] = enriched_meter["connection_type"]

            # Include full grid status if available
            if full_grid_status and "error" not in full_grid_status:
                response["grid"] = full_grid_status
            elif "grid_name" in enriched_meter:
                # Fallback to simple grid info
                response["grid_name"] = enriched_meter["grid_name"]
                if "grid_status" in enriched_meter:
                    response["grid_status"] = enriched_meter["grid_status"]

            if "dcu_status" in enriched_meter:
                response["dcu_status"] = enriched_meter["dcu_status"]

            # Add meter status fields
            if "is_on" in enriched_meter:
                response["is_on"] = enriched_meter["is_on"]
            if "is_on_updated_at" in enriched_meter:
                response["is_on_updated_at"] = enriched_meter["is_on_updated_at"]
            if "kwh_credit_available" in enriched_meter:
                response["kwh_credit_available"] = enriched_meter["kwh_credit_available"]
            if "kwh_credit_available_updated_at" in enriched_meter:
                response["kwh_credit_available_updated_at"] = enriched_meter[
                    "kwh_credit_available_updated_at"
                ]
            if "power_limit" in enriched_meter:
                response["power_limit"] = enriched_meter["power_limit"]
            # FS command propagation: target vs actual power limit
            if "power_limit_should_be" in enriched_meter:
                target = enriched_meter["power_limit_should_be"]
                actual = enriched_meter.get("power_limit")
                response["power_limit_target"] = target
                response["power_limit_pending"] = (
                    target is not None and actual is not None and actual != target
                )
            if "power_limit_updated_at" in enriched_meter:
                response["power_limit_updated_at"] = enriched_meter["power_limit_updated_at"]
            if "last_seen_at" in enriched_meter:
                response["last_seen_at"] = enriched_meter["last_seen_at"]
            if "connection_metrics" in enriched_meter:
                response["connection_metrics"] = enriched_meter["connection_metrics"]

            # Add 30-day consumption summary from TimescaleDB (non-fatal)
            try:
                if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD:
                    import asyncpg as asyncpg_ts

                    ts_conn = await asyncpg_ts.connect(
                        host=TIMESCALE_HOST,
                        port=TIMESCALE_PORT,
                        user=TIMESCALE_USER,
                        password=TIMESCALE_PASSWORD,
                        database=TIMESCALE_DATABASE,
                        ssl="require",
                    )
                    try:
                        summary = await ts_conn.fetchrow(
                            """
                            SELECT SUM(consumption_kwh) as total_kwh,
                                   AVG(consumption_kwh) as avg_hourly_kwh,
                                   MAX(consumption_kwh) as max_hourly_kwh,
                                   COUNT(DISTINCT date_trunc('day', created_at)) as days_with_data
                            FROM meter_snapshot_1_h
                            WHERE meter_external_reference = $1
                              AND created_at >= NOW() - INTERVAL '30 days'
                            """,
                            meter_number,
                        )
                        if summary and summary["total_kwh"] is not None:
                            response["consumption_30d"] = {
                                "total_kwh": round(summary["total_kwh"], 3),
                                "avg_hourly_kwh": round(summary["avg_hourly_kwh"], 3),
                                "max_hourly_kwh": round(summary["max_hourly_kwh"], 3),
                                "days_with_data": summary["days_with_data"],
                            }
                    finally:
                        await ts_conn.close()
            except Exception as e:
                logger.warning(f"Could not fetch consumption summary: {e}")

            return response

        except Exception as e:
            logger.error(f"Error fetching meter information for {meter_number}: {e}")
            return {"error": f"Failed to fetch meter information: {str(e)}"}

    async def list_grid_meters(
        self,
        grid_name: str,
        organization_id: int,
    ) -> Dict[str, Any]:
        """
        List all non-cabin meters for a grid with power limits and status.

        Args:
            grid_name: Grid name (supports fuzzy matching)
            organization_id: Organization ID (injected by orchestrator)

        Returns:
            Dict with grid name and list of meters with key fields
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Resolve grid name with fuzzy matching
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
                matched_name = _find_closest_grid_name(grid_name, available_names)
                if not matched_name:
                    grid_list = ", ".join(available_names[:10])
                    suffix = (
                        f" (and {len(available_names) - 10} more)"
                        if len(available_names) > 10
                        else ""
                    )
                    return {
                        "error": f"Grid '{grid_name}' not found. Available: {grid_list}{suffix}"
                    }

                corrected_grid_name = (
                    matched_name if matched_name.lower() != grid_name.lower() else None
                )

                # Get grid ID
                grid_row = await conn.fetchrow(
                    "SELECT id, name FROM grids WHERE name = $1 AND deleted_at IS NULL LIMIT 1",
                    matched_name,
                )
                if not grid_row:
                    return {"error": f"Grid '{matched_name}' not found"}

                # Query meters for this grid, excluding cabin meters
                meter_rows = await conn.fetch(
                    """
                    SELECT m.external_reference, m.is_on, m.kwh_credit_available,
                           m.power_limit, m.power_limit_should_be,
                           m.power_limit_hps_mode, m.meter_phase,
                           m.last_seen_at,
                           d.is_online as dcu_online
                    FROM meters m
                    LEFT JOIN dcus d ON m.dcu_id = d.id
                    WHERE m.rls_grid_id = $1
                      AND m.is_cabin_meter IS NOT TRUE
                      AND m.deleted_at IS NULL
                    ORDER BY m.external_reference
                    """,
                    grid_row["id"],
                )

                meters = []
                for row in meter_rows:
                    # Communication status
                    dcu_online = row["dcu_online"]
                    last_seen = row["last_seen_at"]
                    if dcu_online is True and last_seen:
                        comms = "online"
                    elif dcu_online is False:
                        comms = "offline (DCU down)"
                    elif last_seen is None:
                        comms = "never seen"
                    else:
                        comms = "offline"

                    limit_actual = row["power_limit"]
                    limit_target = row["power_limit_should_be"]

                    meters.append(
                        {
                            "meter_number": row["external_reference"],
                            "is_on": row["is_on"],
                            "comms_status": comms,
                            "kwh_credit_available": (
                                round(row["kwh_credit_available"], 2)
                                if row["kwh_credit_available"] is not None
                                else None
                            ),
                            "power_limit_w": limit_actual,
                            "power_limit_target_w": limit_target,
                            "power_limit_pending": (
                                limit_target is not None
                                and limit_actual is not None
                                and limit_actual != limit_target
                            ),
                            "power_limit_hps_mode_w": row["power_limit_hps_mode"],
                            "meter_phase": (
                                str(row["meter_phase"]) if row["meter_phase"] else None
                            ),
                        }
                    )

                result = {
                    "grid_name": matched_name,
                    "meter_count": len(meters),
                    "meters": meters,
                }
                if corrected_grid_name:
                    result["corrected_from"] = grid_name

                return result

        except Exception as e:
            logger.error(f"Error listing grid meters: {e}")
            return {"error": f"Failed to list meters: {str(e)}"}

    async def get_meters_on_pole(
        self,
        pole_reference: str,
        organization_id: int,
        grid_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """List all meters connected to a specific pole.

        Args:
            pole_reference: Pole external reference (printed on pole label)
            organization_id: Organization ID (injected by orchestrator)
            grid_name: Optional grid name to narrow the search

        Returns:
            Dict with pole info and list of meters
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Build pole lookup query with org scoping
                if organization_id == STAFF_ORG_ID:
                    # Staff: see all poles, optionally filter by grid
                    if grid_name:
                        available_names = [
                            r["name"]
                            for r in await conn.fetch(
                                "SELECT name FROM grids WHERE deleted_at IS NULL ORDER BY name"
                            )
                        ]
                        matched_grid = _find_closest_grid_name(grid_name, available_names)
                        if not matched_grid:
                            return {"error": f"Grid '{grid_name}' not found"}
                        pole_rows = await conn.fetch(
                            """
                            SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                            FROM poles p
                            JOIN grids g ON p.grid_id = g.id
                            WHERE p.external_reference = $1 AND g.name = $2
                            """,
                            pole_reference,
                            matched_grid,
                        )
                    else:
                        pole_rows = await conn.fetch(
                            """
                            SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                            FROM poles p
                            LEFT JOIN grids g ON p.grid_id = g.id
                            WHERE p.external_reference = $1
                            """,
                            pole_reference,
                        )
                else:
                    # Customer: only poles in their org's grids
                    pole_rows = await conn.fetch(
                        """
                        SELECT p.id, p.external_reference, p.nickname, g.name as grid_name
                        FROM poles p
                        JOIN grids g ON p.grid_id = g.id
                        WHERE p.external_reference = $1
                          AND g.organization_id = $2
                        """,
                        pole_reference,
                        organization_id,
                    )

                if not pole_rows:
                    return {"error": f"Pole '{pole_reference}' not found"}

                # If multiple poles match (different grids), return all
                all_results = []
                for pole_row in pole_rows:
                    meter_rows = await conn.fetch(
                        """
                        SELECT m.external_reference, m.is_on, m.kwh_credit_available,
                               m.power_limit, m.power_limit_should_be,
                               m.power_limit_hps_mode, m.meter_phase,
                               m.balance, m.last_seen_at,
                               m.power_limit_updated_at,
                               m.power_limit_should_be_updated_at,
                               d.is_online as dcu_online,
                               d.last_online_at as dcu_last_online
                        FROM meters m
                        LEFT JOIN dcus d ON m.dcu_id = d.id
                        WHERE m.pole_id = $1
                          AND m.is_cabin_meter IS NOT TRUE
                          AND m.deleted_at IS NULL
                        ORDER BY m.external_reference
                        """,
                        pole_row["id"],
                    )

                    meters = []
                    for row in meter_rows:
                        # Determine communication status from DCU and last_seen_at
                        dcu_online = row["dcu_online"]
                        last_seen = row["last_seen_at"]
                        if dcu_online is True and last_seen:
                            comms_status = "online"
                        elif dcu_online is False:
                            comms_status = "offline (DCU down)"
                        elif last_seen is None:
                            comms_status = "never seen"
                        else:
                            comms_status = "offline"

                        # Detect pending power limit command (FS propagation)
                        limit_actual = row["power_limit"]
                        limit_target = row["power_limit_should_be"]
                        limit_pending = (
                            limit_target is not None
                            and limit_actual is not None
                            and limit_actual != limit_target
                        )

                        meters.append(
                            {
                                "meter_number": row["external_reference"],
                                "is_on": row["is_on"],
                                "comms_status": comms_status,
                                "last_seen_at": (last_seen.isoformat() if last_seen else None),
                                "kwh_credit_available": (
                                    round(row["kwh_credit_available"], 2)
                                    if row["kwh_credit_available"] is not None
                                    else None
                                ),
                                "balance": (
                                    round(row["balance"], 2) if row["balance"] is not None else None
                                ),
                                "power_limit_w": limit_actual,
                                "power_limit_target_w": limit_target,
                                "power_limit_pending": limit_pending,
                                "power_limit_hps_mode_w": row["power_limit_hps_mode"],
                                "power_limit_updated_at": (
                                    row["power_limit_updated_at"].isoformat()
                                    if row["power_limit_updated_at"]
                                    else None
                                ),
                                "meter_phase": (
                                    str(row["meter_phase"]) if row["meter_phase"] else None
                                ),
                            }
                        )

                    all_results.append(
                        {
                            "pole_reference": pole_row["external_reference"],
                            "pole_nickname": pole_row["nickname"],
                            "grid_name": pole_row["grid_name"],
                            "meter_count": len(meters),
                            "meters": meters,
                        }
                    )

                if len(all_results) == 1:
                    return all_results[0]
                return {"poles_found": len(all_results), "results": all_results}

        except Exception as e:
            logger.error(f"Error getting meters on pole: {e}")
            return {"error": f"Failed to get meters on pole: {str(e)}"}

    async def get_meter_consumption(
        self,
        meter_number: str,
        organization_id: int,
        days_back: int = 30,
    ) -> Dict[str, Any]:
        """Get daily consumption history for a meter from TimescaleDB.

        Args:
            meter_number: Meter external reference
            organization_id: Organization ID (injected by orchestrator)
            days_back: Number of days to look back (default 30, max 365)

        Returns:
            Dict with daily consumption data and a base64 chart image
        """
        days_back = min(max(days_back, 1), 365)

        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            # Verify meter belongs to user's organization
            async with pool.acquire() as conn:
                if organization_id == STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        "SELECT id, external_reference, rls_grid_id FROM meters "
                        "WHERE external_reference = $1 AND deleted_at IS NULL LIMIT 1",
                        meter_number,
                    )
                else:
                    meter_row = await conn.fetchrow(
                        "SELECT id, external_reference, rls_grid_id FROM meters "
                        "WHERE external_reference = $1 AND rls_organization_id = $2 "
                        "AND deleted_at IS NULL LIMIT 1",
                        meter_number,
                        organization_id,
                    )

                if not meter_row:
                    return {"error": f"Meter '{meter_number}' not found or not accessible"}

                # Get grid name for chart title
                grid_row = await conn.fetchrow(
                    "SELECT name FROM grids WHERE id = $1", meter_row["rls_grid_id"]
                )
                grid_name = grid_row["name"] if grid_row else "Unknown"

            # Query TimescaleDB for hourly snapshots aggregated by day
            if not (TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD):
                return {"error": "TimescaleDB not configured"}

            import asyncpg as asyncpg_ts

            ts_conn = await asyncpg_ts.connect(
                host=TIMESCALE_HOST,
                port=TIMESCALE_PORT,
                user=TIMESCALE_USER,
                password=TIMESCALE_PASSWORD,
                database=TIMESCALE_DATABASE,
                ssl="require",
            )

            try:
                rows = await ts_conn.fetch(
                    """
                    SELECT date_trunc('day', created_at) as day,
                           SUM(consumption_kwh) as total_kwh,
                           MAX(consumption_kwh) as max_hourly_kwh,
                           AVG(consumption_kwh) as avg_hourly_kwh,
                           COUNT(*) as sample_hours
                    FROM meter_snapshot_1_h
                    WHERE meter_external_reference = $1
                      AND created_at >= NOW() - make_interval(days => $2)
                    GROUP BY day
                    ORDER BY day
                    """,
                    meter_number,
                    days_back,
                )
            finally:
                await ts_conn.close()

            if not rows:
                return {
                    "meter_number": meter_number,
                    "grid_name": grid_name,
                    "days_back": days_back,
                    "message": "No consumption data found for this period",
                    "daily_data": [],
                }

            # Build daily data
            daily_data = []
            for r in rows:
                daily_data.append(
                    {
                        "date": r["day"].strftime("%Y-%m-%d"),
                        "total_kwh": round(r["total_kwh"], 3)
                        if r["total_kwh"] is not None
                        else None,
                        "max_hourly_kwh": round(r["max_hourly_kwh"], 3)
                        if r["max_hourly_kwh"] is not None
                        else None,
                        "avg_hourly_kwh": round(r["avg_hourly_kwh"], 3)
                        if r["avg_hourly_kwh"] is not None
                        else None,
                        "sample_hours": r["sample_hours"],
                    }
                )

            total_consumption = sum(d["total_kwh"] or 0 for d in daily_data)
            avg_daily = total_consumption / len(daily_data) if daily_data else 0

            # Generate chart
            chart_b64 = self._render_consumption_chart(
                daily_data, meter_number, grid_name, days_back
            )

            result: Dict[str, Any] = {
                "meter_number": meter_number,
                "grid_name": grid_name,
                "days_back": days_back,
                "days_with_data": len(daily_data),
                "total_consumption_kwh": round(total_consumption, 3),
                "avg_daily_kwh": round(avg_daily, 3),
                "daily_data": daily_data,
            }
            if chart_b64:
                result["chart_base64"] = chart_b64
            return result

        except Exception as e:
            logger.error(f"Error getting meter consumption: {e}")
            return {"error": f"Failed to get consumption history: {str(e)}"}

    @staticmethod
    def _render_consumption_chart(
        daily_data: list,
        meter_number: str,
        grid_name: str,
        days_back: int,
    ) -> str:
        """Render a bar+line chart of daily consumption. Returns base64 PNG."""
        try:
            import base64
            import io

            import matplotlib

            matplotlib.use("Agg")
            from datetime import datetime

            import matplotlib.dates as mdates
            import matplotlib.pyplot as plt

            dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in daily_data]
            totals = [d["total_kwh"] for d in daily_data]
            maxes = [d["max_hourly_kwh"] for d in daily_data]

            fig, ax1 = plt.subplots(figsize=(12, 5))
            fig.patch.set_facecolor("#1a1a2e")
            ax1.set_facecolor("#1a1a2e")

            # Bar chart for daily total
            bar_width = 0.8 if len(dates) <= 31 else 0.6
            ax1.bar(dates, totals, width=bar_width, color="#5794F2", alpha=0.8, label="Daily Total")
            ax1.set_ylabel("Daily Total (kWh)", color="#c0c0c0")
            ax1.tick_params(axis="y", colors="#c0c0c0")
            ax1.tick_params(axis="x", colors="#c0c0c0")

            # Line for max hourly on secondary axis
            ax2 = ax1.twinx()
            ax2.plot(
                dates,
                maxes,
                color="#FF7383",
                linewidth=1.5,
                marker=".",
                markersize=4,
                label="Max Hourly",
            )
            ax2.set_ylabel("Max Hourly (kWh)", color="#FF7383")
            ax2.tick_params(axis="y", colors="#FF7383")

            # Formatting
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%b %d"))
            if len(dates) > 14:
                ax1.xaxis.set_major_locator(mdates.WeekdayLocator(interval=1))
            fig.autofmt_xdate(rotation=45)

            avg_daily = sum(totals) / len(totals) if totals else 0
            ax1.axhline(
                y=avg_daily,
                color="#73BF69",
                linestyle="--",
                alpha=0.5,
                label=f"Avg: {avg_daily:.2f} kWh/day",
            )

            title = f"Meter {meter_number} — {grid_name} ({days_back}d)"
            ax1.set_title(title, color="white", fontsize=13, pad=10)

            # Combined legend
            lines1, labels1 = ax1.get_legend_handles_labels()
            lines2, labels2 = ax2.get_legend_handles_labels()
            ax1.legend(
                lines1 + lines2,
                labels1 + labels2,
                loc="upper left",
                facecolor="#2a2a3e",
                edgecolor="#444",
                labelcolor="white",
                fontsize=9,
            )

            ax1.spines["top"].set_visible(False)
            ax2.spines["top"].set_visible(False)
            for spine in ax1.spines.values():
                spine.set_color("#444")
            for spine in ax2.spines.values():
                spine.set_color("#444")
            ax1.grid(axis="y", alpha=0.15, color="white")

            buf = io.BytesIO()
            fig.savefig(
                buf, format="png", dpi=150, bbox_inches="tight", facecolor=fig.get_facecolor()
            )
            plt.close(fig)
            buf.seek(0)
            return base64.b64encode(buf.read()).decode("utf-8")
        except Exception as e:
            logger.warning(f"Failed to render consumption chart: {e}")
            return ""

