"""Meter write-action methods (commissioning, relay, tokens, limits) for
CustomerServiceClient.

Split out of customer_mcp_server.py as part of the Phase 4 file split.
"""

import os
from datetime import datetime
from typing import Any, Dict, Literal, Optional
from zoneinfo import ZoneInfo

from servers.customer_server.client_base import (
    _METER_ACTIONS_DISABLED_MSG,
    CUSTOMER_METER_ACTIONS_ENABLED,
    CUSTOMER_METER_POWER_LIMIT_OPTIONS,
    DEFAULT_TIMEZONE,
    STAFF_ORG_ID,
    logger,
)


class ClientActionsMixin:
    async def _fetch_meter(
        self,
        meter_number: str,
        organization_id: int,
        extra_columns: str = "",
    ) -> tuple[Any, Any]:  # (asyncpg.Connection, asyncpg.Record | None)
        """Open an Auth DB connection and look up a meter by external reference.

        Applies org-scoping for non-staff orgs. Caller is responsible for
        closing the returned connection in a finally block.

        Args:
            meter_number: The meter's external reference string.
            organization_id: Resolved org ID; STAFF_ORG_ID skips org-scoping.
            extra_columns: Comma-prefixed extra columns to SELECT (e.g. ", connection_id").

        Returns:
            (conn, row) — row is None if meter not found.
        """
        import asyncpg as _asyncpg

        conn = await _asyncpg.connect(
            host=os.getenv("AUTH_DB_HOST"),
            port=int(os.getenv("AUTH_DB_PORT", "6543")),
            user=os.getenv("AUTH_DB_USER"),
            password=os.getenv("AUTH_DB_PASSWORD"),
            database=os.getenv("AUTH_DB_NAME", "postgres"),
            ssl="require",
            statement_cache_size=0,
        )
        # extra_columns MUST be a hardcoded literal (e.g. ", connection_id") — NEVER user-supplied input.
        select_cols = f"id, external_reference{extra_columns}"
        if organization_id != STAFF_ORG_ID:
            row = await conn.fetchrow(
                f"SELECT {select_cols} FROM meters "
                "WHERE external_reference = $1 AND rls_organization_id = $2 LIMIT 1",
                meter_number,
                organization_id,
            )
        else:
            row = await conn.fetchrow(
                f"SELECT {select_cols} FROM meters WHERE external_reference = $1 LIMIT 1",
                meter_number,
            )
        return conn, row

    async def retry_commissioning(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Retry commissioning for a meter by calling Metering Platform API.

        Args:
            meter_number: Meter number to retry commissioning for
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with retry commissioning status
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        # Check if Metering Platform is configured
        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter commissioning actions."
            }

        if err := self._check_rate_limit("retry_commissioning", meter_number.strip()):
            return {"error": err}

        # Get user's organization_id if not provided
        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter["id"]

                # Look up the most recent commissioning for this meter.
                # meters.last_commissioning_id does not exist — query the
                # meter_commissionings table directly instead.
                commissioning = await conn.fetchrow(
                    """
                    SELECT mc.id, mc.meter_commissioning_status
                    FROM meter_commissionings mc
                    JOIN metering_hardware_install_sessions mhis
                      ON mc.metering_hardware_install_session_id = mhis.id
                    WHERE mhis.meter_id = $1
                    ORDER BY mc.created_at DESC
                    LIMIT 1
                    """,
                    meter_id,
                )

                if not commissioning:
                    return {
                        "error": "No commissioning record found for this meter. Cannot retry commissioning.",
                        "meter_number": meter_number,
                    }

                last_commissioning_id = commissioning["id"]
                status = (commissioning["meter_commissioning_status"] or "").upper()

                if status == "SUCCESSFUL":
                    return {
                        "error": "Meter is already successfully commissioned.",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "commissioning_status": status,
                        "message": "This meter does not need commissioning retry. It is already commissioned.",
                    }

                if status == "PROCESSING":
                    return {
                        "error": "A commissioning attempt is currently in progress.",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "commissioning_status": status,
                        "message": "Please wait for the current commissioning to complete before retrying.",
                    }

            finally:
                await conn.close()

            # Call Metering Platform API to retry commissioning
            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-installs/retry-commissioning"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }
            body = {"id": last_commissioning_id}

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()

                return {
                    "success": True,
                    "meter_number": meter_number,
                    "commissioning_id": last_commissioning_id,
                    "new_commissioning_id": metering_response.get("id"),
                    "message": (
                        "Commissioning retry has been initiated successfully. "
                        "This process typically takes 2-5 minutes to complete. "
                        "You can check the meter_information tool to monitor progress."
                    ),
                }

            except Exception as e:
                logger.error(f"Metering Platform API request failed: {e}")
                error_msg = str(e)

                if "400" in error_msg or "BadRequest" in error_msg:
                    return {
                        "error": f"Cannot retry commissioning: {error_msg}",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                        "message": "The meter may already be processing or successfully commissioned.",
                    }
                else:
                    return {
                        "error": f"Failed to contact commissioning service: {error_msg}",
                        "meter_number": meter_number,
                        "commissioning_id": last_commissioning_id,
                    }

        except Exception as e:
            logger.error(f"Error retrying commissioning: {e}")
            return {"error": f"Failed to retry commissioning: {str(e)}"}

    async def unassign_meter(
        self,
        meter_number: str,
        user_email: str,
        organization_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """
        Unassign a meter from its current connection by calling Metering Platform API.

        Args:
            meter_number: Meter number (external reference) to unassign
            user_email: User email for organization lookup
            organization_id: Optional organization ID (will be looked up if not provided)

        Returns:
            Dictionary with unassignment status
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("unassign_meter", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(
                meter_number, organization_id, ", connection_id, rls_organization_id"
            )
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }

                meter_id = meter["id"]
                connection_id = meter["connection_id"]

                if not connection_id:
                    return {
                        "error": "Meter is not currently assigned to any connection.",
                        "meter_number": meter_number,
                        "message": "This meter does not need unassignment — it has no active connection.",
                    }

            finally:
                await conn.close()

            # Call Metering Platform API to unassign meter
            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{meter_id}/unassign"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers)
                response.raise_for_status()

                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        "Meter has been unassigned successfully. "
                        "It is now available for reassignment to another connection."
                    ),
                }

            except Exception as e:
                logger.error(f"Metering Platform API unassign request failed: {e}")
                error_msg = str(e)

                if "400" in error_msg or "BadRequest" in error_msg:
                    return {
                        "error": "Cannot unassign meter. The meter may not be in a state that allows unassignment.",
                        "meter_number": meter_number,
                    }
                else:
                    return {
                        "error": "Failed to contact meter service. Please try again later.",
                        "meter_number": meter_number,
                    }

        except Exception as e:
            logger.error(f"Error unassigning meter: {e}")
            return {
                "error": "Something went wrong while unassigning the meter. The team has been notified."
            }

    async def set_meter_power_limit(
        self,
        meter_number: str,
        power_limit_watts: int,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Set the HPS power limit for a meter via Metering Platform API.

        Sends a SET_POWER_LIMIT interaction to the meter via POST /meter-interactions/create-one.
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if power_limit_watts not in CUSTOMER_METER_POWER_LIMIT_OPTIONS:
            allowed = ", ".join(str(w) for w in CUSTOMER_METER_POWER_LIMIT_OPTIONS)
            return {
                "error": (f"Invalid power limit: {power_limit_watts}W. Allowed values: {allowed}W.")
            }

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("set_meter_power_limit", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {
                "meter_id": meter_id,
                "meter_interaction_type": "SET_POWER_LIMIT",
                "target_power_limit": power_limit_watts,
            }

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "power_limit_watts": power_limit_watts,
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Power limit set to {power_limit_watts}W for meter {meter_number}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API set_power_limit request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number and limit.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error setting meter power limit: {e}")
            return {
                "error": "Something went wrong while setting the power limit. The team has been notified."
            }

    async def set_meter_date(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Set the current date on a meter via Metering Platform API.

        Sends a SET_DATE interaction to the meter via POST /meter-interactions/create-one.
        The date is the current date in the deployment's local timezone (DEFAULT_TIMEZONE).
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("set_meter_date", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        # Use the deployment's local timezone so the calendar date matches the meter's local time
        now = datetime.now(ZoneInfo(DEFAULT_TIMEZONE))

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {
                "meter_id": meter_id,
                "meter_interaction_type": "SET_DATE",
                "payload_data": {"year": now.year, "month": now.month, "day": now.day},
            }

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "date_set": now.strftime("%Y-%m-%d"),
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Date set to {now.strftime('%Y-%m-%d')} on meter {meter_number}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API set_meter_date request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error setting meter date: {e}")
            return {
                "error": "Something went wrong while setting the meter date. The team has been notified."
            }

    async def send_relay_state(
        self,
        meter_number: str,
        user_email: str,
        interaction_type: Literal["TURN_ON", "TURN_OFF"],
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """Send a TURN_ON or TURN_OFF interaction to a meter via Metering Platform."""
        if interaction_type not in ("TURN_ON", "TURN_OFF"):
            return {"error": f"Invalid interaction_type: {interaction_type}"}

        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        rate_key = "turn_meter_on" if interaction_type == "TURN_ON" else "turn_meter_off"
        if err := self._check_rate_limit(rate_key, meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_id = meter["id"]
            finally:
                await conn.close()

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meter-interactions/create-one"
            headers = {"Content-Type": "application/json", "X-API-KEY": self.metering_api_key}
            body = {"meter_id": meter_id, "meter_interaction_type": interaction_type}

            try:
                response = await http_client.post(url, headers=headers, json=body)
                response.raise_for_status()
                metering_response = await response.json()
                state = "ON" if interaction_type == "TURN_ON" else "OFF"
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "state": state,
                    "interaction_id": metering_response.get("id"),
                    "message": (
                        f"Meter {meter_number} relay turned {state}. "
                        "The change will take effect on the next meter communication."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform API {interaction_type} request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error sending {interaction_type} for meter {meter_number}: {e}")
            return {"error": "Something went wrong. The team has been notified."}

    async def resend_meter_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last prepayment token to a meter via Metering Platform API.

        Looks up the most recent token from the directives table and delivers it
        via POST /meters/:external_reference/tokens/deliver.
        Requires CUSTOMER_METER_ACTIONS_ENABLED=true.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_meter_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                # Use the DB-stored canonical reference in the URL, not the raw input string.
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'TOP_UP'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"Token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending meter token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

    async def resend_clear_tamper_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last CLEAR_TAMPER token to a meter via Metering Platform API.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_clear_tamper_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'CLEAR_TAMPER'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous CLEAR_TAMPER token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"CLEAR_TAMPER token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform CLEAR_TAMPER token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending CLEAR_TAMPER token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

    async def resend_power_limit_token(
        self,
        meter_number: str,
        user_email: str,
        organization_id: int | None = None,
    ) -> dict[str, Any]:
        """
        Resend the last PLS (power limit set) token to a meter via Metering Platform API.
        """
        if not CUSTOMER_METER_ACTIONS_ENABLED:
            return {"error": _METER_ACTIONS_DISABLED_MSG}

        if not meter_number or not meter_number.strip():
            return {"error": "Meter number is required."}
        meter_number = meter_number.strip()

        if not self.metering_api_url or not self.metering_bearer_token:
            return {
                "error": "Metering Platform API not configured. Please contact support to enable meter actions."
            }

        if err := self._check_rate_limit("resend_power_limit_token", meter_number):
            return {"error": err}

        if organization_id is None:
            organization_id = await self.get_user_organization(user_email)
            if organization_id is None:
                return {"error": "Could not determine organization for user"}

        try:
            conn, meter = await self._fetch_meter(meter_number, organization_id)
            try:
                if not meter:
                    return {
                        "meter_found": False,
                        "message": "Meter not found for your organization",
                        "meter_number": meter_number,
                    }
                meter_db_id = meter["id"]
                external_ref = meter["external_reference"]

                directive = await conn.fetchrow(
                    """
                    SELECT token FROM directives
                    WHERE meter_id = $1 AND directive_type = 'PLS'
                    AND token IS NOT NULL AND token != ''
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    meter_db_id,
                )
            finally:
                await conn.close()

            if not directive or not directive["token"]:
                return {
                    "error": "No previous PLS (power limit set) token found for this meter.",
                    "meter_number": meter_number,
                }

            token_code = directive["token"]

            http_client = await self.get_session()
            url = f"{self.metering_api_url}/meters/{external_ref}/tokens/deliver"
            headers = {
                "Content-Type": "application/json",
                "X-API-KEY": self.metering_api_key,
            }

            try:
                response = await http_client.post(url, headers=headers, json={"token": token_code})
                response.raise_for_status()
                return {
                    "success": True,
                    "meter_number": meter_number,
                    "message": (
                        f"PLS (power limit set) token resent successfully to meter {meter_number}. "
                        "The customer should receive it via their registered channel shortly."
                    ),
                }
            except Exception as e:
                logger.error(f"Metering Platform PLS token resend request failed: {e}")
                status = getattr(e, "status", None)
                if status == 400:
                    return {
                        "error": "Invalid request to meter service. Check the meter number.",
                        "meter_number": meter_number,
                    }
                return {
                    "error": "Failed to contact meter service. Please try again later.",
                    "meter_number": meter_number,
                }

        except Exception as e:
            logger.error(f"Error resending PLS token: {e}")
            return {
                "error": "Something went wrong while resending the token. The team has been notified."
            }

