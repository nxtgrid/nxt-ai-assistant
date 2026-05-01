"""
Google OAuth Authentication with Whitelist for Chat Viewer.

Provides secure authentication using Streamlit's native OIDC with two-tier access control:
1. Explicit whitelist of allowed emails
2. Staff organization validation from accounts table

Note: Streamlit's st.login() requires configuration in .streamlit/secrets.toml, but
DigitalOcean doesn't support secrets.toml files. This module loads auth config from
environment variables into st.secrets at import time to work around this limitation.
"""

import base64
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Set

import streamlit as st


def _load_auth_secrets_from_env():
    """
    Load OAuth configuration from environment variables into st.secrets.

    Streamlit's st.login() expects auth config in .streamlit/secrets.toml, but
    DigitalOcean and other cloud platforms don't support secrets files.
    This function injects env vars into Streamlit's secrets singleton.

    Required env vars:
    - GOOGLE_CLIENT_ID: Google OAuth client ID
    - GOOGLE_CLIENT_SECRET: Google OAuth client secret

    Optional env vars:
    - AUTH_REDIRECT_URI: OAuth callback URL (e.g. https://yourapp.example.com/oauth2callback)
    - AUTH_COOKIE_SECRET: Session cookie secret (auto-generated if not set)
    """
    # Only load if not already configured via secrets.toml
    try:
        if hasattr(st, "secrets") and "auth" in st.secrets:
            return  # Already configured via secrets.toml
    except Exception:
        pass  # No secrets file, proceed with env loading

    # Support both GOOGLE_* and AUTH_* naming conventions for compatibility
    client_id = os.getenv("GOOGLE_CLIENT_ID") or os.getenv("AUTH_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET") or os.getenv("AUTH_CLIENT_SECRET")

    if not client_id or not client_secret:
        return  # Can't configure without credentials

    # Generate a stable cookie secret if not provided
    # Use a hash of the client_id to keep it stable across restarts
    cookie_secret = os.getenv("AUTH_COOKIE_SECRET")
    if not cookie_secret:
        # Generate from client_id for stability
        import hashlib

        cookie_secret = hashlib.sha256(client_id.encode()).hexdigest()

    # Allow override via env var, default to the configured domain
    redirect_uri = os.getenv("AUTH_REDIRECT_URI", "http://localhost:8501/oauth2callback")

    # Support custom server metadata URL (useful for non-Google providers)
    server_metadata_url = os.getenv(
        "AUTH_SERVER_METADATA_URL", "https://accounts.google.com/.well-known/openid-configuration"
    )

    auth_config = {
        "auth": {
            "redirect_uri": redirect_uri,
            "cookie_secret": cookie_secret,
            "client_id": client_id,
            "client_secret": client_secret,
            "server_metadata_url": server_metadata_url,
        }
    }

    try:
        from streamlit.runtime.secrets import secrets_singleton

        # Merge with existing secrets if any
        if secrets_singleton._secrets is None:
            secrets_singleton._secrets = {}

        secrets_singleton._secrets.update(auth_config)

        # Also set the environment variables for the secrets
        for key, value in auth_config.items():
            secrets_singleton._maybe_set_environment_variable(key, value)

    except Exception:
        # Log but don't crash - auth check will handle missing config
        print("Warning: Failed to load auth secrets from env", file=sys.stderr)


# Load auth config from environment at module import time
_load_auth_secrets_from_env()


def _get_image_base64(image_path: str) -> str:
    """Convert image to base64 for embedding in HTML."""
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode()


