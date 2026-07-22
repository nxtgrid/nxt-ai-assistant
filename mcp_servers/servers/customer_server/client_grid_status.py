"""Grid status methods (get_grid_status / get_all_grids_status / close) for
CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from servers.customer_server.client_base import (
    DEFAULT_TIMEZONE,
    PLATFORM_BASE_URL,
    STAFF_ORG_ID,
    STATUS_STABILITY_SNAPSHOT_COUNT,
    TIMESCALE_DATABASE,
    TIMESCALE_HOST,
    TIMESCALE_PASSWORD,
    TIMESCALE_PORT,
    TIMESCALE_USER,
    VRM_BATCH_MAX_CONCURRENT,
    logger,
)
from servers.customer_server.formatting import (
    _find_closest_grid_name,
    _format_downtime_summary_text,
    _format_local_timestamp,
    _weather_to_icon,
    is_stale,
)
from servers.equipment_diagnostics_server.platforms.vrm_platform import InverterVoltage, VRMPlatform

from shared.auth import get_auth_service
from shared.auth.auth_service import MANAGED_GENERATION_COLUMN
from shared.utils.geo import parse_location_geom


class ClientGridStatusMixin:
    async def get_grid_status(
        self,
        organization_id: int,
        grid_name: Optional[str] = None,
        grid_id: Optional[int] = None,
        user_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Get comprehensive status for a grid.

        Args:
            organization_id: Organization ID (injected by orchestrator)
            grid_name: Grid name (required unless grid_id is provided)
            grid_id: Grid ID for direct lookup (optional, used by meter_information)
            user_email: Optional email for logging

        Returns:
            Dict with grid status including HPS, FS, DCU status, weather, and latest_state from TimescaleDB
        """
        try:
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Track if we corrected the grid name for the response
                corrected_grid_name = None

                # If grid_id is provided, lookup directly (no fuzzy matching needed)
                if grid_id:
                    grid_row = await conn.fetchrow(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE id = $1
                          AND is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        LIMIT 1
                        """,
                        grid_id,
                    )
                else:
                    # Fetch available grid names for fuzzy matching
                    if organization_id == STAFF_ORG_ID:
                        # Staff sees all grids
                        available_rows = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name
                            """
                        )
                    else:
                        # Customer sees only their organization's grids
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

                    # If grid_name provided, use fuzzy matching to correct it
                    if grid_name:
                        matched_name = _find_closest_grid_name(grid_name, available_names)
                        if matched_name:
                            if matched_name.lower() != grid_name.lower():
                                corrected_grid_name = matched_name
                                logger.info(
                                    f"Corrected grid name '{grid_name}' -> '{matched_name}'"
                                )
                            grid_name = matched_name
                        # If no match found, grid_name stays as-is and query will fail gracefully

                    # Now query with (potentially corrected) grid_name
                    if not grid_name:
                        # No grid name provided — return error with available grids
                        grid_list = ", ".join(available_names[:10])
                        suffix = (
                            f" (and {len(available_names) - 10} more)"
                            if len(available_names) > 10
                            else ""
                        )
                        return {
                            "error": f"No grid name specified. Please provide a grid name. Available grids: {grid_list}{suffix}"
                        }

                    if organization_id == STAFF_ORG_ID:
                        # Staff - no org filter
                        grid_row = await conn.fetchrow(
                            f"""
                            SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                                   is_fs_on, is_fs_on_updated_at,
                                   current_weather, are_all_dcus_online,
                                   generation_gateway_last_seen_at, timezone,
                                   generation_external_site_id, is_hps_on_threshold_kw,
                                   {MANAGED_GENERATION_COLUMN},
                                   kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                                   kwh_tariff_full_service, kwp_tariff,
                                   location_geom::text as location_wkb
                            FROM grids
                            WHERE LOWER(name) = LOWER($1)
                              AND is_hidden_from_reporting IS NOT TRUE
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            grid_name,
                        )
                    else:
                        # Customer - filter by organization
                        grid_row = await conn.fetchrow(
                            f"""
                            SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                                   is_fs_on, is_fs_on_updated_at,
                                   current_weather, are_all_dcus_online,
                                   generation_gateway_last_seen_at, timezone,
                                   generation_external_site_id, is_hps_on_threshold_kw,
                                   {MANAGED_GENERATION_COLUMN},
                                   kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                                   kwh_tariff_full_service, kwp_tariff,
                                   location_geom::text as location_wkb
                            FROM grids
                            WHERE LOWER(name) = LOWER($1)
                              AND organization_id = $2
                              AND is_hidden_from_reporting IS NOT TRUE
                              AND deleted_at IS NULL
                            LIMIT 1
                            """,
                            grid_name,
                            organization_id,
                        )

                if not grid_row:
                    # Get available grid names to help user
                    if organization_id == STAFF_ORG_ID:
                        available = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name LIMIT 20
                            """,
                        )
                    else:
                        available = await conn.fetch(
                            """
                            SELECT name FROM grids
                            WHERE organization_id = $1
                              AND is_hidden_from_reporting IS NOT TRUE AND deleted_at IS NULL
                            ORDER BY name
                            """,
                            organization_id,
                        )

                    available_names = [row["name"] for row in available]
                    return {
                        "error": f"Grid not found: {grid_name or '(no name specified)'}",
                        "available_grids": available_names,
                    }

                grid = dict(grid_row)
                grid_id = grid["id"]
                grid_tz = grid.get("timezone") or DEFAULT_TIMEZONE

                # Determine if this is a staff request
                is_staff = organization_id == STAFF_ORG_ID

                # Initialize TimescaleDB data (will be populated below)
                latest_state = None
                business_snapshot = None
                ts_hps_on = None
                ts_fs_on = None
                ts_created = None
                ts_is_stale = True  # Default to stale if no TimescaleDB data

                # Initialize VRM real-time metrics
                vrm_battery_soc: float | None = None
                vrm_battery_current: float | None = None
                vrm_solar_power_w: float | None = None
                vrm_grid_consumption_w: float | None = None
                vrm_is_on = None  # VRM voltage check for ON/OFF determination
                vrm_power_kw = None  # VRM power for HPS/Isolated determination
                vrm_l1_power_w: float | None = None
                vrm_l2_power_w: float | None = None
                vrm_l3_power_w: float | None = None
                vrm_platform = None

                # Fetch VRM real-time metrics BEFORE TimescaleDB so latest_state can use them
                try:
                    vrm_platform = VRMPlatform()
                    await vrm_platform.initialize()
                    site_id = grid.get("generation_external_site_id")
                    if site_id:
                        sid = str(site_id)
                        try:
                            vrm_results = await asyncio.gather(
                                vrm_platform.get_current_inverter_voltage(sid),
                                vrm_platform.get_current_battery_status(sid),
                                vrm_platform.get_current_pv_power(sid),
                                vrm_platform.get_current_grid_status(sid),
                                return_exceptions=True,
                            )

                            # Inverter voltage/power (total + per-phase)
                            voltage = vrm_results[0]
                            if not isinstance(voltage, (Exception, BaseException)):
                                if not voltage.error:  # type: ignore[union-attr]
                                    # Check if VRM data is stale (gateway >30 min old)
                                    vrm_data_ts = voltage.data_timestamp  # type: ignore[union-attr]
                                    if vrm_data_ts and (
                                        datetime.utcnow() - vrm_data_ts
                                    ) > timedelta(minutes=30):
                                        pass  # Leave vrm_is_on as None → falls to Unknown
                                    else:
                                        vrm_is_on = voltage.is_producing  # type: ignore[union-attr]
                                    vrm_power_kw = voltage.total_power_kw  # type: ignore[union-attr]
                                    vrm_l1_power_w = voltage.l1_power_w  # type: ignore[union-attr]
                                    vrm_l2_power_w = voltage.l2_power_w  # type: ignore[union-attr]
                                    vrm_l3_power_w = voltage.l3_power_w  # type: ignore[union-attr]

                            # Battery SOC and current
                            battery = vrm_results[1]
                            if not isinstance(battery, (Exception, BaseException)):
                                vrm_battery_soc = battery.soc_percent  # type: ignore[union-attr]
                                vrm_battery_current = battery.current_a  # type: ignore[union-attr]

                            # PV/Solar power
                            pv = vrm_results[2]
                            if not isinstance(pv, (Exception, BaseException)):
                                vrm_solar_power_w = pv.total_power_w  # type: ignore[union-attr]

                            # Grid consumption (inverter output to customers)
                            grid_status_vrm = vrm_results[3]
                            if not isinstance(grid_status_vrm, (Exception, BaseException)):
                                vrm_grid_consumption_w = grid_status_vrm.total_power_w  # type: ignore[union-attr]

                        except Exception as vrm_rt_err:
                            logger.warning(f"VRM real-time fetch failed: {vrm_rt_err}")
                except Exception as vrm_init_err:
                    logger.warning(f"VRM init failed for {grid['name']}: {vrm_init_err}")

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
                        try:
                            # Majority voting query for HPS/FS status stability
                            # Uses last 3 snapshots within 1 hour to prevent flapping
                            ts_row = await ts_conn.fetchrow(
                                """
                                WITH recent_snapshots AS (
                                    SELECT
                                        created_at, is_fs_active, is_hps_on, should_fs_be_on,
                                        battery_soc_bs_pct, battery_current_bc_a,
                                        grid_consumption_total_kwh,
                                        pv_energy_to_battery_pb_kwh,
                                        pv_energy_to_grid_pc_kwh,
                                        ROW_NUMBER() OVER (ORDER BY created_at DESC) as rn
                                    FROM grid_energy_snapshot_15_min
                                    WHERE grid_id = $1
                                      AND created_at >= NOW() - INTERVAL '1 hour'
                                )
                                SELECT
                                    MAX(created_at) as created_at,
                                    -- Majority voting for status stability
                                    (COUNT(*) FILTER (WHERE is_fs_active = true) * 2 > COUNT(*)) as is_fs_active,
                                    (COUNT(*) FILTER (WHERE is_hps_on = true) * 2 > COUNT(*)) as is_hps_on,
                                    -- Latest values for other fields
                                    (ARRAY_AGG(should_fs_be_on ORDER BY created_at DESC))[1] as should_fs_be_on,
                                    (ARRAY_AGG(battery_soc_bs_pct ORDER BY created_at DESC))[1] as battery_soc_bs_pct,
                                    (ARRAY_AGG(battery_current_bc_a ORDER BY created_at DESC))[1] as battery_current_bc_a,
                                    (ARRAY_AGG(grid_consumption_total_kwh ORDER BY created_at DESC))[1] as grid_consumption_total_kwh,
                                    (ARRAY_AGG(pv_energy_to_battery_pb_kwh ORDER BY created_at DESC))[1] as pv_energy_to_battery_pb_kwh,
                                    (ARRAY_AGG(pv_energy_to_grid_pc_kwh ORDER BY created_at DESC))[1] as pv_energy_to_grid_pc_kwh,
                                    COUNT(*) as snapshot_count
                                FROM recent_snapshots
                                WHERE rn <= $2
                                """,
                                grid_id,
                                STATUS_STABILITY_SNAPSHOT_COUNT,
                            )

                            if ts_row and ts_row["created_at"]:
                                # Format timestamp and check staleness based on TimescaleDB created_at
                                ts_created = ts_row["created_at"]
                                if ts_created and ts_created.tzinfo is None:
                                    ts_created = ts_created.replace(tzinfo=timezone.utc)

                                # Use TimescaleDB created_at for staleness (30 min threshold)
                                ts_is_stale = is_stale(ts_created)

                                # Get HPS/FS status from TimescaleDB (majority voted)
                                ts_hps_on = ts_row["is_hps_on"]
                                ts_fs_on = ts_row["is_fs_active"]

                                # Determine battery status from VRM current (preferred) or TimescaleDB
                                # VRM convention: positive current = charging, negative = discharging
                                battery_current = vrm_battery_current
                                if battery_current is None:
                                    battery_current = ts_row["battery_current_bc_a"]
                                if battery_current is None:
                                    battery_status = "unknown"
                                elif battery_current > 0:
                                    battery_status = "charging"
                                elif battery_current < 0:
                                    battery_status = "discharging"
                                else:
                                    battery_status = "idle"

                                # Calculate total solar production (to battery + to grid)
                                pv_to_battery = ts_row["pv_energy_to_battery_pb_kwh"] or 0
                                pv_to_grid = ts_row["pv_energy_to_grid_pc_kwh"] or 0
                                total_solar_kwh = pv_to_battery + pv_to_grid

                                # Use VRM real-time values with TimescaleDB fallback
                                latest_state = {
                                    "timestamp": _format_local_timestamp(ts_created, grid_tz),
                                    "is_stale": ts_is_stale,
                                    "is_fs_active": ts_fs_on,
                                    "is_hps_on": ts_hps_on,
                                    "should_fs_be_on": ts_row["should_fs_be_on"],
                                    "battery_soc_pct": (
                                        vrm_battery_soc
                                        if vrm_battery_soc is not None
                                        else ts_row["battery_soc_bs_pct"]
                                    ),
                                    "battery_current_a": battery_current,
                                    "battery_status": battery_status,
                                    "consumption_w": (
                                        vrm_grid_consumption_w
                                        if vrm_grid_consumption_w is not None
                                        else None
                                    ),
                                    "consumption_kwh": ts_row["grid_consumption_total_kwh"],
                                    "solar_power_w": vrm_solar_power_w,
                                    "solar_production_kwh": (
                                        total_solar_kwh if total_solar_kwh else None
                                    ),
                                    "data_source": (
                                        "vrm" if vrm_battery_soc is not None else "timescale"
                                    ),
                                }
                            else:
                                # No TimescaleDB data - use VRM real-time data if available
                                vrm_batt_status = "unknown"
                                if vrm_battery_current is not None:
                                    if vrm_battery_current > 0:
                                        vrm_batt_status = "charging"
                                    elif vrm_battery_current < 0:
                                        vrm_batt_status = "discharging"
                                    else:
                                        vrm_batt_status = "idle"

                                latest_state = {
                                    "timestamp": None,
                                    "is_stale": True,
                                    "is_fs_active": None,
                                    "is_hps_on": None,
                                    "should_fs_be_on": None,
                                    "battery_soc_pct": vrm_battery_soc,
                                    "battery_current_a": vrm_battery_current,
                                    "battery_status": vrm_batt_status,
                                    "consumption_w": vrm_grid_consumption_w,
                                    "consumption_kwh": None,
                                    "solar_power_w": vrm_solar_power_w,
                                    "solar_production_kwh": None,
                                    "data_source": "vrm" if vrm_battery_soc is not None else None,
                                }

                            # Fetch business snapshot for the most recent day with complete data
                            # Filter by grid_name IS NOT NULL to skip incomplete/partial rows
                            business_row = await ts_conn.fetchrow(
                                """
                                SELECT created_at, kwp, kwh,
                                       residential_connection_count, commercial_connection_count,
                                       public_connection_count, lifeline_connection_count,
                                       total_connection_count, total_meter_count,
                                       three_phase_meter_count, total_consumption_kwh,
                                       battery_modules_on_count, battery_modules_off_count,
                                       energy_topup_revenue, monthly_rental, daily_rental,
                                       women_impacted_count, connection_fee_revenue,
                                       fs_single_phase_connection_fee, hps_single_phase_connection_fee,
                                       fs_three_phase_connection_fee
                                FROM grid_business_snapshot_1_d
                                WHERE grid_id = $1
                                  AND grid_name IS NOT NULL
                                ORDER BY created_at DESC
                                LIMIT 1
                                """,
                                grid_id,
                            )

                            business_snapshot = None
                            logger.info(
                                f"Business snapshot query for grid_id={grid_id}: "
                                f"found={business_row is not None}"
                            )
                            if business_row:
                                bs_created = business_row["created_at"]
                                if bs_created and bs_created.tzinfo is None:
                                    bs_created = bs_created.replace(tzinfo=timezone.utc)

                                # Build business snapshot (filtered by mode)
                                # Calculate non-residential (commercial + public)
                                non_residential = (
                                    business_row["commercial_connection_count"] or 0
                                ) + (business_row["public_connection_count"] or 0)
                                three_phase = business_row["three_phase_meter_count"] or 0

                                business_snapshot = {
                                    "snapshot_date": (
                                        bs_created.strftime("%Y-%m-%d") if bs_created else None
                                    ),
                                    "capacity": {
                                        "kwp": business_row["kwp"]
                                        if grid.get(MANAGED_GENERATION_COLUMN)
                                        else None,
                                        "kwh": business_row["kwh"],
                                    },
                                    "connections": {
                                        "residential": business_row["residential_connection_count"],
                                        "non_residential": non_residential,
                                        "total": business_row["total_connection_count"],
                                    },
                                    "meters": {
                                        "total": business_row["total_meter_count"],
                                    },
                                    "consumption_kwh": business_row["total_consumption_kwh"],
                                    "battery_modules": {
                                        "on": business_row["battery_modules_on_count"],
                                        "off": business_row["battery_modules_off_count"],
                                    },
                                }

                                # Only include 3-phase count if non-zero
                                if three_phase > 0:
                                    business_snapshot["meters"]["three_phase"] = three_phase

                                # Staff-only fields
                                if is_staff:
                                    business_snapshot["revenue"] = {
                                        "energy_topup": business_row["energy_topup_revenue"],
                                        "connection_fee": business_row["connection_fee_revenue"],
                                    }
                                    business_snapshot["rental"] = {
                                        "monthly": business_row["monthly_rental"],
                                        "daily": business_row["daily_rental"],
                                    }
                                    business_snapshot["women_impacted_count"] = business_row[
                                        "women_impacted_count"
                                    ]
                                    business_snapshot["connection_fees"] = {
                                        "fs_single_phase": business_row[
                                            "fs_single_phase_connection_fee"
                                        ],
                                        "hps_single_phase": business_row[
                                            "hps_single_phase_connection_fee"
                                        ],
                                        "fs_three_phase": business_row[
                                            "fs_three_phase_connection_fee"
                                        ],
                                    }
                                # Note: *issue_count columns are excluded for both modes

                            # Get yesterday's ON hours while we have the ts_conn
                            yesterday_on = await self._get_yesterday_on_hours(ts_conn, grid_id)

                            # FS daily summary for yesterday + today
                            now_utc = datetime.utcnow()
                            fs_start = (now_utc - timedelta(days=1)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            )
                            fs_end = (now_utc + timedelta(days=1)).replace(
                                hour=0, minute=0, second=0, microsecond=0
                            )
                            fs_daily = await self._get_fs_summary_for_grid(
                                auth_conn=conn,
                                ts_conn=ts_conn,
                                grid_id=grid_id,
                                grid_tz=grid_tz,
                                start_date=fs_start,
                                end_date=fs_end,
                            )
                        finally:
                            await ts_conn.close()
                    else:
                        logger.warning("TimescaleDB not configured, skipping latest_state")
                        yesterday_on = {"on_hours": None, "error": "TimescaleDB not configured"}
                        fs_daily = None
                except Exception as ts_err:
                    logger.error(f"TimescaleDB query error: {ts_err}")
                    # Continue without latest_state on error
                    yesterday_on = {"on_hours": None, "error": str(ts_err)}
                    fs_daily = None

                # Get FS schedule (uses auth DB connection, pass current FS state from TimescaleDB)
                fs_schedule = await self._get_fs_schedule(conn, grid_id, ts_fs_on)

                # Get last FS command delivery stats
                last_fs_delivery = await self._get_last_fs_delivery(conn, grid_id)

                # Get 24h downtime analysis and live weather from VRM
                # (vrm_platform already initialized above for real-time metrics)
                downtime_24h = None
                live_weather = None
                equipment_note = None  # Note if equipment data unavailable
                try:
                    # Reuse vrm_platform if already initialized, otherwise create new
                    if not vrm_platform:
                        vrm_platform = VRMPlatform()
                        await vrm_platform.initialize()

                    # Fetch downtime and weather in parallel
                    downtime_task = vrm_platform.get_downtime_summary(
                        grid["name"], hours=24, timeout_seconds=3.0
                    )
                    weather_task = vrm_platform.get_site_weather(grid["name"], timeout_seconds=3.0)
                    results = await asyncio.gather(
                        downtime_task, weather_task, return_exceptions=True
                    )
                    downtime_result = results[0]
                    weather_result = results[1]

                    # Process downtime result
                    if not isinstance(downtime_result, Exception):
                        if downtime_result.error is None:  # type: ignore[union-attr]
                            downtime_24h = downtime_result.to_dict()  # type: ignore[union-attr]
                            downtime_24h["summary_text"] = _format_downtime_summary_text(
                                downtime_24h, tz_name=grid_tz
                            )
                        elif "not managed" in (downtime_result.error or ""):  # type: ignore[union-attr]
                            # Grid's generation is not managed by the operator
                            equipment_note = downtime_result.error  # type: ignore[union-attr]
                    else:
                        logger.warning(f"Downtime fetch failed: {downtime_result}")

                    # Process weather result
                    if not isinstance(weather_result, Exception):
                        if weather_result.error is None:  # type: ignore[union-attr]
                            live_weather = weather_result.to_dict()  # type: ignore[union-attr]
                    else:
                        logger.warning(f"Weather fetch failed: {weather_result}")

                except Exception as vrm_err:
                    logger.warning(f"VRM fetch failed for {grid['name']}: {vrm_err}")

                # Build response - use TimescaleDB for HPS/FS status with staleness check
                location = parse_location_geom(grid.get("location_wkb"))
                result = {
                    "grid_name": grid["name"],
                    "grid_id": grid_id,
                    "platform_url": f"{PLATFORM_BASE_URL}/grid/{grid_id}/"
                    if PLATFORM_BASE_URL
                    else None,
                    "timezone": grid_tz,
                }
                if location:
                    result["location"] = location

                # Add note if we corrected the grid name
                if corrected_grid_name:
                    result["name_corrected_from"] = corrected_grid_name

                # Determine service status using VRM voltage/power with TimescaleDB fallback
                # - VRM voltage determines ON/OFF (if available)
                # - VRM power vs threshold determines HPS (if ON), with TimescaleDB fallback
                # - FS status always from TimescaleDB (VRM doesn't track FS)
                # - "Likely Isolated" = ON but power below HPS threshold
                # NOTE: Keep this logic consistent with /grids command in list_all_grids_status()
                hps_threshold_kw = grid.get("is_hps_on_threshold_kw")

                if vrm_is_on is None:
                    # No VRM data - fall back to TimescaleDB-only logic
                    if ts_is_stale or (ts_fs_on is None and ts_hps_on is None):
                        service_status = "Unknown"
                    elif ts_fs_on:
                        service_status = "FS"
                    elif ts_hps_on:
                        service_status = "HPS"
                    else:
                        service_status = "Down"
                elif vrm_is_on is False:
                    # VRM says grid is OFF (no inverter voltage)
                    service_status = "Down"
                else:
                    # VRM says grid is ON - determine HPS using VRM power vs threshold
                    # with TimescaleDB fallback if VRM power unavailable
                    if vrm_power_kw is not None and hps_threshold_kw is not None:
                        vrm_hps_on = vrm_power_kw >= float(hps_threshold_kw)
                    else:
                        # Fallback to TimescaleDB if VRM power unavailable
                        vrm_hps_on = ts_hps_on

                    # Power below threshold = Isolated, even if meters report FS
                    if vrm_hps_on is False:
                        service_status = "Likely Isolated"
                    elif ts_fs_on is True:
                        service_status = "FS"
                    elif vrm_hps_on is True:
                        service_status = "HPS"
                    else:
                        service_status = "Likely Isolated"

                # Service status section: current state + yesterday's on hours + downtime
                result["service_status"] = {
                    "service": service_status,
                    "inverter_power_kw": vrm_power_kw,  # VRM total output power
                    "inverter_l1_power_kw": (
                        round(vrm_l1_power_w / 1000, 3) if vrm_l1_power_w is not None else None
                    ),
                    "inverter_l2_power_kw": (
                        round(vrm_l2_power_w / 1000, 3) if vrm_l2_power_w is not None else None
                    ),
                    "inverter_l3_power_kw": (
                        round(vrm_l3_power_w / 1000, 3) if vrm_l3_power_w is not None else None
                    ),
                    "updated_at": _format_local_timestamp(ts_created, grid_tz),
                    "is_stale": ts_is_stale,
                    "yesterday_on_hours": yesterday_on.get("on_hours"),
                    "downtime_24h": downtime_24h,
                }

                # Add note if equipment data is unavailable (non-managed grid)
                if equipment_note:
                    result["equipment_note"] = equipment_note

                # Query DCU status directly from dcus table (not cached grids.are_all_dcus_online)
                # This ensures consistency with /grids command
                dcu_rows = await conn.fetch(
                    """
                    SELECT external_reference, is_online, last_online_at
                    FROM dcus
                    WHERE grid_id = $1
                    ORDER BY external_reference
                    """,
                    grid_id,
                )

                dcu_total = len(dcu_rows)
                dcu_online = sum(1 for row in dcu_rows if row["is_online"])
                all_online = dcu_online == dcu_total and dcu_total > 0

                # Build visual: 📶📶🅇 for 2 online, 1 offline (consistent with /grids)
                if dcu_total > 0:
                    dcu_visual = "📶" * dcu_online + "🅇" * (dcu_total - dcu_online)
                else:
                    dcu_visual = "N/A"

                # Build offline DCUs list
                hardware_url = (
                    f"{PLATFORM_BASE_URL}/grid/{grid_id}/hardware/" if PLATFORM_BASE_URL else None
                )
                offline_dcus = [
                    {
                        "name": row["external_reference"],
                        "last_online_at": _format_local_timestamp(row["last_online_at"], grid_tz),
                        "status_is_stale": is_stale(row["last_online_at"]),
                    }
                    for row in dcu_rows
                    if not row["is_online"]
                ]

                result["dcus"] = {
                    "all_online": all_online,
                    "online": dcu_online,
                    "total": dcu_total,
                    "visual": dcu_visual,
                    "offline_dcus": offline_dcus,
                }
                if offline_dcus:
                    result["dcus"]["hardware_url"] = hardware_url

                # Use live weather from Open-Meteo if available, fallback to DB weather
                weather_icon = _weather_to_icon(grid["current_weather"])
                result["live_weather"] = (
                    live_weather
                    if live_weather
                    else {
                        "icon": weather_icon,
                        "description": grid["current_weather"] or "Unknown",
                        "display": weather_icon,
                    }
                )
                # Keep legacy weather field for backward compatibility
                result["weather"] = grid["current_weather"]
                result["latest_state"] = latest_state
                result["business_snapshot"] = business_snapshot

                # Tariff (Naira per kWh)
                result["kwh_tariff_naira"] = grid.get("kwh_tariff_essential_service")

                # FS Detail section: FS schedule + last FS command delivery
                result["fs_detail"] = {
                    "fs_schedule": fs_schedule,
                    "last_fs_command": last_fs_delivery,
                    "daily_summary": fs_daily,
                }

                # Only include data_freshness when there's an issue (stale data)
                if ts_is_stale:
                    result["data_freshness"] = {
                        "warning": "Data may be stale",
                        "staleness_threshold_minutes": 30,
                    }

                logger.info(
                    f"Grid status for {grid['name']}: HPS={ts_hps_on}, FS={ts_fs_on}, "
                    f"business_snapshot={'present' if business_snapshot else 'None'}, "
                    f"fs_schedule={fs_schedule.get('summary', 'N/A') if fs_schedule else 'None'}, "
                    f"yesterday_on={yesterday_on.get('on_hours', 'N/A')}h"
                )
                return result

        except Exception as e:
            logger.error(f"Error getting grid status: {e}")
            return {"error": f"Failed to get grid status: {str(e)}"}

    async def get_all_grids_status(self, organization_id: int) -> Dict[str, Any]:
        """
        Get status of all grids accessible to the user, grouped by operational status.

        Args:
            organization_id: Organization ID (2 = staff sees all, others see their org only)

        Returns:
            Dict with grids grouped by status category with icons
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
                # Staff sees all grids, others filtered by organization
                if organization_id == STAFF_ORG_ID:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        ORDER BY name
                        """
                    )
                else:
                    rows = await conn.fetch(
                        f"""
                        SELECT id, name, organization_id, is_hps_on, is_hps_on_updated_at,
                               is_fs_on, is_fs_on_updated_at, timezone,
                               current_weather, are_all_dcus_online,
                               generation_gateway_last_seen_at, generation_external_site_id,
                               is_hps_on_threshold_kw, {MANAGED_GENERATION_COLUMN},
                               kwh_tariff, kwh_tariff_essential_service,  -- reference billing schema; adapt column names for your deployment
                               kwh_tariff_full_service, kwp_tariff,
                               location_geom::text as location_wkb
                        FROM grids
                        WHERE organization_id = $1
                          AND is_hidden_from_reporting IS NOT TRUE
                          AND deleted_at IS NULL
                        ORDER BY name
                        """,
                        organization_id,
                    )

                # Categorize grids by status
                # Use 30-minute threshold for staleness (same as /grid command)
                grids_staleness_threshold = timedelta(minutes=30)

                def is_grids_stale(timestamp: Optional[datetime]) -> bool:
                    """Check staleness with 30-minute threshold."""
                    if timestamp is None:
                        return True
                    now = datetime.now(timezone.utc)
                    if timestamp.tzinfo is None:
                        timestamp = timestamp.replace(tzinfo=timezone.utc)
                    return bool((now - timestamp) > grids_staleness_threshold)

                # Fetch latest state from TimescaleDB for all grids (batch query)
                grid_ids = [row["id"] for row in rows]
                ts_data_map: Dict[int, Dict[str, Any]] = {}
                fs_hours_map: Dict[int, float] = {}

                try:
                    import asyncpg as asyncpg_ts

                    if TIMESCALE_HOST and TIMESCALE_USER and TIMESCALE_PASSWORD and grid_ids:
                        ts_conn = await asyncpg_ts.connect(
                            host=TIMESCALE_HOST,
                            port=TIMESCALE_PORT,
                            user=TIMESCALE_USER,
                            password=TIMESCALE_PASSWORD,
                            database=TIMESCALE_DATABASE,
                            ssl="require",
                        )
                        try:
                            # Majority voting query: average over last 3 snapshots within 1 hour
                            # This prevents status flapping when power oscillates around threshold
                            ts_rows = await ts_conn.fetch(
                                """
                                WITH recent_snapshots AS (
                                    SELECT
                                        grid_id, created_at, is_fs_active, is_hps_on,
                                        should_fs_be_on, battery_soc_bs_pct, battery_current_bc_a,
                                        grid_consumption_total_kwh,
                                        ROW_NUMBER() OVER (PARTITION BY grid_id ORDER BY created_at DESC) as rn
                                    FROM grid_energy_snapshot_15_min
                                    WHERE grid_id = ANY($1)
                                      AND created_at >= NOW() - INTERVAL '1 hour'
                                )
                                SELECT
                                    grid_id,
                                    MAX(created_at) as created_at,
                                    -- Majority voting: true if more than half show true
                                    (COUNT(*) FILTER (WHERE is_fs_active = true) * 2 > COUNT(*)) as is_fs_active,
                                    (COUNT(*) FILTER (WHERE is_hps_on = true) * 2 > COUNT(*)) as is_hps_on,
                                    -- Latest values for other fields
                                    (ARRAY_AGG(should_fs_be_on ORDER BY created_at DESC))[1] as should_fs_be_on,
                                    (ARRAY_AGG(battery_soc_bs_pct ORDER BY created_at DESC))[1] as battery_soc_bs_pct,
                                    (ARRAY_AGG(battery_current_bc_a ORDER BY created_at DESC))[1] as battery_current_bc_a,
                                    (ARRAY_AGG(grid_consumption_total_kwh ORDER BY created_at DESC))[1] as grid_consumption_total_kwh,
                                    COUNT(*) as snapshot_count
                                FROM recent_snapshots
                                WHERE rn <= $2
                                GROUP BY grid_id
                                """,
                                grid_ids,
                                STATUS_STABILITY_SNAPSHOT_COUNT,
                            )

                            for ts_row in ts_rows:
                                gid = ts_row["grid_id"]
                                # Determine battery status
                                battery_current = ts_row["battery_current_bc_a"]
                                if battery_current is None:
                                    battery_status = "unknown"
                                elif battery_current > 0:
                                    battery_status = "charging"
                                elif battery_current < 0:
                                    battery_status = "discharging"
                                else:
                                    battery_status = "idle"

                                # Store raw timestamp for later conversion
                                ts_created = ts_row["created_at"]
                                if ts_created and ts_created.tzinfo is None:
                                    ts_created = ts_created.replace(tzinfo=timezone.utc)

                                ts_data_map[gid] = {
                                    "timestamp_utc": ts_created,  # Raw datetime for per-grid tz conversion
                                    "is_fs_active": ts_row["is_fs_active"],
                                    "is_hps_on": ts_row["is_hps_on"],
                                    "should_fs_be_on": ts_row["should_fs_be_on"],
                                    "battery_soc_pct": ts_row["battery_soc_bs_pct"],
                                    "battery_status": battery_status,
                                    "consumption_kwh": ts_row["grid_consumption_total_kwh"],
                                }

                            # Batch FS ON hours for last 24h
                            fs_hours_rows = await ts_conn.fetch(
                                """
                                SELECT
                                    grid_id,
                                    COUNT(*) FILTER (WHERE is_fs_active = true) AS fs_on_slots,
                                    COUNT(*) AS total_slots
                                FROM grid_energy_snapshot_15_min
                                WHERE grid_id = ANY($1)
                                  AND created_at >= NOW() - INTERVAL '24 hours'
                                GROUP BY grid_id
                                """,
                                grid_ids,
                            )
                            for fsr in fs_hours_rows:
                                fs_hours_map[fsr["grid_id"]] = round(
                                    (fsr["fs_on_slots"] or 0) * 0.25, 1
                                )
                        finally:
                            await ts_conn.close()
                except Exception as ts_err:
                    logger.error(f"TimescaleDB batch query error: {ts_err}")
                    # Continue without TimescaleDB data

                # Fetch DCU counts per grid (online vs total)
                dcu_counts_map: Dict[int, Dict[str, int]] = {}
                try:
                    dcu_rows = await conn.fetch(
                        """
                        SELECT grid_id,
                               COUNT(*) as total,
                               COUNT(*) FILTER (WHERE is_online = true) as online
                        FROM dcus
                        WHERE grid_id = ANY($1)
                        GROUP BY grid_id
                        """,
                        grid_ids,
                    )
                    for dcu_row in dcu_rows:
                        dcu_counts_map[dcu_row["grid_id"]] = {
                            "online": dcu_row["online"],
                            "total": dcu_row["total"],
                        }
                except Exception as dcu_err:
                    logger.error(f"DCU counts query error: {dcu_err}")

                # Batch last FS delivery per grid (last 24h)
                fs_delivery_map: Dict[int, Dict[str, Any]] = {}
                try:
                    fs_del_rows = await conn.fetch(
                        """
                        WITH latest_exec AS (
                            SELECT
                                db.grid_id, dbe.successful_count, dbe.total_count,
                                db.fs_command, dbe.created_at,
                                ROW_NUMBER() OVER (
                                    PARTITION BY db.grid_id ORDER BY dbe.id DESC
                                ) as rn
                            FROM directive_batch_executions dbe
                            JOIN directive_batches db ON dbe.directive_batch_id = db.id
                            WHERE db.grid_id = ANY($1)
                              AND db.fs_command IS NOT NULL
                              AND dbe.created_at >= NOW() - INTERVAL '24 hours'
                        )
                        SELECT grid_id, successful_count, total_count, fs_command, created_at
                        FROM latest_exec WHERE rn = 1
                        """,
                        grid_ids,
                    )
                    for fdr in fs_del_rows:
                        total = fdr["total_count"] or 0
                        successful = fdr["successful_count"] or 0
                        delivery_pct = round((successful / total * 100), 1) if total > 0 else 0
                        fs_delivery_map[fdr["grid_id"]] = {
                            "command": fdr["fs_command"],
                            "delivery_pct": delivery_pct,
                            "successful": successful,
                            "total": total,
                        }
                except Exception as fs_del_err:
                    logger.error(f"FS delivery batch query error: {fs_del_err}")

                # Fetch 24h downtime, weather, and inverter voltage for all grids (parallel)
                downtime_map: Dict[str, Dict[str, Any]] = {}
                weather_map: Dict[str, Dict[str, Any]] = {}
                voltage_map: Dict[str, InverterVoltage] = {}  # site_id -> voltage
                equipment_note_map: Dict[str, str] = {}  # Track non-managed grid notes

                # Build map of grid_id -> VRM site_id for voltage lookup
                grid_site_map: Dict[int, str] = {}
                for row in rows:
                    site_id = row.get("generation_external_site_id")
                    if site_id:
                        grid_site_map[row["id"]] = str(site_id)

                try:
                    grid_names_for_downtime = [row["name"] for row in rows]
                    vrm_platform = VRMPlatform()
                    await vrm_platform.initialize()

                    # Build task list: downtime, weather, and voltage (if we have site IDs)
                    downtime_task = vrm_platform.get_batch_downtime_summary(
                        grid_names=grid_names_for_downtime,
                        hours=24,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_grid=3.0,
                    )
                    weather_task = vrm_platform.get_batch_weather(
                        grid_names=grid_names_for_downtime,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_grid=3.0,
                    )

                    # Fetch inverter voltage to determine ON/OFF status
                    site_ids_list = list(set(grid_site_map.values()))
                    voltage_task = vrm_platform.get_batch_inverter_voltage(
                        site_ids=site_ids_list,
                        max_concurrent=VRM_BATCH_MAX_CONCURRENT,
                        timeout_per_site=10.0,
                    )

                    results = await asyncio.gather(
                        downtime_task, weather_task, voltage_task, return_exceptions=True
                    )
                    downtime_results = results[0]
                    weather_results = results[1]
                    voltage_results = results[2]

                    # Process downtime results
                    if not isinstance(downtime_results, Exception):
                        for grid_name, summary in downtime_results.items():  # type: ignore[union-attr]
                            if summary.error is None:
                                downtime_map[grid_name] = summary.to_dict()
                            elif "not managed" in (summary.error or ""):
                                # Track non-managed grids with explanatory note
                                equipment_note_map[grid_name] = summary.error
                        logger.info(
                            f"Downtime fetch: {len(downtime_map)}/{len(grid_names_for_downtime)} grids"
                        )
                    else:
                        logger.error(f"Downtime fetch error: {downtime_results}")

                    # Process weather results
                    if not isinstance(weather_results, Exception):
                        for grid_name, weather in weather_results.items():  # type: ignore[union-attr]
                            if weather.error is None:
                                weather_map[grid_name] = weather.to_dict()
                        logger.info(
                            f"Weather fetch: {len(weather_map)}/{len(grid_names_for_downtime)} grids"
                        )
                    else:
                        logger.error(f"Weather fetch error: {weather_results}")

                    # Process voltage results
                    if not isinstance(voltage_results, Exception):
                        voltage_map = voltage_results  # type: ignore[assignment]
                        online_count = sum(
                            1 for v in voltage_map.values() if v.is_producing and not v.error
                        )
                        logger.info(
                            f"Voltage fetch: {len(voltage_map)} sites, {online_count} producing"
                        )
                    else:
                        logger.error(f"Voltage fetch error: {voltage_results}")

                except Exception as vrm_err:
                    logger.error(f"VRM fetch error: {vrm_err}")
                    # Continue without VRM data

                fs_on_grids = []
                hps_on_grids = []
                likely_isolated_grids = []
                off_grids = []
                unknown_grids = []

                for row in rows:
                    grid = dict(row)
                    grid_id = grid["id"]

                    # Get TimescaleDB data for this grid
                    ts_data = ts_data_map.get(grid_id)
                    grid_tz = grid.get("timezone") or DEFAULT_TIMEZONE

                    # Check staleness based on TimescaleDB created_at (30 min threshold)
                    if ts_data and ts_data.get("timestamp_utc"):
                        ts_timestamp = ts_data["timestamp_utc"]
                        ts_is_stale = is_grids_stale(ts_timestamp)
                    else:
                        ts_is_stale = True  # No TimescaleDB data = stale

                    # Get HPS/FS status from TimescaleDB (majority voted)
                    if ts_data:
                        hps_on = None if ts_is_stale else ts_data.get("is_hps_on")
                        fs_on = None if ts_is_stale else ts_data.get("is_fs_active")
                    else:
                        hps_on = None
                        fs_on = None

                    # Get VRM voltage data for this grid to determine ON/OFF
                    site_id = grid.get("generation_external_site_id")
                    vrm_voltage = voltage_map.get(str(site_id)) if site_id else None
                    vrm_is_on = (
                        vrm_voltage.is_producing if vrm_voltage and not vrm_voltage.error else None
                    )
                    # Get total inverter power (all phases) from VRM
                    vrm_power_kw = vrm_voltage.total_power_kw if vrm_voltage else None

                    # Check if VRM data is stale (gateway hasn't reported in 30+ min)
                    # data_timestamp is UTC-based (utcnow - secondsAgo)
                    vrm_data_stale = False
                    if vrm_voltage and vrm_voltage.data_timestamp:
                        vrm_age = datetime.utcnow() - vrm_voltage.data_timestamp
                        vrm_data_stale = vrm_age > timedelta(minutes=30)

                    # Determine category and icon using new logic:
                    # - VRM voltage determines ON/OFF (if available)
                    # - TimescaleDB majority vote determines HPS/FS (if ON)
                    # - "Likely Isolated" = ON but power below HPS threshold
                    # - Stale VRM data (>30 min) = unknown
                    if vrm_is_on is None or vrm_data_stale:
                        # No VRM data — honest answer is "unknown"
                        category = "unknown"
                        icon = "Ⅹ"
                    elif vrm_is_on is False:
                        # VRM says grid is OFF (no inverter voltage)
                        category = "off"
                        icon = "🔴"
                    else:
                        # VRM says grid is ON - check HPS/FS status
                        hps_threshold_kw = grid.get("is_hps_on_threshold_kw")

                        # Determine HPS on using VRM power vs threshold
                        if vrm_power_kw is not None and hps_threshold_kw is not None:
                            vrm_hps_on = vrm_power_kw >= float(hps_threshold_kw)
                        else:
                            # Fallback to TimescaleDB if VRM power unavailable
                            vrm_hps_on = hps_on

                        if vrm_hps_on is False:
                            # Power below HPS threshold = Isolated, regardless of meter FS state
                            # (meters may report FS while inverter is actually below threshold)
                            category = "likely_isolated"
                            icon = "🔌"
                        elif fs_on is True:
                            category = "fs_on"
                            icon = "🟢"
                        elif vrm_hps_on is True:
                            category = "hps_on"
                            icon = "🟡"
                        else:
                            # vrm_hps_on is None (no power data, no TimescaleDB fallback)
                            category = "unknown"
                            icon = "Ⅹ"

                    # Build DCU status with counts
                    dcu_counts = dcu_counts_map.get(grid_id, {"online": 0, "total": 0})
                    dcu_online = dcu_counts["online"]
                    dcu_total = dcu_counts["total"]
                    # Build visual: 📶📶🅇 for 2 online, 1 offline
                    if dcu_total > 0:
                        dcu_visual = "📶" * dcu_online + "🅇" * (dcu_total - dcu_online)
                    else:
                        dcu_visual = "N/A"

                    # Convert weather to icon
                    weather_icon = _weather_to_icon(grid["current_weather"])

                    # Build latest_state from TimescaleDB data
                    if ts_data:
                        latest_state = {
                            "timestamp": _format_local_timestamp(
                                ts_data.get("timestamp_utc"), grid_tz
                            ),
                            "is_fs_active": ts_data.get("is_fs_active"),
                            "is_hps_on": ts_data.get("is_hps_on"),
                            "should_fs_be_on": ts_data.get("should_fs_be_on"),
                            "battery_soc_pct": ts_data.get("battery_soc_pct"),
                            "battery_status": ts_data.get("battery_status"),
                            "consumption_kwh": ts_data.get("consumption_kwh"),
                            "is_stale": ts_is_stale,
                        }
                    else:
                        latest_state = {
                            "timestamp": None,
                            "is_stale": True,
                            "is_fs_active": None,
                            "is_hps_on": None,
                            "should_fs_be_on": None,
                            "battery_soc_pct": None,
                            "battery_status": "unknown",
                            "consumption_kwh": None,
                        }

                    # Get downtime and weather data for this grid
                    downtime_data = downtime_map.get(grid["name"])
                    weather_data = weather_map.get(grid["name"])
                    equipment_note = equipment_note_map.get(grid["name"])

                    location = parse_location_geom(grid.get("location_wkb"))
                    grid_info = {
                        "name": grid["name"],
                        "grid_id": grid_id,
                        "platform_url": f"{PLATFORM_BASE_URL}/grid/{grid_id}/"
                        if PLATFORM_BASE_URL
                        else None,
                        "timezone": grid_tz,
                        "icon": icon,
                        "hps_on": hps_on,
                        "fs_on": fs_on,
                        "inverter_power_kw": vrm_power_kw,
                        "dcu_status": {
                            "visual": dcu_visual,
                            "online": dcu_online,
                            "total": dcu_total,
                        },
                        "live_weather": (
                            weather_data
                            if weather_data
                            else {
                                "icon": weather_icon,
                                "description": grid["current_weather"] or "Unknown",
                                "display": weather_icon,
                            }
                        ),
                        "latest_state": latest_state,
                        "downtime_24h": downtime_data,
                        "fs_hours_24h": fs_hours_map.get(grid_id),
                        "fs_delivery_24h": fs_delivery_map.get(grid_id),
                    }

                    if location:
                        grid_info["location"] = location

                    # Add note if equipment data unavailable (non-managed grid)
                    if equipment_note:
                        grid_info["equipment_note"] = equipment_note

                    if category == "fs_on":
                        fs_on_grids.append(grid_info)
                    elif category == "hps_on":
                        hps_on_grids.append(grid_info)
                    elif category == "likely_isolated":
                        likely_isolated_grids.append(grid_info)
                    elif category == "off":
                        off_grids.append(grid_info)
                    else:
                        unknown_grids.append(grid_info)

                result = {
                    "grids_by_status": {
                        "fs_on": fs_on_grids,
                        "hps_on": hps_on_grids,
                        "likely_isolated": likely_isolated_grids,
                        "off": off_grids,
                        "unknown": unknown_grids,
                    },
                    "summary": {
                        "total": len(rows),
                        "fs_on": len(fs_on_grids),
                        "hps_on": len(hps_on_grids),
                        "likely_isolated": len(likely_isolated_grids),
                        "off": len(off_grids),
                        "unknown": len(unknown_grids),
                    },
                    "legend": {
                        "status_icons": {
                            "🟢": "FS On (Full Service active)",
                            "🟡": "HPS On (High Power Service on, FS off/unknown)",
                            "🔌": "Likely Isolated (Inverter ON but power below HPS threshold)",
                            "🔴": "Off (No inverter voltage detected)",
                            "Ⅹ": "Unknown (Stale data >30 minutes old)",
                        },
                        "dcu_icons": {
                            "📶": "DCU online",
                            "🅇": "DCU offline",
                            "example": "📶📶🅇 = 2 online, 1 offline",
                        },
                        "weather_icons": {
                            "☀️": "Clear/Sunny",
                            "⛅": "Partly cloudy",
                            "☁️": "Cloudy/Overcast",
                            "🌧️": "Rain",
                            "⛈️": "Thunderstorm",
                            "🌫️": "Fog/Mist",
                            "❄️": "Snow",
                            "💨": "Windy",
                        },
                        "downtime_icons": {
                            "⚡️": "Stable (no downtime in 24h)",
                            "🔻": "Has downtime - check hours and causes",
                        },
                    },
                }

                # Compute fleet_summary for executive summary rendering
                all_grids = (
                    fs_on_grids + hps_on_grids + likely_isolated_grids + off_grids + unknown_grids
                )
                grids_with_faults = []
                grids_with_downtime = []
                low_fs_delivery = []
                offline_dcus = []
                fs_hours_values = []

                for g in all_grids:
                    dt = g.get("downtime_24h")
                    if dt and isinstance(dt, dict):
                        total_min = dt.get("total_downtime_minutes", 0) or 0
                        if total_min > 0:
                            # Find top cause by minutes
                            causes = dt.get("causes", {})
                            top_cause = max(causes, key=causes.get) if causes else "unknown"
                            grids_with_downtime.append(
                                {
                                    "name": g["name"],
                                    "downtime_minutes": round(total_min),
                                    "top_cause": top_cause,
                                }
                            )
                            # Check for grid_fault or unknown causes
                            for cause in causes:
                                if cause in ("grid_fault", "unknown"):
                                    grids_with_faults.append(
                                        {
                                            "name": g["name"],
                                            "fault_type": cause,
                                        }
                                    )
                                    break

                    # Check FS delivery
                    fs_del = g.get("fs_delivery_24h")
                    if fs_del and isinstance(fs_del, dict):
                        delivery_pct = fs_del.get("delivery_pct")
                        if delivery_pct is not None and delivery_pct < 75:
                            low_fs_delivery.append(
                                {
                                    "name": g["name"],
                                    "delivery_pct": delivery_pct,
                                }
                            )

                    # Check offline DCUs
                    dcu = g.get("dcu_status", {})
                    dcu_offline = (dcu.get("total", 0) or 0) - (dcu.get("online", 0) or 0)
                    if dcu_offline > 0:
                        offline_dcus.append(
                            {
                                "name": g["name"],
                                "offline_count": dcu_offline,
                            }
                        )

                    # Collect FS hours for average
                    fsh = g.get("fs_hours_24h")
                    if fsh is not None:
                        fs_hours_values.append(fsh)

                # Sort downtime grids by severity
                grids_with_downtime.sort(key=lambda x: x["downtime_minutes"], reverse=True)

                fleet_avg_fs_hours = (
                    round(sum(fs_hours_values) / len(fs_hours_values), 1)
                    if fs_hours_values
                    else None
                )

                result["fleet_summary"] = {
                    "grids_by_status_count": {
                        "fs_on": len(fs_on_grids),
                        "hps_on": len(hps_on_grids),
                        "isolated": len(likely_isolated_grids),
                        "off": len(off_grids),
                        "unknown": len(unknown_grids),
                    },
                    "total_grids": len(all_grids),
                    "grids_with_faults": grids_with_faults,
                    "grids_with_downtime": grids_with_downtime[:5],
                    "low_fs_delivery": low_fs_delivery,
                    "offline_dcus": offline_dcus,
                    "fleet_avg_fs_hours": fleet_avg_fs_hours,
                }

                logger.info(
                    f"All grids status for org {organization_id}: "
                    f"{len(fs_on_grids)} FS on, {len(hps_on_grids)} HPS on, "
                    f"{len(likely_isolated_grids)} likely isolated, "
                    f"{len(off_grids)} off, {len(unknown_grids)} unknown"
                )
                return result

            finally:
                await conn.close()

        except Exception as e:
            logger.error(f"Error getting all grids status: {e}")
            return {"error": f"Failed to get all grids status: {str(e)}"}

    async def close(self):
        """Close HTTP session."""
        await self.close_session()


# Global client instance
