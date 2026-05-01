"""
Broadcast Verification Service (LLM-as-Judge)

Verifies broadcast messages before sending using the chat-orchestrator's verification endpoint.
This service implements quality control for broadcast messages to customers.

The verification flow:
1. Admin composes broadcast message
2. Verification service calls chat-orchestrator's /api/v1/verify/broadcast endpoint
3. If passed: enable send button
4. If failed: show feedback, disable send until fixed
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

import httpx


@dataclass
class VerificationResult:
    """Result of broadcast verification."""

    passed: bool
    feedback: str = ""
    categories: List[str] = field(default_factory=list)
    raw_response: Optional[str] = None
    error: Optional[str] = None

    def __bool__(self) -> bool:
        """Allow using result directly in conditionals."""
        return self.passed


class BroadcastVerificationService:
    """
    Verifies broadcast message quality by calling the chat-orchestrator's verification endpoint.

    This service delegates verification to the chat-orchestrator which has access to
    Google Docs for fetching verification criteria.
    """

    def __init__(
        self,
        orchestrator_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        """
        Initialize verification service.

        Args:
            orchestrator_url: URL of chat-orchestrator (defaults to CHAT_ORCHESTRATOR_URL env var)
            api_key: API key for authentication (defaults to API_KEY env var)
        """
        self._orchestrator_url = orchestrator_url or os.getenv(
            "CHAT_ORCHESTRATOR_URL", "http://localhost:8000/chat"
        )
        self._api_key = api_key or os.getenv("API_KEY", "")

    def is_enabled(self) -> bool:
        """
        Check if verification service is available.

        Note: The actual VERIFICATION_ENABLED toggle is checked server-side by the
        chat-orchestrator. This method returns True if the service is configured
        (has URL and API key). The endpoint will return passed=True with
        "Verification disabled" feedback if the setting is off server-side.
        """
        return self.is_configured()

    def is_configured(self) -> bool:
        """Check if service is properly configured."""
        return bool(self._orchestrator_url and self._api_key)

    def verify_broadcast(
        self,
        message: str,
        target_groups: Optional[List[str]] = None,
        additional_context: Optional[str] = None,
    ) -> VerificationResult:
        """
        Verify if a broadcast message meets quality standards.

        Calls the chat-orchestrator's /api/v1/verify/broadcast endpoint.
        The endpoint checks VERIFICATION_ENABLED server-side and returns
        passed=True if verification is disabled.

        Args:
            message: The broadcast message to verify
            target_groups: Optional list of target group names (for context)
            additional_context: Optional additional context (not currently used by endpoint)

        Returns:
            VerificationResult with passed status and feedback
        """
        # Check if service is configured
        if not self.is_configured():
            return VerificationResult(
                passed=True,
                feedback="Verification skipped: service not configured",
                error="CHAT_ORCHESTRATOR_URL or API_KEY not set",
            )

        # Build endpoint URL
        endpoint = f"{self._orchestrator_url.rstrip('/')}/api/v1/verify/broadcast"

        # Prepare request payload
        payload = {
            "message": message,
            "target_groups": target_groups or [],
        }

        try:
            print(f"Calling verification endpoint: {endpoint}")

            with httpx.Client(timeout=30.0) as client:
                response = client.post(
                    endpoint,
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "X-Api-Key": self._api_key,
                    },
                )

                if response.status_code == 401:
                    return VerificationResult(
                        passed=True,
                        feedback="Verification skipped: authentication failed",
                        error="Invalid API key",
                    )

                if response.status_code != 200:
                    error_body = response.text
                    print(f"Verification API error {response.status_code}: {error_body}")
                    return VerificationResult(
                        passed=True,
                        feedback=f"Verification API error: {response.status_code}",
                        error=error_body,
                    )

                result = response.json()

                print(
                    f"Verification result: passed={result.get('passed')}, "
                    f"categories={result.get('categories', [])}"
                )

                return VerificationResult(
                    passed=bool(result.get("passed", True)),
                    feedback=str(result.get("feedback", "")),
                    categories=list(result.get("categories", [])),
                    error=result.get("error"),
                )

        except httpx.TimeoutException:
            return VerificationResult(
                passed=True,
                feedback="Verification timed out. You can still send but message was not validated.",
                error="Verification request timed out",
            )

        except Exception as e:
            print(f"Verification failed with error: {e}")
            return VerificationResult(
                passed=True,
                feedback=f"Verification error: {str(e)}. You can still send.",
                error=str(e),
            )


__all__ = ["BroadcastVerificationService", "VerificationResult"]