class ChatViewerAuth:
    """Handle authentication and authorization for chat viewer."""

    def __init__(self):
        """Initialize authentication with Streamlit OIDC and whitelist."""
        # Load whitelist using tolerant parsing (handles commas, semicolons, whitespace)
        allowed_emails_str = os.getenv("ALLOWED_VIEWER_EMAILS", "")
        self.allowed_emails = self._parse_email_whitelist(allowed_emails_str)

        # Staff organization ID for automatic access
        self.staff_org_id = int(os.getenv("STAFF_ORG_ID", "2"))

    @staticmethod
    def _parse_email_whitelist(whitelist_str: str) -> Set[str]:
        """
        Parse email whitelist string with tolerant parsing.

        Handles commas, semicolons, newlines, and extra whitespace.
        Returns lowercase emails for case-insensitive comparison.
        """
        if not whitelist_str:
            return set()
        emails = re.split(r"[,;\n]+", whitelist_str)
        return {email.strip().lower() for email in emails if email.strip()}

    def is_configured(self) -> bool:
        """Check if OAuth is properly configured via st.secrets."""
        try:
            # Check if auth config is available in st.secrets
            auth = st.secrets.get("auth", {})
            required_keys = ["client_id", "client_secret", "redirect_uri", "cookie_secret"]
            return all(auth.get(key) for key in required_keys)
        except Exception:
            return False

    def check_authentication(self) -> Optional[Dict[str, Any]]:
        """
        Check if user is authenticated and authorized.

        Returns:
            User info dict if authorized, None otherwise
        """
        if not self.is_configured():
            st.error(
                "⚠️ Google OAuth not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars."
            )
            return None

        # Check if user is logged in
        if not st.user.is_logged_in:
            return None

        # Get user info from st.user
        email = st.user.email

        if not email:
            st.error("❌ Could not retrieve email from Google account")
            return None

        # Check authorization
        if self.is_authorized(email):
            # Return user info dict
            return {
                "email": email,
                "name": st.user.name if hasattr(st.user, "name") else email.split("@")[0],
            }
        else:
            st.error(f"❌ Access Denied: {email} is not authorized to view this dashboard")
            st.info("Contact your administrator to request access.")
            if st.button("🚪 Logout"):
                st.logout()
            return None

    def is_authorized(self, email: str) -> bool:
        """
        Check if email is authorized to access viewer.

        Args:
            email: User's email address

        Returns:
            True if authorized, False otherwise
        """
        # Check explicit whitelist first (case-insensitive)
        if email.lower() in self.allowed_emails:
            return True

        # Check staff organization (requires auth DB query)
        # This will be implemented when we add auth_service
        # For now, rely on whitelist
        return False

    def get_user_info(self) -> Optional[Dict[str, Any]]:
        """Get currently logged-in user info."""
        if st.user.is_logged_in:
            return {
                "email": st.user.email,
                "name": st.user.name if hasattr(st.user, "name") else st.user.email.split("@")[0],
            }
        return None

    def logout_button(self):
        """Display a logout button in the sidebar."""
        if st.button("🚪 Logout", key="sidebar_logout"):
            st.logout()


def require_authentication() -> Dict[str, Any]:
    """
    Require authentication for page access.

    Call this at the top of each page that requires auth.

    Returns:
        User info dict if authenticated and authorized
    """
    # Create auth instance
    auth = ChatViewerAuth()

    if not auth.is_configured():
        st.error(
            "⚠️ Google OAuth not configured. Required: GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET env vars."
        )
        st.stop()

    user_info = auth.check_authentication()

    if not user_info:
        # Center-align all login page content
        st.markdown(
            """
            <style>
            .main .block-container {
                display: flex;
                flex-direction: column;
                justify-content: center;
                align-items: center;
                text-align: center;
                min-height: 80vh;
            }
            .main .block-container > div {
                display: flex;
                flex-direction: column;
                align-items: center;
                width: 100%;
            }
            .main .block-container > div > div {
                display: flex;
                flex-direction: column;
                align-items: center;
                width: 100%;
            }
            .main img {
                display: block;
                margin-left: auto;
                margin-right: auto;
            }
            .main h1 {
                text-align: center;
                width: 100%;
            }
            .main p {
                text-align: center;
                width: 100%;
            }
            .main [data-testid="stMarkdownContainer"] {
                text-align: center;
                width: 100%;
            }
            /* Horizontal layout for logo and title */
            .auth-header {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 20px;
                margin-bottom: 20px;
            }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Create horizontal layout with image on left and title on right
        anansi_path = Path(__file__).parent.parent / "assets" / "anansi_logo_nobg.png"
        if anansi_path.exists():
            st.markdown(
                f"""
                <div class="auth-header">
                    <img src="data:image/png;base64,{_get_image_base64(str(anansi_path))}" width="138" />
                    <h1 style='margin: 0;'>Anansi</h1>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown("<h1 style='text-align: center;'>Anansi</h1>", unsafe_allow_html=True)

        st.markdown(
            "<p style='text-align: center;'>Please sign in with your Google account to access Anansi.</p>",
            unsafe_allow_html=True,
        )

        # Show login button
        if st.button("🔐 Sign in with Google", type="primary", use_container_width=True):
            st.login()

        st.stop()

    # mypy: user_info is guaranteed to be Dict[str, Any] here due to st.stop() above
    return user_info  # type: ignore[return-value]
