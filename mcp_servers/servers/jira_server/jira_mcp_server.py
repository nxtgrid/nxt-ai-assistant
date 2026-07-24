#!/usr/bin/env python3
"""
Jira Analysis MCP Server

This MCP server provides tools to fetch Jira issues, filter by date and custom fields,
summarize comments, and prepare data for LLM-based categorization and analysis.
"""

import asyncio
import base64
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import aiohttp
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server

# Load environment variables from .env file BEFORE importing shared_code
# This ensures db_settings picks up the correct values
load_dotenv()

from servers.jira_server.tool_schemas import ACTION_TOOL_SCHEMAS, READ_ONLY_TOOL_SCHEMAS
from shared_code.config.action_flags import ActionFlags
from shared_code.stdio_runner import run_stdio_server
from shared_code.tool_registry import ToolRegistry

from shared.utils.date_utils import compose_date_range_query
from shared.utils.http_client import HTTPClientMixin
from shared.utils.response_formatters import compose_json_response

# Configure logging to stderr for Claude Desktop visibility
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("jira-mcp-server")

_STAFF_ORG_ID = int(os.getenv("STAFF_ORG_ID", "2"))

# Startup message to stderr
print("🚀 Jira MCP Server starting...", file=sys.stderr)
print(f"📍 Python path: {sys.path}", file=sys.stderr)
print(f"📂 Working directory: {os.getcwd()}", file=sys.stderr)

server = Server("jira-analysis")
registry = ToolRegistry("jira")
_READ_ONLY_SCHEMAS_BY_NAME = {s["name"]: s for s in READ_ONLY_TOOL_SCHEMAS}
_ACTION_SCHEMAS_BY_NAME = {s["name"]: s for s in ACTION_TOOL_SCHEMAS}


@dataclass
class JiraIssue:
    """Represents a Jira issue with relevant fields"""

    key: str
    summary: str
    status: str
    assignee: Optional[str]
    reporter: str
    created: str
    updated: str
    priority: str
    issue_type: str
    description: Optional[str]
    labels: List[str]
    components: List[str]
    custom_fields: Dict[str, Any]
    comments: List[Dict[str, Any]]


