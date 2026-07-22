"""Jira webhook handlers (ticket comments / status changes).

Split out of chat_orchestrator/handler.py as part of the Phase 5 file split.
"""

from __future__ import annotations

import os
from typing import Any, Dict

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def _handle_jira_webhook(args: Dict[str, Any]) -> Dict[str, Any]:
    """
    Handle Jira webhook for ticket comments and status changes.

    Jira webhook format:
    {
        "webhookEvent": "jira:issue_updated",
        "issue": {
            "key": "SUP-123",
            "fields": {
                "status": {"name": "Closed"},
                "customfield_10057": "grid-123"  // Grid custom field
            }
        },
        "comment": {
            "body": "Comment text",
            "author": {"displayName": "John Doe"},
            "jiraProperties": [
                {"key": "sd.public.comment", "value": {"internal": false}}
            ]
        },
        "changelog": {
            "items": [
                {"field": "status", "toString": "Closed"}
            ]
        }
    }

    Args:
        args: Jira webhook payload

    Returns:
        WebhookResponse dict
    """
    try:
        # Validate API key
        api_key = os.getenv("JIRA_WEBHOOK_API_KEY", "")
        provided_key = args.get("api_key") or args.get("headers", {}).get("X-API-Key")

        if not api_key or provided_key != api_key:
            LOGGER.warning("Unauthorized Jira webhook request: invalid API key")
            return {
                "success": False,
                "error": "Unauthorized",
                "statusCode": 401,
            }

        webhook_event = args.get("webhookEvent", "")
        issue = args.get("issue", {})
        issue_key = issue.get("key")

        if not issue_key:
            LOGGER.warning("Jira webhook missing issue key")
            return {
                "success": False,
                "error": "Missing issue key",
                "statusCode": 400,
            }

        # Handle status change to Closed
        if webhook_event == "jira:issue_updated":
            changelog = args.get("changelog", {})
            status_changes = [
                item
                for item in changelog.get("items", [])
                if item.get("field") == "status" and item.get("toString") == "Closed"
            ]

            if status_changes:
                LOGGER.info(f"Jira ticket {issue_key} closed, handling closure")
                return _handle_jira_closure(issue_key)

        # Handle comment added
        comment = args.get("comment")
        if comment:
            # Check if comment is public (reply to customer)
            jira_properties = comment.get("jsdPublic", False) or any(
                prop.get("key") == "sd.public.comment"
                and not prop.get("value", {}).get("internal", True)
                for prop in comment.get("jiraProperties", [])
            )

            if jira_properties:
                comment_body = comment.get("body", "")
                comment_author = comment.get("author", {}).get("displayName", "Support")

                LOGGER.info(
                    f"Jira ticket {issue_key} received public comment from {comment_author}"
                )
                return _handle_jira_comment(issue_key, comment_body, comment_author)

        # Acknowledge webhook but no action needed
        return {
            "success": True,
            "message": "Webhook processed, no action needed",
            "statusCode": 200,
        }

    except Exception as e:
        LOGGER.exception(f"Error handling Jira webhook: {e}")
        return {
            "success": False,
            "error": str(e),
            "statusCode": 500,
        }


def _handle_jira_closure(issue_key: str) -> Dict[str, Any]:
    """
    Handle Jira ticket closure - find session and send resolution message to customer.

    Args:
        issue_key: Jira ticket key (e.g., "SUP-123")

    Returns:
        Response dict
    """
    # Jira escalation tracking is not currently supported
    # All escalations use Telegram as the primary channel
    LOGGER.warning(f"Jira closure webhook received for {issue_key} but Jira tracking not supported")
    return {
        "success": False,
        "error": "Jira escalation tracking not supported - use Telegram escalations",
        "statusCode": 501,
    }


def _handle_jira_comment(issue_key: str, comment_body: str, comment_author: str) -> Dict[str, Any]:
    """
    Handle Jira comment - forward to customer as if it came from LLM.

    Args:
        issue_key: Jira ticket key
        comment_body: Comment text
        comment_author: Comment author name

    Returns:
        Response dict
    """
    # Jira escalation tracking is not currently supported
    # All escalations use Telegram as the primary channel
    LOGGER.warning(f"Jira comment webhook received for {issue_key} but Jira tracking not supported")
    return {
        "success": False,
        "error": "Jira escalation tracking not supported - use Telegram escalations",
        "statusCode": 501,
    }
