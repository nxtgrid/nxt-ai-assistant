#!/usr/bin/env python3
"""
Meters API MCP Server

This MCP server provides a unified interface to interact with different meter types:
- Calin V1 meters (PLC-based)
- Calin V2 meters (RF-based)
- LoRaWAN meters (via Chirpstack)

The server automatically detects meter type from Supabase 'meters' table and routes
API calls to the appropriate backend implementation.

Unified Interface Tools:
- get_meter_online_status: Get DCU/base station online status for any meter type
- create_reading_task: Create remote reading task for any meter type
- get_reading_task_status: Check reading task status for any meter type
- send_power_limit_token: Send power limit token (write operation, gated by actions_enabled)
- send_token: Send token to meter (write operation, gated by actions_enabled)

Authentication & Credentials:
- All credentials stored securely in .env file
- Calin V1: CALIN_V1_* environment variables
- Calin V2: CALIN_V2_* environment variables (OAuth with token caching)
- Chirpstack: CHIRPSTACK_* environment variables
- Supabase: SUPABASE_URL and SUPABASE_KEY
- No authentication parameters exposed in unified tool calls

Reliability Features:
- 5-second retry mechanism for all API calls
- Clear availability messages when API is down
- Automatic OAuth token refresh for Calin V2
- Handles transient downtimes gracefully

Usage:
- Unified tools automatically route based on meter type from Supabase
- Legacy V1/V2/LoRaWAN specific tools preserved for direct access
- Write operations only enabled when METERS_ACTIONS_ENABLED=true
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import jwt
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import NotificationOptions, Server
from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry

from shared.auth import get_auth_service

from .tool_schemas import TOOL_SCHEMAS

# Load environment variables from .env file
load_dotenv()

STAFF_ORG_ID: int = int(os.getenv("STAFF_ORG_ID", "2"))

# Get Calin V1 credentials from environment
CALIN_V1_BASE_URL = os.getenv("CALIN_V1_BASE_URL", "")
CALIN_V1_USERNAME = os.getenv("CALIN_V1_USERNAME", "")
CALIN_V1_PASSWORD = os.getenv("CALIN_V1_PASSWORD", "")
CALIN_V1_COMPANY = os.getenv("CALIN_V1_COMPANY", "")

# Get Calin V2 credentials from environment
CALIN_V2_BASE_URL = os.getenv("CALIN_V2_BASE_URL", "")
CALIN_V2_USERNAME = os.getenv("CALIN_V2_USERNAME", "")
CALIN_V2_PASSWORD = os.getenv("CALIN_V2_PASSWORD", "")
CALIN_V2_COMPANY = os.getenv("CALIN_V2_COMPANY", "")

# Get Chirpstack credentials from environment
CHIRPSTACK_BASE_URL = os.getenv("CHIRPSTACK_BASE_URL", "")
CHIRPSTACK_API_KEY = os.getenv("CHIRPSTACK_API_KEY", "")
CHIRPSTACK_TENANT_ID = os.getenv("CHIRPSTACK_TENANT_ID", "")

# Get Supabase credentials from environment (chat database with legacy fallback)
SUPABASE_URL = os.getenv("CHAT_DB_URL") or os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY") or os.getenv(
    "SUPABASE_KEY", ""
)  # Public anon key (respects RLS)
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv(
    "CHAT_DB_SERVICE_KEY", ""
)  # Service role key (only for JWT generation)
SUPABASE_JWT_SECRET = os.getenv("SUPABASE_JWT_SECRET", "")  # JWT secret for signing tokens
SUPABASE_DEFAULT_USER_EMAIL = os.getenv(
    "SUPABASE_DEFAULT_USER_EMAIL", ""
)  # Default user email for JWT generation
SUPABASE_USER_EMAIL = os.getenv("SUPABASE_USER_EMAIL", "")  # User email for RLS
SUPABASE_USER_PASSWORD = os.getenv("SUPABASE_USER_PASSWORD", "")  # User password for RLS

# Configure logging to stderr for Claude Desktop visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("meters-mcp-server")

# Startup message to stderr
print("Meters MCP Server starting...", file=sys.stderr)
print(f"Python path: {sys.path}", file=sys.stderr)
print(f"Working directory: {os.getcwd()}", file=sys.stderr)

server = Server("meters-api")
registry = ToolRegistry("meters")
_SCHEMAS_BY_NAME = {s["name"]: s for s in TOOL_SCHEMAS}


class MeterType(Enum):
    """Meter type enumeration"""

    CALIN_V1 = "calin_v1"
    CALIN_V2 = "calin_v2"
    LORAWAN = "lorawan"
    UNKNOWN = "unknown"


class MetersAPIClient:
    """Client for interacting with different meter APIs"""

    def __init__(self):
        self.session: Optional[aiohttp.ClientSession] = None
        # Token cache for V2 API (key: (base_url, username, company), value: (token, expiry))
        self.v2_token_cache: Dict[Tuple[str, str, str], Tuple[str, float]] = {}
        # Legacy single token storage for backward compatibility
        self.v2_token: Optional[str] = None
        self.v2_token_expiry: Optional[float] = None
        # Supabase authentication - cache JWT per user email
        self.supabase_jwt_cache: Dict[str, Tuple[str, float]] = {}  # {user_email: (jwt, expiry)}
        self.current_user_email: Optional[str] = None  # Track current request user

    async def get_session(self) -> aiohttp.ClientSession:
        """Get or create HTTP session"""
        if self.session is None:
            self.session = aiohttp.ClientSession()
        return self.session

    async def close(self):
        """Close HTTP session and clear token cache"""
        if self.session:
            await self.session.close()
            self.session = None

        # Clear token cache
        self.v2_token_cache.clear()
        self.v2_token = None
        self.v2_token_expiry = None
        logger.debug("Cleared OAuth token cache")

    # ===============================================
    # Supabase Authentication and Integration
    # ===============================================

    async def generate_user_jwt(self, user_email: str) -> str:
        """
        Generate a JWT for a specific user by:
        1. Using service role key to query auth.users table for user by email
        2. Extracting user_id from result
        3. Generating JWT with user_id claim using JWT_SECRET
        4. Caching the JWT with expiry
        5. Returning the JWT for use in authenticated requests

        This enables dynamic Row Level Security (RLS) based on the user.

        Args:
            user_email: Email of the user to generate JWT for

        Returns:
            JWT token for the specified user
        """
        if not SUPABASE_SERVICE_ROLE_KEY:
            raise Exception("SUPABASE_SERVICE_ROLE_KEY must be set in .env for JWT generation")
        if not SUPABASE_JWT_SECRET:
            raise Exception("SUPABASE_JWT_SECRET must be set in .env for JWT generation")

        session = await self.get_session()

        # Step 1: Query auth.users table using service role key to get user_id
        url = f"{SUPABASE_URL}/auth/v1/admin/users"
        headers = {
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
            "Content-Type": "application/json",
        }

        logger.debug(f"Looking up user {user_email} in Supabase")

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(
                    f"Failed to query Supabase users (status {response.status}): {error_text}"
                )

            result = await response.json()

        # Find user by email in the users array
        users = result.get("users", [])
        user = next((u for u in users if u.get("email") == user_email), None)

        if not user:
            raise Exception(f"User with email {user_email} not found in Supabase")

        user_id = user.get("id")
        if not user_id:
            raise Exception(f"User {user_email} does not have an ID")

        # Step 2: Generate JWT with user_id claim
        # JWT payload
        current_time = int(time.time())
        expires_in = 3600  # 1 hour
        expiry_time = current_time + expires_in

        payload = {
            "sub": user_id,  # Subject (user ID)
            "email": user_email,
            "role": user.get("role", "authenticated"),
            "iat": current_time,  # Issued at
            "exp": expiry_time,  # Expiry
            "aud": "authenticated",
        }

        # Sign the JWT
        jwt_token = str(jwt.encode(payload, SUPABASE_JWT_SECRET, algorithm="HS256"))

        # Cache the JWT
        self.supabase_jwt_cache[user_email] = (jwt_token, expiry_time * 1000)

        logger.info(f"Generated JWT for user {user_email}")
        return jwt_token

    async def supabase_ensure_token(self, user_email: Optional[str] = None) -> str:
        """
        Ensure we have a valid JWT for the specified user with automatic refresh.
        This token respects Row Level Security policies for that user.

        Args:
            user_email: Email of the user (optional, uses current_user_email if not provided)

        Returns:
            JWT token for the specified user
        """
        # Use provided email or fall back to current_user_email
        email = user_email or self.current_user_email
        if not email:
            raise Exception(
                "No user email provided for JWT generation. user_email parameter is required for all operations."
            )

        current_time = datetime.now().timestamp() * 1000

        # Check if we have a cached JWT that's still valid (with 60 second buffer)
        if email in self.supabase_jwt_cache:
            jwt_token, expiry = self.supabase_jwt_cache[email]
            if expiry - current_time > 60000:
                logger.debug(f"Using cached JWT for {email}")
                return jwt_token
            else:
                logger.debug(f"Cached JWT for {email} expired, regenerating...")

        # Generate new JWT
        return await self.generate_user_jwt(email)

    # ===============================================
    # Supabase Integration for Meter Type Detection
    # ===============================================

    async def get_meter_type(self, meter_no: str) -> MeterType:
        """
        Query Supabase 'meters' table to determine meter type.
        Uses authenticated request that respects Row Level Security (RLS).

        Expected table structure:
        - meter_no: text (primary key or unique)
        - meter_type: text ('calin_v1', 'calin_v2', 'lorawan')
        """
        try:
            # Get authenticated token (respects RLS)
            access_token = await self.supabase_ensure_token()
            session = await self.get_session()

            url = f"{SUPABASE_URL}/rest/v1/meters"
            headers = {
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",  # User's access token, not service key
                "Content-Type": "application/json",
            }
            params = {"meter_no": f"eq.{meter_no}", "select": "meter_type"}

            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    logger.warning(f"Supabase query failed for meter {meter_no}: {response.status}")
                    return MeterType.UNKNOWN

                result = await response.json()
                if not result:
                    logger.warning(
                        f"Meter {meter_no} not found in Supabase or not accessible by user {SUPABASE_USER_EMAIL}"
                    )
                    return MeterType.UNKNOWN

                meter_type_str = result[0].get("meter_type", "").lower()

                # Map string to enum
                type_mapping = {
                    "calin_v1": MeterType.CALIN_V1,
                    "calin_v2": MeterType.CALIN_V2,
                    "lorawan": MeterType.LORAWAN,
                }

                meter_type = type_mapping.get(meter_type_str, MeterType.UNKNOWN)
                logger.info(
                    f"Meter {meter_no} type: {meter_type.value} (accessed by {SUPABASE_USER_EMAIL})"
                )
                return meter_type

        except Exception as e:
            logger.error(f"Error querying meter type for {meter_no}: {str(e)}")
            return MeterType.UNKNOWN

    async def get_meter_info(self, meter_no: str) -> Dict[str, Any]:
        """
        Query Supabase 'meters' table to get complete meter information.
        Uses authenticated request that respects Row Level Security (RLS).

        Expected table structure:
        - meter_no: text (primary key or unique)
        - meter_type: text ('calin_v1', 'calin_v2', 'lorawan')
        - dcu_id: text (for Calin V1/V2 meters)
        - customer_id: text (for Calin V2 meters)
        - dev_eui: text (for LoRaWAN meters)
        - gateway_id: text (for LoRaWAN meters)
        """
        try:
            # Get authenticated token (respects RLS)
            access_token = await self.supabase_ensure_token()
            session = await self.get_session()

            url = f"{SUPABASE_URL}/rest/v1/meters"
            headers = {
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",  # User's access token, not service key
                "Content-Type": "application/json",
            }
            params = {
                "meter_no": f"eq.{meter_no}",
                "select": "id,meter_type,dcu_id,customer_id,dev_eui,gateway_id,last_metering_hardware_install_session_id(last_meter_commissioning_id(created_at,meter_commissioning_status))",
            }

            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    logger.warning(f"Supabase query failed for meter {meter_no}: {response.status}")
                    return {}

                result = await response.json()
                if not result:
                    logger.warning(
                        f"Meter {meter_no} not found in Supabase or not accessible by user {SUPABASE_USER_EMAIL}"
                    )
                    return {}

                meter_info = result[0]
                session_data = (
                    meter_info.pop("last_metering_hardware_install_session_id", None) or {}
                )
                commissioning_data = session_data.get("last_meter_commissioning_id") or {}
                meter_info["commissioning_date"] = commissioning_data.get("created_at")
                meter_info["commissioning_status"] = commissioning_data.get(
                    "meter_commissioning_status"
                )
                meter_id = meter_info.get("id")
                logger.info(
                    f"Retrieved info for meter {meter_no} (accessed by {SUPABASE_USER_EMAIL}): {meter_info}"
                )

                # Query last token from directives
                if meter_id:
                    try:
                        directive_url = f"{SUPABASE_URL}/rest/v1/directives"
                        directive_params = {
                            "meter_id": f"eq.{meter_id}",
                            "token": "not.is.null",
                            "select": "token,directive_type,created_at",
                            "order": "created_at.desc",
                            "limit": "1",
                        }

                        async with session.get(
                            directive_url, headers=headers, params=directive_params
                        ) as directive_response:
                            if directive_response.status == 200:
                                directive_result = await directive_response.json()
                                if directive_result:
                                    directive = directive_result[0]
                                    if directive.get("token"):
                                        meter_info["last_token"] = directive["token"]
                                        meter_info["last_token_type"] = directive.get(
                                            "directive_type"
                                        )
                                        meter_info["last_token_created_at"] = directive.get(
                                            "created_at"
                                        )
                    except Exception as e:
                        logger.warning(f"Could not get last token for meter {meter_no}: {e}")

                # Remove internal ID before returning
                meter_info.pop("id", None)
                return dict(meter_info)

        except Exception as e:
            logger.error(f"Error querying meter info for {meter_no}: {str(e)}")
            return {}

    def _get_v1_credentials(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        company_name: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """Get V1 API credentials, using environment variables as defaults"""
        return (
            base_url or CALIN_V1_BASE_URL,
            username or CALIN_V1_USERNAME,
            password or CALIN_V1_PASSWORD,
            company_name or CALIN_V1_COMPANY,
        )

    def _get_v2_credentials(
        self,
        base_url: Optional[str] = None,
        username: Optional[str] = None,
        password: Optional[str] = None,
        company: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """Get V2 API credentials, using environment variables as defaults"""
        return (
            base_url or CALIN_V2_BASE_URL,
            username or CALIN_V2_USERNAME,
            password or CALIN_V2_PASSWORD,
            company or CALIN_V2_COMPANY,
        )

    def _get_chirpstack_credentials(self) -> Tuple[str, str, str]:
        """Get Chirpstack API credentials from environment"""
        return (CHIRPSTACK_BASE_URL, CHIRPSTACK_API_KEY, CHIRPSTACK_TENANT_ID)

    async def _retry_api_call(self, api_func, api_version: str, *args, **kwargs) -> Dict[str, Any]:
        """
        Retry an API call with 5-second delay on failure.
        Returns result with availability note if API is unavailable.
        """
        try:
            # First attempt
            return await api_func(*args, **kwargs)  # type: ignore[no-any-return]
        except Exception:
            # Wait 5 seconds before retry
            await asyncio.sleep(5)
            try:
                # Second attempt
                return await api_func(*args, **kwargs)  # type: ignore[no-any-return]
            except Exception as retry_error:
                # API unavailable after retry
                return {
                    "error": str(retry_error),
                    "availability_note": f"{api_version} API was not available at the moment. The API may be experiencing transient downtime. Please try again later.",
                    "api_available": False,
                }

    # ===============================================
    # Calin V1 API Methods (PLC-based meters)
    # ===============================================

    async def _v1_send_target_state_impl(
        self, meter_no: str, target: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_send_target_state"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/COMM_RemoteControl"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "MeterNo": meter_no,
            "DataItem": f"Switch {target}",
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        task_id = result.get("Result", {}).get("TaskNo")
        if not task_id:
            raise Exception(
                f"Calin V1 did not return task ID for meter {meter_no}: {result.get('Reason')}"
            )

        return {"taskId": task_id}

    async def v1_send_target_state(
        self, meter_no: str, target: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Send target state (On/Off) to meter via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_send_target_state_impl, "Calin V1", meter_no, target, is_simulated
        )

    async def _v1_generate_topup_token_impl(
        self, meter_number: str, kwh: float, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_generate_topup_token"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/POS_Purchase"
        data = {
            "company_name": company_name,
            "user_name": username,
            "password": password,
            "password_vend": password,
            "meter_number": meter_number,
            "is_vend_by_unit": True,
            "amount": kwh,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        token = ((result or {}).get("result") or {}).get("token")
        if not token:
            raise Exception(
                f"Calin V1 did not return token for meter {meter_number}: {(result or {}).get('reason')}"
            )

        return {"token": token}

    async def v1_generate_topup_token(
        self, meter_number: str, kwh: float, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Generate top-up token via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_generate_topup_token_impl, "Calin V1", meter_number, kwh, is_simulated
        )

    async def _v1_generate_power_limit_token_impl(
        self, meter_number: str, power_limit: float, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_generate_power_limit_token"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/Maintenance_SetMaxPower"
        data = {
            "company_name": company_name,
            "user_name": username,
            "password": password,
            "meter_number": meter_number,
            "max_power": power_limit,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        token = result.get("result")
        if not token:
            raise Exception(
                f"Calin V1 did not return power limit token for meter {meter_number}: {result.get('reason')}"
            )

        return {"token": token}

    async def v1_generate_power_limit_token(
        self, meter_number: str, power_limit: float, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Generate power limit token via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_generate_power_limit_token_impl,
            "Calin V1",
            meter_number,
            power_limit,
            is_simulated,
        )

    async def _v1_generate_clear_tamper_token_impl(
        self, meter_number: str, issue_date: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_generate_clear_tamper_token"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/Maintenance_ClearTamper"
        data = {
            "company_name": company_name,
            "user_name": username,
            "password": password,
            "meter_number": meter_number,
            "issue_date": issue_date,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        token = result.get("result")
        if not token:
            raise Exception(
                f"Calin V1 did not return tamper token for meter {meter_number}: {result.get('reason')}"
            )

        return {"token": token}

    async def v1_generate_clear_tamper_token(
        self, meter_number: str, issue_date: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Generate clear tamper token via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_generate_clear_tamper_token_impl,
            "Calin V1",
            meter_number,
            issue_date,
            is_simulated,
        )

    async def _v1_generate_clear_credit_token_impl(
        self, meter_number: str, issue_date: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_generate_clear_credit_token"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/Maintenance_ClearCredit"
        data = {
            "company_name": company_name,
            "user_name": username,
            "password": password,
            "meter_number": meter_number,
            "issue_date": issue_date,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        token = result.get("result")
        if not token:
            raise Exception(
                f"Calin V1 did not return clear credit token for meter {meter_number}: {result.get('reason')}"
            )

        return {"token": token}

    async def v1_generate_clear_credit_token(
        self, meter_number: str, issue_date: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Generate clear credit token via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_generate_clear_credit_token_impl,
            "Calin V1",
            meter_number,
            issue_date,
            is_simulated,
        )

    async def _v1_send_token_impl(
        self, meter_no: str, token: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_send_token"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/COMM_RemoteToken"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "MeterNo": meter_no,
            "Token": token,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        task_id = result.get("Result", {}).get("TaskNo")
        if not task_id:
            raise Exception(
                f"Calin V1 did not return task ID for meter {meter_no}: {result.get('Reason')}"
            )

        return {"taskId": task_id}

    async def v1_send_token(
        self, meter_no: str, token: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Send token to meter via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_send_token_impl, "Calin V1", meter_no, token, is_simulated
        )

    async def _v1_send_remote_reading_impl(
        self, meter_no: str, data_item: str, is_simulated: bool = False
    ) -> Dict[str, Any]:
        """Internal implementation of v1_send_remote_reading"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/COMM_RemoteReading"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "MeterNo": meter_no,
            "DataItem": data_item,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        task_id = result.get("Result", {}).get("TaskNo")
        if not task_id:
            raise Exception(
                f"Calin V1 did not return task ID for meter {meter_no}: {result.get('Reason')}"
            )

        return {"taskId": task_id}

    async def v1_send_remote_reading(
        self,
        meter_no: str,
        data_item: str,
        is_simulated: bool = False,
        auto_check_status: bool = True,
    ) -> Dict[str, Any]:
        """
        Send remote reading request via Calin V1 with retry.

        Args:
            meter_no: Meter number
            data_item: Reading type/data item
            is_simulated: Simulation flag
            auto_check_status: If True, waits 15 seconds and checks status automatically

        Returns:
            If auto_check_status=True: Complete result with reading data
            If auto_check_status=False: Just the task ID for manual status checking
        """
        # Step 1: Send downlink command
        result = await self._retry_api_call(
            self._v1_send_remote_reading_impl, "Calin V1", meter_no, data_item, is_simulated
        )
        task_id = result.get("taskId")

        if not auto_check_status:
            return result

        # Step 2: Wait 15 seconds for meter to respond
        logger.info(f"Waiting 15 seconds for meter {meter_no} to respond to reading request...")
        await asyncio.sleep(15)

        # Step 3: Check task status
        logger.info(f"Checking reading task status for meter {meter_no}, task {task_id}...")
        status_result = await self.v1_get_task_status(task_id, "reading", is_simulated)

        return {
            "taskId": task_id,
            "meter_no": meter_no,
            "reading_type": data_item,
            "status": status_result,
        }

    async def _v1_get_task_status_impl(
        self,
        task_no: str,
        task_type: str,  # 'control', 'reading', or 'token'
        is_simulated: bool = False,
    ) -> Dict[str, Any]:
        """Internal implementation of v1_get_task_status"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        # Determine endpoint based on task type
        endpoints = {
            "control": "/COMM_RemoteControlTask",
            "reading": "/COMM_RemoteReadingTask",
            "token": "/COMM_RemoteTokenTask",
        }

        url = f"{base_url}{endpoints[task_type]}"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "TaskNo": task_no,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        return dict(result)

    async def v1_get_task_status(
        self,
        task_no: str,
        task_type: str,  # 'control', 'reading', or 'token'
        is_simulated: bool = False,
    ) -> Dict[str, Any]:
        """Get task status via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_get_task_status_impl, "Calin V1", task_no, task_type, is_simulated
        )

    async def _v1_get_hourly_data_impl(
        self, meter_no: str, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        """Internal implementation of v1_get_hourly_data"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/COMM_HourlyDataNew"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "MeterNo": meter_no,
            "StartDate": start_date,
            "EndDate": end_date,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        return dict(result)

    async def v1_get_hourly_data(
        self, meter_no: str, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        """Get hourly data via Calin V1 with retry"""
        return await self._retry_api_call(
            self._v1_get_hourly_data_impl, "Calin V1", meter_no, start_date, end_date
        )

    async def _v1_get_online_status_impl(self, meter_no: str) -> Dict[str, Any]:
        """Internal implementation of v1_get_online_status"""
        base_url, username, password, company_name = self._get_v1_credentials()
        session = await self.get_session()

        url = f"{base_url}/COMM_OnlineStatus"
        data = {
            "CompanyName": company_name,
            "UserName": username,
            "Password": password,
            "MeterNo": meter_no,
        }

        async with session.post(url, json=data) as response:
            result = await response.json()

        return dict(result)

    async def v1_get_online_status(self, meter_no: str) -> Dict[str, Any]:
        """Get online status via Calin V1 with retry"""
        return await self._retry_api_call(self._v1_get_online_status_impl, "Calin V1", meter_no)

    # ===============================================
    # Calin V2 API Methods (RF-based meters)
    # ===============================================

    async def v2_login(self, base_url: str, username: str, password: str, company: str) -> str:
        """Login to Calin V2 and get token"""
        session = await self.get_session()

        url = f"{base_url}/API/User/Login"
        credentials = {"userId": username, "password": password, "company": company}

        logger.debug(f"Logging into Calin V2 API: {base_url} for user {username}")

        async with session.post(
            url, json=credentials, timeout=aiohttp.ClientTimeout(total=5)
        ) as response:
            result = await response.json()

        token = ((result or {}).get("result") or {}).get("token")
        if not token:
            raise Exception(f"Calin V2 login failed: {(result or {}).get('reason')}")

        # Decode token to get expiry time
        decoded = jwt.decode(token, options={"verify_signature": False})
        expiry_time = decoded.get("exp", 0) * 1000

        # Store in cache with compound key
        cache_key = (base_url, username, company)
        self.v2_token_cache[cache_key] = (token, expiry_time)

        # Keep legacy storage for backward compatibility
        self.v2_token = token
        self.v2_token_expiry = expiry_time

        logger.debug(
            f"Cached V2 token for {username}@{company}, expires: {datetime.fromtimestamp(expiry_time / 1000)}"
        )

        return str(token)

    async def v2_ensure_token(
        self, base_url: str, username: str, password: str, company: str
    ) -> str:
        """Ensure we have a valid V2 token with automatic refresh"""
        cache_key = (base_url, username, company)
        current_time = datetime.now().timestamp() * 1000

        # Check if we have a cached token for this combination
        if cache_key in self.v2_token_cache:
            cached_token, cached_expiry = self.v2_token_cache[cache_key]

            # Check if token is still valid (with 30 second buffer)
            if cached_expiry and cached_expiry - current_time > 30000:
                logger.debug(f"Using cached V2 token for {username}@{company}")
                return cached_token
            else:
                logger.debug(f"Cached token expired for {username}@{company}, refreshing...")

        # Clean up expired tokens periodically (every 10th call)
        if len(self.v2_token_cache) > 0 and hash(cache_key) % 10 == 0:
            self._cleanup_expired_tokens()

        # Need to login/refresh
        return await self.v2_login(base_url, username, password, company)

    def _cleanup_expired_tokens(self):
        """Clean up expired tokens from cache to prevent memory leaks"""
        current_time = datetime.now().timestamp() * 1000
        expired_keys = []

        for cache_key, (token, expiry) in self.v2_token_cache.items():
            if expiry and expiry <= current_time:
                expired_keys.append(cache_key)

        for key in expired_keys:
            del self.v2_token_cache[key]
            logger.debug(f"Cleaned up expired token for {key[1]}@{key[2]}")

    def get_token_cache_status(self) -> Dict[str, Any]:
        """Get current token cache status for debugging"""
        current_time = datetime.now().timestamp() * 1000
        active_tokens: List[Dict[str, Any]] = []
        expired_tokens: List[Dict[str, Any]] = []

        for cache_key, (token, expiry) in self.v2_token_cache.items():
            base_url, username, company = cache_key
            token_info: Dict[str, Any] = {
                "base_url": base_url,
                "username": username,
                "company": company,
                "expires_at": datetime.fromtimestamp(expiry / 1000).isoformat() if expiry else None,
                "is_valid": expiry and expiry > current_time,
            }

            if token_info["is_valid"]:
                active_tokens.append(token_info)
            else:
                expired_tokens.append(token_info)

        cache_status = {
            "total_cached_tokens": len(self.v2_token_cache),
            "active_tokens": active_tokens,
            "expired_tokens": expired_tokens,
        }

        return cache_status

    async def _v2_send_target_state_impl(
        self,
        customer_id: str,
        meter_id: str,
        target: str,  # 'On' or 'Off'
    ) -> Dict[str, Any]:
        """Internal implementation of v2_send_target_state"""
        # Get credentials from environment
        base_url, username, password, company = self._get_v2_credentials()

        # Automatically handle OAuth authentication with caching
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        protocol_id = 20000 if target == "On" else 20001

        url = f"{base_url}/API/RemoteMeterTask/CreateControlTask"
        data = [
            {
                "customerId": customer_id,
                "meterId": meter_id,
                "protocolId": protocol_id,
                "company": company,
            }
        ]

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        task_id = result.get("result", [{}])[0].get("id")
        if not task_id:
            raise Exception(f"Calin V2 did not return task ID for meter {meter_id}")

        return {"taskId": task_id}

    async def v2_send_target_state(
        self,
        customer_id: str,
        meter_id: str,
        target: str,  # 'On' or 'Off'
    ) -> Dict[str, Any]:
        """Send target state via Calin V2 (OAuth handled automatically) with retry"""
        return await self._retry_api_call(
            self._v2_send_target_state_impl, "Calin V2", customer_id, meter_id, target
        )

    async def v2_generate_topup_token(
        self, meter_id: str, kwh: float, pos_password: str, serial_number: str, issue_date: str
    ) -> Dict[str, Any]:
        """Generate top-up token via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Token/CreditToken/Generate"
        data = {
            "meterId": meter_id,
            "isPreview": False,
            "isVendByTotalPaid": False,
            "amount": kwh,
            "authorizationPassword": pos_password,
            "serialNumber": serial_number,
            "company": company,
            "issueDate": issue_date,
        }

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        credit_token = ((result or {}).get("result") or {}).get("token")
        if not credit_token:
            raise Exception(f"Calin V2 did not return token for meter {meter_id}")

        return {"token": credit_token}

    async def v2_generate_power_limit_token(
        self, meter_id: str, power_limit: float, issue_date: str
    ) -> Dict[str, Any]:
        """Generate power limit token via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Token/SetMaximumPowerLimitToken/Generate"
        data = {
            "meterId": meter_id,
            "maximumPower": power_limit,
            "company": company,
            "issueDate": issue_date,
        }

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        power_token = ((result or {}).get("result") or {}).get("token")
        if not power_token:
            raise Exception(f"Calin V2 did not return power limit token for meter {meter_id}")

        return {"token": power_token}

    async def v2_generate_clear_tamper_token(
        self, meter_id: str, issue_date: str
    ) -> Dict[str, Any]:
        """Generate clear tamper token via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Token/ClearTamperToken/Generate"
        data = {"meterId": meter_id, "company": company, "issueDate": issue_date}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        tamper_token = ((result or {}).get("result") or {}).get("token")
        if not tamper_token:
            raise Exception(f"Calin V2 did not return tamper token for meter {meter_id}")

        return {"token": tamper_token}

    async def v2_generate_clear_credit_token(
        self, meter_id: str, issue_date: str
    ) -> Dict[str, Any]:
        """Generate clear credit token via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Token/ClearCreditToken/Generate"
        data = {"meterId": meter_id, "company": company, "issueDate": issue_date}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        credit_token = ((result or {}).get("result") or {}).get("token")
        if not credit_token:
            raise Exception(f"Calin V2 did not return clear credit token for meter {meter_id}")

        return {"token": credit_token}

    async def v2_send_token(
        self, customer_id: str, meter_id: str, token_data: str
    ) -> Dict[str, Any]:
        """Send token via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/RemoteMeterTask/CreateTokenTask"
        data = [
            {
                "customerId": customer_id,
                "meterId": meter_id,
                "protocolId": 30000,
                "data": token_data,
                "company": company,
            }
        ]

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        task_id = result.get("result", [{}])[0].get("id")
        if not task_id:
            raise Exception(f"Calin V2 did not return task ID for meter {meter_id}")

        return {"taskId": task_id}

    async def v2_send_remote_reading(
        self, customer_id: str, meter_id: str, protocol_id: int, auto_check_status: bool = True
    ) -> Dict[str, Any]:
        """
        Send remote reading request via Calin V2.

        Args:
            customer_id: Customer ID
            meter_id: Meter ID
            protocol_id: Protocol ID for reading type
            auto_check_status: If True, waits 15 seconds and checks status automatically

        Returns:
            If auto_check_status=True: Complete result with reading data
            If auto_check_status=False: Just the task ID for manual status checking
        """
        # Step 1: Send downlink command
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/RemoteMeterTask/CreateReadingTask"
        data = [
            {
                "customerId": customer_id,
                "meterId": meter_id,
                "protocolId": protocol_id,
                "company": company,
            }
        ]

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        task_id = result.get("result", [{}])[0].get("id")
        if not task_id:
            raise Exception(f"Calin V2 did not return task ID for meter {meter_id}")

        if not auto_check_status:
            return {"taskId": task_id}

        # Step 2: Wait 15 seconds for meter to respond
        logger.info(f"Waiting 15 seconds for meter {meter_id} to respond to reading request...")
        await asyncio.sleep(15)

        # Step 3: Check task status
        logger.info(f"Checking reading task status for meter {meter_id}, task {task_id}...")
        status_result = await self.v2_get_task_status(task_id, "reading")

        return {
            "taskId": task_id,
            "meter_id": meter_id,
            "protocol_id": protocol_id,
            "status": status_result,
        }

    async def v2_get_task_status(
        self,
        task_id: int,
        task_type: str,  # 'control', 'reading', or 'token'
    ) -> Dict[str, Any]:
        """Get task status via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        # Determine endpoint based on task type
        endpoints = {
            "control": "/API/RemoteMeterTask/GetControlTask",
            "reading": "/API/RemoteMeterTask/GetReadingTask",
            "token": "/API/RemoteMeterTask/GetTokenTask",
        }

        url = f"{base_url}{endpoints[task_type]}"
        data = {"id": task_id, "lang": "en", "company": company}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    async def v2_get_daily_report(
        self, concentrator_id: str, meter_id: str, date: str
    ) -> Dict[str, Any]:
        """Get daily report via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/DailyReport/Read"
        data = {
            "concentratorId": concentrator_id,
            "meterId": meter_id,
            "date": date,
            "company": company,
        }

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    async def v2_get_concentrator_online_status(self, concentrator_id: str) -> Dict[str, Any]:
        """Get concentrator online status via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/ConcentratorOnlineStatus/Read"
        data = {"concentratorId": concentrator_id, "company": company}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    async def v2_read_concentrator_file(self, file_path: str) -> Dict[str, Any]:
        """Read concentrator file via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/ConcentratorFile/Read"
        data = {"filePath": file_path, "company": company}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    async def v2_create_concentrator(self, concentrator_data: Dict[str, Any]) -> Dict[str, Any]:
        """Create concentrator via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Concentrator/Create"
        data = {**concentrator_data, "company": company}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    async def v2_delete_concentrator(self, concentrator_id: str) -> Dict[str, Any]:
        """Delete concentrator via Calin V2"""
        base_url, username, password, company = self._get_v2_credentials()
        token = await self.v2_ensure_token(base_url, username, password, company)
        session = await self.get_session()

        url = f"{base_url}/API/Concentrator/Delete"
        data = {"concentratorId": concentrator_id, "company": company}

        headers = {"Authorization": f"Bearer {token}"}

        async with session.post(url, json=data, headers=headers) as response:
            result = await response.json()

        return dict(result)

    # ===============================================
    # LoRaWAN/Chirpstack API Methods (Calin Protocol)
    # ===============================================

    def _generate_token(self) -> str:
        """Generate random token for downlink-uplink correlation"""
        import secrets

        return secrets.token_hex(8)

    def _format_meter_number(self, meter_no: str) -> str:
        """Format meter number as 12-digit string with leading zeros"""
        return meter_no.zfill(12)

    def _encode_calin_reading_command(self, reading_type: str, phase: str = "A") -> List[int]:
        """
        Encode Calin LoRaWAN reading command bytes.

        Command format: [0xB6, 0xXY]
        - 0xB6: Command prefix for meter readings
        - 0xXY: Phase and type (X=type, Y=phase)
          - Type 1: Voltage (0x1X where X=1,2,3 for phases A,B,C)
          - Type 2: Current (0x2X where X=1,2,3 for phases A,B,C)
          - Type 3: Power (0x3X where X=0,1,2 for phases A,B,C)
        """
        # Phase mapping: A=1, B=2, C=3 (for voltage/current)
        # Phase mapping: A=0, B=1, C=2 (for power)
        phase_map_vc = {"A": 1, "B": 2, "C": 3}  # Voltage/Current
        phase_map_p = {"A": 0, "B": 1, "C": 2}  # Power

        reading_map = {
            "voltage": (0x10, phase_map_vc),
            "current": (0x20, phase_map_vc),
            "power": (0x30, phase_map_p),
            "current_credit": (0xB6, None),  # Special case
            "relay_status": (0xB7, None),  # Special case
            "energy": (0xB8, None),  # Special case
        }

        if reading_type not in reading_map:
            raise Exception(f"Unsupported LoRaWAN reading type: {reading_type}")

        base_code, phase_mapping = reading_map[reading_type]

        if phase_mapping:
            # Phase-specific reading
            phase_code = phase_mapping.get(phase, 1)
            return [0xB6, base_code | phase_code]
        else:
            # Non-phase-specific reading
            return [base_code]

    def _encode_plr_command(self, state: str) -> List[int]:
        """
        Encode Power Line Relay (PLR) command.

        PLR command structure (21 bytes):
        [0x00, 0x01, 0x00, 0x66, 0x00, 0x01, 0x00, 0x0D, 0xC0, 0x01,
         0xC1, 0x00, 0x47, 0x00, 0x00, 0x11, 0x00, 0x00, 0xFF, 0xXX, 0x00]

        Where XX = 0x03 for ON, 0x04 for OFF
        """
        state_code = 0x03 if state.lower() == "on" else 0x04

        return [
            0x00,
            0x01,
            0x00,
            0x66,
            0x00,
            0x01,
            0x00,
            0x0D,
            0xC0,
            0x01,
            0xC1,
            0x00,
            0x47,
            0x00,
            0x00,
            0x11,
            0x00,
            0x00,
            0xFF,
            state_code,
            0x00,
        ]

    async def _lorawan_get_gateway_status_impl(self, gateway_id: str) -> Dict[str, Any]:
        """Internal implementation of lorawan_get_gateway_status (base station online status)"""
        base_url, api_key, tenant_id = self._get_chirpstack_credentials()
        session = await self.get_session()

        url = f"{base_url}/api/gateways/{gateway_id}"
        headers = {"Authorization": f"Bearer {api_key}", "Grpc-Metadata-X-Tenant-Id": tenant_id}

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                raise Exception(f"Chirpstack API returned status {response.status}")

            result = await response.json()

        return dict(result)

    async def lorawan_get_gateway_status(self, gateway_id: str) -> Dict[str, Any]:
        """Get LoRaWAN gateway (base station) online status via Chirpstack with retry"""
        return await self._retry_api_call(
            self._lorawan_get_gateway_status_impl, "Chirpstack", gateway_id
        )

    async def _lorawan_send_downlink_impl(
        self,
        dev_eui: str,
        meter_no: str,
        command_bytes: List[int],
        request_type: str,
        phase: str = "A",
        fport: int = 1,
    ) -> Dict[str, Any]:
        """
        Internal implementation of lorawan_send_downlink (send Calin downlink command).

        Args:
            dev_eui: Device EUI
            meter_no: 12-digit meter number
            command_bytes: Command byte array
            request_type: Type of request (READ_VOLTAGE, READ_CURRENT, etc.)
            phase: Phase (A/B/C) for phase-specific commands
            fport: LoRaWAN port number (default: 1)

        Returns:
            Dict with taskId (token), fCntDown, and command details
        """
        import base64

        base_url, api_key, tenant_id = self._get_chirpstack_credentials()
        session = await self.get_session()

        # Generate correlation token
        token = self._generate_token()

        # Format meter number
        formatted_meter = self._format_meter_number(meter_no)

        # Encode command bytes to base64
        command_data = base64.b64encode(bytes(command_bytes)).decode("utf-8")

        url = f"{base_url}/api/devices/{dev_eui}/queue"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Grpc-Metadata-X-Tenant-Id": tenant_id,
            "Content-Type": "application/json",
        }

        payload = {
            "deviceQueueItem": {
                "devEui": dev_eui,
                "confirmed": True,  # Always use confirmed for Calin commands
                "fPort": fport,
                "data": command_data,
            }
        }

        async with session.post(url, json=payload, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(
                    f"Chirpstack downlink failed (status {response.status}): {error_text}"
                )

            result = await response.json()

        return {
            "taskId": token,  # Use token as task ID for correlation
            "fCntDown": result.get("fCntDown"),
            "meter_number": formatted_meter,
            "request_type": request_type,
            "directive_phase": phase,
            "command_bytes": command_bytes,
            "note": "Uplink response will be correlated using this token. Check device uplink messages in Chirpstack.",
        }

    async def lorawan_send_downlink(
        self,
        dev_eui: str,
        meter_no: str,
        command_bytes: List[int],
        request_type: str,
        phase: str = "A",
        fport: int = 1,
    ) -> Dict[str, Any]:
        """Send Calin downlink command to LoRaWAN meter via Chirpstack with retry"""
        return await self._retry_api_call(
            self._lorawan_send_downlink_impl,
            "Chirpstack",
            dev_eui,
            meter_no,
            command_bytes,
            request_type,
            phase,
            fport,
        )

    async def _lorawan_create_reading_task_impl(
        self, dev_eui: str, meter_no: str, reading_type: str, phase: str = "A"
    ) -> Dict[str, Any]:
        """
        Internal implementation of lorawan_create_reading_task.

        Creates a Calin LoRaWAN reading task using proper command encoding.
        """
        # Encode reading command based on type
        command_bytes = self._encode_calin_reading_command(reading_type, phase)

        # Map reading type to request type string
        request_type_map = {
            "voltage": f"READ_VOLTAGE_PHASE_{phase}",
            "current": f"READ_CURRENT_PHASE_{phase}",
            "power": f"READ_POWER_PHASE_{phase}",
            "current_credit": "READ_CURRENT_CREDIT",
            "relay_status": "READ_RELAY_STATUS",
            "energy": "READ_ENERGY",
        }

        request_type = request_type_map.get(reading_type, f"READ_{reading_type.upper()}")

        # Send downlink command
        result = await self._lorawan_send_downlink_impl(
            dev_eui=dev_eui,
            meter_no=meter_no,
            command_bytes=command_bytes,
            request_type=request_type,
            phase=phase,
            fport=1,
        )

        return result

    async def lorawan_create_reading_task(
        self,
        dev_eui: str,
        meter_no: str,
        reading_type: str,
        phase: str = "A",
        auto_check_status: bool = True,
    ) -> Dict[str, Any]:
        """
        Create reading task for Calin LoRaWAN meter with retry.

        Args:
            dev_eui: Device EUI
            meter_no: Meter number
            reading_type: Type of reading
            phase: Phase (A/B/C)
            auto_check_status: If True, waits 15 seconds and notes uplink check needed

        Returns:
            If auto_check_status=True: Result with note about checking uplink
            If auto_check_status=False: Just the task ID/token
        """
        # Step 1: Send downlink command
        result = await self._retry_api_call(
            self._lorawan_create_reading_task_impl,
            "Chirpstack",
            dev_eui,
            meter_no,
            reading_type,
            phase,
        )

        if not auto_check_status:
            return result

        # Step 2: Wait 15 seconds for meter to respond via uplink
        logger.info(f"Waiting 15 seconds for LoRaWAN meter {meter_no} to respond via uplink...")
        await asyncio.sleep(15)

        # Step 3: Note about uplink correlation
        # For LoRaWAN, the response comes via uplink which needs to be processed
        # by the webhook endpoint, not a direct API call
        token = result.get("taskId")
        return {
            **result,
            "wait_completed": True,
            "uplink_check_note": f"15 second wait completed. Check Chirpstack device uplinks for meter {meter_no} with correlation token {token}. The meter response will arrive as an uplink message.",
            "next_step": "Query Chirpstack uplink messages or webhook data to get the meter reading response",
        }

    async def _lorawan_send_relay_control_impl(
        self,
        dev_eui: str,
        meter_no: str,
        state: str,  # "on" or "off"
    ) -> Dict[str, Any]:
        """Internal implementation of lorawan_send_relay_control (PLR command)"""
        # Encode PLR command
        command_bytes = self._encode_plr_command(state)

        request_type = f"PLR_{state.upper()}"

        # Send downlink command
        result = await self._lorawan_send_downlink_impl(
            dev_eui=dev_eui,
            meter_no=meter_no,
            command_bytes=command_bytes,
            request_type=request_type,
            phase="A",  # PLR is not phase-specific
            fport=1,
        )

        return result

    async def lorawan_send_relay_control(
        self, dev_eui: str, meter_no: str, state: str
    ) -> Dict[str, Any]:
        """Send relay control (on/off) to Calin LoRaWAN meter with retry"""
        return await self._retry_api_call(
            self._lorawan_send_relay_control_impl, "Chirpstack", dev_eui, meter_no, state
        )

    async def _lorawan_send_token_impl(
        self, dev_eui: str, meter_no: str, token: str
    ) -> Dict[str, Any]:
        """
        Internal implementation of lorawan_send_token.

        Note: Token format and encoding depends on meter protocol.
        This is a placeholder - actual implementation needs meter-specific encoding.
        """

        # Token encoding will depend on Calin LoRaWAN token protocol
        # This is a simplified version - actual implementation may differ
        token_bytes = list(token.encode("utf-8"))

        result = await self._lorawan_send_downlink_impl(
            dev_eui=dev_eui,
            meter_no=meter_no,
            command_bytes=token_bytes,
            request_type="SEND_TOKEN",
            phase="A",
            fport=1,
        )

        return result

    async def lorawan_send_token(self, dev_eui: str, meter_no: str, token: str) -> Dict[str, Any]:
        """Send STS token to Calin LoRaWAN meter with retry"""
        return await self._retry_api_call(
            self._lorawan_send_token_impl, "Chirpstack", dev_eui, meter_no, token
        )

    # ===============================================
    # Unified Interface Methods (Route based on meter type)
    # ===============================================

    async def unified_get_dcu_status(
        self,
        meter_no: str,
        user_email: str,
        dcu_id: Optional[str] = None,
        gateway_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unified method to get DCU/concentrator/base station online status.
        Automatically routes to V1/V2/LoRaWAN based on meter type from Supabase.
        Automatically retrieves dcu_id and gateway_id from Supabase if not provided.

        Args:
            meter_no: Meter number to look up type
            user_email: User email for RLS authentication (required)
            dcu_id: DCU/concentrator ID (optional - auto-retrieved from Supabase if not provided)
            gateway_id: LoRaWAN gateway ID (optional - auto-retrieved from Supabase if not provided)
        """
        # Set current user email for this request
        self.current_user_email = user_email

        # Get complete meter info from Supabase
        meter_info = await self.get_meter_info(meter_no)
        if not meter_info:
            raise Exception(f"Meter {meter_no} not found in Supabase")

        meter_type_str = meter_info.get("meter_type", "").lower()
        type_mapping = {
            "calin_v1": MeterType.CALIN_V1,
            "calin_v2": MeterType.CALIN_V2,
            "lorawan": MeterType.LORAWAN,
        }
        meter_type = type_mapping.get(meter_type_str, MeterType.UNKNOWN)

        if meter_type == MeterType.CALIN_V1:
            # V1 uses meter online status endpoint (no DCU ID needed)
            return await self.v1_get_online_status(meter_no)

        elif meter_type == MeterType.CALIN_V2:
            # V2 uses concentrator online status - get dcu_id from Supabase if not provided
            dcu_id = dcu_id or meter_info.get("dcu_id")
            if not dcu_id:
                raise Exception(
                    f"dcu_id not provided and not found in Supabase for meter {meter_no}"
                )
            return await self.v2_get_concentrator_online_status(dcu_id)

        elif meter_type == MeterType.LORAWAN:
            # LoRaWAN uses gateway status - get gateway_id from Supabase if not provided
            gateway_id = gateway_id or meter_info.get("gateway_id")
            if not gateway_id:
                raise Exception(
                    f"gateway_id not provided and not found in Supabase for meter {meter_no}"
                )
            return await self.lorawan_get_gateway_status(gateway_id)

        else:
            raise Exception(f"Unknown meter type for meter {meter_no}")

    async def find_meters_by_device_id(self, device_id: str) -> List[Dict[str, Any]]:
        """
        Find meters served by a DCU/concentrator or LoRaWAN gateway (base station).

        Queries the Supabase 'meters' table by dcu_id or gateway_id. Used to resolve
        a device ID (as referenced in alert tickets) to a device type and the meters
        it serves, without requiring a meter number up front.
        """
        try:
            access_token = await self.supabase_ensure_token()
            session = await self.get_session()

            url = f"{SUPABASE_URL}/rest/v1/meters"
            headers = {
                "apikey": SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            }
            params = {
                "or": f"(dcu_id.eq.{device_id},gateway_id.eq.{device_id})",
                "select": "meter_no,meter_type,dcu_id,gateway_id",
                "limit": "10",
            }

            async with session.get(url, headers=headers, params=params) as response:
                if response.status != 200:
                    logger.warning(
                        f"Supabase device lookup failed for {device_id}: {response.status}"
                    )
                    return []
                return list(await response.json() or [])
        except Exception as e:
            logger.warning(f"Supabase device lookup failed for {device_id}: {e}")
            return []

    async def unified_get_device_status_by_id(
        self,
        device_id: str,
        user_email: str,
        device_type: Optional[str] = None,
        matched_meters: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """
        Get online status for a DCU/concentrator or LoRaWAN base station by its
        device ID, without requiring a meter number (alert tickets reference the
        device ID directly, e.g. 'DCU 230401080' or 'Base Station a84041ffff29d4da').

        Device type is resolved from the Supabase 'meters' table when not provided,
        falling back to the ID format (gateway EUIs are 16-char hex, DCU IDs numeric).

        Args:
            device_id: DCU/concentrator ID or LoRaWAN gateway/base station ID
            user_email: User email for RLS authentication (required)
            device_type: Optional 'dcu' or 'base_station' to skip auto-detection
            matched_meters: Pre-fetched result of find_meters_by_device_id (optional)
        """
        self.current_user_email = user_email

        if matched_meters is None:
            matched_meters = await self.find_meters_by_device_id(device_id)
        matched_types = {(m.get("meter_type") or "").lower() for m in matched_meters}

        resolved_type = (device_type or "").lower().replace(" ", "_") or None
        if resolved_type == "gateway":
            resolved_type = "base_station"
        if not resolved_type:
            if any(m.get("gateway_id") == device_id for m in matched_meters):
                resolved_type = "base_station"
            elif any(m.get("dcu_id") == device_id for m in matched_meters):
                resolved_type = "dcu"
            else:
                # No meter record found - infer from ID format
                is_hex_eui = len(device_id) >= 16 and not device_id.isdigit()
                resolved_type = "base_station" if is_hex_eui else "dcu"

        if resolved_type == "base_station":
            status = await self.lorawan_get_gateway_status(device_id)
        elif matched_types == {"calin_v1"}:
            # V1 has no DCU-level status endpoint; use a served meter's online
            # status as proxy (same behavior as unified_get_dcu_status for V1)
            status = await self.v1_get_online_status(matched_meters[0]["meter_no"])
        else:
            status = await self.v2_get_concentrator_online_status(device_id)

        return {
            "device_id": device_id,
            "device_type": resolved_type,
            "meters_on_device": [m.get("meter_no") for m in matched_meters],
            "status": status,
        }

    async def unified_create_reading_task(
        self,
        meter_no: str,
        reading_type: str,
        user_email: str,
        customer_id: Optional[str] = None,
        dev_eui: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unified method to create remote reading task.
        Automatically routes to V1/V2/LoRaWAN based on meter type from Supabase.
        Automatically retrieves customer_id and dev_eui from Supabase if not provided.

        Args:
            meter_no: Meter number to look up type
            reading_type: Type of reading (e.g., 'voltage', 'Current Credit', protocol_id for V2)
            user_email: User email for RLS authentication (required)
            customer_id: Customer ID (optional - auto-retrieved from Supabase for V2 meters)
            dev_eui: Device EUI (optional - auto-retrieved from Supabase for LoRaWAN meters)
        """
        # Set current user email for this request
        self.current_user_email = user_email

        # Get complete meter info from Supabase
        meter_info = await self.get_meter_info(meter_no)
        if not meter_info:
            raise Exception(f"Meter {meter_no} not found in Supabase")

        meter_type_str = meter_info.get("meter_type", "").lower()
        type_mapping = {
            "calin_v1": MeterType.CALIN_V1,
            "calin_v2": MeterType.CALIN_V2,
            "lorawan": MeterType.LORAWAN,
        }
        meter_type = type_mapping.get(meter_type_str, MeterType.UNKNOWN)

        if meter_type == MeterType.CALIN_V1:
            # V1 uses data_item string
            return await self.v1_send_remote_reading(meter_no, reading_type)

        elif meter_type == MeterType.CALIN_V2:
            # V2 uses protocol_id (integer) - get customer_id from Supabase if not provided
            customer_id = customer_id or meter_info.get("customer_id")
            if not customer_id:
                raise Exception(
                    f"customer_id not provided and not found in Supabase for meter {meter_no}"
                )

            # Map reading type to protocol ID if string provided
            protocol_mapping = {
                "voltage": 5,
                "power": 11,
                "current_credit": 39,
                "relay_status": 37,
                "power_down_count": 47,
                "maximum_power_threshold": 46,
                "special_status": 43,
                "meter_version": 45,
            }

            # Try to convert to int if numeric string, otherwise use mapping
            try:
                protocol_id = int(reading_type)
            except ValueError:
                protocol_id = protocol_mapping.get(reading_type.lower().replace(" ", "_"))
                if protocol_id is None:
                    raise Exception(f"Unknown reading type for V2: {reading_type}")

            return await self.v2_send_remote_reading(customer_id, meter_no, protocol_id)

        elif meter_type == MeterType.LORAWAN:
            # LoRaWAN uses downlink commands with Calin protocol - get dev_eui from Supabase if not provided
            dev_eui = dev_eui or meter_info.get("dev_eui")
            if not dev_eui:
                raise Exception(
                    f"dev_eui not provided and not found in Supabase for meter {meter_no}"
                )
            return await self.lorawan_create_reading_task(dev_eui, meter_no, reading_type)

        else:
            raise Exception(f"Unknown meter type for meter {meter_no}")

    async def unified_get_reading_task_status(
        self, meter_no: str, task_id: str, user_email: str
    ) -> Dict[str, Any]:
        """
        Unified method to get reading task status.
        Automatically routes to V1/V2/LoRaWAN based on meter type from Supabase.

        Args:
            meter_no: Meter number to look up type
            task_id: Task ID from create_reading_task
            user_email: User email for RLS authentication (required)
        """
        # Set current user email for this request
        self.current_user_email = user_email

        meter_type = await self.get_meter_type(meter_no)

        if meter_type == MeterType.CALIN_V1:
            return await self.v1_get_task_status(task_id, "reading")

        elif meter_type == MeterType.CALIN_V2:
            # V2 task IDs are integers
            return await self.v2_get_task_status(int(task_id), "reading")

        elif meter_type == MeterType.LORAWAN:
            # For LoRaWAN, task status would need to query uplink messages
            # This is a simplified implementation
            return {
                "taskId": task_id,
                "status": "Check uplink messages in Chirpstack for meter response",
                "note": "LoRaWAN reading tasks are asynchronous - check device uplink data",
            }

        else:
            raise Exception(f"Unknown meter type for meter {meter_no}")

    async def unified_send_power_limit_token(
        self,
        meter_no: str,
        power_limit: float,
        user_email: str,
        customer_id: Optional[str] = None,
        dev_eui: Optional[str] = None,
        issue_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unified method to send power limit token (write operation).
        Automatically routes to V1/V2/LoRaWAN based on meter type from Supabase.
        Automatically retrieves customer_id from Supabase if not provided.

        Args:
            meter_no: Meter number to look up type
            power_limit: Power limit in watts
            user_email: User email for RLS authentication (required)
            customer_id: Customer ID (optional - auto-retrieved from Supabase for V2 meters)
            dev_eui: Device EUI (not used for this operation)
            issue_date: Issue date (required for V2 tokens, e.g., '2024-01-01T00:00:00Z')
        """
        # Set current user email for this request
        self.current_user_email = user_email

        # Get complete meter info from Supabase
        meter_info = await self.get_meter_info(meter_no)
        if not meter_info:
            raise Exception(f"Meter {meter_no} not found in Supabase")

        meter_type_str = meter_info.get("meter_type", "").lower()
        type_mapping = {
            "calin_v1": MeterType.CALIN_V1,
            "calin_v2": MeterType.CALIN_V2,
            "lorawan": MeterType.LORAWAN,
        }
        meter_type = type_mapping.get(meter_type_str, MeterType.UNKNOWN)

        if meter_type == MeterType.CALIN_V1:
            # V1: Generate token then send it
            token_result = await self.v1_generate_power_limit_token(meter_no, power_limit)
            send_result = await self.v1_send_token(meter_no, token_result["token"])
            return {**token_result, **send_result}

        elif meter_type == MeterType.CALIN_V2:
            # V2: Generate token then send it - get customer_id from Supabase if not provided
            customer_id = customer_id or meter_info.get("customer_id")
            if not customer_id:
                raise Exception(
                    f"customer_id not provided and not found in Supabase for meter {meter_no}"
                )
            if not issue_date:
                raise Exception(
                    "issue_date required for Calin V2 power limit tokens (e.g., '2024-01-01T00:00:00Z')"
                )

            token_result = await self.v2_generate_power_limit_token(
                meter_no, power_limit, issue_date
            )
            send_result = await self.v2_send_token(customer_id, meter_no, token_result["token"])
            return {**token_result, **send_result}

        elif meter_type == MeterType.LORAWAN:
            # LoRaWAN: Power limit tokens not supported via Calin LoRaWAN protocol
            # Use relay control (on/off) instead
            raise Exception(
                "Power limit tokens not supported for LoRaWAN meters. Use relay control (on/off) instead."
            )

        else:
            raise Exception(f"Unknown meter type for meter {meter_no}")

    async def unified_send_token(
        self,
        meter_no: str,
        token: str,
        user_email: str,
        customer_id: Optional[str] = None,
        dev_eui: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Unified method to send token to meter (write operation).
        Automatically routes to V1/V2/LoRaWAN based on meter type from Supabase.
        Automatically retrieves customer_id and dev_eui from Supabase if not provided.

        Args:
            meter_no: Meter number to look up type
            token: Token string to send
            user_email: User email for RLS authentication (required)
            customer_id: Customer ID (optional - auto-retrieved from Supabase for V2 meters)
            dev_eui: Device EUI (optional - auto-retrieved from Supabase for LoRaWAN meters)
        """
        # Set current user email for this request
        self.current_user_email = user_email

        # Get complete meter info from Supabase
        meter_info = await self.get_meter_info(meter_no)
        if not meter_info:
            raise Exception(f"Meter {meter_no} not found in Supabase")

        meter_type_str = meter_info.get("meter_type", "").lower()
        type_mapping = {
            "calin_v1": MeterType.CALIN_V1,
            "calin_v2": MeterType.CALIN_V2,
            "lorawan": MeterType.LORAWAN,
        }
        meter_type = type_mapping.get(meter_type_str, MeterType.UNKNOWN)

        if meter_type == MeterType.CALIN_V1:
            return await self.v1_send_token(meter_no, token)

        elif meter_type == MeterType.CALIN_V2:
            # Get customer_id from Supabase if not provided
            customer_id = customer_id or meter_info.get("customer_id")
            if not customer_id:
                raise Exception(
                    f"customer_id not provided and not found in Supabase for meter {meter_no}"
                )
            return await self.v2_send_token(customer_id, meter_no, token)

        elif meter_type == MeterType.LORAWAN:
            # Get dev_eui from Supabase if not provided
            dev_eui = dev_eui or meter_info.get("dev_eui")
            if not dev_eui:
                raise Exception(
                    f"dev_eui not provided and not found in Supabase for meter {meter_no}"
                )
            return await self.lorawan_send_token(dev_eui, meter_no, token)

        else:
            raise Exception(f"Unknown meter type for meter {meter_no}")


# Global client instance
client = MetersAPIClient()


async def _verify_meter_org_access(meter_no: str, user_email: str) -> Optional[str]:
    """Return an error string if meter_no is not owned by user_email's org, else None.

    Staff (STAFF_ORG_ID) bypass the check. Uses AUTH_DB meters table with
    rls_organization_id for the same org-scoping that customer_server applies.
    """
    import asyncpg as _asyncpg

    auth_service = get_auth_service()
    permissions = await auth_service.get_user_permissions(email=user_email)
    if not permissions or not permissions.organization_ids:
        return "User not found or has no organization"

    org_id = int(permissions.organization_ids[0])
    if org_id == STAFF_ORG_ID:
        return None

    conn = await _asyncpg.connect(
        host=os.getenv("AUTH_DB_HOST"),
        port=int(os.getenv("AUTH_DB_PORT", "6543")),
        user=os.getenv("AUTH_DB_USER"),
        password=os.getenv("AUTH_DB_PASSWORD"),
        database=os.getenv("AUTH_DB_NAME", "postgres"),
        ssl="require",
        statement_cache_size=0,
    )
    try:
        row = await conn.fetchrow(
            "SELECT id FROM meters WHERE external_reference = $1 AND rls_organization_id = $2 LIMIT 1",
            meter_no,
            org_id,
        )
    finally:
        await conn.close()

    if not row:
        return f"Meter {meter_no} is not accessible for your organization"
    return None


@registry.tool("get_meter_dcu_status", _SCHEMAS_BY_NAME["get_meter_dcu_status"])
async def _tool_get_meter_dcu_status(arguments: Dict[str, Any]) -> List[types.TextContent]:
    user_email = arguments.get("user_email")
    if not user_email:
        result = {"error": "Authentication required: user_email missing from request"}
    else:
        org_error = await _verify_meter_org_access(arguments["meter_no"], user_email)
        if org_error:
            result = {"error": org_error}
        else:
            result = await client.unified_get_dcu_status(
                meter_no=arguments["meter_no"],
                user_email=user_email,
                dcu_id=arguments.get("dcu_id"),
                gateway_id=arguments.get("gateway_id"),
            )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


@registry.tool("get_dcu_status_by_id", _SCHEMAS_BY_NAME["get_dcu_status_by_id"])
async def _tool_get_dcu_status_by_id(arguments: Dict[str, Any]) -> List[types.TextContent]:
    user_email = arguments.get("user_email")
    if not user_email:
        result = {"error": "Authentication required: user_email missing from request"}
    else:
        device_id = arguments["device_id"]
        matched_meters = await client.find_meters_by_device_id(device_id)

        # Org scoping: staff can query any device; customers only devices
        # that serve at least one meter belonging to their organization
        auth_service = get_auth_service()
        permissions = await auth_service.get_user_permissions(email=user_email)
        if not permissions or not permissions.organization_ids:
            result = {"error": "User not found or has no organization"}
        else:
            org_error = None
            if int(permissions.organization_ids[0]) != STAFF_ORG_ID:
                if not matched_meters:
                    org_error = f"Device {device_id} is not accessible for your organization"
                else:
                    org_error = await _verify_meter_org_access(
                        matched_meters[0]["meter_no"], user_email
                    )
            if org_error:
                result = {"error": org_error}
            else:
                result = await client.unified_get_device_status_by_id(
                    device_id=device_id,
                    user_email=user_email,
                    device_type=arguments.get("device_type"),
                    matched_meters=matched_meters,
                )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


@registry.tool("create_meter_reading_task", _SCHEMAS_BY_NAME["create_meter_reading_task"])
async def _tool_create_meter_reading_task(arguments: Dict[str, Any]) -> List[types.TextContent]:
    user_email = arguments.get("user_email")
    if not user_email:
        result = {"error": "Authentication required: user_email missing from request"}
    else:
        org_error = await _verify_meter_org_access(arguments["meter_no"], user_email)
        if org_error:
            result = {"error": org_error}
        else:
            result = await client.unified_create_reading_task(
                meter_no=arguments["meter_no"],
                reading_type=arguments["reading_type"],
                user_email=user_email,
                customer_id=arguments.get("customer_id"),
                dev_eui=arguments.get("dev_eui"),
            )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


@registry.tool(
    "get_meter_reading_task_status", _SCHEMAS_BY_NAME["get_meter_reading_task_status"]
)
async def _tool_get_meter_reading_task_status(
    arguments: Dict[str, Any],
) -> List[types.TextContent]:
    user_email = arguments.get("user_email")
    if not user_email:
        result = {"error": "Authentication required: user_email missing from request"}
    else:
        result = await client.unified_get_reading_task_status(
            meter_no=arguments["meter_no"],
            task_id=arguments["task_id"],
            user_email=user_email,
        )
    return [types.TextContent(type="text", text=json.dumps(result, indent=2))]


handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def main():
    """Main entry point"""
    await run_stdio_server(
        server,
        name="meters-api",
        label="Meters",
        capabilities=server.get_capabilities(
            notification_options=NotificationOptions(), experimental_capabilities={}
        ),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Meters server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Meters server crashed: {e}", file=sys.stderr)
        sys.exit(1)
