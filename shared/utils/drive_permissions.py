"""Google Drive file permission checker.

Checks whether a user (by email) has read or write access to a specific
Google Drive file.

Strategy (in order):
1. permissions.list() — works when the service account is a direct permission
   holder on the file. Returns explicit shares including 'anyone' link-share.
2. files.get() fallback — used when permissions.list() returns empty (Shared
   Drive files don't enumerate inherited permissions) or throws 403 (service
   account has no direct share but the file may be "anyone with link"). If the
   service account can reach the file at all, read access is granted.

Known limitation: users with access via Google Groups will be incorrectly
denied. Workaround: share the file directly with the user's email.
Write access always requires an explicit direct share; the files.get()
fallback only grants read.
"""

import asyncio
import logging

from googleapiclient.discovery import build

from shared.utils.google_auth import get_drive_credentials

LOGGER = logging.getLogger(__name__)

ROLE_RANK = {
    "reader": 0,
    "commenter": 1,
    "writer": 2,
    "fileOrganizer": 3,
    "organizer": 4,
    "owner": 5,
}


async def user_can_access(
    file_id: str,
    user_email: str | None,
    need_write: bool = False,
) -> bool:
    """Check if a user has access to a Google Drive file.

    Returns False (fail-closed) if email is None. Checks exact email match
    and 'anyone' link sharing. Logs denied access at WARNING level for audit.

    Falls back to files.get() when permissions.list() fails or returns empty.
    If the service account can reach the file at all (link-share or Shared
    Drive inherited access), read access is granted.
    """
    if not user_email:
        LOGGER.warning(f"Drive access denied: no user email for file={file_id}")
        return False

    required = "writer" if need_write else "reader"
    required_rank = ROLE_RANK[required]

    try:
        creds = get_drive_credentials()
        service = build("drive", "v3", credentials=creds)

        # Try permissions.list() first. This fails with 403 when the service
        # account is not a direct permission holder (e.g. "anyone with link"
        # files where the SA has no explicit share). Treat that as "unknown"
        # rather than "denied" and fall through to the files.get() fallback.
        permissions: list | None = None
        try:
            resp = await asyncio.to_thread(
                lambda: service.permissions()
                .list(
                    fileId=file_id,
                    fields="permissions(emailAddress,role,type)",
                    supportsAllDrives=True,
                )
                .execute()
            )
            permissions = resp.get("permissions", [])
        except Exception as e:
            LOGGER.debug(
                f"permissions.list failed for {file_id} (likely no direct SA share): {e}"
                " — falling back to files.get"
            )

        if permissions:
            for perm in permissions:
                # Exact email match with sufficient role
                if (
                    perm.get("emailAddress", "").lower() == user_email.lower()
                    and ROLE_RANK.get(perm.get("role", ""), -1) >= required_rank
                ):
                    return True
                # 'Anyone with link' — allow for read only
                if perm.get("type") == "anyone" and not need_write:
                    return True

        # Fall back to files.get() when permissions.list() failed or returned
        # empty (Shared Drive files don't enumerate inherited permissions).
        # If the service account can fetch the file at all it means the file is
        # accessible (e.g. "anyone with link"). Treat that as read-access OK.
        if permissions is None or not permissions:
            LOGGER.debug(f"permissions.list empty/failed for {file_id} — trying files.get fallback")
            meta = await asyncio.to_thread(
                lambda: service.files()
                .get(
                    fileId=file_id,
                    fields="id,permissions(emailAddress,role,type)",
                    supportsAllDrives=True,
                )
                .execute()
            )
            for perm in meta.get("permissions", []):
                if (
                    perm.get("emailAddress", "").lower() == user_email.lower()
                    and ROLE_RANK.get(perm.get("role", ""), -1) >= required_rank
                ):
                    return True
                if perm.get("type") == "anyone" and not need_write:
                    return True

            # Service account reached the file but sees no explicit permissions
            # → inherited/link-share access. Grant read; write still requires
            # an explicit direct share.
            if not need_write and meta.get("id"):
                LOGGER.info(
                    f"Drive fallback: granting read access to {user_email} for {file_id}"
                    " (service account can access file — link or inherited share)"
                )
                return True

        LOGGER.warning(f"Drive access denied: user={user_email} file={file_id} required={required}")
        return False

    except Exception as e:
        LOGGER.error(f"Permission check failed for file={file_id}: {e}")
        return False  # Fail closed
