"""Email utilities for parsing and validating email whitelists.

This module provides tolerant parsing of email lists that handles:
- Multiple separators (commas, semicolons, newlines)
- Extra whitespace
- Case-insensitive comparison
"""

import re
from typing import Set


def parse_email_whitelist(whitelist_str: str) -> Set[str]:
    """
    Parse an email whitelist string into a set of normalized emails.

    Handles multiple formats tolerantly:
    - Comma-separated: "a@x.co, b@x.co"
    - Semicolon-separated: "a@x.co; b@x.co"
    - Mixed separators: "a@x.co, b@x.co; c@x.co"
    - Extra whitespace: "  a@x.co ,  b@x.co  "
    - Newline-separated: "a@x.co\nb@x.co"

    All emails are normalized to lowercase for case-insensitive comparison.

    Args:
        whitelist_str: Raw whitelist string from environment variable

    Returns:
        Set of lowercase email addresses
    """
    if not whitelist_str:
        return set()

    # Split on commas, semicolons, or newlines
    emails = re.split(r"[,;\n]+", whitelist_str)

    # Normalize: strip whitespace, lowercase, filter empty
    return {email.strip().lower() for email in emails if email.strip()}


def is_email_in_whitelist(email: str, whitelist: Set[str]) -> bool:
    """
    Check if an email is in the whitelist (case-insensitive).

    Args:
        email: Email address to check
        whitelist: Set of allowed emails (should be lowercase from parse_email_whitelist)

    Returns:
        True if email is in whitelist
    """
    if not email:
        return False
    return email.lower() in whitelist


__all__ = ["parse_email_whitelist", "is_email_in_whitelist"]
