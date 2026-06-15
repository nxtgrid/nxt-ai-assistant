#!/usr/bin/env python3
"""
Jira Analysis MCP Server

This MCP server provides tools to fetch Jira issues, filter by date and custom fields,
summarize comments, and prepare data for LLM-based categorization and analysis.
"""

import asyncio
import base64
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import mcp.server.stdio
import mcp.types as types
from dotenv import load_dotenv
from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.types import ServerCapabilities

# Load environment variables from .env file BEFORE importing shared_code
# This ensures db_settings picks up the correct values
load_dotenv()

from shared_code.config.action_flags import ActionFlags
from shared_code.utils.http_client import HTTPClientMixin

from shared.utils.date_utils import (
    compose_date_range_query,
    filter_by_date_range,
)
from shared.utils.response_formatters import compose_error_response, compose_json_response

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


@dataclass
class CommentSummary:
    """Represents a summarized comment analysis"""

    total_comments: int
    comment_authors: List[str]
    date_range: Dict[str, str]
    key_themes: List[str]
    sentiment_indicators: List[str]
    action_items: List[str]
    latest_comments: List[Dict[str, Any]]


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

    async def check_user_mentions(
        self, issue_keys: List[str], user_email: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Check if a user is mentioned in issues or their comments

        Args:
            issue_keys: List of issue keys to check
            user_email: User email to check for mentions (if None, uses current user)

        Returns:
            Dictionary with mention information for each issue
        """
        if not self.base_url or not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        # Get user account ID
        if user_email:
            account_id = await self.find_user_by_email(user_email)
            if not account_id:
                raise ValueError(f"Could not find user with email: {user_email}")
        else:
            current_user = await self.get_current_user()
            account_id = current_user.get("accountId")
            user_email = current_user.get("emailAddress")

        results = {}

        for issue_key in issue_keys:
            try:
                issue_data = await self.get_issue(issue_key)
                issue = self.parse_issue(issue_data)

                # Check if user is assignee or reporter
                is_assignee = False
                is_reporter = False

                assignee_data = issue_data.get("fields", {}).get("assignee")
                if assignee_data and assignee_data.get("accountId") == account_id:
                    is_assignee = True

                reporter_data = issue_data.get("fields", {}).get("reporter")
                if reporter_data and reporter_data.get("accountId") == account_id:
                    is_reporter = True

                # Check for mentions in description
                mentioned_in_description = False
                description = issue_data.get("fields", {}).get("description")
                if description:
                    # Check for mention syntax [~accountId] or plain text mentions
                    desc_str = json.dumps(description)
                    if f"[~{account_id}]" in desc_str or account_id in desc_str:
                        mentioned_in_description = True

                # Check for mentions in comments
                mentioned_in_comments = []
                for comment in issue.comments:
                    comment_author_id = None
                    # Get the author account ID from the original data
                    for orig_comment in (
                        issue_data.get("fields", {}).get("comment", {}).get("comments", [])
                    ):
                        if orig_comment.get("id") == comment.get("id"):
                            comment_author_id = orig_comment.get("author", {}).get("accountId")
                            break

                    # Check if user is mentioned in comment body or is the comment author
                    is_author = comment_author_id == account_id
                    is_mentioned = f"[~{account_id}]" in comment.get(
                        "body", ""
                    ) or account_id in str(comment)

                    if is_mentioned or is_author:
                        mentioned_in_comments.append(
                            {
                                "comment_id": comment.get("id"),
                                "author": comment.get("author"),
                                "created": comment.get("created"),
                                "is_author": is_author,
                                "is_mentioned": is_mentioned,
                                "body_preview": comment.get("body", "")[:200],
                            }
                        )

                results[issue_key] = {
                    "user_email": user_email,
                    "account_id": account_id,
                    "is_assignee": is_assignee,
                    "is_reporter": is_reporter,
                    "mentioned_in_description": mentioned_in_description,
                    "mentioned_in_comments": mentioned_in_comments,
                    "total_mentions": len(mentioned_in_comments),
                    "is_involved": is_assignee
                    or is_reporter
                    or mentioned_in_description
                    or len(mentioned_in_comments) > 0,
                }

            except Exception as e:
                results[issue_key] = {"error": str(e)}

        return results

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

    async def get_schedule_participants(self) -> List[Dict[str, Any]]:
        """
        Get all participants (team members) from the on-call schedule rotations.

        This returns all users who are part of any rotation in the schedule,
        not just who is currently on-call.

        Returns:
            List of participants with user names and emails
        """
        session = await self.get_session()

        if not self.auth_header:
            raise ValueError("Jira client not properly configured. Call setup_auth first.")

        if not self.ops_cloud_id or not self.ops_schedule_id:
            raise ValueError(
                "Jira Service Management Ops not configured. "
                "Set JIRA_OPS_CLOUD_ID and JIRA_OPS_SCHEDULE_ID environment variables."
            )

        # Get schedule details which includes rotations
        url = (
            f"https://api.atlassian.com/jsm/ops/api/{self.ops_cloud_id}/v1/"
            f"schedules/{self.ops_schedule_id}"
        )

        headers = {"Authorization": self.auth_header, "Accept": "application/json"}

        async with session.get(url, headers=headers) as response:
            if response.status != 200:
                error_text = await response.text()
                raise Exception(f"JSM Ops API error ({response.status}): {error_text}")

            schedule_data = await response.json()
            logger.info(f"Schedule data keys: {schedule_data.keys()}")

        # Extract unique user IDs from all rotations
        user_ids = set()
        rotations = schedule_data.get("rotations", [])
        logger.info(f"Found {len(rotations)} rotations in schedule")

        for rotation in rotations:
            participants = rotation.get("participants", [])
            for participant in participants:
                if participant.get("type") == "user":
                    user_id = participant.get("id")
                    if user_id:
                        user_ids.add(user_id)
                        logger.debug(f"Found user {user_id} in rotation {rotation.get('name')}")

        logger.info(f"Found {len(user_ids)} unique users in schedule rotations")

        # Fetch user details for each user ID
        participants_list = []
        for user_id in user_ids:
            user_info = await self.get_user_by_account_id(user_id)
            if user_info:
                participants_list.append(
                    {
                        "account_id": user_id,
                        "display_name": user_info.get("displayName"),
                        "email": user_info.get("emailAddress"),
                    }
                )
            else:
                logger.warning(f"Could not fetch user info for account_id {user_id}")

        # Sort by display name for consistent output
        participants_list.sort(key=lambda x: x.get("display_name", "") or "")

        logger.info(f"Returning {len(participants_list)} schedule participants")
        return participants_list

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

    def filter_comments_by_date(
        self,
        comments: List[Dict[str, Any]],
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Filter comments by date range using shared utility"""
        return list(filter_by_date_range(comments, start_date, end_date, "created"))

    def summarize_comments(self, comments: List[Dict[str, Any]]) -> CommentSummary:
        """Summarize comments for analysis"""
        if not comments:
            return CommentSummary(
                total_comments=0,
                comment_authors=[],
                date_range={},
                key_themes=[],
                sentiment_indicators=[],
                action_items=[],
                latest_comments=[],
            )

        # Basic stats
        total_comments = len(comments)
        authors = list(
            set([comment.get("author", "") for comment in comments if comment.get("author")])
        )

        # Date range
        dates = [comment.get("created", "") for comment in comments if comment.get("created")]
        dates.sort()
        date_range = {"earliest": dates[0] if dates else "", "latest": dates[-1] if dates else ""}

        # Extract key themes (simple keyword analysis)
        all_text = " ".join([comment.get("body", "") for comment in comments])
        words = all_text.lower().split()

        # Common issue-related keywords
        theme_keywords = {
            "bug": ["bug", "error", "issue", "problem", "broken", "failing", "failure"],
            "enhancement": ["feature", "enhancement", "improvement", "upgrade", "add"],
            "documentation": ["docs", "documentation", "readme", "guide", "manual"],
            "testing": ["test", "testing", "qa", "quality", "verification"],
            "performance": ["performance", "slow", "fast", "speed", "optimization"],
            "security": ["security", "vulnerability", "auth", "permission", "access"],
        }

        key_themes = []
        for theme, keywords in theme_keywords.items():
            if any(keyword in words for keyword in keywords):
                key_themes.append(theme)

        # Sentiment indicators (simple keyword analysis)
        positive_words = [
            "good",
            "great",
            "excellent",
            "perfect",
            "working",
            "fixed",
            "resolved",
            "completed",
        ]
        negative_words = [
            "bad",
            "terrible",
            "broken",
            "issue",
            "problem",
            "error",
            "failed",
            "blocked",
        ]

        sentiment_indicators = []
        if any(word in words for word in positive_words):
            sentiment_indicators.append("positive")
        if any(word in words for word in negative_words):
            sentiment_indicators.append("negative")

        # Action items (simple pattern matching)
        action_patterns = ["todo", "action", "need to", "should", "must", "will", "plan to"]
        action_items = []
        for comment in comments:
            body = comment.get("body", "").lower()
            for pattern in action_patterns:
                if pattern in body:
                    # Extract sentence containing the action pattern
                    sentences = body.split(".")
                    for sentence in sentences:
                        if pattern in sentence:
                            action_items.append(sentence.strip())
                            break

        # Latest comments (last 5)
        sorted_comments = sorted(comments, key=lambda x: x.get("created", ""), reverse=True)
        latest_comments = sorted_comments[:5]

        return CommentSummary(
            total_comments=total_comments,
            comment_authors=authors,
            date_range=date_range,
            key_themes=key_themes,
            sentiment_indicators=sentiment_indicators,
            action_items=action_items[:10],  # Limit to 10 action items
            latest_comments=latest_comments,
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

    def prepare_llm_input(
        self,
        issues: List[JiraIssue],
        comment_summaries: Dict[str, CommentSummary],
        include_descriptions: bool = True,
        include_comments: bool = True,
        max_description_length: int = 500,
        max_comment_length: int = 300,
    ) -> Dict[str, Any]:
        """Prepare structured data for LLM categorization"""

        def truncate_text(text: str, max_length: int) -> str:
            if len(text) <= max_length:
                return text
            return text[:max_length] + "..."

        llm_data = {
            "summary": {
                "total_issues": len(issues),
                "issue_types": list(set([issue.issue_type for issue in issues])),
                "statuses": list(set([issue.status for issue in issues])),
                "priorities": list(set([issue.priority for issue in issues])),
                "total_comments": sum(
                    [summary.total_comments for summary in comment_summaries.values()]
                ),
                "date_range": {
                    "earliest_created": min([issue.created for issue in issues]) if issues else "",
                    "latest_updated": max([issue.updated for issue in issues]) if issues else "",
                },
            },
            "issues": [],
        }

        issues_list: List[Dict[str, Any]] = llm_data["issues"]  # type: ignore[assignment]

        for issue in issues:
            comment_summary = comment_summaries.get(
                issue.key,
                CommentSummary(
                    total_comments=0,
                    comment_authors=[],
                    date_range={},
                    key_themes=[],
                    sentiment_indicators=[],
                    action_items=[],
                    latest_comments=[],
                ),
            )

            issue_data = {
                "key": issue.key,
                "summary": issue.summary,
                "status": issue.status,
                "priority": issue.priority,
                "issue_type": issue.issue_type,
                "assignee": issue.assignee,
                "reporter": issue.reporter,
                "created": issue.created,
                "updated": issue.updated,
                "labels": issue.labels,
                "components": issue.components,
                "custom_fields": {
                    k: str(v) for k, v in issue.custom_fields.items() if v is not None
                },
            }

            if include_descriptions and issue.description:
                issue_data["description"] = truncate_text(issue.description, max_description_length)

            if include_comments:
                issue_data["comment_analysis"] = {
                    "total_comments": comment_summary.total_comments,
                    "comment_authors": comment_summary.comment_authors,
                    "key_themes": comment_summary.key_themes,
                    "sentiment_indicators": comment_summary.sentiment_indicators,
                    "action_items": comment_summary.action_items[:5],  # Top 5 action items
                    "latest_comment_preview": truncate_text(
                        (
                            comment_summary.latest_comments[0].get("body", "")
                            if comment_summary.latest_comments
                            else ""
                        ),
                        max_comment_length,
                    ),
                }

            issues_list.append(issue_data)

        return llm_data


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


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    """List available tools based on actions_enabled flag"""
    actions_enabled = ActionFlags.is_actions_enabled("jira")

    # Read-only tools (always available)
    tools = [
        types.Tool(
            name="jira_search_issues_with_comments",
            description="Search Jira issues in the OPS project. Returns: key, summary, status, assignee, reporter, priority, issue_type, grid, organization, created, updated, comments. Filter by grid, organization, status, assignee (accepts person's name like 'Chovwe' or email), date range, labels, or text search. To find someone's tickets, use the assignee parameter with their name. Defaults to last 90 days if no dates specified. When presenting results, always include the created date in short format (e.g. '15 Mar') alongside each ticket. IMPORTANT: for topic or category questions (e.g. 'tickets about DCUs being offline', 'grid downtime tickets'), do NOT rely on text_search — tickets rarely use the same words as the question. Instead fetch the open tickets without text_search (the list is small) and judge from the summaries yourself which ones match the topic.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text_search": {
                        "type": "string",
                        "description": "Literal keyword match against summary/description — only finds tickets that contain these exact words. Monitoring alert tickets use templated phrasing that often differs from how a user describes the issue (e.g. a DCU outage ticket reads 'DCU 230401080 in Okpokunou could have a problem, causing Meter Issues', not 'DCU offline'). Use only for distinctive literal strings like a meter number, device ID, or grid name; otherwise omit and filter the results yourself.",
                    },
                    "grid": {"type": "string", "description": "Grid name to filter by"},
                    "organization": {
                        "type": "string",
                        "description": "Organization name to filter by (e.g., 'Calin')",
                    },
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue statuses to filter by (optional)",
                    },
                    "assignee": {
                        "type": "string",
                        "description": "Assignee name or email (optional). Accepts person's name (e.g., 'Chovwe', 'Vaibhav') with fuzzy matching, email address, 'me' for current user, or 'unassigned' for unassigned issues.",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Created after date (YYYY-MM-DD), defaults to 90 days ago",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Created before date (YYYY-MM-DD), defaults to today",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": "Updated after date (YYYY-MM-DD), defaults to 90 days ago",
                    },
                    "updated_before": {
                        "type": "string",
                        "description": "Updated before date (YYYY-MM-DD), defaults to today",
                    },
                    "labels": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Labels to filter by (optional)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 50,
                        "description": "Maximum number of results",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="jira_get_issue",
            description="Get detailed information about a specific Jira issue. Returns: key, summary, description, status, assignee, reporter, priority, issue_type, grid, organization, created, updated, labels, components, custom_fields, comments.",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_key": {
                        "type": "string",
                        "description": "Jira issue key (e.g., PROJ-123)",
                    }
                },
                "required": ["issue_key"],
            },
        ),
        types.Tool(
            name="jira_analyze_comments",
            description="Analyze and summarize comments from Jira issues with date filtering",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of issue keys to analyze",
                    },
                    "comment_start_date": {
                        "type": "string",
                        "description": "Filter comments after this date (ISO format)",
                    },
                    "comment_end_date": {
                        "type": "string",
                        "description": "Filter comments before this date (ISO format)",
                    },
                    "include_sentiment": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include sentiment analysis",
                    },
                    "include_themes": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include theme extraction",
                    },
                    "include_action_items": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include action item extraction",
                    },
                },
                "required": ["issue_keys"],
            },
        ),
        types.Tool(
            name="jira_prepare_llm_categorization",
            description="Prepare filtered Jira issues and comment analysis for LLM-based categorization",
            inputSchema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project key or name"},
                    "issue_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue types to include",
                    },
                    "statuses": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Issue statuses to include",
                    },
                    "created_after": {
                        "type": "string",
                        "description": "Include issues created after this date (YYYY-MM-DD)",
                    },
                    "created_before": {
                        "type": "string",
                        "description": "Include issues created before this date (YYYY-MM-DD)",
                    },
                    "updated_after": {
                        "type": "string",
                        "description": "Include issues updated after this date (YYYY-MM-DD)",
                    },
                    "updated_before": {
                        "type": "string",
                        "description": "Include issues updated before this date (YYYY-MM-DD)",
                    },
                    "custom_field_filters": {
                        "type": "object",
                        "description": "Custom field filters (field_id: value)",
                    },
                    "comment_start_date": {
                        "type": "string",
                        "description": "Include comments after this date (ISO format)",
                    },
                    "comment_end_date": {
                        "type": "string",
                        "description": "Include comments before this date (ISO format)",
                    },
                    "max_results": {
                        "type": "integer",
                        "default": 100,
                        "description": "Maximum number of issues to analyze",
                    },
                    "include_descriptions": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include issue descriptions",
                    },
                    "include_comments": {
                        "type": "boolean",
                        "default": True,
                        "description": "Include comment analysis",
                    },
                    "max_description_length": {
                        "type": "integer",
                        "default": 500,
                        "description": "Maximum description length",
                    },
                    "max_comment_length": {
                        "type": "integer",
                        "default": 300,
                        "description": "Maximum comment preview length",
                    },
                },
                "required": [],
            },
        ),
        types.Tool(
            name="jira_get_fields",
            description="Get all available Jira fields including custom fields",
            inputSchema={"type": "object", "properties": {}, "required": []},
            visible_to_customer=False,
        ),
        types.Tool(
            name="jira_generate_categorization_prompt",
            description="Generate a structured prompt for LLM categorization based on Jira data",
            inputSchema={
                "type": "object",
                "properties": {
                    "categorization_type": {
                        "type": "string",
                        "enum": ["priority", "theme", "sentiment", "workload", "custom"],
                        "description": "Type of categorization to perform",
                    },
                    "custom_categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Custom categories for classification",
                    },
                    "analysis_focus": {
                        "type": "string",
                        "enum": ["issues_only", "comments_only", "both"],
                        "default": "both",
                        "description": "Focus analysis on issues, comments, or both",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": ["json", "csv", "summary"],
                        "default": "json",
                        "description": "Desired output format",
                    },
                },
                "required": ["categorization_type"],
            },
        ),
        types.Tool(
            name="jira_check_mentions",
            description="Check if the current user (or specified user) is mentioned in Jira issues or comments",
            inputSchema={
                "type": "object",
                "properties": {
                    "issue_keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of issue keys to check for mentions",
                    },
                    "user_email": {
                        "type": "string",
                        "description": "User email to check (optional, defaults to current authenticated user)",
                    },
                },
                "required": ["issue_keys"],
            },
        ),
        types.Tool(
            name="jira_get_on_call",
            description="Get on-call schedule for a date range from Jira Service Management Ops. Returns a clean list of on-call periods with user names, email addresses, and time ranges. Automatically queries 1 day before start_date and 1 day after end_date to ensure complete coverage.",
            inputSchema={
                "type": "object",
                "properties": {
                    "start_date": {
                        "type": "string",
                        "description": "ISO 8601 formatted start datetime (e.g., '2025-10-24T00:00:00Z'). Required.",
                    },
                    "end_date": {
                        "type": "string",
                        "description": "ISO 8601 formatted end datetime. Optional. If not provided, defaults to start_date.",
                    },
                },
                "required": ["start_date"],
            },
        ),
        types.Tool(
            name="jira_get_schedule_participants",
            description="Get all team members from the on-call schedule rotations. Returns all users who are part of any rotation in the schedule, not just who is currently on-call. Useful for getting the full list of people who can be assigned to on-call duties.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        ),
        types.Tool(
            name="jira_get_organization_options",
            description="Get all available organization options for JIRA tickets in the OPS project. Returns the list of organizations that can be assigned to tickets.",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": [],
            },
            visible_to_customer=False,
        ),
    ]

    # Action tools (only available when actions are enabled)
    if actions_enabled:
        tools.extend(
            [
                types.Tool(
                    name="jira_add_comment",
                    description="[ACTION - MODIFIES JIRA] Add a comment to a Jira issue. This tool creates a new comment on the specified issue.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., PROJ-123)",
                            },
                            "comment_text": {
                                "type": "string",
                                "description": "The comment text to add",
                            },
                        },
                        "required": ["issue_key", "comment_text"],
                    },
                ),
                types.Tool(
                    name="jira_get_transitions",
                    description="[READ-ONLY] Get available status transitions for a Jira issue. This tool only retrieves information, it does not change the issue status.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., PROJ-123)",
                            }
                        },
                        "required": ["issue_key"],
                    },
                ),
                types.Tool(
                    name="jira_change_status",
                    description="[ACTION - MODIFIES JIRA] Change the status of a Jira issue by applying a transition. This tool modifies the issue workflow state. Only works if the issue is assigned to the current user.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., PROJ-123)",
                            },
                            "transition": {
                                "type": "string",
                                "description": "Transition ID or name (e.g., '31' or 'In Progress')",
                            },
                            "current_user_email": {
                                "type": "string",
                                "description": "Email of the user making the change (optional, defaults to authenticated user)",
                            },
                        },
                        "required": ["issue_key", "transition"],
                    },
                ),
                types.Tool(
                    name="jira_add_on_call_override",
                    description="[ACTION - MODIFIES JSM SCHEDULE] Add an on-call override for a specific user and time period in JSM Ops schedule. This tool creates a new on-call assignment in the schedule. Use this to assign someone to be on-call for a specific date/time range (e.g., 9am-5pm, 5pm-7pm). User will be looked up by name from the system.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "user_name": {
                                "type": "string",
                                "description": "Name of the user to add as on-call (e.g., 'Vaibhav', 'John Doe'). System will look up their email automatically.",
                            },
                            "start_time": {
                                "type": "string",
                                "description": "ISO 8601 formatted start datetime (e.g., '2025-10-24T09:00:00Z' for 9am UTC)",
                            },
                            "end_time": {
                                "type": "string",
                                "description": "ISO 8601 formatted end datetime (e.g., '2025-10-24T17:00:00Z' for 5pm UTC)",
                            },
                        },
                        "required": ["user_name", "start_time", "end_time"],
                    },
                ),
            ]
        )

    logger.info(f"JIRA server: actions_enabled={actions_enabled}, {len(tools)} tools available")
    return tools


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any]) -> List[types.TextContent]:
    """Handle tool calls"""
    # Normalize tool name - accept both "search_issues_with_comments" and "jira_search_issues_with_comments"
    # This handles the case where the orchestrator strips the prefix when routing via bridge
    if not name.startswith("jira_"):
        name = f"jira_{name}"

    try:
        if name == "jira_search_issues_with_comments":
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

        elif name == "jira_get_issue":
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

        elif name == "jira_analyze_comments":
            issue_keys = arguments["issue_keys"]
            comment_summaries_dict: Dict[str, Dict[str, Any]] = {}

            for issue_key in issue_keys:
                try:
                    issue_data = await client.get_issue(issue_key)
                    issue = client.parse_issue(issue_data)

                    # Filter comments by date if specified
                    filtered_comments = client.filter_comments_by_date(
                        issue.comments,
                        arguments.get("comment_start_date"),
                        arguments.get("comment_end_date"),
                    )

                    # Summarize comments
                    summary = client.summarize_comments(filtered_comments)
                    comment_summaries_dict[issue_key] = {
                        "total_comments": summary.total_comments,
                        "comment_authors": summary.comment_authors,
                        "date_range": summary.date_range,
                        "key_themes": (
                            summary.key_themes if arguments.get("include_themes", True) else []
                        ),
                        "sentiment_indicators": (
                            summary.sentiment_indicators
                            if arguments.get("include_sentiment", True)
                            else []
                        ),
                        "action_items": (
                            summary.action_items
                            if arguments.get("include_action_items", True)
                            else []
                        ),
                        "latest_comments": [
                            {
                                "author": comment["author"],
                                "created": comment["created"],
                                "body_preview": (
                                    comment["body"][:200] + "..."
                                    if len(comment["body"]) > 200
                                    else comment["body"]
                                ),
                            }
                            for comment in summary.latest_comments
                        ],
                    }
                except Exception as e:
                    comment_summaries_dict[issue_key] = {"error": str(e)}

            result = {"comment_analysis": comment_summaries_dict}

        elif name == "jira_prepare_llm_categorization":
            # Build JQL query
            jql = client.build_jql_query(
                project=arguments.get("project"),
                issue_types=arguments.get("issue_types"),
                statuses=arguments.get("statuses"),
                created_after=arguments.get("created_after"),
                created_before=arguments.get("created_before"),
                updated_after=arguments.get("updated_after"),
                updated_before=arguments.get("updated_before"),
                custom_field_filters=arguments.get("custom_field_filters"),
            )

            # Search issues
            search_result = await client.search_issues(
                jql=jql, max_results=arguments.get("max_results", 100)
            )

            # Parse issues and analyze comments
            issues: List[JiraIssue] = []
            comment_summaries: Dict[str, CommentSummary] = {}

            for issue_data in search_result.get("issues", []):
                issue = client.parse_issue(issue_data)
                issues.append(issue)

                # Filter and summarize comments
                filtered_comments = client.filter_comments_by_date(
                    issue.comments,
                    arguments.get("comment_start_date"),
                    arguments.get("comment_end_date"),
                )

                comment_summaries[issue.key] = client.summarize_comments(filtered_comments)

            # Prepare LLM input
            llm_input = client.prepare_llm_input(
                issues,
                comment_summaries,
                include_descriptions=arguments.get("include_descriptions", True),
                include_comments=arguments.get("include_comments", True),
                max_description_length=arguments.get("max_description_length", 500),
                max_comment_length=arguments.get("max_comment_length", 300),
            )

            result = {
                "jql_used": jql,
                "llm_input": llm_input,
                "processing_summary": {
                    "issues_processed": len(issues),
                    "total_comments_analyzed": int(
                        sum([s.total_comments for s in comment_summaries.values()])
                    ),
                    "date_filters_applied": {
                        "comment_start_date": arguments.get("comment_start_date"),
                        "comment_end_date": arguments.get("comment_end_date"),
                    },
                },
            }

        elif name == "jira_get_fields":
            fields = await client.get_fields()

            # Organize fields by type
            standard_fields = []
            custom_fields = []

            for field in fields:
                field_info = {
                    "id": field.get("id"),
                    "name": field.get("name"),
                    "type": field.get("schema", {}).get("type"),
                    "custom": field.get("custom", False),
                }

                if field.get("custom"):
                    custom_fields.append(field_info)
                else:
                    standard_fields.append(field_info)

            result = {
                "standard_fields": standard_fields,
                "custom_fields": custom_fields,
                "total_fields": len(fields),
            }

        elif name == "jira_generate_categorization_prompt":
            categorization_type = arguments["categorization_type"]
            custom_categories = arguments.get("custom_categories", [])
            analysis_focus = arguments.get("analysis_focus", "both")
            output_format = arguments.get("output_format", "json")

            # Generate categorization prompt based on type
            prompts = {
                "priority": {
                    "system": "You are analyzing Jira issues to categorize them by priority and urgency.",
                    "categories": ["Critical", "High", "Medium", "Low", "Backlog"],
                    "criteria": "Consider issue impact, urgency, customer affect, and business value.",
                },
                "theme": {
                    "system": "You are analyzing Jira issues to categorize them by thematic content.",
                    "categories": [
                        "Bug Fix",
                        "Feature Request",
                        "Technical Debt",
                        "Documentation",
                        "Performance",
                        "Security",
                        "Infrastructure",
                    ],
                    "criteria": "Analyze issue content, description, and comments to identify primary themes.",
                },
                "sentiment": {
                    "system": "You are analyzing Jira issues to categorize them by sentiment and tone.",
                    "categories": ["Positive", "Neutral", "Negative", "Urgent", "Frustrated"],
                    "criteria": "Focus on language tone, urgency indicators, and emotional content in descriptions and comments.",
                },
                "workload": {
                    "system": "You are analyzing Jira issues to categorize them by estimated workload and complexity.",
                    "categories": [
                        "Quick Fix",
                        "Small Task",
                        "Medium Task",
                        "Large Task",
                        "Epic/Project",
                    ],
                    "criteria": "Consider issue complexity, scope, dependencies, and estimated effort.",
                },
                "custom": {
                    "system": "You are analyzing Jira issues to categorize them using custom categories.",
                    "categories": (
                        custom_categories
                        if custom_categories
                        else ["Category A", "Category B", "Category C"]
                    ),
                    "criteria": "Use the provided custom categories to classify issues based on your analysis.",
                },
            }

            prompt_config = prompts[categorization_type]

            focus_instructions = {
                "issues_only": "Focus your analysis only on issue titles, descriptions, and metadata. Ignore comment data.",
                "comments_only": "Focus your analysis only on comment content and sentiment. Ignore issue descriptions.",
                "both": "Analyze both issue content (title, description, metadata) and comment analysis data.",
            }

            format_instructions = {
                "json": "Return results as a JSON object with issue keys as keys and categories as values.",
                "csv": "Return results as CSV format with columns: issue_key, category, confidence, reasoning.",
                "summary": "Return a summary report with category distributions and key insights.",
            }

            prompt = f"""
{prompt_config["system"]}

CATEGORIZATION TASK:
- Type: {categorization_type.title()} Categorization
- Categories: {", ".join(prompt_config["categories"])}
- Criteria: {prompt_config["criteria"]}

ANALYSIS FOCUS:
{focus_instructions[analysis_focus]}

OUTPUT FORMAT:
{format_instructions[output_format]}

INSTRUCTIONS:
1. Analyze each issue in the provided data
2. Assign it to one of the specified categories
3. Provide confidence score (0-1) for your categorization
4. Include brief reasoning for each categorization
5. Identify patterns and trends across issues

The Jira data will be provided in the following message. Please categorize all issues according to the above specifications.
"""

            result = {
                "categorization_type": categorization_type,
                "prompt": prompt,
                "categories": prompt_config["categories"],
                "analysis_focus": analysis_focus,
                "output_format": output_format,
                "usage_instructions": "Send this prompt followed by your Jira data from jira_prepare_llm_categorization to an LLM for analysis.",
            }

        elif name == "jira_add_comment":
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

        elif name == "jira_check_mentions":
            issue_keys = arguments["issue_keys"]
            user_email = arguments.get("user_email")

            mention_results = await client.check_user_mentions(issue_keys, user_email)

            result = {
                "checked_issues": len(issue_keys),
                "results": mention_results,
                "summary": {
                    "total_issues_with_involvement": sum(
                        1
                        for r in mention_results.values()
                        if isinstance(r, dict) and r.get("is_involved")
                    ),
                    "total_mentions": sum(
                        r.get("total_mentions", 0)
                        for r in mention_results.values()
                        if isinstance(r, dict)
                    ),
                    "issues_as_assignee": [
                        k
                        for k, r in mention_results.items()
                        if isinstance(r, dict) and r.get("is_assignee")
                    ],
                    "issues_as_reporter": [
                        k
                        for k, r in mention_results.items()
                        if isinstance(r, dict) and r.get("is_reporter")
                    ],
                    "issues_with_mentions": [
                        k
                        for k, r in mention_results.items()
                        if isinstance(r, dict) and r.get("total_mentions", 0) > 0
                    ],
                },
            }

        elif name == "jira_get_transitions":
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

        elif name == "jira_change_status":
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

        elif name == "jira_assign_issue":
            if not ActionFlags.is_actions_enabled("jira"):
                raise ValueError(
                    "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable assignments."
                )

            result = await client.assign_issue(
                issue_key=arguments["issue_key"],
                assignee=arguments["assignee"],
                current_user_email=arguments.get("user_email"),
            )

        elif name == "jira_get_on_call":
            start_date = arguments["start_date"]
            end_date = arguments.get("end_date")

            on_call_periods = await client.get_on_call(start_date, end_date)

            result = {"on_call_periods": on_call_periods, "total_periods": len(on_call_periods)}

        elif name == "jira_get_schedule_participants":
            participants = await client.get_schedule_participants()

            result = {
                "participants": participants,
                "total_participants": len(participants),
            }

        elif name == "jira_get_organization_options":
            org_field_id = await client.get_organization_field_id()
            if not org_field_id:
                result = {
                    "field_id": None,
                    "options": [],
                    "error": "Organization field not found in JIRA",
                }
            else:
                options = await client.get_field_options(org_field_id, "OPS")
                result = {
                    "field_id": org_field_id,
                    "options": options,
                    "total_options": len(options),
                }

        elif name == "jira_add_on_call_override":
            # Check if actions are enabled
            if not ActionFlags.is_actions_enabled("jira"):
                raise ValueError(
                    "JIRA actions are disabled. Set JIRA_ACTIONS_ENABLED=true to enable on-call override creation."
                )

            user_name = arguments["user_name"]
            start_time = arguments["start_time"]
            end_time = arguments["end_time"]

            override_result = await client.add_on_call_override(user_name, start_time, end_time)
            result = override_result

        elif name == "jira_get_ticket_statistics":
            days = arguments.get("days", 30)
            result = await get_ticket_statistics(days=int(days))

        else:
            raise ValueError(f"Unknown tool: {name}")

        return list(compose_json_response(result, default=str))

    except Exception as e:
        logger.error(f"Error in {name}: {str(e)}")
        return list(compose_error_response(e))


async def main():
    """Main entry point"""
    try:
        print("✅ Jira server initialized successfully", file=sys.stderr)
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            print("🔌 Connected to stdio streams", file=sys.stderr)
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="jira-analysis",
                    server_version="1.0.0",
                    capabilities=ServerCapabilities(),
                ),
            )
    except Exception as e:
        print(f"❌ Fatal error in Jira server: {e}", file=sys.stderr)
        import traceback

        traceback.print_exc(file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("🛑 Jira server stopped by user", file=sys.stderr)
    except Exception as e:
        print(f"❌ Jira server crashed: {e}", file=sys.stderr)
        sys.exit(1)
