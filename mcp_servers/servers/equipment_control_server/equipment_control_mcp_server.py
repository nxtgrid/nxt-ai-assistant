"""MCP Equipment Control Server - Handles production equipment control operations."""

import asyncio
import json
import os
import socket
import ssl
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import aiohttp
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.types import (
    Resource,
    ServerCapabilities,
    TextContent,
    Tool,
)

# Load environment variables from .env file BEFORE importing shared_code
load_dotenv()

from shared_code.config.action_flags import ActionFlags
from shared_code.config.settings import server_settings
from shared_code.database.connections import db_manager
from shared_code.utils.logger import setup_logger

from shared.auth import get_auth_service
from shared.utils.email_utils import parse_email_whitelist
from shared.utils.response_formatters import compose_error_response, compose_json_response

logger = setup_logger("equipment-control-server")

# Startup message to stderr
print("🚀 Equipment Control MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

# Initialize MCP server
server = Server("equipment-control-server")

# VRM MQTT Configuration (direct MQTT connection to Victron)
# Auth: username=VRM_MQTT_USER (email), password=VRM_MQTT_PASSWORD ("Token <personal-access-token>")
# Broker is per-installation: sum(ord(c) for c in portal_id) % 128 → mqtt{n}.victronenergy.com
VRM_MQTT_PORT = 8883


def _get_vrm_broker(portal_id: str) -> str:
    return f"mqtt{sum(ord(c) for c in portal_id.lower().strip()) % 128}.victronenergy.com"


VRM_MQTT_USER = os.getenv("VRM_MQTT_USER")  # VRM account email address
VRM_MQTT_PASSWORD = os.getenv(
    "VRM_MQTT_PASSWORD"
)  # VRM personal access token, formatted as "Token <token>"
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# VRM REST API Configuration (for checking site online status)
VRM_API_BASE = "https://vrmapi.victronenergy.com/v2"
VRM_TOKEN = os.getenv("VRM_TOKEN")

# Permission required for equipment control
EQUIPMENT_CONTROL_PERMISSION = "equipment.control"

# Whitelist of users allowed to run equipment control commands
# This is the FIRST gate - checked before all other permission checks
# Format: comma-separated email addresses
EQUIPMENT_CONTROL_ALLOWED_USERS_ENV = os.getenv("EQUIPMENT_CONTROL_ALLOWED_USERS", "")

# Rate limits for equipment control actions (in minutes)
# These prevent repeated actions on the same site within the specified time window
RATE_LIMITS = {
    "restart_inverter": 30,  # 30 minutes minimum between inverter restarts
    "restart_comms_chain": 15,  # 15 minutes minimum between comms chain restarts
}

# Auto-scheduling configuration for follow-up checks
# After equipment control actions, schedule a check to verify success
FOLLOWUP_DELAYS = {
    "restart_inverter": 2,  # Check 2 minutes after inverter restart
    "restart_comms_chain": 10,  # Check 10 minutes after comms reboot (longer reconnect time)
}

# Per-gateway asyncio locks to serialize concurrent control commands per gateway.
# Prevents TOCTOU race between check_rate_limit (SELECT) and log_equipment_action (INSERT)
# where two concurrent callers both read zero recent rows and both proceed.
_gateway_locks: Dict[str, asyncio.Lock] = {}


def check_user_whitelist(user_email: str) -> tuple[bool, Optional[str]]:
    """
    Check if user is in the equipment control whitelist.

    This is the FIRST gate before any other permission checks.
    Uses tolerant parsing: handles commas, semicolons, whitespace, case-insensitive.
    Returns:
        (allowed, error_message) - allowed=True if user can proceed
    """
    if not EQUIPMENT_CONTROL_ALLOWED_USERS_ENV:
        return False, "Equipment control is not configured (no allowed users in whitelist)"

    # Use shared utility for tolerant parsing (handles commas, semicolons, whitespace)
    allowed_users = parse_email_whitelist(EQUIPMENT_CONTROL_ALLOWED_USERS_ENV)

    if not allowed_users:
        return False, "Equipment control is not configured (whitelist is empty)"

    if not user_email:
        return False, "User email not provided"

    # Case-insensitive comparison (whitelist is already lowercase)
    if user_email.lower() not in allowed_users:
        logger.warning(
            f"User {user_email} not in equipment control whitelist. Allowed: {allowed_users}"
        )
        return False, f"User {user_email} is not authorized for equipment control"

    logger.info(f"User {user_email} is in equipment control whitelist")
    return True, None


async def check_user_permission(user_email: str) -> bool:
    """Check if user has equipment control permission using AuthService."""
    try:
        # Use AuthService to get user permissions
        auth_service = get_auth_service()
        permissions = await auth_service.get_user_permissions(user_email)

        # Admin role has all permissions
        if permissions.is_admin:
            logger.info(f"User {user_email} is admin - granting equipment control access")
            return True

        # Check for specific equipment control permission
        # Note: permissions.roles is a list of role strings, not individual permissions
        # If you have a permissions field in UserPermissions, you'd check that instead
        # For now, we'll check if it's in the roles list
        if EQUIPMENT_CONTROL_PERMISSION in permissions.roles:
            logger.info(f"User {user_email} has {EQUIPMENT_CONTROL_PERMISSION} permission")
            return True

        logger.warning(
            f"User {user_email} does not have {EQUIPMENT_CONTROL_PERMISSION} permission. "
            f"Roles: {permissions.roles}, Admin: {permissions.is_admin}"
        )
        return False

    except Exception as e:
        logger.error(f"Error checking user permission: {e}")
        import traceback

        logger.error(traceback.format_exc())
        return False


async def _ensure_db() -> bool:
    """Lazily initialize db_manager if not already connected."""
    if not db_manager.supabase_client:
        await db_manager.initialize_supabase()
    return db_manager.supabase_client is not None


async def log_equipment_action(
    action_name: str,
    grid_name: str,
    site_id: str,
    requester_email: str,
    chat_id: Optional[str],
    session_id: Optional[str],
    success: bool,
    error_message: Optional[str] = None,
    api_response: Optional[dict] = None,
) -> None:
    """Log equipment action to audit table."""
    if not await _ensure_db():
        logger.warning("Supabase not available - action not logged to audit table")
        return

    try:
        action_data = {
            "action_name": action_name,
            "grid_name": grid_name,
            "site_id": site_id,
            "requester_email": requester_email,
            "chat_id": chat_id,
            "session_id": session_id,
            "success": success,
            "error_message": error_message,
            "api_response": api_response,
        }

        await asyncio.to_thread(
            db_manager.supabase_client.table("equipment_actions").insert(action_data).execute
        )
        logger.info(f"Logged equipment action: {action_name} on {grid_name} by {requester_email}")

    except Exception as e:
        logger.error(f"Failed to log equipment action: {e}")


async def get_vrm_ids_from_grid(
    grid_name: str,
) -> tuple[Optional[str], Optional[str], Optional[str], bool]:
    """Get VRM IDs from grids table by grid name.

    Uses AuthService to query the AUTH database where the grids table lives.

    VRM uses two different IDs:
    - generation_external_site_id: For VRM REST API calls (checking if site is online)
    - generation_external_gateway_id: For MQTT commands (actual equipment control)

    Also returns whether the grid's generation is managed by the operator.
    If False, equipment control should not be available.

    Returns:
        Tuple of (site_id, gateway_id, actual_grid_name, is_generation_managed)
        - site_id, gateway_id, actual_grid_name may be None if not found
        - is_generation_managed is False if grid not found or flag is NULL/False
    """
    try:
        auth_service = get_auth_service()
        return await auth_service.get_grid_vrm_ids(grid_name)

    except Exception as e:
        logger.error(f"Error looking up grid VRM IDs: {e}")
        return (None, None, None, False)


async def get_similar_grid_names(grid_name: str, limit: int = 3) -> List[str]:
    """Get similar grid names for suggestions when a grid is not found.

    Uses fuzzy matching to find the closest matching grid names.

    Args:
        grid_name: The grid name that wasn't found
        limit: Maximum number of suggestions to return

    Returns:
        List of similar grid names, sorted by similarity score
    """
    try:
        auth_service = get_auth_service()
        all_grids = await auth_service.get_all_grid_names()

        if not all_grids:
            return []

        # Use rapidfuzz to find similar names
        from rapidfuzz import fuzz, process

        # Get top matches with scores
        matches = process.extract(
            grid_name,
            all_grids,
            scorer=fuzz.token_set_ratio,
            limit=limit,
        )

        # Return names with score > 50 (somewhat similar)
        return [match[0] for match in matches if match[1] > 50]

    except Exception as e:
        logger.error(f"Error getting similar grid names: {e}")
        return []


async def check_site_online(site_id: str) -> tuple[bool, Optional[str]]:
    """
    Check if a VRM site is online by querying the VRM REST API.

    Uses the system-overview endpoint to check the last data timestamp.
    A site is considered online if it has reported data within the last 2 minutes.

    IMPORTANT: This function uses generation_external_site_id (for REST API),
    NOT generation_external_gateway_id (which is for MQTT commands).

    Args:
        site_id: VRM site ID (generation_external_site_id from grids table)

    Returns:
        (is_online: bool, error_message: str or None)
        - If is_online=False, error_message explains why
    """
    if not VRM_TOKEN:
        logger.warning("VRM_TOKEN not configured - skipping online check")
        # If we can't check, assume online and let the command try
        return True, None

    try:
        headers = {
            "X-Authorization": f"Token {VRM_TOKEN}",
            "Content-Type": "application/json",
        }

        # Query the system-overview endpoint for last timestamp
        url = f"{VRM_API_BASE}/installations/{site_id}/system-overview"

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as response:
                if response.status == 200:
                    data = await response.json()
                    records = data.get("records", {})

                    # Find the Gateway device and check its lastConnection timestamp
                    devices = records.get("devices", [])
                    gateway_device = None
                    for device in devices:
                        if device.get("name") == "Gateway":
                            gateway_device = device
                            break

                    if gateway_device:
                        last_connection = gateway_device.get("lastConnection")
                        if last_connection:
                            # Convert to datetime and check if recent (within 2 min)
                            last_time = datetime.fromtimestamp(last_connection, tz=timezone.utc)
                            now = datetime.now(timezone.utc)
                            age_minutes = (now - last_time).total_seconds() / 60

                            if age_minutes <= 15:
                                # Log with site_id for debugging (server-side only)
                                logger.info(
                                    f"Site is online (Gateway lastConnection {age_minutes:.1f} min ago)"
                                )
                                return True, None
                            else:
                                logger.warning(
                                    f"Site appears offline (Gateway lastConnection {age_minutes:.1f} min ago)"
                                )
                                return (
                                    False,
                                    f"Site appears to be offline - Gateway last connected {age_minutes:.0f} minutes ago",
                                )

                    # No Gateway device or no timestamp - assume online and let command try
                    logger.info("Site has no Gateway device or lastConnection - assuming online")
                    return True, None

                elif response.status == 404:
                    # Log the ID for debugging, but don't expose to user
                    logger.error("Site not found in VRM (site_id logged for debugging)")
                    return False, "Site not found in VRM - please contact support"

                else:
                    await response.text()
                    logger.error(f"VRM API error checking site status: {response.status}")
                    # On API error, let the command try anyway
                    return True, None

    except asyncio.TimeoutError:
        logger.warning("Timeout checking site status - assuming online")
        return True, None
    except Exception as e:
        logger.error(f"Error checking site online status: {e}")
        # On error, let the command try anyway
        return True, None


async def check_inverter_status(site_id: str) -> Dict[str, Any]:
    """
    Check inverter output voltages to determine if the inverter is actively
    producing power. Used as a pre-check before restart_inverter.

    Uses VRMPlatform for voltage reading (shared with /grid and /grids commands).
    Blocks restart if any phase voltage exceeds the ON threshold (50V).

    Returns dict with voltages, is_producing, should_block.
    On API failure, returns should_block=False so pre-checks never block due to API errors.
    """
    from servers.equipment_diagnostics_server.platforms.vrm_platform import VRMPlatform

    voltages: Dict[str, Optional[float]] = {"l1": None, "l2": None, "l3": None}
    is_producing = False

    if not VRM_TOKEN:
        logger.warning("VRM_TOKEN not configured - skipping inverter pre-check")
        return {
            "voltages": voltages,
            "is_producing": is_producing,
            "should_block": False,
            "error": "VRM_TOKEN not configured",
        }

    try:
        vrm = VRMPlatform()
        await vrm.initialize()

        voltage_result = await vrm.get_current_inverter_voltage(site_id)
        voltages["l1"] = voltage_result.l1_voltage_v
        voltages["l2"] = voltage_result.l2_voltage_v
        voltages["l3"] = voltage_result.l3_voltage_v
        is_producing = voltage_result.is_producing

        should_block = is_producing

        logger.info(
            f"Inverter pre-check for site {site_id}: "
            f"voltages={voltages}, producing={is_producing}, block={should_block}"
        )

        return {
            "voltages": voltages,
            "is_producing": is_producing,
            "should_block": should_block,
        }

    except Exception as e:
        logger.warning(f"Inverter pre-check failed (allowing command): {e}")
        return {
            "voltages": voltages,
            "is_producing": is_producing,
            "should_block": False,
            "error": str(e),
        }


async def check_dcu_status(grid_name: str) -> Dict[str, Any]:
    """
    Check DCU online/offline status for a grid.

    Queries the Auth DB via AuthService for DCU connectivity information.
    Never blocks the command - just provides status info for inclusion in response.

    Returns dict with total, online, offline counts and list of offline DCU names.
    On failure, returns zeroes with an error key.
    """
    total = 0
    online = 0
    offline = 0
    offline_dcus: List[str] = []

    try:
        auth_service = get_auth_service()
        dcu_rows = await auth_service.get_dcu_status_by_grid_name(grid_name)

        if not dcu_rows:
            return {
                "total": total,
                "online": online,
                "offline": offline,
                "offline_dcus": offline_dcus,
                "error": "No DCUs found for this grid",
            }

        total = len(dcu_rows)
        for dcu in dcu_rows:
            if dcu["is_online"]:
                online += 1
            else:
                offline += 1
                if dcu["external_reference"]:
                    offline_dcus.append(dcu["external_reference"])

        # Build visual: 📶📶🅇 for 2 online, 1 offline (same as /grids)
        visual = "📶" * online + "🅇" * (total - online) if total > 0 else "N/A"

        logger.info(f"DCU pre-check for {grid_name}: {online}/{total} online, {offline} offline")

        return {
            "total": total,
            "online": online,
            "offline": offline,
            "visual": visual,
            "offline_dcus": offline_dcus,
        }

    except Exception as e:
        logger.warning(f"DCU pre-check failed: {e}")
        return {
            "total": total,
            "online": online,
            "offline": offline,
            "offline_dcus": offline_dcus,
            "error": str(e),
        }


async def check_rate_limit(action_name: str, portal_id: str) -> tuple[bool, Optional[dict]]:
    """
    Check if action is allowed based on rate limiting using portal_id (site ID).

    Rate limits ALL attempts (not just successful ones) to prevent abuse.

    Returns:
        (allowed: bool, last_action: dict or None)
        - If allowed=False, last_action contains the blocking action details
    """
    if not await _ensure_db():
        logger.warning("Supabase not available for rate limit check - allowing action")
        return True, None

    min_delay_minutes = RATE_LIMITS.get(action_name, 0)
    if min_delay_minutes == 0:
        return True, None

    try:
        cutoff_time = datetime.now(timezone.utc) - timedelta(minutes=min_delay_minutes)

        query = (
            db_manager.supabase_client.table("equipment_actions")
            .select("id, created_at, requester_email")
            .eq("action_name", action_name)
            .eq("site_id", portal_id)  # Use portal_id for rate limiting
            .gte("created_at", cutoff_time.isoformat())
            .order("created_at", desc=True)
            .limit(1)
        )
        result = await asyncio.to_thread(query.execute)

        if result.data and len(result.data) > 0:
            last_action = result.data[0]
            return False, last_action

        return True, None

    except Exception as e:
        logger.error(f"Error checking rate limit: {e}")
        # Fail open - allow action if rate limit check fails
        return True, None


async def schedule_followup_check(
    action_name: str,
    grid_name: str,
    chat_id: Optional[str],
    topic_id: Optional[str] = None,
    user_email: Optional[str] = None,
) -> Optional[str]:
    """
    Schedule a follow-up equipment check after a control action.

    Uses the same scheduled_messages table as the schedule_server to create
    a one-time command that will run after a delay.

    Args:
        action_name: The action that was performed (restart_inverter, restart_comms_chain)
        grid_name: Name of the grid
        chat_id: Telegram chat ID where the result should be sent
        topic_id: Optional topic/thread ID within the chat
        user_email: Email of the user who initiated the action

    Returns:
        Schedule ID if created, None if scheduling failed
    """
    if not await _ensure_db():
        logger.warning("Supabase not available - cannot schedule follow-up check")
        return None

    if not chat_id:
        logger.warning("No chat_id provided - cannot schedule follow-up check")
        return None

    # Get delay for this action type
    delay_minutes = FOLLOWUP_DELAYS.get(action_name, 5)

    # Build the follow-up command
    # Use /grid command for comprehensive status (power, DCU connectivity, etc.)
    command = f"/grid {grid_name}"

    # Generate schedule ID
    schedule_id = str(uuid.uuid4())

    # Calculate scheduled time
    scheduled_for = datetime.now(timezone.utc) + timedelta(minutes=delay_minutes)

    # Build payload matching schedule_server format
    payload = {
        "schedule_id": schedule_id,
        "chat_id": chat_id,
        "topic_id": topic_id,
        "command": command,
        "user_context": {
            "user_email": user_email or "",
            "is_staff": True,  # Equipment control is staff-only
        },
        "metadata": {
            "source": "equipment_control_followup",
            "original_action": action_name,
            "grid_name": grid_name,
        },
    }

    try:
        query = db_manager.supabase_client.table("scheduled_messages").insert(
            {
                "message_type": "user_command",
                "payload": payload,
                "scheduled_for": scheduled_for.isoformat(),
                "created_by": user_email or "system",
            }
        )
        result = await asyncio.to_thread(query.execute)

        if result.data:
            logger.info(
                f"Scheduled follow-up check for {grid_name} in {delay_minutes} minutes "
                f"(schedule_id: {schedule_id}, chat_id: {chat_id})"
            )
            return schedule_id
        else:
            logger.error("Failed to insert scheduled follow-up check")
            return None

    except Exception as e:
        logger.error(f"Error scheduling follow-up check: {e}")
        return None


def send_mqtt_command(
    portal_id: str,
    device_type: str,
    instance: str,
    topic_stub: str,
    value: int,
) -> Dict[str, Any]:
    """Send MQTT command directly to VRM broker.

    Args:
        portal_id: VRM portal ID (generation_external_gateway_id from grids table)
        device_type: Device type (e.g., 'vebus', 'system')
        instance: Device instance (e.g., '276', '0')
        topic_stub: Topic stub (e.g., 'SystemReset', 'Reboot')
        value: Command value (e.g., 1)

    Returns:
        Dict with status and message

    Raises:
        Exception: If MQTT connection or publish fails
    """
    if not VRM_MQTT_USER or not VRM_MQTT_PASSWORD:
        raise Exception("VRM_MQTT_USER and VRM_MQTT_PASSWORD must be configured")

    broker = _get_vrm_broker(portal_id)
    topic = f"W/{portal_id}/{device_type}/{instance}/{topic_stub}"
    payload = json.dumps({"value": value})

    logger.info(f"Sending MQTT command to topic: {topic} via {broker}")

    # Track connection state
    connected = False

    def on_connect(client, userdata, flags, reason_code, properties=None):
        nonlocal connected
        if reason_code == 0:
            logger.info("MQTT connected successfully")
            connected = True
        else:
            logger.error(f"MQTT connection failed with code: {reason_code}")

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        logger.info(f"MQTT disconnected with code: {reason_code}")

    # Unique client ID per connection — reusing the same ID causes broker-side
    # "client taken over" disconnects when a prior connection is still lingering.
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"anansi_ec_{uuid.uuid4().hex[:8]}",
    )
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.username_pw_set(VRM_MQTT_USER, VRM_MQTT_PASSWORD)

    # Configure TLS
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    try:
        # Start the network loop BEFORE connecting (required for connection handshake)
        client.loop_start()

        # Bound the TCP connect to 15 s; the OS default can be 75-130 s
        prior_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(15)
        try:
            client.connect(broker, VRM_MQTT_PORT, 60)
        finally:
            socket.setdefaulttimeout(prior_timeout)

        # Wait for connection to be established (up to 10 seconds)
        for _ in range(100):
            if connected:
                break
            time.sleep(0.1)

        if not connected:
            raise Exception("MQTT connection timeout - failed to connect to broker")

        # Publish the command with QoS 1 (at least once delivery)
        result = client.publish(topic, payload, qos=1)

        # wait_for_publish returns None on timeout — check is_published() explicitly
        result.wait_for_publish(timeout=10)
        if not result.is_published():
            raise Exception(
                "MQTT publish confirmation timeout — broker did not acknowledge command"
            )

        logger.info(f"MQTT command sent successfully to {topic}")
        return {"status": "success", "message": f"Command sent to {topic}"}

    except Exception as e:
        logger.error(f"MQTT error: {e}")
        raise
    finally:
        # disconnect() first so the network thread exits cleanly, then join it
        client.disconnect()
        client.loop_stop()


