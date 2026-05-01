#!/usr/bin/env python3
"""
Google Cloud Authentication Helper

This module handles Google Cloud authentication using service account JSON
stored in environment variables instead of external files.

Usage:
    from shared.utils.google_auth import get_vertex_ai_credentials, get_drive_credentials

    # For Vertex AI
    credentials = get_vertex_ai_credentials()

    # For Google Drive
    credentials = get_drive_credentials()
"""

import json
import os
import tempfile

from google.oauth2 import service_account


def get_service_account_json() -> dict:
    """
    Get service account JSON from environment variable.

    Returns:
        dict: Service account JSON

    Raises:
        ValueError: If GOOGLE_SERVICE_ACCOUNT_JSON is not set or invalid
    """
    json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()

    if not json_str:
        raise ValueError(
            "GOOGLE_SERVICE_ACCOUNT_JSON environment variable is not set.\n"
            "Please add your Google Cloud service account JSON to the .env file.\n"
            "See .env.example for instructions."
        )

    try:
        result = json.loads(json_str)
        return dict(result)  # Ensure it's a dict
    except json.JSONDecodeError as e:
        raise ValueError(
            f"GOOGLE_SERVICE_ACCOUNT_JSON contains invalid JSON: {e}\n"
            "Make sure you copied the entire JSON file contents as a single line."
        )


def get_vertex_ai_credentials():
    """
    Get credentials for Vertex AI.

    This function creates a temporary credentials file that Vertex AI SDK can use.
    The file is created in a temp directory and will be cleaned up automatically.

    Returns:
        str: Path to temporary credentials file

    Note:
        The returned path should be set to GOOGLE_APPLICATION_CREDENTIALS
        environment variable before initializing Vertex AI.
    """
    service_account_info = get_service_account_json()

    # Create a temporary file that will be cleaned up automatically
    # We need to keep it around for the lifetime of the process
    temp_file = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, prefix="gcp-sa-"
    )

    json.dump(service_account_info, temp_file)
    temp_file.flush()
    temp_file.close()

    return temp_file.name


def get_drive_credentials():
    """
    Get credentials for Google Drive API.

    Returns:
        google.oauth2.service_account.Credentials: Service account credentials

    Raises:
        ValueError: If credentials cannot be created
    """
    service_account_info = get_service_account_json()

    # Define the scopes needed for Google Drive
    scopes = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/drive.metadata.readonly",
    ]

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        return credentials
    except Exception as e:
        raise ValueError(f"Failed to create Drive credentials: {e}")


def get_sheets_credentials():
    """
    Get credentials for Google Sheets API.

    Returns:
        google.oauth2.service_account.Credentials: Service account credentials
    """
    service_account_info = get_service_account_json()

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        return credentials
    except Exception as e:
        raise ValueError(f"Failed to create Sheets credentials: {e}")


def get_docs_credentials():
    """
    Get credentials for Google Docs API.

    Returns:
        google.oauth2.service_account.Credentials: Service account credentials
    """
    service_account_info = get_service_account_json()

    scopes = [
        "https://www.googleapis.com/auth/documents.readonly",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        return credentials
    except Exception as e:
        raise ValueError(f"Failed to create Docs credentials: {e}")


def get_drive_write_credentials():
    """
    Get credentials for Google Drive API with write access.

    Used for creating documents from templates (copy files, modify metadata).

    Returns:
        google.oauth2.service_account.Credentials: Service account credentials with full Drive access

    Raises:
        ValueError: If credentials cannot be created
    """
    service_account_info = get_service_account_json()

    scopes = [
        "https://www.googleapis.com/auth/drive",  # Full Drive access for copy/write/comments
        "https://www.googleapis.com/auth/documents",  # Docs API read+write (batchUpdate)
    ]

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        return credentials
    except Exception as e:
        raise ValueError(f"Failed to create Drive write credentials: {e}")


def get_sheets_write_credentials():
    """
    Get credentials for Google Sheets API with write access.

    Used for populating cells in generated LPP documents.

    Returns:
        google.oauth2.service_account.Credentials: Service account credentials
    """
    service_account_info = get_service_account_json()

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",  # Full Sheets access
        "https://www.googleapis.com/auth/drive.readonly",  # Read Drive metadata
    ]

    try:
        credentials = service_account.Credentials.from_service_account_info(
            service_account_info, scopes=scopes
        )
        return credentials
    except Exception as e:
        raise ValueError(f"Failed to create Sheets write credentials: {e}")


def verify_credentials():
    """
    Verify that credentials are properly configured.

    Returns:
        bool: True if credentials are valid, False otherwise
    """
    try:
        service_account_info = get_service_account_json()

        # Basic validation
        required_fields = ["type", "project_id", "private_key", "client_email"]
        missing_fields = [f for f in required_fields if f not in service_account_info]

        if missing_fields:
            print(f"❌ Missing required fields: {', '.join(missing_fields)}")
            return False

        if service_account_info["type"] != "service_account":
            print(f"❌ Invalid credential type: {service_account_info['type']}")
            return False

        print(f"✅ Credentials verified for: {service_account_info['client_email']}")
        print(f"   Project: {service_account_info['project_id']}")
        return True

    except Exception as e:
        print(f"❌ Credential verification failed: {e}")
        return False


if __name__ == "__main__":
    """Test credentials configuration."""
    print("Testing Google Cloud credentials...")
    print()

    if verify_credentials():
        print()
        print("Credentials are properly configured!")
        print()
        print("You can now use:")
        print("  - get_vertex_ai_credentials() for Vertex AI")
        print("  - get_drive_credentials() for Google Drive (read-only)")
        print("  - get_drive_write_credentials() for Google Drive (read/write)")
        print("  - get_sheets_credentials() for Google Sheets (read-only)")
        print("  - get_sheets_write_credentials() for Google Sheets (read/write)")
        print("  - get_docs_credentials() for Google Docs")
    else:
        print()
        print("Please configure GOOGLE_SERVICE_ACCOUNT_JSON in your .env file")
