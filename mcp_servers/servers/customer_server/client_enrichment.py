"""Meter enrichment / order recipient info for CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

from typing import Any, Dict, Optional

from servers.customer_server.client_base import STAFF_ORG_ID, logger

from shared.auth import get_auth_service


class ClientEnrichmentMixin:
    async def _get_meter_enriched_info(
        self, meter_id: int, client=None, organization_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Get enriched meter information including customer, connection, and grid details.

        Args:
            meter_id: Meter ID to enrich
            client: DEPRECATED - no longer used (kept for compatibility)
            organization_id: Optional organization ID for filtering (bypassed for staff, see STAFF_ORG_ID)

        Returns:
            Dict with enriched meter information including:
            - meter_no, meter_id
            - customer_name, customer_id
            - connection_type, connection_id
            - grid_name, grid_id
            - grid_status (from is_hps_on or similar field)
        """
        try:
            # Use direct database connection instead of Supabase API to bypass RLS
            auth_service = get_auth_service()
            pool = await auth_service._get_db_pool()

            async with pool.acquire() as conn:
                # Query meter with connection, grid, DCU references, and status fields
                # Apply organization filter unless staff
                if organization_id and organization_id != STAFF_ORG_ID:
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, connection_id, rls_grid_id, dcu_id,
                               is_on, kwh_credit_available, power_limit, power_limit_hps_mode,
                               power_limit_should_be, power_limit_updated_at,
                               power_limit_should_be_updated_at, last_seen_at,
                               connection_metrics, is_on_updated_at, kwh_credit_available_updated_at,
                               rls_organization_id
                        FROM meters
                        WHERE id = $1
                          AND rls_organization_id = $2
                        LIMIT 1
                        """,
                        meter_id,
                        organization_id,
                    )
                else:
                    # Staff (org 2) - no organization filter
                    meter_row = await conn.fetchrow(
                        """
                        SELECT id, external_reference, connection_id, rls_grid_id, dcu_id,
                               is_on, kwh_credit_available, power_limit, power_limit_hps_mode,
                               power_limit_should_be, power_limit_updated_at,
                               power_limit_should_be_updated_at, last_seen_at,
                               connection_metrics, is_on_updated_at, kwh_credit_available_updated_at,
                               rls_organization_id
                        FROM meters
                        WHERE id = $1
                        LIMIT 1
                        """,
                        meter_id,
                    )

                if not meter_row:
                    return {"error": "Meter not found"}

                meter = dict(meter_row)
            enriched = {
                "meter_no": meter.get(
                    "external_reference"
                ),  # Schema uses external_reference for meter number
            }

            # Add meter status fields
            if meter.get("is_on") is not None:
                enriched["is_on"] = meter.get("is_on")
            if meter.get("is_on_updated_at") is not None:
                enriched["is_on_updated_at"] = meter.get("is_on_updated_at")
            if meter.get("kwh_credit_available") is not None:
                enriched["kwh_credit_available"] = meter.get("kwh_credit_available")
            if meter.get("kwh_credit_available_updated_at") is not None:
                enriched["kwh_credit_available_updated_at"] = meter.get(
                    "kwh_credit_available_updated_at"
                )
            if meter.get("power_limit") is not None:
                enriched["power_limit"] = meter.get("power_limit")
            if meter.get("power_limit_should_be") is not None:
                enriched["power_limit_should_be"] = meter.get("power_limit_should_be")
            if meter.get("power_limit_updated_at") is not None:
                enriched["power_limit_updated_at"] = meter.get("power_limit_updated_at")
            if meter.get("power_limit_should_be_updated_at") is not None:
                enriched["power_limit_should_be_updated_at"] = meter.get(
                    "power_limit_should_be_updated_at"
                )
            if meter.get("last_seen_at") is not None:
                enriched["last_seen_at"] = meter.get("last_seen_at")
            if meter.get("power_limit_hps_mode") is not None:
                enriched["power_limit_hps_mode"] = meter.get("power_limit_hps_mode")
            if meter.get("connection_metrics"):
                enriched["connection_metrics"] = meter.get("connection_metrics")

                # Get connection and customer info
                connection_id = meter.get("connection_id")
                if connection_id:
                    try:
                        # Schema has boolean flags for connection type instead of single field
                        connection_row = await conn.fetchrow(
                            """
                            SELECT id, customer_id, is_residential, is_commercial, is_public
                            FROM connections
                            WHERE id = $1
                            LIMIT 1
                            """,
                            connection_id,
                        )

                        if connection_row:
                            # Derive connection type from boolean flags
                            if connection_row.get("is_residential"):
                                enriched["connection_type"] = "Residential"
                            elif connection_row.get("is_commercial"):
                                enriched["connection_type"] = "Commercial"
                            elif connection_row.get("is_public"):
                                enriched["connection_type"] = "Public"
                            else:
                                enriched["connection_type"] = "Unknown"

                            # Get customer name from accounts table via customer.account_id
                            customer_id = connection_row.get("customer_id")
                            if customer_id:
                                customer_row = await conn.fetchrow(
                                    """
                                    SELECT id, account_id
                                    FROM customers
                                    WHERE id = $1
                                    LIMIT 1
                                    """,
                                    customer_id,
                                )

                                if customer_row:
                                    # Get full_name from accounts table
                                    account_id = customer_row.get("account_id")
                                    if account_id:
                                        account_row = await conn.fetchrow(
                                            """
                                            SELECT id, full_name
                                            FROM accounts
                                            WHERE id = $1
                                            LIMIT 1
                                            """,
                                            account_id,
                                        )

                                        if account_row:
                                            enriched["customer_name"] = account_row.get("full_name")
                    except Exception as e:
                        logger.warning(f"Could not enrich connection/customer info: {e}")

                # Get grid info - schema uses rls_grid_id
                grid_id = meter.get("rls_grid_id")
                if grid_id:
                    try:
                        # Schema has is_hps_on boolean field for grid status
                        grid_row = await conn.fetchrow(
                            """
                            SELECT id, name, is_hps_on
                            FROM grids
                            WHERE id = $1
                            LIMIT 1
                            """,
                            grid_id,
                        )

                        if grid_row:
                            enriched["grid_name"] = grid_row.get("name")

                            # Use is_hps_on to determine grid status
                            if "is_hps_on" in grid_row and grid_row["is_hps_on"] is not None:
                                enriched["grid_status"] = (
                                    "grid is energized" if grid_row["is_hps_on"] else "grid is down"
                                )
                    except Exception as e:
                        logger.warning(f"Could not enrich grid info: {e}")

                # Get DCU online status
                dcu_id = meter.get("dcu_id")
                if dcu_id:
                    try:
                        dcu_row = await conn.fetchrow(
                            """
                            SELECT id, is_online, last_online_at
                            FROM dcus
                            WHERE id = $1
                            LIMIT 1
                            """,
                            dcu_id,
                        )

                        if dcu_row:
                            if "is_online" in dcu_row and dcu_row["is_online"] is not None:
                                enriched["dcu_status"] = (
                                    "dcu is online" if dcu_row["is_online"] else "dcu is offline"
                                )
                    except Exception as e:
                        logger.warning(f"Could not enrich DCU info: {e}")

                # Get last token from directives
                try:
                    token_row = await conn.fetchrow(
                        """
                        SELECT token, directive_type::text, created_at
                        FROM directives
                        WHERE meter_id = $1
                          AND token IS NOT NULL
                          AND token != ''
                        ORDER BY created_at DESC
                        LIMIT 1
                        """,
                        meter_id,
                    )

                    if token_row:
                        enriched["last_token"] = token_row["token"]
                        enriched["last_token_type"] = token_row["directive_type"]
                        enriched["last_token_created_at"] = token_row["created_at"]
                except Exception as e:
                    logger.warning(f"Could not get last token for meter {meter_id}: {e}")

            return enriched

        except Exception as e:
            logger.error(f"Error enriching meter info: {e}")
            return {"error": f"Failed to enrich meter info: {str(e)}"}

    async def _get_order_recipient_info(self, order: Dict[str, Any], conn=None) -> Dict[str, Any]:
        """
        Get enriched recipient information for an order.

        Args:
            order: Order dict with meta_receiver_type and meta_receiver_id
            conn: asyncpg connection (used for agent lookup; meter path uses AuthService pool)

        Returns:
            Dict with recipient information:
            - If meter: enriched meter info (customer, grid, connection)
            - If agent: agent name and email from accounts table
        """
        try:
            # Schema uses meta_receiver_type instead of receiving_type
            receiver_type = order.get("meta_receiver_type", "METER").upper()

            if receiver_type == "AGENT":
                # Use accounts table for agent recipients
                # Schema uses meta_receiver_id for agent ID
                agent_id = order.get("meta_receiver_id")
                if not agent_id:
                    return {"type": "agent", "error": "No meta_receiver_id in order"}

                try:
                    agent_row = await conn.fetchrow(
                        """
                        SELECT id, full_name
                        FROM accounts
                        WHERE id = $1
                        LIMIT 1
                        """,
                        agent_id,
                    )

                    if agent_row:
                        return {
                            "type": "agent",
                            "agent_name": agent_row["full_name"],
                        }
                    else:
                        return {"type": "agent", "error": "Agent not found"}
                except Exception as e:
                    logger.warning(f"Could not fetch agent info: {e}")
                    return {"type": "agent", "error": str(e)}

            else:
                # Default: meter recipient - use meta_receiver_id as meter_id
                meter_id = order.get("meta_receiver_id")
                if not meter_id:
                    return {"type": "meter", "error": "No meta_receiver_id in order"}

                # Get organization_id from order for filtering
                organization_id = order.get("rls_organization_id")

                # Get enriched meter info (uses AuthService pool internally)
                meter_info = await self._get_meter_enriched_info(
                    meter_id, organization_id=organization_id
                )
                meter_info["type"] = "meter"
                return meter_info

        except Exception as e:
            logger.error(f"Error getting recipient info: {e}")
            return {"error": f"Failed to get recipient info: {str(e)}"}