@server.list_tools()
async def handle_list_tools() -> List[Tool]:
    """List available equipment control tools."""
    tools = [
        Tool(
            name="restart_inverter",
            description="[ACTION - RESTARTS PHYSICAL EQUIPMENT] Restart the inverter at a specific site (requires equipment.control permission). This tool PHYSICALLY RESTARTS inverter hardware at the site. CRITICAL SAFETY CHECK REQUIRED: Before calling this action, you MUST verify with the user that there is no cause for repeated shorts at the site. Restarting inverters without checking for underlying electrical faults could cause serious equipment damage or create safety hazards. Always confirm the user has investigated the root cause before proceeding.",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid": {"type": "string", "description": "Grid name"},
                    "user_email": {
                        "type": "string",
                        "description": "User email for permission check (required)",
                    },
                },
                "required": ["grid", "user_email"],
            },
            visible_to_customer=False,
        ),
        Tool(
            name="restart_comms_chain",
            description="[ACTION - RESTARTS PHYSICAL EQUIPMENT] Restart the communications chain at a specific site (requires equipment.control permission). This tool PHYSICALLY RESTARTS communication hardware (Cerbo, router, DCU) at the site, causing temporary downtime. This tool handles multiple reboot-related requests including: 'reboot comm chain', 'reboot cerbo', 'reboot router', and 'reboot DCU'. IMPORTANT: Before calling this action, you MUST verify with the user that the communications chain still has connectivity problems and that a restart is necessary. WARNING: When rebooting the DCU, only DCUs connected to the power plant will be rebooted - confirm this is the intended behavior with the user. Note that once restarted, it can take up to 10 minutes for the site to fully reconnect and resume normal operations. Always confirm this downtime is acceptable before proceeding.",
            inputSchema={
                "type": "object",
                "properties": {
                    "grid": {"type": "string", "description": "Grid name"},
                    "user_email": {
                        "type": "string",
                        "description": "User email for permission check (required)",
                    },
                },
                "required": ["grid", "user_email"],
            },
            visible_to_customer=False,
        ),
    ]

    logger.info(f"Equipment control server: {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[TextContent]:
    """Handle tool calls."""
    try:
        # Check if actions are enabled for equipment_control
        if not ActionFlags.is_actions_enabled("equipment_control"):
            return [
                TextContent(
                    type="text",
                    text="Equipment control actions are currently disabled. Set EQUIPMENT_CONTROL_ACTIONS_ENABLED=true to enable.",
                )
            ]

        # Extract user_email for permission check
        user_email = arguments.get("user_email")
        if not user_email:
            return [
                TextContent(
                    type="text", text="user_email is required for equipment control operations"
                )
            ]

        # Check user whitelist - this is the ONLY permission gate
        # The whitelist (EQUIPMENT_CONTROL_ALLOWED_USERS env var) is the source of truth
        # We removed the secondary database permission check because:
        # 1. The auth database doesn't have a roles column populated
        # 2. The whitelist is explicit and controlled via environment variable
        # 3. Having two permission systems is confusing and error-prone
        whitelist_allowed, whitelist_error = check_user_whitelist(user_email)
        if not whitelist_allowed:
            return [
                TextContent(
                    type="text",
                    text=f"❌ Access denied: {whitelist_error}",
                )
            ]

        # Extract context passed from orchestrator (for audit logging)
        chat_id = arguments.get("chat_id")
        session_id = arguments.get("session_id")

        # Get grid name and lookup VRM IDs
        # VRM uses two different IDs:
        # - site_id (generation_external_site_id): For REST API calls (checking if site is online)
        # - gateway_id (generation_external_gateway_id): For MQTT commands (actual equipment control)
        grid_name = arguments.get("grid")
        if not grid_name:
            return [TextContent(type="text", text="grid is required")]

        site_id, gateway_id, actual_grid_name, is_generation_managed = await get_vrm_ids_from_grid(
            grid_name
        )

        # Check if grid was found at all
        if not actual_grid_name:
            # Grid not found - get suggestions for similar names
            suggestions = await get_similar_grid_names(grid_name)
            if suggestions:
                suggestions_text = ", ".join(f"'{s}'" for s in suggestions)
                return [
                    TextContent(
                        type="text",
                        text=f"❌ Grid '{grid_name}' not found. Did you mean: {suggestions_text}?",
                    )
                ]
            else:
                return [
                    TextContent(
                        type="text",
                        text=f"❌ Grid '{grid_name}' not found. Please check the grid name and try again.",
                    )
                ]

        # Use the actual grid name (may have been fuzzy matched)
        grid_name = actual_grid_name

        # Check if generation is managed by the operator
        if not is_generation_managed:
            org_name = os.getenv("ORGANIZATION_NAME", "the operator")
            logger.info(
                f"Equipment control blocked for {grid_name}: generation not managed by {org_name}"
            )
            return [
                TextContent(
                    type="text",
                    text=f"❌ Equipment control is not available for '{grid_name}'. "
                    f"This grid's generation is not managed by {org_name}.",
                )
            ]

        # Validate we have both IDs needed for equipment control
        if not site_id:
            return [
                TextContent(
                    type="text",
                    text=f"❌ Grid '{grid_name}' found but VRM site is not configured. "
                    "Please contact support to set up VRM integration.",
                )
            ]
        if not gateway_id:
            return [
                TextContent(
                    type="text",
                    text=f"❌ Grid '{grid_name}' found but VRM gateway is not configured. "
                    "Please contact support to set up VRM integration.",
                )
            ]

        # Check if site is online before attempting any equipment control
        # Uses site_id (generation_external_site_id) for REST API check
        is_online, offline_error = await check_site_online(site_id)
        if not is_online:
            logger.warning(f"Site {grid_name} is offline - blocking {name}")
            # Log the blocked attempt (use gateway_id for rate limiting consistency)
            await log_equipment_action(
                action_name=name,
                grid_name=grid_name,
                site_id=gateway_id,  # Use gateway_id for audit log consistency
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message=f"Site offline: {offline_error}",
            )
            return [
                TextContent(
                    type="text",
                    text=f"❌ Cannot execute {name} on grid '{grid_name}': {offline_error}. "
                    f"The command cannot be processed while the site is offline.",
                )
            ]

        # Run pre-checks before routing to handlers
        pre_check_info: Dict[str, Any] = {}

        if name == "restart_inverter":
            # Check inverter status - block if inverter is healthy (producing + inverting)
            inverter_status = await check_inverter_status(site_id)
            pre_check_info["inverter_status"] = inverter_status

            if inverter_status.get("should_block"):
                voltages = inverter_status.get("voltages", {})
                voltage_parts = []
                for phase, label in [("l1", "L1"), ("l2", "L2"), ("l3", "L3")]:
                    v = voltages.get(phase)
                    if v is not None:
                        voltage_parts.append(f"{label}={v:.0f}V")
                voltage_str = ", ".join(voltage_parts) if voltage_parts else "unavailable"

                block_msg = (
                    f"The inverter on grid '{grid_name}' appears to be operating normally "
                    f"(output voltages: {voltage_str}). "
                    f"Restarting is not recommended while the inverter is actively producing power. "
                    f"If you still need to restart, please confirm the inverter is actually faulty."
                )
                logger.info(
                    f"Pre-check blocked restart_inverter for {grid_name}: voltages={voltages}"
                )

                # Log the blocked attempt
                await log_equipment_action(
                    action_name=name,
                    grid_name=grid_name,
                    site_id=gateway_id,
                    requester_email=user_email,
                    chat_id=chat_id,
                    session_id=session_id,
                    success=False,
                    error_message=f"Pre-check blocked: inverter producing ({voltage_str})",
                )

                return [TextContent(type="text", text=f"⚠️ {block_msg}")]

        elif name == "restart_comms_chain":
            # Check DCU status - never blocks, just provides info for the response
            dcu_status = await check_dcu_status(grid_name)
            pre_check_info["dcu_status"] = dcu_status

        # Route to appropriate handler (use gateway_id for MQTT commands)
        # Rate limiting is enforced inside each handler, right before MQTT command is sent
        if name == "restart_inverter":
            return await restart_inverter(
                gateway_id, grid_name, user_email, chat_id, session_id, pre_check_info
            )
        elif name == "restart_comms_chain":
            return await restart_comms_chain(
                gateway_id, grid_name, user_email, chat_id, session_id, pre_check_info
            )
        else:
            return [TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        logger.error(f"Error in tool {name}: {e}")
        return list(compose_error_response(e))


async def restart_inverter(
    gateway_id: str,
    grid_name: str,
    user_email: str,
    chat_id: Optional[str] = None,
    session_id: Optional[str] = None,
    pre_check_info: Optional[Dict[str, Any]] = None,
) -> List[TextContent]:
    """Restart the inverter at a site using direct MQTT.

    Args:
        gateway_id: VRM gateway ID (generation_external_gateway_id) for MQTT commands
        pre_check_info: Dict with inverter_status pre-check results
    """
    action_name = "restart_inverter"

    if gateway_id not in _gateway_locks:
        _gateway_locks[gateway_id] = asyncio.Lock()
    async with _gateway_locks[gateway_id]:
        # Check rate limit right before sending command (prevents code loops downstream)
        allowed, last_action = await check_rate_limit(action_name, gateway_id)
        if not allowed:
            last_time = last_action.get("created_at", "unknown")
            last_user = last_action.get("requester_email", "unknown")
            min_delay = RATE_LIMITS.get(action_name, 0)

            error_msg = (
                f"Rate limit exceeded: {action_name} was already performed on grid '{grid_name}' "
                f"at {last_time} by {last_user}. "
                f"Minimum delay is {min_delay} minutes between actions."
            )
            logger.warning(error_msg)

            # Log the denied attempt
            await log_equipment_action(
                action_name=action_name,
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message=f"Rate limited - last action at {last_time}",
            )

            return [TextContent(type="text", text=error_msg)]

        try:
            # Run blocking MQTT call off the event loop to avoid starving async tasks
            result = await asyncio.to_thread(
                send_mqtt_command,
                portal_id=gateway_id,
                device_type="vebus",
                instance="276",
                topic_stub="SystemReset",
                value=1,
            )

            # Response data - DO NOT include IDs (they are internal identifiers)
            response_data: Dict[str, Any] = {
                "action": "restart_inverter",
                "grid": grid_name,
                "status": "success",
                "user": user_email,
            }

            # Include pre-check voltages in response
            if pre_check_info and "inverter_status" in pre_check_info:
                inv = pre_check_info["inverter_status"]
                response_data["pre_check"] = {
                    "voltages": inv.get("voltages", {}),
                }

            # Log with ID for server-side debugging only
            logger.info(f"Inverter restart initiated by {user_email} for grid {grid_name}")

            # Log successful action to audit table
            await log_equipment_action(
                action_name="restart_inverter",
                grid_name=grid_name,
                site_id=gateway_id,  # Stored for audit/rate limiting purposes
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=True,
                api_response=result,
            )

            # Schedule follow-up check to verify inverter came back online
            # Result will be sent to the same chat that initiated the action
            followup_id = await schedule_followup_check(
                action_name="restart_inverter",
                grid_name=grid_name,
                chat_id=chat_id,
                user_email=user_email,
            )
            if followup_id:
                delay = FOLLOWUP_DELAYS.get("restart_inverter", 2)
                response_data["followup_scheduled"] = True
                response_data["followup_message"] = (
                    f"A follow-up grid status check has been scheduled for {delay} minutes "
                    f"from now to verify the inverter came back online."
                )

            return list(compose_json_response(response_data))

        except asyncio.CancelledError:
            logger.warning(f"Inverter restart cancelled mid-flight for grid {grid_name}")
            await log_equipment_action(
                action_name="restart_inverter",
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message="Request cancelled",
            )
            raise

        except Exception as e:
            logger.error(f"Error restarting inverter: {e}")

            # Log failed action to audit table
            await log_equipment_action(
                action_name="restart_inverter",
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message=str(e),
            )

            return list(compose_error_response(Exception(f"Failed to restart inverter: {str(e)}")))


async def restart_comms_chain(
    gateway_id: str,
    grid_name: str,
    user_email: str,
    chat_id: Optional[str] = None,
    session_id: Optional[str] = None,
    pre_check_info: Optional[Dict[str, Any]] = None,
) -> List[TextContent]:
    """Restart the communications chain at a site using direct MQTT.

    Args:
        gateway_id: VRM gateway ID (generation_external_gateway_id) for MQTT commands
        pre_check_info: Dict with dcu_status pre-check results
    """
    action_name = "restart_comms_chain"

    if gateway_id not in _gateway_locks:
        _gateway_locks[gateway_id] = asyncio.Lock()
    async with _gateway_locks[gateway_id]:
        # Check rate limit right before sending command (prevents code loops downstream)
        allowed, last_action = await check_rate_limit(action_name, gateway_id)
        if not allowed:
            last_time = last_action.get("created_at", "unknown")
            last_user = last_action.get("requester_email", "unknown")
            min_delay = RATE_LIMITS.get(action_name, 0)

            error_msg = (
                f"Rate limit exceeded: {action_name} was already performed on grid '{grid_name}' "
                f"at {last_time} by {last_user}. "
                f"Minimum delay is {min_delay} minutes between actions."
            )
            logger.warning(error_msg)

            # Log the denied attempt
            await log_equipment_action(
                action_name=action_name,
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message=f"Rate limited - last action at {last_time}",
            )

            return [TextContent(type="text", text=error_msg)]

        try:
            # Run blocking MQTT call off the event loop to avoid starving async tasks
            # Topic: W/{portal_id}/platform/0/Device/Reboot
            # See: https://github.com/victronenergy/venus/issues/1256
            result = await asyncio.to_thread(
                send_mqtt_command,
                portal_id=gateway_id,
                device_type="platform",
                instance="0",
                topic_stub="Device/Reboot",
                value=1,
            )

            # Response data - DO NOT include IDs (they are internal identifiers)
            response_data: Dict[str, Any] = {
                "action": "restart_comms_chain",
                "grid": grid_name,
                "status": "success",
                "user": user_email,
            }

            # Include DCU status before restart in response
            if pre_check_info and "dcu_status" in pre_check_info:
                dcu = pre_check_info["dcu_status"]
                dcu_total = dcu.get("total", 0)
                dcu_online = dcu.get("online", 0)
                dcu_visual = dcu.get("visual", "N/A")
                offline_dcus = dcu.get("offline_dcus", [])

                response_data["dcu_status_before_restart"] = (
                    f"DCU: {dcu_visual} ({dcu_online}/{dcu_total} online)"
                )
                if offline_dcus:
                    response_data["dcu_status_before_restart"] += (
                        f" — offline: {', '.join(offline_dcus)}"
                    )

            # Log with ID for server-side debugging only
            logger.info(f"Comms chain restart initiated by {user_email} for grid {grid_name}")

            # Log successful action to audit table
            await log_equipment_action(
                action_name="restart_comms_chain",
                grid_name=grid_name,
                site_id=gateway_id,  # Stored for audit/rate limiting purposes
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=True,
                api_response=result,
            )

            # Schedule follow-up check to verify site came back online
            # Result will be sent to the same chat that initiated the action
            followup_id = await schedule_followup_check(
                action_name="restart_comms_chain",
                grid_name=grid_name,
                chat_id=chat_id,
                user_email=user_email,
            )
            if followup_id:
                delay = FOLLOWUP_DELAYS.get("restart_comms_chain", 10)
                response_data["followup_scheduled"] = True
                response_data["followup_message"] = (
                    f"A follow-up grid status check has been scheduled for {delay} minutes "
                    f"from now to verify the site and DCUs came back online."
                )

            return list(compose_json_response(response_data))

        except asyncio.CancelledError:
            logger.warning(f"Comms chain restart cancelled mid-flight for grid {grid_name}")
            await log_equipment_action(
                action_name="restart_comms_chain",
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message="Request cancelled",
            )
            raise

        except Exception as e:
            logger.error(f"Error restarting comms chain: {e}")

            # Log failed action to audit table
            await log_equipment_action(
                action_name="restart_comms_chain",
                grid_name=grid_name,
                site_id=gateway_id,
                requester_email=user_email,
                chat_id=chat_id,
                session_id=session_id,
                success=False,
                error_message=str(e),
            )

            return list(
                compose_error_response(Exception(f"Failed to restart comms chain: {str(e)}"))
            )


@server.list_resources()
async def handle_list_resources() -> List[Resource]:
    """List available resources."""
    return [
        Resource(
            uri="equipment-control://config",
            name="Equipment Control Configuration",
            description="Current equipment control server configuration",
            mimeType="application/json",
        ),
        Resource(
            uri="equipment-control://status",
            name="Equipment Control Status",
            description="Equipment control API connection status",
            mimeType="application/json",
        ),
    ]


@server.read_resource()
async def handle_read_resource(uri: str) -> str:
    """Read resource content."""
    if uri == "equipment-control://config":
        config = {
            "mqtt_broker": "mqtt{n}.victronenergy.com (per-installation)",
            "mqtt_port": VRM_MQTT_PORT,
            "actions_enabled": ActionFlags.is_actions_enabled("equipment_control"),
            "server_name": server_settings.server_name,
            "server_version": server_settings.server_version,
            "rate_limits": RATE_LIMITS,
        }
        return json.dumps(config, indent=2)
    elif uri == "equipment-control://status":
        status = {
            "configured": VRM_MQTT_USER is not None and VRM_MQTT_PASSWORD is not None,
            "mqtt_broker": "mqtt{n}.victronenergy.com (per-installation)",
            "actions_enabled": ActionFlags.is_actions_enabled("equipment_control"),
        }
        return json.dumps(status, indent=2)
    else:
        raise ValueError(f"Unknown resource: {uri}")


async def main():
    """Main server function."""
    try:
        logger.info("Starting Equipment Control MCP Server...")
        print("✅ Equipment control server initialized successfully", file=sys.stderr)

        # Initialize Supabase connection for permission checks
        await db_manager.initialize_supabase()

        # Initialize server
        options = InitializationOptions(
            server_name="equipment-control-server",
            server_version="1.0.0",
            capabilities=ServerCapabilities(),
        )

        async with stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(read_stream, write_stream, options)
    except Exception as e:
        print(f"❌ Fatal error in equipment control server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Equipment control server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Equipment control server crashed: {e}", file=sys.stderr)
        sys.exit(1)
