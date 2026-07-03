"""Record ID generation.

AppSheet used short opaque alphanumeric keys. We preserve existing IDs on import
and mint new ones here for app-created records. Hex tokens keep them collision-safe
and URL-friendly.
"""

from __future__ import annotations

import secrets


def new_id() -> str:
    return secrets.token_hex(8)  # 16 hex chars
