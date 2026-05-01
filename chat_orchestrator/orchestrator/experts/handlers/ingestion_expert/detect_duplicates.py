"""Detect duplicates step handler for Document Ingestion Expert.

This step checks for duplicate documents and chunks before storage:

1. Cross-source dedup: Check if content_hash already exists in a *different* document
   — catches same file uploaded from two different Drive URLs
2. Document-level: Check if source_id already exists
   - If revision_id unchanged → auto-skip (Drive-certified no change)
   - If content_hash identical → auto-skip (content unchanged despite different revision)
   - Otherwise → show Replace/Incorporate/Skip prompt with change percentage
3. Chunk-level: Hash each chunk's normalized content
   - Compare against existing chunk hashes
   - Mark duplicates for skipping during storage
"""

import asyncio
import difflib
import hashlib
import os
import re
from typing import Dict, Optional, Set

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


def normalize_content(text: str) -> str:
    """Normalize text for consistent hashing.

    - Lowercase
    - Collapse whitespace
    - Strip leading/trailing whitespace
    - Remove common formatting artifacts

    Args:
        text: Raw text content

    Returns:
        Normalized text for hashing
    """
    # Lowercase
    text = text.lower()

    # Replace all whitespace sequences with single space
    text = re.sub(r"\s+", " ", text)

    # Strip
    text = text.strip()

    return text


