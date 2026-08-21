"""File-type-agnostic Google Drive comment handling.

The Drive `comments` API is the same for Docs and Sheets: `comments.list`
takes a file ID and knows nothing about the file's type. Only *locating*
the thing a comment points at, and *applying* an edit to it, differ --
those live in doc_editing.py and sheet_editing.py respectively.

Everything here was previously private to doc_editing.py and is unchanged
in behaviour; it moved so sheet_editing.py can reuse it rather than
reimplement the reply-thread folding and mention stripping.
"""

import asyncio
import functools
import logging
import re
from dataclasses import dataclass

from googleapiclient.discovery import build

from shared.utils.google_auth import get_drive_write_credentials

LOGGER = logging.getLogger(__name__)

# Partial match for the service account email in comments
BOT_MENTION = "@anansi-chatbot"


@dataclass(frozen=True)
class Annotation:
    """One unresolved @anansi-chatbot comment, independent of file type.

    `quoted_text` is Drive's `quotedFileContent` -- the highlighted run in a
    Doc, or the cell's content in a Sheet. Spike 0 established it is served
    as `text/html`, so callers matching it against live file content must
    HTML-unescape first. It is `""` when the commented range had no content,
    which for a Sheet means an empty cell and makes the comment unlocatable.
    """

    comment_id: str
    quoted_text: str
    instruction: str
    author_email: str
    created_time: str


@functools.lru_cache(maxsize=1)
def _get_drive_service():
    """Cached Drive v3 service (write credentials). Built once per process."""
    creds = get_drive_write_credentials()
    return build("drive", "v3", credentials=creds)


def strip_bot_mention(text: str) -> str:
    """Remove @anansibot mentions from comment text.

    A single regex pass, not a plain-string .replace() first -- the service
    account's own address (@anansi-chatbot.iam.gserviceaccount.com) has
    "@anansi-chatbot" as a strict prefix, so replacing that substring first
    would consume it and leave the ".iam.gserviceaccount.com" suffix behind
    for the regex to find nothing left to match.
    """
    return re.sub(r"@?anansi-chatbot[-\w.@]*", "", text, flags=re.IGNORECASE).strip()


def build_thread_instruction(comment: dict, initial_author: str) -> str:
    """Build a single instruction string from a comment and its reply thread.

    If all messages are from the same author, concatenates plainly.
    If multiple authors, prefixes each reply with the author's name.
    """
    initial_text = strip_bot_mention(comment.get("content", ""))
    replies = comment.get("replies", [])

    if not replies:
        return initial_text

    initial_email = comment.get("author", {}).get("emailAddress", "")
    all_same_author = all(
        r.get("author", {}).get("emailAddress", "") == initial_email for r in replies
    )

    if all_same_author:
        parts = [initial_text]
        for r in replies:
            reply_text = strip_bot_mention(r.get("content", ""))
            if reply_text:
                parts.append(reply_text)
        return "\n".join(parts)

    parts = [f"[{initial_author or 'Author'}]: {initial_text}"]
    for r in replies:
        reply_text = strip_bot_mention(r.get("content", ""))
        if reply_text:
            reply_author = r.get("author", {}).get("displayName", "Someone")
            parts.append(f"[{reply_author}]: {reply_text}")
    return "\n".join(parts)


async def scan_annotations(file_id: str) -> list[Annotation]:
    """Scan any Drive file for pending @anansibot comments.

    Works identically for Docs and Sheets -- `comments.list` is keyed on the
    file, so for a spreadsheet this returns comments across *every* tab in
    one call. There is no per-tab scan and none is needed.
    """
    drive_service = _get_drive_service()

    resp = await asyncio.to_thread(
        lambda: drive_service.comments()
        .list(
            fileId=file_id,
            fields="comments(id,content,resolved,quotedFileContent,createdTime,"
            "author(emailAddress,displayName),"
            "replies(content,author(emailAddress,displayName)))",
            includeDeleted=False,
        )
        .execute()
    )

    pending = [
        c
        for c in resp.get("comments", [])
        if not c.get("resolved") and BOT_MENTION in (c.get("content", "").lower())
    ]

    results = []
    for c in pending:
        results.append(
            Annotation(
                comment_id=c["id"],
                quoted_text=(c.get("quotedFileContent") or {}).get("value", ""),
                instruction=build_thread_instruction(
                    c, c.get("author", {}).get("displayName", "")
                ),
                author_email=c.get("author", {}).get("emailAddress", ""),
                created_time=c.get("createdTime", ""),
            )
        )
    return results


async def reply_and_resolve(file_id: str, comment_id: str, message: str) -> bool:
    """Reply to a comment and resolve it. Returns False on failure.

    Callers MUST NOT treat a write as complete when this returns False --
    an unresolved thread is the only signal a human has that the bot did
    not finish, and edit_section already enforces this ordering for Docs.
    """
    try:
        drive_service = _get_drive_service()
        await asyncio.to_thread(
            lambda: drive_service.replies()
            .create(
                fileId=file_id,
                commentId=comment_id,
                fields="id",
                body={"action": "resolve", "content": message[:200]},
            )
            .execute()
        )
        return True
    except Exception as e:
        LOGGER.warning(f"Could not resolve comment {comment_id} on {file_id}: {e}")
        return False


async def reply_without_resolving(file_id: str, comment_id: str, message: str) -> bool:
    """Reply to a comment but leave the thread open.

    Used for the failure paths -- stale quote, ambiguous match, no catalogue
    match. The human needs the explanation *and* needs the thread to stay
    open so they can see something is outstanding.
    """
    try:
        drive_service = _get_drive_service()
        await asyncio.to_thread(
            lambda: drive_service.replies()
            .create(
                fileId=file_id,
                commentId=comment_id,
                fields="id",
                body={"content": message[:200]},
            )
            .execute()
        )
        return True
    except Exception as e:
        LOGGER.warning(f"Could not reply to comment {comment_id} on {file_id}: {e}")
        return False


async def pin_revision(file_id: str) -> bool:
    """Pin the current revision before editing for rollback safety.

    Non-fatal: returns False rather than raising, because losing rollback
    safety is not a reason to refuse an edit the user asked for.
    """
    try:
        service = _get_drive_service()
        await asyncio.to_thread(
            lambda: service.revisions()
            .update(fileId=file_id, revisionId="head", body={"keepForever": True})
            .execute()
        )
        LOGGER.info(f"Pinned pre-edit revision for {file_id}")
        return True
    except Exception as e:
        LOGGER.warning(f"Could not pin revision for {file_id}: {e}")
        return False


async def get_file_mime_type(file_id: str) -> str:
    """Drive mimeType, used to dispatch between the Doc and Sheet paths."""
    service = _get_drive_service()
    meta = await asyncio.to_thread(
        lambda: service.files()
        .get(fileId=file_id, fields="mimeType", supportsAllDrives=True)
        .execute()
    )
    return str(meta.get("mimeType", ""))


MIME_DOC = "application/vnd.google-apps.document"
MIME_SHEET = "application/vnd.google-apps.spreadsheet"
