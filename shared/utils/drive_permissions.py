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
from dataclasses import dataclass

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


def _permission_grants(
    perm: dict,
    user_email: str,
    required_rank: int,
    need_write: bool,
) -> bool:
    """Whether one Drive permission entry grants this user the required role.

    Extracted so the permissions.list() and files.get() branches below cannot
    drift apart -- they previously carried two near-identical copies of this
    logic, and only one of them would have gained `domain` support.

    'group' entries deliberately never grant: expanding group membership needs
    the Admin SDK, which is not wired up. Direct shares are the workaround.
    """
    if ROLE_RANK.get(perm.get("role", ""), -1) < required_rank:
        return False

    if perm.get("emailAddress", "").lower() == user_email.lower():
        return True

    perm_type = perm.get("type")
    if perm_type == "anyone":
        return not need_write
    if perm_type == "domain":
        domain = (perm.get("domain") or "").lower()
        # endswith on "@" + domain, not a bare suffix check: "notexample.com"
        # must not satisfy a permission for "example.com".
        return bool(domain) and user_email.lower().endswith("@" + domain)
    return False


@dataclass(frozen=True)
class AccessCheck:
    """The outcome of an access check, with enough detail to explain a denial.

    ``user_can_access`` collapses this to a bool, which is right for its many
    existing callers (signing, ingestion, the knowledge MCP server, the gdoc
    context provider) -- they only ever branch on allowed/denied. Anywhere
    that shows the result to a human instead -- the Context admin dialog
    attaching a document, for one -- should call ``check_access`` directly,
    so a denial can say why rather than just no. ``reason`` is always safe to
    show a user; it never carries anything ``_permission_grants`` didn't
    already have to look at to make the decision.
    """

    allowed: bool
    reason: str


async def check_access(
    file_id: str,
    user_email: str | None,
    need_write: bool = False,
    strict: bool = False,
) -> AccessCheck:
    """Like ``user_can_access``, but with a human-readable reason either way.

    Every branch here existed in ``user_can_access`` already, as a bool plus
    a LOGGER call; this only attaches a reason string to each, without
    changing which branch is taken or what it decides. See that function's
    docstring for what ``strict``/``need_write`` mean.
    """
    if not user_email:
        LOGGER.warning(f"Drive access denied: no user email for file={file_id}")
        return AccessCheck(False, "You're not signed in, so access can't be checked.")

    required = "writer" if need_write else "reader"
    required_rank = ROLE_RANK[required]

    def _no_match_reason() -> str:
        # Reached only after at least one Drive call above succeeded (a
        # throw goes to the except block below instead), so the bot itself
        # could see something about this file -- the gap is specifically
        # that no permission entry names this user.
        if need_write:
            return (
                f"Write access is required, but no sharing entry grants "
                f"{user_email} writer (or owner) on this file."
            )
        return (
            f"The bot's Google account can see this file's sharing list, but no "
            f"entry names {user_email} directly. Sharing it with the bot alone "
            f"isn't enough — share it with {user_email} too. (Access only "
            f"through a Google Group isn't detected here; share directly with "
            f"your email instead.)"
        )

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
                    fields="permissions(emailAddress,role,type,domain)",
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
                if _permission_grants(perm, user_email, required_rank, need_write):
                    return AccessCheck(True, "Access confirmed via the file's sharing list.")

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
                    fields="id,permissions(emailAddress,role,type,domain)",
                    supportsAllDrives=True,
                )
                .execute()
            )
            for perm in meta.get("permissions", []):
                if _permission_grants(perm, user_email, required_rank, need_write):
                    return AccessCheck(True, "Access confirmed via the file's sharing list.")

            # Service account reached the file but sees no matching permission
            # → inherited/link-share access. Grant read; write still requires
            # an explicit direct share. strict=True withholds it: this branch
            # would otherwise grant every caller read on any link-shared or
            # Shared Drive file.
            if not need_write and meta.get("id"):
                if strict:
                    LOGGER.info(
                        f"Strict check withheld {file_id} from {user_email}: reachable "
                        f"by the service account but no permission entry matches"
                    )
                    return AccessCheck(False, _no_match_reason())
                LOGGER.info(
                    f"Drive fallback: granting read access to {user_email} for {file_id}"
                    " (service account can access file — link or inherited share)"
                )
                return AccessCheck(
                    True,
                    "The bot's Google account can open this file (link or inherited share).",
                )

        LOGGER.warning(f"Drive access denied: user={user_email} file={file_id} required={required}")
        return AccessCheck(False, _no_match_reason())

    except Exception as e:
        LOGGER.error(f"Permission check failed for file={file_id}: {e}")
        return AccessCheck(False, f"Couldn't check access to this file: {e}")  # Fail closed


async def user_can_access(
    file_id: str,
    user_email: str | None,
    need_write: bool = False,
    strict: bool = False,
) -> bool:
    """Check if a user has access to a Google Drive file.

    Returns False (fail-closed) if email is None. Logs denied access at
    WARNING level for audit.

    ``strict=True`` removes only the final "service account could reach the
    file, so allow read" fallback. Everything else -- explicit email match,
    'anyone' link sharing, domain-wide sharing, and the files.get() retry when
    permissions.list() is unavailable -- behaves identically. Callers that
    surface document *content* to an end user must pass strict=True: without
    it, any link-shared or Shared Drive file is readable by everyone.

    A thin bool-shaped wrapper around ``check_access`` (see there for the
    actual decision logic) -- kept as its own function so the several
    existing callers that only ever branch on true/false don't need to
    learn about ``AccessCheck``.
    """
    return (await check_access(file_id, user_email, need_write, strict)).allowed