def hash_content(text: str) -> str:
    """Generate SHA256 hash of normalized content.

    Args:
        text: Text to hash (will be normalized first)

    Returns:
        Hex digest of SHA256 hash
    """
    normalized = normalize_content(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def check_version_status(
    existing_doc: Dict, new_content: str, new_revision_id: Optional[str]
) -> Dict:
    """Check whether the new content differs from the stored version.

    Returns a dict with:
      status: "unchanged" | "changed"
      change_pct: human-readable percentage e.g. "24%" (only when changed)
      new_hash: sha256 of new content (for storage)
    """
    stored_revision_id = (existing_doc.get("metadata") or {}).get("revision_id")
    stored_hash = existing_doc.get("content_hash")
    new_hash = hash_content(new_content)

    # Tier 1: Drive-certified no change (cheapest check)
    if new_revision_id and stored_revision_id and new_revision_id == stored_revision_id:
        return {"status": "unchanged", "new_hash": new_hash}

    # Tier 2: Content hash identical despite different revision (e.g. saved with no edits)
    if stored_hash and new_hash == stored_hash:
        return {"status": "unchanged", "new_hash": new_hash}

    # Measure change percentage using word-level diff for the user prompt
    old_content = existing_doc.get("content") or ""
    if old_content:
        sm = difflib.SequenceMatcher(None, old_content.split(), new_content.split())
        change_pct = f"{(1 - sm.ratio()) * 100:.0f}%"
    else:
        change_pct = "unknown"

    return {"status": "changed", "change_pct": change_pct, "new_hash": new_hash}


def get_document_by_content_hash_sync(
    content_hash: str,
    exclude_source_id: Optional[str] = None,
    organization_id: Optional[int] = None,
) -> Optional[Dict]:
    """Synchronous version of get_document_by_content_hash for use with asyncio.to_thread."""
    from supabase import create_client  # type: ignore[attr-defined]

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        return None

    supabase = create_client(url, key)

    try:
        query = (
            supabase.table("documents")
            .select("id, title, source_id, created_at")
            .eq("content_hash", content_hash)
        )
        if organization_id is not None:
            query = query.filter("metadata->>organization_id", "eq", str(organization_id))
        result = query.limit(2).execute()
        for row in result.data:
            if exclude_source_id and row.get("source_id") == exclude_source_id:
                continue
            return dict(row)
        return None
    except Exception as e:
        LOGGER.warning(f"Error checking content hash: {e}")
        return None


def _get_existing_document_sync(source_id: str) -> Optional[Dict]:
    """Synchronous helper for get_existing_document."""
    from supabase import create_client  # type: ignore[attr-defined]

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        return None

    supabase = create_client(url, key)
    result = (
        supabase.table("documents")
        .select("id, title, source_id, created_at, metadata, content_hash, content")
        .eq("source_id", source_id)
        .limit(1)
        .execute()
    )
    if result.data:
        return dict(result.data[0])
    return None


async def get_existing_document(source_id: str) -> Optional[Dict]:
    """Check if a document with this source_id already exists.

    Args:
        source_id: Google Drive file ID or other source identifier

    Returns:
        Document record if exists, None otherwise
    """
    try:
        return await asyncio.to_thread(_get_existing_document_sync, source_id)
    except Exception as e:
        LOGGER.warning(f"Error checking for existing document: {e}")
        return None


def _get_existing_chunk_hashes_sync(document_id: str) -> Set[str]:
    """Synchronous helper for get_existing_chunk_hashes."""
    from supabase import create_client  # type: ignore[attr-defined]

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        return set()

    supabase = create_client(url, key)
    result = supabase.table("chunks").select("content").eq("document_id", document_id).execute()
    hashes = set()
    for chunk in result.data:
        hashes.add(hash_content(chunk["content"]))
    return hashes


async def get_existing_chunk_hashes(document_id: str) -> Set[str]:
    """Get content hashes of all chunks for a document.

    Args:
        document_id: UUID of the document

    Returns:
        Set of content hashes
    """
    try:
        hashes = await asyncio.to_thread(_get_existing_chunk_hashes_sync, document_id)
        LOGGER.info(f"Found {len(hashes)} existing chunk hashes for document {document_id}")
        return hashes
    except Exception as e:
        LOGGER.warning(f"Error fetching existing chunks: {e}")
        return set()


def _delete_document_and_chunks_sync(document_id: str) -> bool:
    """Synchronous helper for delete_document_and_chunks."""
    from supabase import create_client  # type: ignore[attr-defined]

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        return False

    supabase = create_client(url, key)
    # Delete chunks first (FK → documents; entity_mentions/relationship_evidence CASCADE from chunks)
    supabase.table("chunks").delete().eq("document_id", document_id).execute()
    # Note: entities table has no document_id column — orphaned entities are acceptable
    supabase.table("documents").delete().eq("id", document_id).execute()
    return True


async def delete_document_and_chunks(document_id: str) -> bool:
    """Delete a document and all its associated chunks.

    Args:
        document_id: UUID of the document to delete

    Returns:
        True if successful
    """
    try:
        result = await asyncio.to_thread(_delete_document_and_chunks_sync, document_id)
        LOGGER.info(f"Deleted document {document_id} and its chunks")
        return result
    except Exception as e:
        LOGGER.error(f"Error deleting document: {e}")
        return False


@register_step("detect_duplicates")
async def detect_duplicates(context: StepContext) -> StepResult:
    """Detect duplicate documents and chunks before storage.

    Checks:
    1. If document with same source_id exists, prompt user for action
    2. If incorporating, compute chunk hashes to identify new vs existing content

    Args:
        context: Step execution context

    Returns:
        StepResult with deduplication info
    """
    source_id = context.get_state("source_id")
    # Check if we're handling user response
    awaiting_duplicate_decision = context.get_state("awaiting_duplicate_decision")
    existing_doc_id = context.get_state("existing_document_id")

    if awaiting_duplicate_decision and context.user_input:
        response = context.user_input.strip().lower()

        cancel_words = {"cancel", "skip", "abort", "quit", "exit", "stop", "no"}
        if response in cancel_words or response == "1":
            LOGGER.info("User chose to skip duplicate document")
            return StepResult(
                data={"duplicate_action": "skip"},
                state_updates={"awaiting_duplicate_decision": False},
                skip_remaining=True,
                progress_message="Ingestion cancelled - document already exists",
            )

        elif response in ["2", "replace"]:
            LOGGER.info(f"User chose to replace existing document {existing_doc_id}")
            await context.send_progress_to_user("Removing old document...")

            success = await delete_document_and_chunks(existing_doc_id)
            if not success:
                return StepResult.failure("Failed to remove old document. Please try again.")

            return StepResult(
                data={
                    "duplicate_action": "replace",
                    "deleted_document_id": existing_doc_id,
                },
                state_updates={
                    "awaiting_duplicate_decision": False,
                    "existing_document_id": None,
                    "duplicate_mode": "replace",
                },
                progress_message="Old document removed, proceeding with fresh ingestion",
            )

        elif response in ["3", "incorporate"]:
            duplicate_type = context.get_state("duplicate_type")
            if duplicate_type == "cross_source":
                # "Ingest anyway" for cross-source dedup = new independent document
                LOGGER.info("User chose to ingest cross-source duplicate as separate entry")
                return StepResult(
                    data={"duplicate_action": "ingest_anyway"},
                    state_updates={
                        "awaiting_duplicate_decision": False,
                        "duplicate_mode": "new",
                        "existing_document_id": None,
                    },
                    progress_message="Proceeding with ingestion as a separate document",
                )

            # For same-source duplicates: incorporate (add new chunks to existing doc)
            LOGGER.info(f"User chose to incorporate new content into {existing_doc_id}")
            await context.send_progress_to_user("Analyzing existing chunks...")

            return StepResult(
                data={
                    "duplicate_action": "incorporate",
                    "existing_document_id": existing_doc_id,
                },
                state_updates={
                    "awaiting_duplicate_decision": False,
                    "duplicate_mode": "incorporate",
                    "existing_document_id": existing_doc_id,
                },
                progress_message="Will incorporate new content into existing document",
            )

        else:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please choose an option:\n"
                    "1. **Skip** - Cancel ingestion\n"
                    "2. **Replace** - Delete old and ingest fresh\n"
                    "3. **Incorporate** - Keep old, add only new content"
                ),
            )

    # First run — check for duplicates
    cleaned_content = (
        context.get_state("cleaned_content") or context.get_state("document_content") or ""
    )
    new_revision_id = context.get_state("revision_id")

    # --- Cross-source dedup: same content, different URL ---
    if cleaned_content:
        new_hash = hash_content(cleaned_content)
        cross_source_match = await asyncio.to_thread(
            get_document_by_content_hash_sync, new_hash, source_id, context.organization_id
        )
        if cross_source_match:
            cs_title = cross_source_match.get("title", "Untitled")
            cs_date = (cross_source_match.get("created_at") or "")[:10]
            LOGGER.info(f"Cross-source duplicate found: {cs_title}")
            return StepResult(
                needs_user_input=True,
                state_updates={
                    "awaiting_duplicate_decision": True,
                    "existing_document_id": cross_source_match["id"],
                    "existing_document_title": cs_title,
                    "duplicate_type": "cross_source",
                },
                user_prompt=(
                    f"This content is identical to an existing document:\n"
                    f"*{cs_title}* (ingested {cs_date})\n\n"
                    "1. *Skip* - Don't ingest duplicate content\n"
                    "2. *Replace* - Replace the existing entry with this source\n"
                    "3. *Ingest anyway* - Store as a separate entry"
                ),
            )
    else:
        new_hash = None

    if not source_id:
        LOGGER.info("No source_id, skipping duplicate detection")
        return StepResult(
            data={"duplicate_action": "none", "is_new_document": True},
            state_updates={"duplicate_mode": "new"},
            progress_message="New document, no duplicates to check",
        )

    existing_doc = await get_existing_document(source_id)

    if existing_doc:
        existing_title = existing_doc.get("title", "Untitled")
        existing_id = existing_doc["id"]
        created_at = existing_doc.get("created_at", "unknown date")

        LOGGER.info(f"Found existing document: {existing_title} ({existing_id})")
        date_str = created_at[:10] if len(created_at) > 10 else created_at

        # Check if anything actually changed before prompting
        if cleaned_content:
            version_status = check_version_status(existing_doc, cleaned_content, new_revision_id)
            if version_status["status"] == "unchanged":
                return StepResult(
                    data={"duplicate_action": "skip", "reason": "unchanged"},
                    state_updates={"duplicate_mode": "skip"},
                    skip_remaining=True,
                    progress_message=f"*{existing_title}* is already up to date (last ingested {date_str}, no changes detected)",
                )
            change_pct = version_status.get("change_pct", "")
            change_note = (
                f"\n~{change_pct} of content has changed."
                if change_pct and change_pct != "unknown"
                else ""
            )
        else:
            change_note = ""
        return StepResult(
            state_updates={
                "awaiting_duplicate_decision": True,
                "existing_document_id": existing_id,
                "existing_document_title": existing_title,
            },
            needs_user_input=True,
            user_prompt=(
                f"*{existing_title}* already exists (ingested {date_str}).{change_note}\n\n"
                "What would you like to do?\n\n"
                "1. *Skip* - Cancel this ingestion\n"
                "2. *Replace* - Delete old document, ingest fresh\n"
                "3. *Incorporate* - Keep existing, add only new content"
            ),
        )

    # No existing document
    LOGGER.info(f"No existing document with source_id={source_id}")
    return StepResult(
        data={"duplicate_action": "none", "is_new_document": True},
        state_updates={"duplicate_mode": "new"},
        progress_message="No duplicates found, proceeding with ingestion",
    )