class JiraClient(HTTPClientMixin):
    """Client for interacting with Jira API"""

    def __init__(self):
        super().__init__()
        self.base_url: Optional[str] = None
        self.auth_header: Optional[str] = None
        self.ops_cloud_id: Optional[str] = None
        self.ops_schedule_id: Optional[str] = None
        self.organization_field_id: Optional[str] = None
        self._organization_field_id_cached: bool = False
        self._auth_pool = None  # Auth database pool for user lookups

        # Auto-configure from environment variables if available
        self._auto_configure_from_env()

    async def _get_auth_pool(self):
        """Get or create auth database connection pool for user lookups."""
        if self._auth_pool is None:
            try:
                import asyncpg

                # Use AUTH_DB_* environment variables for auth database
                auth_host = os.getenv("AUTH_DB_HOST")
                auth_port = int(os.getenv("AUTH_DB_PORT", "6543"))
                auth_user = os.getenv("AUTH_DB_USER")
                auth_password = os.getenv("AUTH_DB_PASSWORD")
                auth_db = os.getenv("AUTH_DB_NAME", "postgres")

                if not all([auth_host, auth_user, auth_password]):
                    logger.error("AUTH_DB_* environment variables not configured")
                    return None

                self._auth_pool = await asyncpg.create_pool(
                    host=auth_host,
                    port=auth_port,
                    database=auth_db,
                    user=auth_user,
                    password=auth_password,
                    ssl="require",
                    statement_cache_size=0,  # Required for PgBouncer
                    min_size=1,
                    max_size=5,
                )
                logger.info("Auth database pool created for user lookups")
            except Exception as e:
                logger.error(f"Failed to create auth database pool: {e}")
                return None
        return self._auth_pool

    def _auto_configure_from_env(self):
        """Automatically configure from environment variables if present"""
        base_url = os.getenv("JIRA_BASE_URL")
        username = os.getenv("JIRA_USERNAME")
        api_token = os.getenv("JIRA_API_TOKEN")

        if base_url and username and api_token:
            self.setup_auth(base_url, username, api_token)
            logger.info("Jira client auto-configured from environment variables")

        # Configure JSM Ops settings
        self.ops_cloud_id = os.getenv("JIRA_OPS_CLOUD_ID")
        self.ops_schedule_id = os.getenv("JIRA_OPS_SCHEDULE_ID")
        if self.ops_cloud_id and self.ops_schedule_id:
            logger.info("Jira Service Management Ops configured for on-call schedules")

        # Configure organization custom field ID (optional - will auto-discover if not set)
        self.organization_field_id = os.getenv("JIRA_ORGANIZATION_FIELD_ID")
        if self.organization_field_id:
            self._organization_field_id_cached = True
            logger.info(f"Organization field configured: {self.organization_field_id}")

    async def close(self):
        """Close HTTP session"""
        await self.close_session()

    def setup_auth(self, base_url: str, username: str, api_token: str):
        """Setup authentication for Jira API"""
        self.base_url = base_url.rstrip("/")
        auth_string = f"{username}:{api_token}"
        auth_bytes = auth_string.encode("ascii")
        auth_b64 = base64.b64encode(auth_bytes).decode("ascii")
        self.auth_header = f"Basic {auth_b64}"

    async def search_issues(
        self,
        jql: str,
        fields: Optional[List[str]] = None,
        expand: Optional[List[str]] = None,
        max_results: int = 50,
        start_at: int = 0,
    ) -> Dict[str, Any]:
        """Search for Jira issues using JQL"""
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        # Default fields if not specified
        if fields is None:
            fields = [
                "summary",
                "status",
                "assignee",
                "reporter",
                "created",
                "updated",
                "priority",
                "issuetype",
                "description",
                "labels",
                "components",
                "comment",
                "customfield_10057",  # Grid field
            ]
            # Add organization field if configured
            if self.organization_field_id:
                fields.append(self.organization_field_id)

        if expand is None:
            expand = ["changelog"]

        # Build query parameters for GET request
        params = {
            "jql": jql,
            "fields": ",".join(fields),
            "expand": ",".join(expand),
            "maxResults": max_results,
            "startAt": start_at,
        }

        url = f"{self.base_url}/rest/api/3/search/jql"

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Jira API error ({response.status}): {error_text}")

            return dict(await response.json())

    async def get_issue(self, issue_key: str, expand: Optional[List[str]] = None) -> Dict[str, Any]:
        """Get a specific Jira issue"""
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/issue/{issue_key}"

        if expand is None:
            expand = ["changelog", "comments"]

        params = {"expand": ",".join(expand)}

        headers = {"Authorization": self.auth_header, "Content-Type": "application/json"}

        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Jira API error ({response.status}): {error_text}")

            return dict(await response.json())

    async def get_fields(self) -> List[Dict[str, Any]]:
        """Get all available Jira fields"""
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/field"

        headers = {"Authorization": self.auth_header, "Content-Type": "application/json"}

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Jira API error ({response.status}): {error_text}")

            return list(await response.json())

    async def get_organization_field_id(self) -> Optional[str]:
        """
        Get the custom field ID for the organization field.

        First checks if already configured via env var or cached.
        Otherwise searches for fields with 'organization' in the name.

        Returns:
            Field ID (e.g., 'customfield_10058') or None if not found.
        """
        # Return cached value if available
        if self._organization_field_id_cached:
            return self.organization_field_id

        try:
            fields = await self.get_fields()

            for field in fields:
                name = (field.get("name") or "").lower()
                # Look for fields with 'organization' in the name
                if "organization" in name:
                    field_id = field.get("id")
                    if field_id:
                        self.organization_field_id = str(field_id)
                        self._organization_field_id_cached = True
                        logger.info(
                            f"Discovered organization field: {field.get('name')} -> {field_id}"
                        )
                        return str(field_id)

            logger.warning("No organization field found in JIRA fields")
            self._organization_field_id_cached = True  # Cache the negative result too
            return None

        except Exception as e:
            logger.error(f"Error discovering organization field: {e}")
            return None

    async def get_field_options(self, field_id: str, project_key: str = "OPS") -> List[str]:
        """
        Get allowed values for a custom select/dropdown field.

        Uses the issue create metadata API to fetch field options.

        Args:
            field_id: Custom field ID (e.g., 'customfield_10058')
            project_key: Project key to get options for (default: OPS)

        Returns:
            List of option values (strings)
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured")

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        # Get issue types for the project first
        url = f"{self.base_url}/rest/api/3/issue/createmeta/{project_key}/issuetypes"

        try:
            async with session.get(url, headers=headers) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.warning(f"Failed to get createmeta ({response.status}): {error_text}")
                    return []

                data = await response.json()

            # Parse through issue types to find the field and its options
            options = []
            issue_types = data.get("issueTypes", data.get("values", []))

            for issue_type in issue_types:
                issue_type_id = issue_type.get("id")
                if not issue_type_id:
                    continue

                # Get field metadata for this issue type
                fields_url = (
                    f"{self.base_url}/rest/api/3/issue/createmeta/"
                    f"{project_key}/issuetypes/{issue_type_id}"
                )

                async with session.get(fields_url, headers=headers) as fields_response:
                    if fields_response.status != 200:
                        continue
                    fields_data = await fields_response.json()

                # Look for the specific field in values
                values_list = fields_data.get("values", [])
                for field_info in values_list:
                    if field_info.get("fieldId") == field_id:
                        allowed_values = field_info.get("allowedValues", [])

                        for value in allowed_values:
                            if isinstance(value, dict):
                                val = value.get("value") or value.get("name")
                                if val and val not in options:
                                    options.append(val)
                            elif isinstance(value, str) and value not in options:
                                options.append(value)

                        if options:  # Found options, no need to check other issue types
                            break

                if options:
                    break

            logger.info(f"Found {len(options)} options for field {field_id}")
            return options

        except Exception as e:
            logger.error(f"Error fetching field options: {e}")
            return []

    async def find_user_by_email(self, email: str) -> Optional[str]:
        """
        Find Jira user account ID by email address

        Args:
            email: User email address

        Returns:
            Account ID if found, None otherwise
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/user/search"
        params = {"query": email}

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.warning(f"User search failed ({response.status}): {error_text}")
                return None

            users = await response.json()

            # Find exact email match
            for user in users:
                if user.get("emailAddress", "").lower() == email.lower():
                    return str(user.get("accountId")) if user.get("accountId") else None

            # If no exact match, return first result if available
            if users:
                return str(users[0].get("accountId")) if users[0].get("accountId") else None

            return None

    async def find_user_by_name_in_jira(self, name: str, project_key: str = "OPS") -> Optional[str]:
        """
        Find Jira user account ID by searching displayName directly in Jira.

        Tries multiple endpoints:
        1. /user/search - general user search
        2. /user/assignable/search - users assignable to project (more reliable)

        Args:
            name: User name to search for
            project_key: Project to check assignable users (default OPS)

        Returns:
            Account ID if found, None otherwise
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            logger.warning("Jira client not properly configured for name search")
            return None

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        # Helper to find matching user from results
        def find_matching_user(users: list, search_name: str) -> Optional[str]:
            search_lower = search_name.lower()
            # Try exact displayName match
            for user in users:
                display_name = user.get("displayName", "")
                if display_name.lower() == search_lower:
                    return str(user.get("accountId")) if user.get("accountId") else None

            # Try partial match (name contained in displayName)
            for user in users:
                display_name = user.get("displayName", "")
                if search_lower in display_name.lower():
                    return str(user.get("accountId")) if user.get("accountId") else None

            # Return first result if any
            if users:
                return str(users[0].get("accountId")) if users[0].get("accountId") else None
            return None

        try:
            # Method 1: Try /user/search endpoint
            url = f"{self.base_url}/rest/api/3/user/search"
            params = {"query": name}

            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    users = await response.json()
                    if users:
                        account_id = find_matching_user(users, name)
                        if account_id:
                            logger.info(f"Found user via /user/search: '{name}' -> {account_id}")
                            return account_id
                    logger.info(f"No users from /user/search for: {name}")
                else:
                    error_text = await response.text()
                    logger.warning(f"Jira /user/search failed ({response.status}): {error_text}")

            # Method 2: Try /user/assignable/search endpoint (more reliable for assignees)
            url = f"{self.base_url}/rest/api/3/user/assignable/search"
            params = {"query": name, "project": project_key}

            async with session.get(url, params=params, headers=headers) as response:
                if response.status == 200:
                    users = await response.json()
                    if users:
                        account_id = find_matching_user(users, name)
                        if account_id:
                            logger.info(
                                f"Found user via /user/assignable/search: '{name}' -> {account_id}"
                            )
                            return account_id
                    logger.info(f"No users from /user/assignable/search for: {name}")
                else:
                    error_text = await response.text()
                    logger.warning(
                        f"Jira /user/assignable/search failed ({response.status}): {error_text}"
                    )

            logger.warning(f"Could not find Jira user by name: {name}")
            return None

        except Exception as e:
            logger.error(f"Error in Jira user name search: {e}")
            return None

    async def get_current_user(self) -> Dict[str, Any]:
        """
        Get the current authenticated user's information

        Returns:
            Dictionary with user information including accountId, emailAddress, displayName
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/myself"

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Failed to get current user ({response.status}): {error_text}")

            return dict(await response.json())

    async def get_user_by_account_id(self, account_id: str) -> Optional[Dict[str, Any]]:
        """
        Get user information by account ID

        Args:
            account_id: Jira user account ID

        Returns:
            Dictionary with user information including accountId, emailAddress, displayName
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/user"
        params = {"accountId": account_id}

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, params=params, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.warning(
                    f"Failed to get user by account ID ({response.status}): {error_text}"
                )
                return None

            return dict(await response.json())

    async def add_comment(self, issue_key: str, comment_text: str) -> Dict[str, Any]:
        """
        Add a comment to a Jira issue

        Args:
            issue_key: Jira issue key (e.g., PROJ-123)
            comment_text: The comment text to add

        Returns:
            Dictionary with the created comment information
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/comment"

        # Format comment body using Atlassian Document Format
        comment_body = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": comment_text}]}
                ],
            }
        }

        headers = {
            "Authorization": self.auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with session.post(url, json=comment_body, headers=headers) as response:
            if response.status not in (200, 201):
                error_text = await response.text()
                raise Exception(f"Failed to add comment ({response.status}): {error_text}")

            return dict(await response.json())

    async def get_available_transitions(self, issue_key: str) -> List[Dict[str, Any]]:
        """
        Get available status transitions for a Jira issue

        Args:
            issue_key: Jira issue key (e.g., PROJ-123)

        Returns:
            List of available transitions with id, name, and target status
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"Failed to get transitions ({response.status}): {error_text}")

            data = await response.json()
            transitions = []

            for transition in data.get("transitions", []):
                transitions.append(
                    {
                        "id": transition.get("id"),
                        "name": transition.get("name"),
                        "to_status": transition.get("to", {}).get("name"),
                        "has_screen": transition.get("hasScreen", False),
                    }
                )

            return transitions

    async def get_on_call(
        self, start_date: str, end_date: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Get on-call schedule for a date range from Jira Service Management Ops

        Args:
            start_date: ISO 8601 formatted start datetime (e.g., "2025-10-24T00:00:00Z"). Required.
            end_date: ISO 8601 formatted end datetime. If None, defaults to start_date.

        Returns:
            List of on-call periods with user names and times
        """
        session = await self.get_session()

        if not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        if not self.ops_cloud_id or not self.ops_schedule_id:
            raise ValueError(
                "Jira Service Management Ops not configured. "
                "Set JIRA_OPS_CLOUD_ID and JIRA_OPS_SCHEDULE_ID environment variables."
            )

        # Parse dates to iterate through each day
        from datetime import datetime, timedelta

        start_dt = datetime.fromisoformat(start_date.replace("Z", "+00:00")).replace(
            hour=1, minute=0, second=0, microsecond=0
        )

        if end_date is None:
            end_dt = start_dt
        else:
            end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))

        # Construct the JSM Ops API URL for on-calls
        url = (
            f"https://api.atlassian.com/jsm/ops/api/{self.ops_cloud_id}/v1/"
            f"schedules/{self.ops_schedule_id}/on-calls"
        )

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        # Collect all on-call data by querying each day at two different times
        all_on_call_data = []
        current_dt = start_dt

        while current_dt <= end_dt:
            # Query at 14:15 UTC (daytime) and 17:15 UTC (evening)
            for hour, minute, label in [(14, 15, "daytime"), (17, 15, "evening")]:
                query_dt = current_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
                query_time = query_dt.isoformat().replace("+00:00", "Z")

                params = {"flat": "true", "date": query_time}

                logger.info(f"Querying on-call for {label} at {query_time}")

                async with session.get(url, params=params, headers=headers) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"JSM Ops API error ({response.status}): {error_text}")
                        continue

                    on_call_data = await response.json()
                    logger.info(
                        f"On-call response keys: {on_call_data.keys() if on_call_data else 'empty'}"
                    )

                    if on_call_data and "onCallUsers" in on_call_data:
                        user_ids = on_call_data["onCallUsers"]
                        logger.info(f"Found {len(user_ids)} on-call users at {query_time}")
                        for user_id in user_ids:
                            logger.info(f"User ID: {user_id}")
                            all_on_call_data.append(
                                {
                                    "query_time": query_time,
                                    "user_id": user_id,
                                    "date": current_dt.date().isoformat(),
                                    "label": label,
                                }
                            )
                    else:
                        logger.warning(f"No onCallUsers found for {label} at {query_time}")

            # Move to next day
            current_dt += timedelta(days=1)

        # Extract and format on-call periods with user information
        on_call_periods = await self._extract_on_call_from_snapshots(all_on_call_data)

        return on_call_periods

    async def _extract_on_call_from_snapshots(
        self, all_on_call_data: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """
        Extract and format on-call information from snapshot queries

        Args:
            all_on_call_data: List of on-call snapshots from multiple queries

        Returns:
            List of formatted on-call periods with user information
        """
        logger.info(f"Processing {len(all_on_call_data)} on-call snapshots")

        # Collect unique user IDs
        user_ids = set()
        for snapshot in all_on_call_data:
            user_ids.add(snapshot["user_id"])

        logger.info(f"Found {len(user_ids)} unique users in on-call snapshots")

        # Fetch user information for all unique user IDs
        user_info_cache = {}
        for user_id in user_ids:
            user_info = await self.get_user_by_account_id(user_id)
            if user_info:
                user_info_cache[user_id] = {
                    "displayName": user_info.get("displayName"),
                    "emailAddress": user_info.get("emailAddress"),
                }
            else:
                logger.warning(f"Could not fetch user info for {user_id}")

        # Format the on-call periods
        on_call_periods = []
        for snapshot in all_on_call_data:
            user_id = snapshot["user_id"]
            user_name = "Unknown User"
            user_email = None

            if user_id in user_info_cache:
                user_name = user_info_cache[user_id]["displayName"]
                user_email = user_info_cache[user_id]["emailAddress"]

            on_call_periods.append(
                {
                    "user_name": user_name,
                    "user_email": user_email,
                    "query_time": snapshot["query_time"],
                    "date": snapshot["date"],
                    "shift": snapshot["label"],
                }
            )

        logger.info(f"Extracted {len(on_call_periods)} on-call periods")
        return on_call_periods

    async def _extract_on_call_periods_old(
        self, timeline_data: Dict[str, Any], requested_start: str, requested_end: str
    ) -> List[Dict[str, Any]]:
        """
        Extract and format on-call periods from timeline data with user display names

        Args:
            timeline_data: Raw timeline data from Jira API
            requested_start: Originally requested start date (for filtering)
            requested_end: Originally requested end date (for filtering)

        Returns:
            List of formatted on-call periods with user information, filtered to requested range
        """
        debug_info = {
            "response_keys": list(timeline_data.keys()),
            "has_finalTimeline": "finalTimeline" in timeline_data,
            "user_ids_found": 0,
            "rotations_found": 0,
            "total_periods_in_response": 0,
        }

        # Collect all unique user IDs from the timeline
        user_ids = set()

        def extract_user_ids(obj):
            """Recursively extract user IDs from nested structure"""
            if isinstance(obj, dict):
                if obj.get("type") == "user" and "id" in obj:
                    user_ids.add(obj["id"])
                for value in obj.values():
                    extract_user_ids(value)
            elif isinstance(obj, list):
                for item in obj:
                    extract_user_ids(item)

        extract_user_ids(timeline_data)
        debug_info["user_ids_found"] = len(user_ids)
        logger.info(f"Found {len(user_ids)} unique user IDs in timeline")

        # Fetch user information for all unique user IDs
        user_info_cache = {}
        for user_id in user_ids:
            user_info = await self.get_user_by_account_id(user_id)
            if user_info:
                user_info_cache[user_id] = {
                    "displayName": user_info.get("displayName"),
                    "emailAddress": user_info.get("emailAddress"),
                }

        # Extract on-call periods from the timeline
        on_call_periods = []

        # Check for finalTimeline structure
        if "finalTimeline" in timeline_data:
            debug_info["finalTimeline_keys"] = list(timeline_data["finalTimeline"].keys())
            logger.info(f"finalTimeline keys: {timeline_data['finalTimeline'].keys()}")

            if "rotations" in timeline_data["finalTimeline"]:
                rotations = timeline_data["finalTimeline"]["rotations"]
                debug_info["rotations_found"] = len(rotations)
                logger.info(f"Found {len(rotations)} rotations")

                for rotation in rotations:
                    rotation_name = rotation.get("name", "Unnamed Rotation")

                    if "periods" in rotation:
                        periods = rotation["periods"]
                        debug_info["total_periods_in_response"] += len(periods)  # type: ignore[operator]
                        logger.info(f"Rotation '{rotation_name}' has {len(periods)} periods")

                        for period in periods:
                            # Check for flattened responders first (most detailed), then responder
                            responders = []

                            if "flattenedResponders" in period:
                                responders = period["flattenedResponders"]
                                logger.info(f"Found {len(responders)} flattenedResponders")
                            elif "responder" in period:
                                responders = [period["responder"]]
                                logger.info("Found single responder")

                            for responder in responders:
                                if responder.get("type") == "user":
                                    user_id = responder.get("id")
                                    user_name = "Unknown User"
                                    user_email = None

                                    if user_id and user_id in user_info_cache:
                                        user_name = user_info_cache[user_id]["displayName"]
                                        user_email = user_info_cache[user_id]["emailAddress"]
                                    else:
                                        logger.warning(f"User ID {user_id} not found in cache")

                                    on_call_periods.append(
                                        {
                                            "user_name": user_name,
                                            "user_email": user_email,
                                            "start_time": period.get("startDate"),
                                            "end_time": period.get("endDate"),
                                            "rotation": rotation_name,
                                        }
                                    )
                                else:
                                    logger.info(
                                        f"Skipping non-user responder type: {responder.get('type')}"
                                    )
            else:
                logger.warning("No 'rotations' key found in finalTimeline")
        else:
            logger.warning("No 'finalTimeline' key found in response")

        debug_info["periods_extracted"] = len(on_call_periods)
        logger.info(f"Extracted {len(on_call_periods)} on-call periods before filtering")

        # Filter periods to only include those that overlap with the requested date range
        from datetime import datetime

        requested_start_dt = datetime.fromisoformat(requested_start.replace("Z", "+00:00"))
        requested_end_dt = datetime.fromisoformat(requested_end.replace("Z", "+00:00"))

        logger.info(f"Filtering periods: requested range {requested_start} to {requested_end}")

        filtered_periods = []
        for period in on_call_periods:
            period_start = datetime.fromisoformat(period["start_time"].replace("Z", "+00:00"))
            period_end = datetime.fromisoformat(period["end_time"].replace("Z", "+00:00"))

            # Check if period overlaps with requested range
            # Period overlaps if: period_start <= requested_end AND period_end >= requested_start
            overlaps = period_start <= requested_end_dt and period_end >= requested_start_dt

            logger.info(
                f"Period: {period['start_time']} to {period['end_time']}, "
                f"User: {period['user_name']}, Overlaps: {overlaps}"
            )

            if overlaps:
                filtered_periods.append(period)
            else:
                logger.warning(
                    f"Filtered OUT: period {period['start_time']} to {period['end_time']} "
                    f"does not overlap with {requested_start} to {requested_end}"
                )

        debug_info["periods_after_filtering"] = len(filtered_periods)
        logger.info(f"Filtered to {len(filtered_periods)} on-call periods within requested range")
        logger.info(f"Debug info: {debug_info}")
        return filtered_periods

    async def find_user_email_by_name(self, user_name: str) -> Optional[str]:
        """
        Find user email by searching accounts table.

        Uses a multi-phase approach for robust matching:
        1. Exact case-insensitive match on full name
        2. ILIKE substring match (handles partial names like first name only)
        3. Word-by-word matching (matches if search term equals any word in full name)
        4. Fuzzy matching for typos/misspellings (lower threshold for short names)

        Args:
            user_name: Name of the user (can be partial, case-insensitive, or have typos)

        Returns:
            User email if found, None otherwise
        """
        try:
            # Use auth database pool for user lookups (accounts table is in auth DB)
            auth_pool = await self._get_auth_pool()
            if not auth_pool:
                logger.error("Auth database pool not available for user lookup")
                return None

            # Normalize search term
            search_term = user_name.strip()
            if not search_term:
                logger.warning("Empty user name provided")
                return None

            logger.info(f"Searching for user '{search_term}' in auth database")

            async with auth_pool.acquire() as conn:
                # Phase 1: Try exact case-insensitive match first
                # Filter to staff org since only staff have Jira accounts
                results = await conn.fetch(
                    f"""
                    SELECT email, full_name
                    FROM public.accounts
                    WHERE LOWER(full_name) = LOWER($1)
                    AND deleted_at IS NULL
                    AND organization_id = {_STAFF_ORG_ID}
                    LIMIT 1
                    """,
                    search_term,
                )

                if results:
                    user_email = str(results[0]["email"])
                    logger.info(
                        f"Exact match for '{search_term}': {results[0]['full_name']} ({user_email})"
                    )
                    return user_email

                # Phase 2: Try ILIKE substring match (handles partial names)
                results = await conn.fetch(
                    f"""
                    SELECT email, full_name
                    FROM public.accounts
                    WHERE full_name ILIKE $1
                    AND deleted_at IS NULL
                    AND organization_id = {_STAFF_ORG_ID}
                    ORDER BY full_name
                    LIMIT 10
                    """,
                    f"%{search_term}%",
                )

                if results and len(results) > 0:
                    if len(results) > 1:
                        logger.info(
                            f"Multiple ILIKE matches for '{search_term}': "
                            f"{[r['full_name'] for r in results]}, using first"
                        )
                    user_email = str(results[0]["email"])
                    logger.info(
                        f"ILIKE match for '{search_term}': {results[0]['full_name']} ({user_email})"
                    )
                    return user_email

                # Phase 3 & 4: Fetch all staff users for word matching and fuzzy matching
                logger.info(f"No ILIKE match for '{search_term}', trying advanced matching...")
                all_users = await conn.fetch(
                    f"""
                    SELECT email, full_name
                    FROM public.accounts
                    WHERE deleted_at IS NULL
                    AND full_name IS NOT NULL
                    AND organization_id = {_STAFF_ORG_ID}
                    """
                )

                if not all_users:
                    logger.warning("No users found in accounts table")
                    return None

                # Build name -> email mapping
                name_to_email = {r["full_name"]: r["email"] for r in all_users if r["full_name"]}

                # Phase 3: Word-by-word matching (first name or last name match)
                search_lower = search_term.lower()
                for full_name, email in name_to_email.items():
                    # Split full name into words and check if search matches any word
                    name_words = full_name.lower().split()
                    if search_lower in name_words:
                        logger.info(f"Word match for '{search_term}': {full_name} ({email})")
                        return str(email)

                # Phase 4: Fuzzy matching for typos
                valid_names = list(name_to_email.keys())
                try:
                    from rapidfuzz import fuzz, process

                    # Use lower threshold for short names (likely first names with typos)
                    # Short names need more tolerance: "Sani" vs "Sanni"
                    threshold = 60 if len(search_term) <= 6 else 75

                    # Use token_set_ratio for best partial matching
                    # This handles: "Chovwe" matching "Chovwe Okonkwo"
                    matches = process.extract(
                        search_term,
                        valid_names,
                        scorer=fuzz.token_set_ratio,
                        limit=3,
                    )

                    if matches and matches[0][1] >= threshold:
                        matched_name = matches[0][0]
                        score = matches[0][1]
                        user_email = str(name_to_email[matched_name])
                        logger.info(
                            f"Fuzzy matched '{search_term}' -> '{matched_name}' "
                            f"(score: {score}%, threshold: {threshold}%), email: {user_email}"
                        )
                        return user_email
                    elif matches:
                        # Log near-misses for debugging
                        logger.info(
                            f"Best fuzzy matches for '{search_term}' (below threshold {threshold}%): "
                            f"{[(m[0], m[1]) for m in matches[:3]]}"
                        )
                except ImportError:
                    logger.warning("rapidfuzz not available for fuzzy matching")

            logger.warning(f"No user found with name matching: {search_term}")
            return None

        except Exception as e:
            logger.error(f"Error looking up user by name: {e}")
            import traceback

            logger.error(traceback.format_exc())
            return None

    async def add_on_call_override(
        self, user_name: str, start_time: str, end_time: str
    ) -> Dict[str, Any]:
        """
        Add an on-call override for a specific user and time period

        Args:
            user_name: Name of the user to add as on-call (will be looked up in Supabase)
            start_time: ISO 8601 formatted start datetime (e.g., "2025-10-24T09:00:00Z")
            end_time: ISO 8601 formatted end datetime (e.g., "2025-10-24T17:00:00Z")

        Returns:
            Dictionary with the created override information
        """
        session = await self.get_session()

        if not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        if not self.ops_cloud_id or not self.ops_schedule_id:
            raise ValueError(
                "Jira Service Management Ops not configured. "
                "Set JIRA_OPS_CLOUD_ID and JIRA_OPS_SCHEDULE_ID environment variables."
            )

        # Look up user email from Supabase by name
        user_email = await self.find_user_email_by_name(user_name)
        if not user_email:
            raise ValueError(f"Could not find user with name: {user_name}")

        # Look up Jira account ID by email
        account_id = await self.find_user_by_email(user_email)
        if not account_id:
            raise ValueError(f"Could not find Jira user with email: {user_email}")

        # Construct the JSM Ops API URL for overrides
        url = (
            f"https://api.atlassian.com/jsm/ops/api/{self.ops_cloud_id}/v1/"
            f"schedules/{self.ops_schedule_id}/overrides"
        )

        # Create override payload
        override_payload = {
            "responder": {"type": "user", "id": account_id},
            "startDate": start_time,
            "endDate": end_time,
        }

        headers = {
            "Authorization": self.auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        async with session.post(url, json=override_payload, headers=headers) as response:
            if response.status not in (200, 201):
                error_text = await response.text()
                raise Exception(f"JSM Ops API error ({response.status}): {error_text}")

            result = await response.json()

            return {
                "success": True,
                "user_email": user_email,
                "account_id": account_id,
                "start_time": start_time,
                "end_time": end_time,
                "override_data": result,
            }

    async def transition_issue(
        self,
        issue_key: str,
        transition_id_or_name: str,
        current_user_email: Optional[str] = None,
        current_user_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Change the status of a Jira issue via transition.

        If the ticket is unassigned, auto-assigns to the requesting user before
        transitioning. If the user can't be found in Jira, the transition is blocked.

        Args:
            issue_key: Jira issue key (e.g., PROJ-123)
            transition_id_or_name: Transition ID or name (e.g., "31" or "In Progress")
            current_user_email: Email of the user making the change (for assignment and audit)
            current_user_name: Display name of the user for audit trail

        Returns:
            Dictionary with transition result
        """
        session = await self.get_session()

        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        headers = {
            "Authorization": self.auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Get available transitions
        available_transitions = await self.get_available_transitions(issue_key)

        # Find the transition by ID or name
        transition_id = None

        for trans in available_transitions:
            if (
                trans["id"] == transition_id_or_name
                or trans["name"].lower() == transition_id_or_name.lower()
            ):
                transition_id = trans["id"]
                break

        if not transition_id:
            available_names = [t["name"] for t in available_transitions]
            raise ValueError(
                f"Transition '{transition_id_or_name}' not found for issue {issue_key}. "
                f"Available transitions: {', '.join(available_names)}"
            )

        # Check assignment before transitioning — unassigned tickets must be claimed first
        issue_data = await self.get_issue(issue_key)
        current_assignee = issue_data.get("fields", {}).get("assignee")
        auto_assigned = False
        display_name = current_user_name or current_user_email or "Unknown user"

        if not current_assignee:
            if not current_user_email:
                raise ValueError(
                    f"Cannot transition {issue_key}: the ticket is unassigned and no "
                    f"requester email is available to auto-assign."
                )

            # Try to find and assign the requester
            account_id = await self.find_user_by_email(current_user_email)
            if not account_id:
                raise ValueError(
                    f"Cannot transition {issue_key}: the ticket is unassigned and "
                    f"'{current_user_email}' was not found in Jira. "
                    f"Please assign the ticket manually first."
                )

            assign_url = f"{self.base_url}/rest/api/3/issue/{issue_key}/assignee"
            assign_body = {"accountId": account_id}
            async with session.put(
                assign_url, json=assign_body, headers=headers
            ) as assign_response:
                if assign_response.status in (200, 204):
                    auto_assigned = True
                    logger.info(
                        f"Auto-assigned {issue_key} to {current_user_email} before transition"
                    )
                else:
                    error_text = await assign_response.text()
                    logger.warning(f"Auto-assign failed for {issue_key}: {error_text}")
                    raise ValueError(
                        f"Cannot transition {issue_key}: the ticket is unassigned and "
                        f"auto-assignment to '{display_name}' failed. "
                        f"Please assign the ticket manually first."
                    )

        # Perform the transition
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/transitions"
        transition_body = {"transition": {"id": transition_id}}

        async with session.post(url, json=transition_body, headers=headers) as response:
            if response.status not in (200, 204):
                error_text = await response.text()
                logger.error(f"JIRA transition failed for {issue_key}: {error_text}")
                raise ValueError(
                    f"Failed to change status of {issue_key}. Please try again or contact support."
                )

            # Get updated issue to confirm new status
            updated_issue = await self.get_issue(issue_key)
            new_status = updated_issue.get("fields", {}).get("status", {}).get("name")

            # Add audit comment showing who made the change
            audit_comment = f"{display_name} transitioned ticket to {new_status} via Support bot"
            if auto_assigned:
                audit_comment += f" (auto-assigned to {display_name})"
            try:
                await self.add_comment(issue_key, audit_comment)
            except Exception as e:
                logger.warning(f"Failed to add audit comment to {issue_key}: {e}")

            result = {
                "success": True,
                "issue_key": issue_key,
                "new_status": new_status,
                "message": f"Successfully changed {issue_key} status to {new_status}",
            }
            if auto_assigned:
                result["auto_assigned"] = True
                result["message"] += f" and assigned to {display_name}"

            return result

    async def assign_issue(
        self,
        issue_key: str,
        assignee: str,
        current_user_email: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assign or reassign a Jira issue.

        Args:
            issue_key: Jira issue key (e.g., OPS-123)
            assignee: 'me', 'unassigned', or a name/email to fuzzy-match
            current_user_email: Email of the requesting user (for 'me')

        Returns:
            Dict with success status and assignment details
        """
        session = await self.get_session()
        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not configured")

        headers = {
            "Authorization": self.auth_header,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        # Resolve account ID
        account_id: Optional[str] = None
        display_name = assignee

        if assignee.lower() == "unassigned":
            account_id = None  # Will send null to unassign
            display_name = "Unassigned"
        elif assignee.lower() == "me":
            if not current_user_email:
                return {
                    "success": False,
                    "error": "Cannot self-assign: your email is not available",
                }
            account_id = await self.find_user_by_email(current_user_email)
            if not account_id:
                return {
                    "success": False,
                    "error": f"'{current_user_email}' not found as a Jira user",
                }
            display_name = current_user_email
        else:
            # Try email first, then name search
            if "@" in assignee:
                account_id = await self.find_user_by_email(assignee)
            if not account_id:
                account_id = await self.find_user_by_name_in_jira(assignee)
            if not account_id:
                return {
                    "success": False,
                    "error": (
                        f"'{assignee}' is not a Jira user. "
                        f"Try using their full name or email address."
                    ),
                }

        # Perform assignment
        url = f"{self.base_url}/rest/api/3/issue/{issue_key}/assignee"
        body = {"accountId": account_id}  # null accountId = unassign

        async with session.put(url, json=body, headers=headers) as response:
            if response.status in (200, 204):
                # Add audit comment
                action = (
                    "unassigned"
                    if assignee.lower() == "unassigned"
                    else f"assigned to {display_name}"
                )
                try:
                    requester = current_user_email or "Staff"
                    await self.add_comment(
                        issue_key, f"{requester} {action} this ticket via Support bot"
                    )
                except Exception as e:
                    logger.warning(f"Failed to add assign audit comment: {e}")

                return {
                    "success": True,
                    "issue_key": issue_key,
                    "assignee": display_name,
                    "message": f"Successfully {action} {issue_key}",
                }
            else:
                error_text = await response.text()
                logger.error(f"Failed to assign {issue_key}: {error_text}")
                return {
                    "success": False,
                    "error": f"Failed to assign {issue_key}: {error_text}",
                }

    def parse_issue(self, issue_data: Dict[str, Any]) -> JiraIssue:
        """Parse Jira issue data into JiraIssue object"""
        fields = issue_data.get("fields", {})

        # Extract basic fields
        key = issue_data.get("key", "")
        summary = fields.get("summary", "")
        status = fields.get("status", {}).get("name", "")
        assignee = fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None
        reporter = fields.get("reporter", {}).get("displayName", "")
        created = fields.get("created", "")
        updated = fields.get("updated", "")
        priority = fields.get("priority", {}).get("name", "")
        issue_type = fields.get("issuetype", {}).get("name", "")

        # Safely extract description from Atlassian Document Format
        description = None
        if fields.get("description"):
            desc_content = fields.get("description", {}).get("content", [])
            if desc_content and len(desc_content) > 0:
                first_content = desc_content[0].get("content", [])
                if first_content and len(first_content) > 0:
                    description = first_content[0].get("text", "")

        # Extract labels and components
        labels = [label for label in fields.get("labels", [])]
        components = [comp.get("name", "") for comp in fields.get("components", [])]

        # Extract custom fields (fields that start with customfield_)
        custom_fields = {}
        for field_key, field_value in fields.items():
            if field_key.startswith("customfield_"):
                custom_fields[field_key] = field_value

        # Extract comments
        comments = []
        comment_data = fields.get("comment", {})
        if comment_data and "comments" in comment_data:
            for comment in comment_data["comments"]:
                comment_text = ""
                if comment.get("body") and comment["body"].get("content"):
                    # Parse Atlassian Document Format
                    for content in comment["body"]["content"]:
                        if content.get("content"):
                            for text_content in content["content"]:
                                if text_content.get("text"):
                                    comment_text += text_content["text"]

                comments.append(
                    {
                        "id": comment.get("id"),
                        "author": comment.get("author", {}).get("displayName", ""),
                        "created": comment.get("created", ""),
                        "updated": comment.get("updated", ""),
                        "body": comment_text,
                    }
                )

        return JiraIssue(
            key=key,
            summary=summary,
            status=status,
            assignee=assignee,
            reporter=reporter,
            created=created,
            updated=updated,
            priority=priority,
            issue_type=issue_type,
            description=description,
            labels=labels,
            components=components,
            custom_fields=custom_fields,
            comments=comments,
        )

    def build_jql_query(
        self,
        project: Optional[str] = None,
        issue_types: Optional[List[str]] = None,
        statuses: Optional[List[str]] = None,
        assignee: Optional[str] = None,
        reporter: Optional[str] = None,
        created_after: Optional[str] = None,
        created_before: Optional[str] = None,
        updated_after: Optional[str] = None,
        updated_before: Optional[str] = None,
        custom_field_filters: Optional[Dict[str, str]] = None,
        labels: Optional[List[str]] = None,
        components: Optional[List[str]] = None,
        priority: Optional[List[str]] = None,
        text_search: Optional[str] = None,
        additional_jql: Optional[str] = None,
        exclude_done: bool = False,
    ) -> str:
        """Build JQL query from filters"""
        conditions = []

        if project:
            conditions.append(f'project = "{project}"')

        if text_search:
            # Search in summary, description, and title (title is same as summary in Jira)
            conditions.append(f'(summary ~ "{text_search}" OR description ~ "{text_search}")')

        if issue_types:
            types_str = ", ".join([f'"{t}"' for t in issue_types])
            conditions.append(f"type IN ({types_str})")

        if statuses:
            statuses_str = ", ".join([f'"{s}"' for s in statuses])
            conditions.append(f"status IN ({statuses_str})")
        elif exclude_done:
            # Use statusCategory to exclude Done tickets without guessing status names
            conditions.append('statusCategory != "Done"')

        if assignee:
            if assignee.lower() == "unassigned":
                conditions.append("assignee is EMPTY")
            else:
                conditions.append(f'assignee = "{assignee}"')

        if reporter:
            conditions.append(f'reporter = "{reporter}"')

        if created_after:
            conditions.append(f'created >= "{created_after}"')

        if created_before:
            conditions.append(f'created <= "{created_before}"')

        if updated_after:
            conditions.append(f'updated >= "{updated_after}"')

        if updated_before:
            conditions.append(f'updated <= "{updated_before}"')

        if labels:
            for label in labels:
                conditions.append(f'labels = "{label}"')

        if components:
            components_str = ", ".join([f'"{c}"' for c in components])
            conditions.append(f"component IN ({components_str})")

        if priority:
            priority_str = ", ".join([f'"{p}"' for p in priority])
            conditions.append(f"priority IN ({priority_str})")

        if custom_field_filters:
            for field_id, value in custom_field_filters.items():
                if value.lower() == "empty":
                    conditions.append(f'"{field_id}" is EMPTY')
                elif value.lower() == "not empty":
                    conditions.append(f'"{field_id}" is not EMPTY')
                else:
                    conditions.append(f'"{field_id}" = "{value}"')

        if additional_jql:
            conditions.append(f"({additional_jql})")

        return " AND ".join(conditions) if conditions else "project is not EMPTY"


def _extract_custom_field_value(field_value: Any) -> Optional[str]:
    """
    Extract display value from JIRA custom field.

    JIRA returns custom fields as:
    - String for text fields
    - Dict with 'value' key for select fields: {"value": "Option Name", "id": "123"}
    - List of dicts for multi-select fields: [{"value": "Option 1"}, {"value": "Option 2"}]
    - None if empty
    """
    if field_value is None:
        return None
    if isinstance(field_value, str):
        return field_value
    if isinstance(field_value, dict):
        val = field_value.get("value") or field_value.get("name")
        return str(val) if val else None
    if isinstance(field_value, list):
        values = []
        for item in field_value:
            if isinstance(item, dict):
                val = item.get("value") or item.get("name")
                if val:
                    values.append(val)
            elif isinstance(item, str):
                values.append(item)
        return ", ".join(values) if values else None
    return str(field_value)


async def get_ticket_statistics(days: int = 30) -> Dict[str, Any]:
    """Get aggregated ticket statistics for the last N days.

    Fetches all tickets (open + closed) created in the date range and
    returns per-grid counts, top issue types, and grids above average.

    Args:
        days: Number of days to look back (default 30)

    Returns:
        Dict with tickets_by_grid, tickets_by_type, total, avg, grids_above_average
    """
    created_after = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    jql = client.build_jql_query(
        project="OPS",
        created_after=created_after,
    )

    search_result = await client.search_issues(
        jql=jql,
        fields=["summary", "issuetype", "status", "labels", "customfield_10057", "created"],
        expand=[],
        max_results=200,
    )

    tickets_by_grid: Dict[str, int] = {}
    tickets_by_type: Dict[str, int] = {}
    total = 0

    for issue_data in search_result.get("issues", []):
        total += 1
        fields = issue_data.get("fields", {})

        # Count by grid (customfield_10057)
        grid_value = _extract_custom_field_value(fields.get("customfield_10057"))
        grid_name = grid_value or "Unspecified"
        tickets_by_grid[grid_name] = tickets_by_grid.get(grid_name, 0) + 1

        # Count by issue type
        issue_type = fields.get("issuetype", {}).get("name", "Unknown")
        tickets_by_type[issue_type] = tickets_by_type.get(issue_type, 0) + 1

    # Calculate average and find grids above average
    grid_counts = [v for k, v in tickets_by_grid.items() if k != "Unspecified"]
    avg_per_grid = round(sum(grid_counts) / len(grid_counts), 1) if grid_counts else 0
    threshold = avg_per_grid * 1.5

    above_avg_list: List[Dict[str, Any]] = [
        {"name": name, "count": count}
        for name, count in tickets_by_grid.items()
        if count > threshold and name != "Unspecified"
    ]
    above_avg_list.sort(key=lambda x: x["count"], reverse=True)
    grids_above_average = above_avg_list

    # Sort by count descending
    sorted_by_grid = sorted(tickets_by_grid.items(), key=lambda x: x[1], reverse=True)
    sorted_by_type = sorted(tickets_by_type.items(), key=lambda x: x[1], reverse=True)

    return {
        "period_days": days,
        "total_tickets": total,
        "avg_tickets_per_grid": avg_per_grid,
        "grids_above_average": grids_above_average,
        "tickets_by_grid": [{"grid": k, "count": v} for k, v in sorted_by_grid[:15]],
        "tickets_by_type": [{"type": k, "count": v} for k, v in sorted_by_type],
    }


# Global client instance
client = JiraClient()


@registry.tool("search_issues_with_comments", _READ_ONLY_SCHEMAS_BY_NAME["search_issues_with_comments"])
async def _tool_search_issues_with_comments(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # Only apply date range defaults when at least one boundary is explicitly provided.
    # Without explicit dates, no date filter is applied — this avoids silently
    # excluding old-but-still-open tickets from broad queries like "all open tickets".
    created_after = arguments.get("created_after")
    created_before = arguments.get("created_before")
    if created_after or created_before:
        date_range = compose_date_range_query(
            created_after, created_before, default_days=90
        )
        created_after = date_range["start_date"]
        created_before = date_range["end_date"]

    updated_after = arguments.get("updated_after")
    updated_before = arguments.get("updated_before")
    if updated_after or updated_before:
        update_range = compose_date_range_query(
            updated_after, updated_before, default_days=90
        )
        updated_after = update_range["start_date"]
        updated_before = update_range["end_date"]

    # Handle assignee email lookup
    assignee_value = None
    assignee_input = arguments.get("assignee")
    assignee_lookup_note = None  # Track lookup status for user feedback
    if assignee_input:
        if assignee_input.lower() == "unassigned":
            assignee_value = "unassigned"
        elif assignee_input.lower() == "me":
            # Use the injected user_email for "me"
            user_email = arguments.get("user_email")
            if user_email:
                account_id = await client.find_user_by_email(user_email)
                if account_id:
                    assignee_value = account_id
                    logger.info(f"Resolved 'me' to user: {user_email}")
                else:
                    logger.warning(f"Could not find user with email: {user_email}")
                    assignee_lookup_note = (
                        f"Could not find Jira account for your email ({user_email})"
                    )
            else:
                logger.warning("assignee='me' but no user_email in arguments")
                assignee_lookup_note = "Could not determine your email for 'me' lookup"
        else:
            # Try to resolve assignee - could be email or name
            if "@" in assignee_input:
                # Looks like an email - use directly
                account_id = await client.find_user_by_email(assignee_input)
                if account_id:
                    assignee_value = account_id
                else:
                    logger.warning(f"Could not find user with email: {assignee_input}")
                    assignee_lookup_note = (
                        f"Could not find Jira account for email: {assignee_input}"
                    )
            else:
                # Looks like a name - fuzzy match to email first
                user_email = await client.find_user_email_by_name(assignee_input)
                if user_email:
                    account_id = await client.find_user_by_email(user_email)
                    if account_id:
                        assignee_value = account_id
                        logger.info(
                            f"Resolved assignee name '{assignee_input}' -> email '{user_email}'"
                        )
                    else:
                        # Email lookup failed - try searching Jira by name directly
                        logger.info(
                            f"Email lookup failed for '{user_email}', trying Jira name search"
                        )
                        account_id = await client.find_user_by_name_in_jira(assignee_input)
                        if account_id:
                            assignee_value = account_id
                            logger.info(
                                f"Found Jira user by name search: '{assignee_input}' -> {account_id}"
                            )
                        else:
                            logger.warning(
                                f"Found email '{user_email}' for '{assignee_input}' "
                                f"but could not find Jira account by email or name"
                            )
                            assignee_lookup_note = (
                                f"Found '{assignee_input}' in system (email: {user_email}) "
                                f"but no matching Jira account"
                            )
                else:
                    # No email found - try searching Jira by name directly as last resort
                    logger.info(
                        f"No email found for '{assignee_input}', trying Jira name search"
                    )
                    account_id = await client.find_user_by_name_in_jira(assignee_input)
                    if account_id:
                        assignee_value = account_id
                        logger.info(
                            f"Found Jira user by direct name search: '{assignee_input}' -> {account_id}"
                        )
                    else:
                        logger.warning(f"Could not find user with name: {assignee_input}")
                        assignee_lookup_note = (
                            f"Could not find user '{assignee_input}' in system or Jira. "
                            f"Check spelling or use full name."
                        )

    # Ensure organization field is discovered before search
    org_field_id = await client.get_organization_field_id()

    # Handle grid and organization filters for custom fields
    custom_field_filters = {}
    grid_lookup_note = None  # Track grid lookup status
    grid_name = arguments.get("grid")
    if grid_name:
        # Fuzzy match grid name against valid options
        try:
            valid_grids = await client.get_field_options("customfield_10057")
            if valid_grids:
                from shared.utils.grid_matcher import find_best_grid_match

                matched_grid, was_fuzzy, score = find_best_grid_match(
                    grid_name, valid_grids, threshold=80
                )
                if matched_grid:
                    if was_fuzzy:
                        logger.info(
                            f"Fuzzy matched grid '{grid_name}' -> '{matched_grid}' (score: {score}%)"
                        )
                        grid_lookup_note = f"Matched '{grid_name}' to '{matched_grid}'"
                    custom_field_filters["customfield_10057"] = matched_grid
                else:
                    # No match found - use original (will likely return empty results)
                    logger.warning(f"No grid match found for '{grid_name}'")
                    custom_field_filters["customfield_10057"] = grid_name
                    grid_lookup_note = (
                        f"Grid '{grid_name}' not found in Jira. "
                        f"Available grids: {', '.join(valid_grids[:5])}..."
                    )
            else:
                # Couldn't get valid grids - use original
                custom_field_filters["customfield_10057"] = grid_name
        except ImportError:
            # rapidfuzz not available - use original
            custom_field_filters["customfield_10057"] = grid_name

    # Handle organization filter
    organization_name = arguments.get("organization")
    if organization_name and org_field_id:
        custom_field_filters[org_field_id] = organization_name

    # Build JQL query with hardcoded project OPS
    # Normalize statuses: "Open" isn't a real Jira status (ours are "To Do",
    # "In Progress", "Done"). When the LLM passes "Open", the user means
    # "all non-done tickets", so drop all statuses and use statusCategory != Done.
    explicit_statuses = arguments.get("statuses")
    if explicit_statuses and any(s.lower() == "open" for s in explicit_statuses):
        # "Open" signals intent for all non-done tickets
        explicit_statuses = None

    exclude_done = arguments.get("exclude_done", not explicit_statuses)

    jql = client.build_jql_query(
        project="OPS",
        text_search=arguments.get("text_search"),
        statuses=explicit_statuses,
        assignee=assignee_value,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        custom_field_filters=custom_field_filters if custom_field_filters else None,
        labels=arguments.get("labels"),
        exclude_done=exclude_done,
    )

    # Search issues
    search_result = await client.search_issues(
        jql=jql, max_results=arguments.get("max_results", 50)
    )

    # Parse issues with full comment data
    issues_dict_list: List[Dict[str, Any]] = []
    for issue_data in search_result.get("issues", []):
        issue = client.parse_issue(issue_data)
        # Extract organization value if field is configured
        org_value = None
        if org_field_id:
            org_value = _extract_custom_field_value(issue.custom_fields.get(org_field_id))

        issues_dict_list.append(
            {
                "key": issue.key,
                "summary": issue.summary,
                "status": issue.status,
                "assignee": issue.assignee or "Unassigned",
                "reporter": issue.reporter,
                "priority": issue.priority,
                "issue_type": issue.issue_type,
                "grid": _extract_custom_field_value(
                    issue.custom_fields.get("customfield_10057")
                ),
                "organization": org_value,
                "created": issue.created,
                "updated": issue.updated,
                "comment_count": len(issue.comments),
                "comments": issue.comments,
            }
        )

    # The newer /rest/api/3/search/jql endpoint does not return a "total"
    # field (it uses cursor-based pagination). Fall back to issues_returned
    # so the LLM sees a consistent value instead of 0 vs N.
    api_total = search_result.get("total")
    total_found = api_total if api_total is not None else len(issues_dict_list)
    result = {
        "jql_used": jql,
        "total_found": total_found,
        "issues_returned": len(issues_dict_list),
        "issues": issues_dict_list,
    }
    # Add lookup notes to help user understand any issues
    notes = []
    if assignee_lookup_note:
        result["assignee_lookup_note"] = assignee_lookup_note
        notes.append(f"Assignee: {assignee_lookup_note}")
    if grid_lookup_note:
        result["grid_lookup_note"] = grid_lookup_note
        notes.append(f"Grid: {grid_lookup_note}")
    if notes:
        result["lookup_notes"] = notes
    return list(compose_json_response(result, default=str))


@registry.tool("get_issue", _READ_ONLY_SCHEMAS_BY_NAME["get_issue"])
async def _tool_get_issue(arguments: Dict[str, Any]) -> List[types.TextContent]:
    issue_data = await client.get_issue(arguments["issue_key"])
    issue = client.parse_issue(issue_data)

    # Get organization field value
    org_field_id = await client.get_organization_field_id()
    org_value = None
    if org_field_id:
        org_value = _extract_custom_field_value(issue.custom_fields.get(org_field_id))

    result = {
        "key": issue.key,
        "summary": issue.summary,
        "description": issue.description,
        "status": issue.status,
        "assignee": issue.assignee,
        "reporter": issue.reporter,
        "priority": issue.priority,
        "issue_type": issue.issue_type,
        "grid": _extract_custom_field_value(issue.custom_fields.get("customfield_10057")),
        "organization": org_value,
        "created": issue.created,
        "updated": issue.updated,
        "labels": issue.labels,
        "components": issue.components,
        "custom_fields": issue.custom_fields,
        "comments": issue.comments,
    }
    return list(compose_json_response(result, default=str))


@registry.tool("get_ticket_statistics", _READ_ONLY_SCHEMAS_BY_NAME["get_ticket_statistics"])
async def _tool_get_ticket_statistics(arguments: Dict[str, Any]) -> List[types.TextContent]:
    days = arguments.get("days", 30)
    result = await get_ticket_statistics(days=int(days))
    return list(compose_json_response(result, default=str))


def _jira_ops_unavailable_reason() -> Optional[str]:
    """Return a human-readable reason if JSM Ops isn't configured, else None.

    Checked upfront so a missing-config on-call query returns a clean
    {"available": False, "reason": ...} instead of the generic "Error: ..."
    every other unhandled-exception tool call falls back to (see
    ToolRegistry.handle_call_tool) -- an LLM caller should be able to tell a
    customer plainly that the schedule is unavailable rather than surface a
    raw config-error string.
    """
    if not client.auth_header:
        return "Jira is not configured, so the on-call schedule is unavailable."
    if not client.ops_cloud_id or not client.ops_schedule_id:
        return "JSM Ops on-call schedule is not configured."
    return None


@registry.tool("get_on_call", _READ_ONLY_SCHEMAS_BY_NAME["get_on_call"])
async def _tool_get_on_call(arguments: Dict[str, Any]) -> List[types.TextContent]:
    start_date = arguments["start_date"]
    end_date = arguments.get("end_date")

    config_reason = _jira_ops_unavailable_reason()
    if config_reason:
        result = {"available": False, "reason": config_reason}
        return list(compose_json_response(result, default=str))

    try:
        on_call_periods = await client.get_on_call(start_date, end_date)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"get_on_call: Jira/JSM unreachable: {e}")
        result = {
            "available": False,
            "reason": "On-call schedule is unavailable because Jira/JSM is offline.",
        }
        return list(compose_json_response(result, default=str))

    result = {
        "available": True,
        "on_call_periods": on_call_periods,
        "total_periods": len(on_call_periods),
    }
    return list(compose_json_response(result, default=str))


@registry.tool("add_comment", _ACTION_SCHEMAS_BY_NAME["add_comment"], gated=True, refuse_when_disabled=False)
async def _tool_add_comment(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # Check if actions are enabled
    if not ActionFlags.is_actions_enabled("jira"):
        raise ValueError(
            "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable comment creation."
        )

    issue_key = arguments["issue_key"]
    comment_text = arguments["comment_text"]

    # Prefix comment with user attribution from auth DB
    user_name = arguments.get("user_name")
    user_email = arguments.get("user_email")
    display_name = user_name or user_email or "Unknown user"
    prefixed_comment = f"{display_name} via Support bot: {comment_text}"

    comment_result = await client.add_comment(issue_key, prefixed_comment)

    result = {
        "success": True,
        "issue_key": issue_key,
        "comment_id": comment_result.get("id"),
        "created": comment_result.get("created"),
        "message": f"Comment added successfully to {issue_key}",
    }
    return list(compose_json_response(result, default=str))


@registry.tool("get_transitions", _ACTION_SCHEMAS_BY_NAME["get_transitions"], gated=True, refuse_when_disabled=False)
async def _tool_get_transitions(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # Check if actions are enabled
    if not ActionFlags.is_actions_enabled("jira"):
        raise ValueError(
            "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable transition queries."
        )

    issue_key = arguments["issue_key"]
    transitions = await client.get_available_transitions(issue_key)

    result = {
        "issue_key": issue_key,
        "available_transitions": transitions,
        "total_transitions": len(transitions),
    }
    return list(compose_json_response(result, default=str))


@registry.tool("change_status", _ACTION_SCHEMAS_BY_NAME["change_status"], gated=True, refuse_when_disabled=False)
async def _tool_change_status(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # Check if actions are enabled
    if not ActionFlags.is_actions_enabled("jira"):
        raise ValueError(
            "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable status changes."
        )

    issue_key = arguments["issue_key"]
    transition = arguments["transition"]
    current_user_email = arguments.get("user_email")
    current_user_name = arguments.get("user_name")

    transition_result = await client.transition_issue(
        issue_key, transition, current_user_email, current_user_name
    )
    result = transition_result
    return list(compose_json_response(result, default=str))


@registry.tool("assign_issue", _ACTION_SCHEMAS_BY_NAME["assign_issue"], gated=True, refuse_when_disabled=False)
async def _tool_assign_issue(arguments: Dict[str, Any]) -> List[types.TextContent]:
    if not ActionFlags.is_actions_enabled("jira"):
        raise ValueError(
            "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable assignments."
        )

    result = await client.assign_issue(
        issue_key=arguments["issue_key"],
        assignee=arguments["assignee"],
        current_user_email=arguments.get("user_email"),
    )
    return list(compose_json_response(result, default=str))


@registry.tool("add_on_call_override", _ACTION_SCHEMAS_BY_NAME["add_on_call_override"], gated=True, refuse_when_disabled=False)
async def _tool_add_on_call_override(arguments: Dict[str, Any]) -> List[types.TextContent]:
    # Check if actions are enabled
    if not ActionFlags.is_actions_enabled("jira"):
        raise ValueError(
            "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable on-call override creation."
        )

    config_reason = _jira_ops_unavailable_reason()
    if config_reason:
        result = {"available": False, "success": False, "reason": config_reason}
        return list(compose_json_response(result, default=str))

    user_name = arguments["user_name"]
    start_time = arguments["start_time"]
    end_time = arguments["end_time"]

    try:
        override_result = await client.add_on_call_override(user_name, start_time, end_time)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.warning(f"add_on_call_override: Jira/JSM unreachable: {e}")
        result = {
            "available": False,
            "success": False,
            "reason": "On-call schedule is unavailable because Jira/JSM is offline.",
        }
        return list(compose_json_response(result, default=str))

    result = {"available": True, **override_result}
    return list(compose_json_response(result, default=str))



handle_list_tools = server.list_tools()(registry.handle_list_tools)
handle_call_tool = server.call_tool()(registry.handle_call_tool)


async def main():
    """Main entry point"""
    await run_stdio_server(server, name="jira-analysis", label="Jira")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Jira server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Jira server crashed: {e}", file=sys.stderr)
        sys.exit(1)
