"""Shared config + infra for CustomerServiceClient.

Config constants and ClientBaseMixin (``__init__``, rate limiting, Supabase
client resolution, org lookup) split out of customer_mcp_server.py as part of
the Phase 4 file split. See client.py for the composed class and the index of
which mixin holds what.
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from shared.auth import get_auth_service
from shared.utils.http_client import HTTPClientMixin

logger = logging.getLogger("customer-server")

# Auth Database configuration (contains orders, directives, meters, connections, customers, grids)
AUTH_SUPABASE_URL = os.getenv("AUTH_SUPABASE_URL", "")
AUTH_SUPABASE_KEY = os.getenv("AUTH_SUPABASE_KEY", "")
AUTH_SUPABASE_ANON_KEY = os.getenv("AUTH_SUPABASE_ANON_KEY", "")

# Payment processor configuration
PAYMENT_PROCESSOR_API_URL = os.getenv("PAYMENT_PROCESSOR_API_URL", "")
PAYMENT_PROCESSOR_SECRET_KEY = os.getenv("PAYMENT_PROCESSOR_SECRET_KEY")

# Metering Platform API configuration (meter commissioning service)
METERING_API_URL = os.getenv("METERING_API_URL", "")
METERING_BEARER_TOKEN = os.getenv("METERING_BEARER_TOKEN", "")
METERING_API_KEY = os.getenv("METERING_API_KEY", "")

# TimescaleDB configuration (grid energy snapshots)
TIMESCALE_HOST = os.getenv("TIMESCALE_HOST", "")
TIMESCALE_PORT = int(os.getenv("TIMESCALE_PORT", "37244"))
TIMESCALE_DATABASE = os.getenv("TIMESCALE_DATABASE", "tsdb")
TIMESCALE_USER = os.getenv("TIMESCALE_USER", "")
TIMESCALE_PASSWORD = os.getenv("TIMESCALE_PASSWORD", "")

# Default staleness threshold for status data (30 minutes)
STALENESS_THRESHOLD = timedelta(minutes=30)

# Max concurrent VRM API calls for batch operations (downtime, weather, voltage)
VRM_BATCH_MAX_CONCURRENT = int(os.getenv("VRM_BATCH_MAX_CONCURRENT", "12"))

# Staff organization ID (controls staff-only views in customer tools)
STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))
# Default timezone for display and fallback when grid has no timezone configured
DEFAULT_TIMEZONE: str = os.getenv("DEFAULT_TIMEZONE", "UTC")
CUSTOMER_METER_ACTIONS_ENABLED: bool = (
    os.getenv("CUSTOMER_METER_ACTIONS_ENABLED", "false").lower() == "true"
)
# IMPORTANT: If you change this env var, also update the "enum" array in tool_definitions.json
# for the set_meter_power_limit tool's power_limit_watts property so they stay in sync.
try:
    CUSTOMER_METER_POWER_LIMIT_OPTIONS: list[int] = [
        int(x.strip())
        for x in os.getenv("CUSTOMER_METER_POWER_LIMIT_OPTIONS", "200,600").split(",")
        if x.strip()
    ]
except ValueError:
    logger.warning(
        "CUSTOMER_METER_POWER_LIMIT_OPTIONS is malformed; falling back to [200, 600]. "
        "Expected comma-separated integers, e.g. '200,600'."
    )
    CUSTOMER_METER_POWER_LIMIT_OPTIONS = [200, 600]

_METER_ACTIONS_DISABLED_MSG: str = (
    "Meter write actions are disabled. Set CUSTOMER_METER_ACTIONS_ENABLED=true to enable."
)

# Rate limiting for meter write actions — keyed by "{action}:{meter_number}"
_last_action_times: dict[str, datetime] = {}
_ACTION_COOLDOWNS: dict[str, timedelta] = {
    "set_meter_power_limit": timedelta(minutes=5),
    "set_meter_date": timedelta(minutes=5),
    "turn_meter_on": timedelta(minutes=5),
    "turn_meter_off": timedelta(minutes=5),
    "resend_meter_token": timedelta(minutes=10),
    "resend_clear_tamper_token": timedelta(minutes=10),
    "resend_power_limit_token": timedelta(minutes=10),
    "retry_commissioning": timedelta(minutes=15),
    "unassign_meter": timedelta(hours=1),
}

# Base URL for the grid management platform (used to build direct links in tool responses).
# Optional — if unset, platform_url fields are omitted from tool output.
PLATFORM_BASE_URL: str = os.getenv("PLATFORM_BASE_URL", "").rstrip("/")

# Status stability: average over recent snapshots to prevent flapping
STATUS_STABILITY_SNAPSHOT_COUNT = 3  # Use majority voting over 3 snapshots
STATUS_STABILITY_MAX_LOOKBACK_MINUTES = 60  # Don't go beyond 1 hour even if fewer snapshots



class ClientBaseMixin(HTTPClientMixin):
    """``__init__``, rate limiting, Supabase client resolution, and org lookup."""

    def __init__(self):
        super().__init__()
        self.auth_supabase_url = AUTH_SUPABASE_URL
        self.auth_supabase_key = AUTH_SUPABASE_KEY
        self.auth_supabase_anon_key = AUTH_SUPABASE_ANON_KEY
        self.payment_processor_url = PAYMENT_PROCESSOR_API_URL
        self.payment_processor_key = PAYMENT_PROCESSOR_SECRET_KEY
        self.metering_api_url = METERING_API_URL.rstrip("/") if METERING_API_URL else METERING_API_URL
        self.metering_bearer_token = METERING_BEARER_TOKEN
        self.metering_api_key = METERING_API_KEY

    def _check_rate_limit(self, action: str, meter_number: str) -> str | None:
        """Return an error string if the action is within its cooldown window, else None."""
        key = f"{action}:{meter_number}"
        last = _last_action_times.get(key)
        cooldown = _ACTION_COOLDOWNS[action]
        if last and (datetime.now(timezone.utc) - last) < cooldown:
            remaining = (
                int((cooldown - (datetime.now(timezone.utc) - last)).total_seconds() / 60) + 1
            )
            return (
                f"This action was recently performed on meter {meter_number}. "
                f"Please wait {remaining} more minute(s) before retrying."
            )
        _last_action_times[key] = datetime.now(timezone.utc)
        return None

    async def _get_supabase_client(self):
        """Get Supabase client for AUTH database."""
        if not self.auth_supabase_url or not (
            self.auth_supabase_key or self.auth_supabase_anon_key
        ):
            raise Exception(
                "Auth Supabase not configured. Set AUTH_SUPABASE_URL and (AUTH_SUPABASE_KEY or AUTH_SUPABASE_ANON_KEY) in environment."
            )

        from supabase import create_client

        # Use anon key for RLS-based access, or service key if anon key not available
        key = self.auth_supabase_anon_key or self.auth_supabase_key
        return create_client(self.auth_supabase_url, key)

    async def get_user_organization(self, user_email: str) -> Optional[int]:
        """
        Get organization_id for a user by their email.

        Args:
            user_email: User's email address

        Returns:
            Organization ID or None if not found
        """
        try:
            # Use AuthService to get user permissions
            auth_service = get_auth_service()
            permissions = await auth_service.get_user_permissions(user_email)

            if permissions.organization_ids:
                org_id = int(permissions.organization_ids[0])
                logger.info(f"Found organization_id {org_id} for user {user_email}")
                return org_id

            logger.warning(f"No organization found for user {user_email}")
            return None

        except Exception as e:
            logger.error(f"Error getting user organization: {e}")
            return None
