"""Embed and store step handler for Document Ingestion Expert.

This is the final step that:
1. Handles user approval/rejection/reclassification responses
2. Chunks the document using paragraph-aware chunking
3. For support_example docs: Performs per-chunk procedure matching with user prompts
4. Embeds chunks using VectorEmbedder with appropriate task type
5. Stores to Supabase pgvector database (documents, chunks, entities tables)
6. Handles deduplication when in "incorporate" mode
"""

import asyncio
import os
import uuid
from typing import Any, Dict, List, Set, Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Maps document type to embedding task type. Module-level to avoid rebuilding per call.
TASK_TYPE_MAP = {
    "support_example": "QUESTION_ANSWERING",
    "faq": "QUESTION_ANSWERING",
    "sop": "RETRIEVAL_DOCUMENT",
    "technical": "RETRIEVAL_DOCUMENT",
    "policy": "RETRIEVAL_DOCUMENT",
}


class SimpleChunk:
    """Simple chunk dataclass for embedding."""

    def __init__(self, content: str, start_char: int = 0, end_char: int = 0):
        self.content = content
        self.start_char = start_char
        self.end_char = end_char


def _split_into_sections(content: str) -> List[str]:
    """Split content into sections, keeping table blocks together.

    Consecutive lines starting with '|' are grouped into a single section
    so they don't get split across chunks.

    Args:
        content: Document content

    Returns:
        List of sections (paragraphs or table blocks)
    """
    paragraphs = content.split("\n\n")
    sections: List[str] = []
    table_buffer: List[str] = []

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        # Check if this paragraph is a table block (all lines start with |)
        lines = para.split("\n")
        is_table = all(line.strip().startswith("|") for line in lines if line.strip())

        if is_table:
            table_buffer.append(para)
        else:
            if table_buffer:
                # Flush accumulated table as one section
                sections.append("\n\n".join(table_buffer))
                table_buffer = []
            sections.append(para)

    if table_buffer:
        sections.append("\n\n".join(table_buffer))

    return sections


def _split_table_at_rows(table_text: str, chunk_size: int) -> List[str]:
    """Split a large pipe-format table into chunks at row boundaries.

    Each chunk includes the header row and separator so it's self-contained.

    Args:
        table_text: Full table text (pipe-delimited rows)
        chunk_size: Target chunk size in characters

    Returns:
        List of self-contained table chunk strings
    """
    lines = table_text.split("\n")
    if len(lines) < 3:
        return [table_text]

    # Extract header and separator (first two non-empty lines)
    header_lines: List[str] = []
    data_lines: List[str] = []
    for line in lines:
        if not line.strip():
            continue
        if len(header_lines) < 2:
            header_lines.append(line)
        else:
            data_lines.append(line)

    if not data_lines:
        return [table_text]

    header_text = "\n".join(header_lines)
    header_size = len(header_text) + 1  # +1 for newline before data

    chunks: List[str] = []
    current_rows: List[str] = []
    current_size = header_size

    for row in data_lines:
        row_size = len(row) + 1  # +1 for newline

        if current_size + row_size > chunk_size and current_rows:
            chunks.append(header_text + "\n" + "\n".join(current_rows))
            current_rows = []
            current_size = header_size

        current_rows.append(row)
        current_size += row_size

    if current_rows:
        chunks.append(header_text + "\n" + "\n".join(current_rows))

    return chunks


def chunk_content(content: str, chunk_size: int = 1000, overlap: int = 200) -> List[SimpleChunk]:
    """Chunk content into overlapping segments.

    Table-aware, paragraph-based chunking that preserves semantic boundaries.
    Consecutive table rows are kept together. If a pipe-format table exceeds
    chunk_size, it is split at row boundaries with the header repeated.

    Args:
        content: Document content
        chunk_size: Target chunk size in characters
        overlap: Overlap between chunks

    Returns:
        List of SimpleChunk objects
    """
    if not content:
        return []

    sections = _split_into_sections(content)
    chunks: List[SimpleChunk] = []
    current_chunk: List[str] = []
    current_size = 0
    start_char = 0

    for section in sections:
        section_size = len(section)

        # If a single table section exceeds chunk_size, split it at row boundaries
        if section_size > chunk_size and section.strip().startswith("|"):
            # Flush current chunk first
            if current_chunk:
                chunk_text = "\n\n".join(current_chunk)
                chunks.append(
                    SimpleChunk(
                        content=chunk_text,
                        start_char=start_char,
                        end_char=start_char + len(chunk_text),
                    )
                )
                start_char += len(chunk_text) - overlap
                current_chunk = []
                current_size = 0

            # Split the table and add each part as its own chunk
            table_parts = _split_table_at_rows(section, chunk_size)
            for part in table_parts:
                chunks.append(
                    SimpleChunk(
                        content=part,
                        start_char=start_char,
                        end_char=start_char + len(part),
                    )
                )
                start_char += len(part) - overlap
            continue

        if current_size + section_size > chunk_size and current_chunk:
            # Save current chunk
            chunk_text = "\n\n".join(current_chunk)
            chunks.append(
                SimpleChunk(
                    content=chunk_text,
                    start_char=start_char,
                    end_char=start_char + len(chunk_text),
                )
            )
            start_char += len(chunk_text) - overlap
            current_chunk = []
            current_size = 0

        current_chunk.append(section)
        current_size += section_size + 2  # +2 for \n\n

    # Add final chunk
    if current_chunk:
        chunk_text = "\n\n".join(current_chunk)
        chunks.append(
            SimpleChunk(
                content=chunk_text,
                start_char=start_char,
                end_char=start_char + len(chunk_text),
            )
        )

    return chunks


async def embed_chunks(
    chunks: List[SimpleChunk], doc_type: str = "general"
) -> List[Tuple[SimpleChunk, List[float]]]:
    """Embed chunks using Google's text-embedding-005 via Vertex AI.

    Args:
        chunks: List of SimpleChunk objects
        doc_type: Document type for task type selection

    Returns:
        List of (chunk, embedding) tuples
    """
    from shared.utils.vertex_embeddings import get_embeddings

    task_type = TASK_TYPE_MAP.get(doc_type, "RETRIEVAL_DOCUMENT")

    results = []
    batch_size = 100

    for i in range(0, len(chunks), batch_size):
        batch = chunks[i : i + batch_size]
        texts = [c.content for c in batch]

        try:
            embeddings = await get_embeddings(texts, task_type=task_type)

            for j, chunk in enumerate(batch):
                results.append((chunk, embeddings[j]))

        except Exception as e:
            LOGGER.error(f"Batch embedding failed: {e}")
            # Skip failed batch
            continue

    return results


async def store_to_database(
    content: str,
    embedded_chunks: List[Tuple[SimpleChunk, List[float]]],
    entities: List[Dict],
    relationships: List[Dict],
    metadata: Dict[str, Any],
    doc_type: str,
    duplicate_mode: str = "new",
    existing_document_id: str | None = None,
    existing_chunk_hashes: Set[str] | None = None,
    chunk_procedure_map: Dict[int, List[str]] | None = None,
) -> Dict[str, Any]:
    """Store document, chunks, entities, mentions, relationships, and evidence to Supabase.

    Args:
        content: Full document content
        embedded_chunks: List of (chunk, embedding) tuples
        entities: List of entity dicts
        relationships: List of relationship dicts with source, target, type, description
        metadata: Document metadata
        doc_type: Document type
        duplicate_mode: "new", "replace", or "incorporate"
        existing_document_id: Document ID to add chunks to (for incorporate mode)
        existing_chunk_hashes: Set of content hashes of existing chunks
        chunk_procedure_map: Optional mapping of chunk index to procedure_ids for
            per-chunk procedure assignments (used for support_example docs)

    Returns:
        Dict with stored document ID and counts
    """
    from supabase import create_client  # type: ignore[attr-defined]

    from orchestrator.experts.handlers.ingestion_expert.detect_duplicates import hash_content

    url = os.getenv("CHAT_DB_URL")
    key = os.getenv("CHAT_DB_SERVICE_KEY")

    if not url or not key:
        raise ValueError("CHAT_DB_URL and CHAT_DB_SERVICE_KEY must be set")

    supabase = create_client(url, key)

    # Use existing document ID for incorporate mode, otherwise generate new
    if duplicate_mode == "incorporate" and existing_document_id:
        doc_id = existing_document_id
        LOGGER.info(f"Incorporating into existing document: {doc_id}")
    else:
        doc_id = str(uuid.uuid4())

    task_type = TASK_TYPE_MAP.get(doc_type, "RETRIEVAL_DOCUMENT")

    # Insert document (skip for incorporate mode - document already exists)
    if duplicate_mode != "incorporate":
        # Store content hash for change detection (full content is in chunks)
        content_hash = hash_content(content)

        doc_metadata = {
            "doc_type": doc_type,
            "audience": metadata.get("audience", "staff"),
            "classification_confidence": metadata.get("classification_confidence", 0.5),
            "content_hash": content_hash,  # For detecting content changes
            "content_length": len(content),  # Original length for reference
        }
        # Store org_id for cross-tenant filtering on future cross-source dedup checks
        if metadata.get("organization_id") is not None:
            doc_metadata["organization_id"] = metadata["organization_id"]
        # Include source_url if available
        if metadata.get("source_url"):
            doc_metadata["source_url"] = metadata["source_url"]
        # Include version tracking for Google Docs change detection
        if metadata.get("revision_id"):
            doc_metadata["revision_id"] = metadata["revision_id"]
        if metadata.get("modified_time"):
            doc_metadata["modified_time"] = metadata["modified_time"]

        # Store truncated preview in raw_content (schema requires it)
        # Full searchable content is stored in chunks table
        content_preview = content[:500] + "..." if len(content) > 500 else content

        doc_data = {
            "id": doc_id,
            "source_type": metadata.get("source_type", "manual_upload"),
            "source_id": metadata.get("source_id", ""),
            "content_type": metadata.get("file_type", "text"),  # google_doc, pdf, text
            "title": metadata.get("title", "Untitled"),
            "raw_content": content_preview,  # Preview only - full content in chunks
            "content": content,  # Full text for version diff on re-ingestion
            "content_hash": content_hash,  # Top-level column for indexed cross-source dedup
            "metadata": doc_metadata,
        }

        try:
            await asyncio.to_thread(lambda: supabase.table("documents").insert(doc_data).execute())
            LOGGER.info(f"Inserted document {doc_id}")
        except Exception as e:
            LOGGER.error(f"Failed to insert document: {e}")
            raise
    else:
        # Incorporate mode: only update version metadata (revision_id/modified_time).
        # Do NOT overwrite content/content_hash — the canonical content is the original
        # ingestion's, and overwriting it would break change detection on future re-ingests.
        # Note: content baseline is the original ingestion; diff % on 3rd+ ingestion is measured
        # against that original, not the most-recently-incorporated version. This is intentional.
        LOGGER.info(f"Skipping document insert (incorporate mode) - using existing {doc_id}")
        try:
            incorporate_meta_update = {}
            if metadata.get("revision_id"):
                incorporate_meta_update["revision_id"] = metadata["revision_id"]
            if metadata.get("modified_time"):
                incorporate_meta_update["modified_time"] = metadata["modified_time"]
            if incorporate_meta_update:
                # Merge metadata fields without overwriting existing keys
                existing_meta_result = await asyncio.to_thread(
                    lambda: supabase.table("documents")
                    .select("metadata")
                    .eq("id", doc_id)
                    .single()
                    .execute()
                )
                existing_meta = (existing_meta_result.data or {}).get("metadata") or {}
                await asyncio.to_thread(
                    lambda: supabase.table("documents")
                    .update({"metadata": {**existing_meta, **incorporate_meta_update}})
                    .eq("id", doc_id)
                    .execute()
                )
                LOGGER.info(f"Updated version metadata for incorporated document {doc_id}")
        except Exception as e:
            LOGGER.warning(f"Failed to update incorporate metadata: {e}")

    # Insert chunks (with deduplication for incorporate mode)
    chunks_skipped = 0
    existing_hashes = existing_chunk_hashes or set()
    chunk_id_map: Dict[int, str] = {}  # chunk_index -> chunk UUID for entity_mentions
    all_chunk_data: List[Dict] = []

    for i, (chunk, embedding) in enumerate(embedded_chunks):
        # Check for duplicate in incorporate mode
        if duplicate_mode == "incorporate" and existing_hashes:
            chunk_hash = hash_content(chunk.content)
            if chunk_hash in existing_hashes:
                LOGGER.debug(f"Skipping duplicate chunk {i}: {chunk.content[:50]}...")
                chunks_skipped += 1
                continue

        # Determine procedure_ids for this chunk:
        # 1. Use chunk_procedure_map if provided (per-chunk matching for support_example)
        # 2. Fall back to document-level procedure_ids from metadata
        if chunk_procedure_map is not None and i in chunk_procedure_map:
            chunk_procedure_ids = chunk_procedure_map[i]
        else:
            chunk_procedure_ids = metadata.get("procedure_ids", [])

        chunk_uuid = str(uuid.uuid4())
        chunk_id_map[i] = chunk_uuid
        all_chunk_data.append(
            {
                "id": chunk_uuid,
                "document_id": doc_id,
                "chunk_index": i,
                "content": chunk.content,
                "embedding": embedding,
                "embedding_task_type": task_type,
                "chunk_metadata": {
                    "position": {"start": chunk.start_char, "end": chunk.end_char},
                    "allowed_role_ids": metadata.get("allowed_role_ids", []),
                    "allowed_org_ids": metadata.get("allowed_org_ids", []),
                    "procedure_ids": chunk_procedure_ids,
                },
            }
        )

    if chunks_skipped > 0:
        LOGGER.info(f"Deduplication: {chunks_skipped} duplicate chunks skipped")

    chunks_inserted = 0
    if all_chunk_data:
        try:
            insert_result = await asyncio.to_thread(
                lambda: supabase.table("chunks").insert(all_chunk_data).execute()
            )
            chunks_inserted = len(insert_result.data or [])
            if chunks_inserted < len(all_chunk_data):
                LOGGER.warning(
                    f"Chunk insert partial: expected {len(all_chunk_data)}, got {chunks_inserted}"
                )
        except Exception as e:
            LOGGER.error(f"Failed to batch insert chunks: {e}")
            raise

    # If no chunks were inserted and this is a new document, clean up
    if chunks_inserted == 0 and duplicate_mode != "incorporate":
        LOGGER.error(f"No chunks inserted for document {doc_id}, cleaning up")
        from orchestrator.experts.handlers.ingestion_expert.detect_duplicates import (
            delete_document_and_chunks,
        )

        await delete_document_and_chunks(doc_id)
        raise ValueError("Failed to insert any chunks - document cleaned up")

    # Insert entities (batch embed, upsert to deduplicate across documents)
    from shared.utils.vertex_embeddings import get_embeddings

    entities_inserted = 0
    entity_id_map: Dict[str, str] = {}  # entity name -> entity UUID

    if entities:
        # Batch embed all entities in one API call
        entity_texts = [f"{e['name']}: {e.get('description', e['type'])}" for e in entities]
        try:
            entity_embeddings = await get_embeddings(entity_texts, task_type="RETRIEVAL_DOCUMENT")
        except Exception as e:
            LOGGER.error(f"Batch entity embedding failed: {e}")
            entity_embeddings = []

        for idx, entity in enumerate(entities):
            if idx >= len(entity_embeddings):
                LOGGER.warning(f"No embedding for entity: {entity.get('name')}")
                continue

            try:
                entity_uuid = str(uuid.uuid4())
                entity_data = {
                    "id": entity_uuid,
                    "name": entity["name"],
                    "type": entity["type"],
                    "description": entity.get("description", ""),
                    "embedding": entity_embeddings[idx],
                    "embedding_task_type": "RETRIEVAL_DOCUMENT",
                    "metadata": {"source_documents": [doc_id]},
                }

                # Upsert: if entity with same name+type exists, update it
                _entity_data = entity_data  # capture for lambda
                result = await asyncio.to_thread(
                    lambda: supabase.table("entities")
                    .upsert(_entity_data, on_conflict="name,type")
                    .execute()
                )

                # Capture the actual entity ID (may differ from entity_uuid if existing)
                if result.data and len(result.data) > 0:
                    actual_id = result.data[0].get("id", entity_uuid)
                    entity_id_map[entity["name"]] = actual_id
                else:
                    entity_id_map[entity["name"]] = entity_uuid

                entities_inserted += 1

            except Exception as e:
                LOGGER.error(f"Failed to upsert entity {entity.get('name')}: {e}")
                continue

    # Insert entity_mentions (link entities to chunks and documents)
    mentions_inserted = 0
    first_chunk_id = next(iter(chunk_id_map.values()), None) if chunk_id_map else None

    # Batch insert entity_mentions
    if entity_id_map and first_chunk_id:
        all_mention_data = []
        for entity in entities:
            ent_name = entity["name"]
            ent_id = entity_id_map.get(ent_name)
            if not ent_id:
                continue
            all_mention_data.append(
                {
                    "id": str(uuid.uuid4()),
                    "entity_id": ent_id,
                    "chunk_id": first_chunk_id,
                    "document_id": doc_id,
                    "mention_text": ent_name,
                    "context": entity.get("description", "")[:500],
                    "confidence": 1.0,
                }
            )
        if all_mention_data:
            try:
                _mention_batch = all_mention_data
                mention_result = await asyncio.to_thread(
                    lambda: supabase.table("entity_mentions").insert(_mention_batch).execute()
                )
                mentions_inserted = len(mention_result.data or [])
            except Exception as e:
                LOGGER.error(f"Failed to batch insert entity mentions: {e}")

    # Insert relationships (per-item: needs entity_id_map lookup + captures returned ID)
    relationships_inserted = 0
    relationship_id_map: Dict[str, str] = {}  # "source->target" -> relationship UUID

    for rel in relationships:
        source_id = entity_id_map.get(rel.get("source", ""))
        target_id = entity_id_map.get(rel.get("target", ""))

        if not source_id or not target_id:
            LOGGER.debug(
                f"Skipping relationship {rel.get('source')} -> {rel.get('target')}: "
                "entity not found"
            )
            continue

        try:
            rel_uuid = str(uuid.uuid4())
            _rel_data = {
                "id": rel_uuid,
                "source_entity_id": source_id,
                "target_entity_id": target_id,
                "relationship_type": rel.get("type", "related_to"),
                "description": rel.get("description", ""),
                "strength": 1.0,
                "metadata": {"source_documents": [doc_id]},
            }

            await asyncio.to_thread(
                lambda: supabase.table("relationships").insert(_rel_data).execute()
            )
            rel_key = f"{rel.get('source')}->{rel.get('target')}"
            relationship_id_map[rel_key] = rel_uuid
            relationships_inserted += 1

        except Exception as e:
            LOGGER.error(
                f"Failed to insert relationship {rel.get('source')} -> {rel.get('target')}: {e}"
            )
            continue

    # Batch insert relationship_evidence
    evidence_inserted = 0

    if relationship_id_map and first_chunk_id:
        all_evidence_data = []
        for rel in relationships:
            rel_key = f"{rel.get('source')}->{rel.get('target')}"
            rel_id = relationship_id_map.get(rel_key)
            if not rel_id:
                continue
            all_evidence_data.append(
                {
                    "id": str(uuid.uuid4()),
                    "relationship_id": rel_id,
                    "chunk_id": first_chunk_id,
                    "document_id": doc_id,
                    "evidence_text": rel.get("description", "")[:500],
                    "confidence": 1.0,
                }
            )
        if all_evidence_data:
            try:
                _evidence_batch = all_evidence_data
                evidence_result = await asyncio.to_thread(
                    lambda: supabase.table("relationship_evidence")
                    .insert(_evidence_batch)
                    .execute()
                )
                evidence_inserted = len(evidence_result.data or [])
            except Exception as e:
                LOGGER.error(f"Failed to batch insert relationship evidence: {e}")

    return {
        "document_id": doc_id,
        "chunks_inserted": chunks_inserted,
        "chunks_skipped": chunks_skipped,
        "entities_inserted": entities_inserted,
        "mentions_inserted": mentions_inserted,
        "relationships_inserted": relationships_inserted,
        "evidence_inserted": evidence_inserted,
        "duplicate_mode": duplicate_mode,
    }


# Available document types for reclassification
DOC_TYPE_OPTIONS = {
    "1": ("sop", "Standard Operating Procedure"),
    "2": ("faq", "FAQ / Q&A"),
    "3": ("support_example", "Support Conversation (available in customer mode)"),
    "4": ("technical", "Technical Documentation"),
    "5": ("policy", "Policy / Guidelines"),
}


def _format_chunk_preview(chunk: SimpleChunk, max_length: int = 150) -> str:
    """Format a chunk content preview for display.

    Args:
        chunk: The chunk to preview
        max_length: Maximum characters to show

    Returns:
        Truncated content with ellipsis if needed
    """
    content = chunk.content.strip().replace("\n", " ")
    if len(content) > max_length:
        return content[:max_length] + "..."
    return content


def _format_procedure_list_for_chunk(procedures: List) -> str:
    """Format procedure list for chunk matching prompt.

    Args:
        procedures: List of Procedure objects

    Returns:
        Formatted numbered list string
    """
    lines = []
    for p in procedures:
        lines.append(f"  **{p.number}.** {p.title}")
    return "\n".join(lines)


def _is_intro_chunk(chunk: SimpleChunk, chunk_index: int) -> bool:
    """Detect if a chunk is an introduction/header section that should be auto-skipped.

    Intro chunks typically:
    - Start with '# Introduction' or similar header
    - Contain meta-content describing the document purpose
    - Are the first chunk and don't contain actual conversation content

    Args:
        chunk: The chunk to check
        chunk_index: Position of chunk in the list (0-based)

    Returns:
        True if this appears to be an intro/meta chunk that should be skipped
    """
    content_lower = chunk.content.lower().strip()
    content_start = content_lower[:500]  # Check first 500 chars

    # Patterns that indicate intro/meta content
    intro_patterns = [
        "# introduction",
        "## introduction",
        "this document contains",
        "this document provides",
        "this document describes",
        "the following examples",
        "examples of good",
        "examples of 'good'",
        "table of contents",
    ]

    # Check if content matches intro patterns
    for pattern in intro_patterns:
        if pattern in content_start:
            LOGGER.info(f"Chunk {chunk_index} detected as intro (pattern: '{pattern}')")
            return True

    # First chunk that's very short and looks like a header section
    if chunk_index == 0 and len(chunk.content) < 300:
        # Check if it's mostly headers (lines starting with #)
        lines = chunk.content.strip().split("\n")
        header_lines = sum(1 for line in lines if line.strip().startswith("#"))
        if header_lines > 0 and header_lines >= len(lines) / 2:
            LOGGER.info(f"Chunk {chunk_index} detected as header-only intro")
            return True

    return False


@register_step("embed_and_store")
async def embed_and_store(context: StepContext) -> StepResult:
    """Handle approval and store document to vector database.

    This step:
    1. Checks for user approval response
    2. On approve: chunks, embeds, and stores the document
    3. On reject: cancels the ingestion
    4. On modify: prompts user to update source and restart
    5. On reclassify: allows user to change document type

    Args:
        context: Step execution context

    Returns:
        StepResult with storage results or approval prompts
    """
    # Check if resuming after user input
    awaiting_approval = context.get_state("awaiting_approval")
    awaiting_reclassification = context.get_state("awaiting_reclassification")
    awaiting_access_selection = context.get_state("awaiting_access_selection")

    # Per-chunk procedure matching state variables (for support_example documents)
    awaiting_chunk_procedure_selection = context.get_state("awaiting_chunk_procedure_selection")
    awaiting_chunk_doc_update = context.get_state("awaiting_chunk_doc_update")

    # ==========================================================================
    # Access Level Selection
    # ==========================================================================
    if awaiting_access_selection and context.user_input:
        response = context.user_input.strip().lower()

        if response in ["cancel", "skip", "abort", "quit", "exit", "stop", "no"]:
            return StepResult(skip_remaining=True, progress_message="Ingestion cancelled.")

        ACCESS_OPTIONS: dict[str, dict] = {
            "1": {"audience": "all", "roles": [], "label": "Everyone (public)"},
            "2": {
                "audience": "staff",
                "roles": [1, 2, 3],
                "label": "All Staff (Admin, Engineer, Support)",
            },
            "3": {
                "audience": "staff",
                "roles": [1, 2],
                "label": "Technical Staff (Admin, Engineer)",
            },
            "4": {"audience": "staff", "roles": [1], "label": "Admin Only"},
        }

        if response in ACCESS_OPTIONS:
            selected = ACCESS_OPTIONS[response]
            proposed_metadata = context.get_state("proposed_metadata") or {}
            proposed_metadata["audience"] = selected["audience"]
            proposed_metadata["allowed_role_ids"] = selected["roles"]

            return StepResult(
                state_updates={
                    "awaiting_access_selection": False,
                    "awaiting_approval": True,
                    "proposed_metadata": proposed_metadata,
                },
                needs_user_input=True,
                user_prompt=(
                    f"Access changed to **{selected['label']}**.\n\n"
                    "Reply with:\n"
                    "1. **Approve** - Ingest the document\n"
                    "2. **Reject** - Cancel ingestion\n"
                    "3. **Reclassify** - Change document type\n"
                    "4. **Change Access** - Modify access level"
                ),
            )
        else:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Select access level:\n\n"
                    "1. **Everyone** - Public (customers + staff)\n"
                    "2. **All Staff** - Admin, Engineer, Support\n"
                    "3. **Technical** - Admin, Engineer only\n"
                    "4. **Admin Only** - Administrators only"
                ),
            )

    # ==========================================================================
    # Per-Chunk Procedure Matching (for support_example documents)
    # ==========================================================================
    if awaiting_chunk_doc_update and context.user_input:
        # User has finished adding a new procedure to the doc
        response = context.user_input.strip().lower()

        # Check for cancel commands first
        if response in ["cancel", "abort", "quit", "exit", "stop"]:
            LOGGER.info("User cancelled procedure doc update")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        if response in ["done", "ready", "updated", "added"]:
            from orchestrator.services.artifacts_provider import clear_gdoc_cache
            from orchestrator.services.procedure_provider import (
                ProcedureProvider,
                match_content_to_procedures,
            )

            # Clear cache and reload procedures
            doc_id = os.getenv("CUSTOMER_SUPPORT_DOC_ID", "")
            clear_gdoc_cache(doc_id)

            provider = ProcedureProvider()
            provider.clear_cache()
            procedures = provider.get_procedures(force_reload=True)

            # Get unmatched chunks and re-check them against new procedures
            unmatched_chunk_indices = context.get_state("unmatched_chunk_indices") or []
            chunk_procedure_map = context.get_state("chunk_procedure_map") or {}
            chunks_data = context.get_state("chunks_for_matching") or []

            LOGGER.info(
                f"Reloaded procedures, re-checking {len(unmatched_chunk_indices)} unmatched chunks"
            )

            # Re-check all remaining unmatched chunks
            still_unmatched = []
            for idx in unmatched_chunk_indices:
                if idx >= len(chunks_data):
                    continue
                current_chunk_text = chunks_data[idx]
                match_result = await match_content_to_procedures(
                    current_chunk_text[:3000], procedures
                )
                if match_result:
                    matched_proc, confidence = match_result
                    chunk_procedure_map[idx] = [matched_proc.id]
                    LOGGER.info(f"Chunk {idx} now matches: {matched_proc.title}")
                else:
                    still_unmatched.append(idx)

            if still_unmatched:
                # Still have unmatched chunks - continue prompting
                current_idx = still_unmatched[0]
                current_chunk_text = (
                    chunks_data[current_idx] if current_idx < len(chunks_data) else ""
                )
                chunk_preview = _format_chunk_preview(SimpleChunk(current_chunk_text))
                proc_list = _format_procedure_list_for_chunk(procedures)

                return StepResult(
                    state_updates={
                        "awaiting_chunk_doc_update": False,
                        "awaiting_chunk_procedure_selection": True,
                        "unmatched_chunk_indices": still_unmatched,
                        "current_chunk_index": current_idx,
                        "chunk_procedure_map": chunk_procedure_map,
                    },
                    needs_user_input=True,
                    user_prompt=(
                        f"✅ Re-matched {len(unmatched_chunk_indices) - len(still_unmatched)} "
                        f"chunks to new procedure.\n\n"
                        f"**Chunk {still_unmatched.index(current_idx) + 1} of {len(still_unmatched)}** "
                        "still unmatched:\n"
                        f'> "{chunk_preview}"\n\n'
                        f"**Options:**\n{proc_list}\n"
                        f"  **P.** Add new procedure\n"
                        f"  **0.** Skip this chunk\n\n"
                        "Enter procedure **number**, **P**, or **0**:"
                    ),
                )
            else:
                # All chunks now matched!
                LOGGER.info("All chunks matched after adding new procedure")
                return StepResult(
                    state_updates={
                        "awaiting_chunk_doc_update": False,
                        "chunk_matching_in_progress": False,
                        "chunk_procedure_map": chunk_procedure_map,
                        "unmatched_chunk_indices": [],
                        "awaiting_approval": True,
                    },
                    needs_user_input=True,
                    user_prompt=(
                        "✅ All chunks now matched to procedures!\n\n"
                        "Reply with:\n"
                        "1. **Approve** - Ingest the document\n"
                        "2. **Reject** - Cancel ingestion\n"
                        "3. **Reclassify** - Change document type\n"
                        "4. **Change Access** - Modify access level"
                    ),
                )
        else:
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please add the suggested procedure to your Customer Support Doc, "
                    "then reply **'done'** when ready."
                ),
            )

    if awaiting_chunk_procedure_selection and context.user_input:
        # User is selecting a procedure for a specific chunk
        from orchestrator.services.procedure_provider import (
            ProcedureProvider,
            generate_suggested_procedure,
        )

        response = context.user_input.strip().lower()

        # Check for cancel commands first (abort entire workflow, not just skip chunk)
        if response in ["cancel", "abort", "quit", "exit", "stop"]:
            LOGGER.info("User cancelled chunk procedure selection")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        unmatched_chunk_indices = context.get_state("unmatched_chunk_indices") or []
        current_chunk_index = context.get_state("current_chunk_index")
        chunk_procedure_map = context.get_state("chunk_procedure_map") or {}
        chunks_data = context.get_state("chunks_for_matching") or []

        provider = ProcedureProvider()
        procedures = provider.get_procedures()

        if response == "0" or response == "skip":
            # Skip this chunk - mark as empty procedure_ids
            chunk_procedure_map[current_chunk_index] = []
            LOGGER.info(f"User skipped chunk {current_chunk_index}")

            # Move to next unmatched chunk
            remaining = [i for i in unmatched_chunk_indices if i != current_chunk_index]

            if remaining:
                next_idx = remaining[0]
                chunk_preview = (
                    _format_chunk_preview(SimpleChunk(chunks_data[next_idx]))
                    if next_idx < len(chunks_data)
                    else ""
                )
                proc_list = _format_procedure_list_for_chunk(procedures)

                return StepResult(
                    state_updates={
                        "unmatched_chunk_indices": remaining,
                        "current_chunk_index": next_idx,
                        "chunk_procedure_map": chunk_procedure_map,
                    },
                    needs_user_input=True,
                    user_prompt=(
                        f"**Chunk {remaining.index(next_idx) + 1} of {len(remaining)}** "
                        "needs procedure:\n"
                        f'> "{chunk_preview}"\n\n'
                        f"**Options:**\n{proc_list}\n"
                        f"  **P.** Add new procedure\n"
                        f"  **0.** Skip this chunk\n\n"
                        "Enter procedure **number**, **P**, or **0**:"
                    ),
                )
            else:
                # All chunks processed
                return StepResult(
                    state_updates={
                        "awaiting_chunk_procedure_selection": False,
                        "chunk_matching_in_progress": False,
                        "chunk_procedure_map": chunk_procedure_map,
                        "unmatched_chunk_indices": [],
                        "awaiting_approval": True,
                    },
                    needs_user_input=True,
                    user_prompt=(
                        "✅ All chunks processed!\n\n"
                        "Reply with:\n"
                        "1. **Approve** - Ingest the document\n"
                        "2. **Reject** - Cancel ingestion\n"
                        "3. **Reclassify** - Change document type\n"
                        "4. **Change Access** - Modify access level"
                    ),
                )

        if response == "p" or response == "new":
            # Generate suggested procedure
            await context.send_progress_to_user("Generating suggested procedure...")

            current_chunk_text = (
                chunks_data[current_chunk_index] if current_chunk_index < len(chunks_data) else ""
            )
            suggested = await generate_suggested_procedure(current_chunk_text, procedures)

            if not suggested:
                proc_list = _format_procedure_list_for_chunk(procedures)
                return StepResult(
                    needs_user_input=True,
                    user_prompt=(
                        "Sorry, couldn't generate a suggested procedure.\n\n"
                        f"**Options:**\n{proc_list}\n"
                        f"  **0.** Skip this chunk\n\n"
                        "Enter procedure **number** or **0**:"
                    ),
                )

            return StepResult(
                state_updates={
                    "awaiting_chunk_procedure_selection": False,
                    "awaiting_chunk_doc_update": True,
                    "suggested_procedure_text": suggested,
                },
                needs_user_input=True,
                user_prompt=(
                    "**Suggested new procedure:**\n\n"
                    f"```markdown\n{suggested}\n```\n\n"
                    "Please add this to your Customer Support Doc, then reply **'done'**."
                ),
            )

        # Try to parse as procedure number
        try:
            selected_num = int(response)
            selected_proc = next((p for p in procedures if p.number == selected_num), None)

            if selected_proc:
                chunk_procedure_map[current_chunk_index] = [selected_proc.id]
                LOGGER.info(
                    f"User linked chunk {current_chunk_index} to "
                    f"Procedure {selected_proc.number}: {selected_proc.title}"
                )

                # Move to next unmatched chunk
                remaining = [i for i in unmatched_chunk_indices if i != current_chunk_index]

                if remaining:
                    next_idx = remaining[0]
                    chunk_preview = (
                        _format_chunk_preview(SimpleChunk(chunks_data[next_idx]))
                        if next_idx < len(chunks_data)
                        else ""
                    )
                    proc_list = _format_procedure_list_for_chunk(procedures)

                    return StepResult(
                        state_updates={
                            "unmatched_chunk_indices": remaining,
                            "current_chunk_index": next_idx,
                            "chunk_procedure_map": chunk_procedure_map,
                        },
                        needs_user_input=True,
                        user_prompt=(
                            f"✅ Linked to **{selected_proc.title}**\n\n"
                            f"**Chunk {remaining.index(next_idx) + 1} of {len(remaining)}** "
                            "needs procedure:\n"
                            f'> "{chunk_preview}"\n\n'
                            f"**Options:**\n{proc_list}\n"
                            f"  **P.** Add new procedure\n"
                            f"  **0.** Skip this chunk\n\n"
                            "Enter procedure **number**, **P**, or **0**:"
                        ),
                    )
                else:
                    # All chunks processed
                    return StepResult(
                        state_updates={
                            "awaiting_chunk_procedure_selection": False,
                            "chunk_matching_in_progress": False,
                            "chunk_procedure_map": chunk_procedure_map,
                            "unmatched_chunk_indices": [],
                            "awaiting_approval": True,
                        },
                        needs_user_input=True,
                        user_prompt=(
                            f"✅ Linked to **{selected_proc.title}**\n\n"
                            "All chunks processed!\n\n"
                            "Reply with:\n"
                            "1. **Approve** - Ingest the document\n"
                            "2. **Reject** - Cancel ingestion\n"
                            "3. **Reclassify** - Change document type\n"
                            "4. **Change Access** - Modify access level"
                        ),
                    )
            else:
                proc_list = _format_procedure_list_for_chunk(procedures)
                return StepResult(
                    needs_user_input=True,
                    user_prompt=(
                        f"Procedure {selected_num} not found.\n\n"
                        f"**Options:**\n{proc_list}\n"
                        f"  **P.** Add new procedure\n"
                        f"  **0.** Skip this chunk\n\n"
                        "Enter procedure **number**, **P**, or **0**:"
                    ),
                )
        except ValueError:
            proc_list = _format_procedure_list_for_chunk(procedures)
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please enter a procedure **number**, **P** to add new procedure, "
                    "or **0** to skip.\n\n"
                    f"**Options:**\n{proc_list}"
                ),
            )

    if awaiting_reclassification and context.user_input:
        response = context.user_input.strip()

        # Check for cancel commands first
        if response.lower() in ["cancel", "skip", "abort", "quit", "exit", "stop"]:
            LOGGER.info("User cancelled reclassification")
            return StepResult(
                skip_remaining=True,
                progress_message="Ingestion cancelled.",
            )

        # Check if user selected a number or typed a type name
        new_type = None
        new_type_display = None

        if response in DOC_TYPE_OPTIONS:
            new_type, new_type_display = DOC_TYPE_OPTIONS[response]
        else:
            # Try to match by type name
            response_lower = response.lower()
            for key, (doc_type, display) in DOC_TYPE_OPTIONS.items():
                if response_lower == doc_type or response_lower in display.lower():
                    new_type = doc_type
                    new_type_display = display
                    break

        if new_type:
            # Update the classification and return to approval
            LOGGER.info(f"User reclassified document as: {new_type}")

            # Get access defaults for new doc type
            from orchestrator.experts.handlers.ingestion_expert.prepare_approval import (
                DOC_TYPE_ACCESS,
            )

            access = DOC_TYPE_ACCESS.get(new_type, DOC_TYPE_ACCESS["technical"])

            # Update proposed metadata
            proposed_metadata = context.get_state("proposed_metadata") or {}
            proposed_metadata["doc_type"] = new_type
            proposed_metadata["audience"] = access["audience"]
            proposed_metadata["allowed_role_ids"] = access["roles"]
            proposed_metadata["classification_confidence"] = 1.0  # User confirmed

            # For support_example, procedure matching will happen per-chunk during storage
            # Just proceed to approval
            if new_type == "support_example":
                LOGGER.info(
                    "Reclassified to support_example - procedure matching deferred to per-chunk"
                )
                return StepResult(
                    state_updates={
                        "awaiting_reclassification": False,
                        "detected_doc_type": new_type,
                        "classification_confidence": 1.0,
                        "proposed_metadata": proposed_metadata,
                        "awaiting_approval": True,
                    },
                    needs_user_input=True,
                    user_prompt=(
                        f"Classification changed to **{new_type_display}**.\n\n"
                        "Procedure matching will happen per-chunk after approval.\n\n"
                        "Reply with:\n"
                        "1. **Approve** - Ingest with new classification\n"
                        "2. **Reject** - Cancel ingestion\n"
                        "3. **Reclassify** - Change document type again\n"
                        "4. **Change Access** - Modify access level"
                    ),
                )

            return StepResult(
                state_updates={
                    "awaiting_reclassification": False,
                    "awaiting_approval": True,
                    "detected_doc_type": new_type,
                    "classification_confidence": 1.0,
                    "proposed_metadata": proposed_metadata,
                },
                needs_user_input=True,
                user_prompt=(
                    f"Classification changed to **{new_type_display}**.\n\n"
                    "Reply with:\n"
                    "1. **Approve** - Ingest with new classification\n"
                    "2. **Reject** - Cancel ingestion\n"
                    "3. **Reclassify** - Change document type again\n"
                    "4. **Change Access** - Modify access level"
                ),
            )
        else:
            # Invalid selection - re-prompt
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "Please select a document type by number:\n\n"
                    "1. **SOP** - Standard Operating Procedure\n"
                    "2. **FAQ** - Frequently Asked Questions\n"
                    "3. **Support** - Support Conversation\n"
                    "4. **Technical** - Technical Documentation\n"
                    "5. **Policy** - Policy / Guidelines"
                ),
            )

    if awaiting_approval and context.user_input:
        response = context.user_input.strip().lower()

        if response in ["1", "approve", "yes", "ok", "y"]:
            # Continue to storage
            pass
        elif response in ["2", "reject", "no", "cancel", "n"]:
            return StepResult(
                data={"storage_status": "rejected"},
                state_updates={"approval_status": "rejected"},
                skip_remaining=True,
                progress_message="Document ingestion cancelled",
            )
        elif response in ["3", "reclassify", "classify", "type", "change type"]:
            return StepResult(
                state_updates={"awaiting_reclassification": True, "awaiting_approval": False},
                needs_user_input=True,
                user_prompt=(
                    "Select document type:\n\n"
                    "1. **SOP** - Standard Operating Procedure\n"
                    "2. **FAQ** - Frequently Asked Questions\n"
                    "3. **Support** - Support Conversation\n"
                    "4. **Technical** - Technical Documentation\n"
                    "5. **Policy** - Policy / Guidelines"
                ),
            )
        elif response in ["4", "access", "change access"]:
            return StepResult(
                state_updates={"awaiting_access_selection": True, "awaiting_approval": False},
                needs_user_input=True,
                user_prompt=(
                    "Select access level:\n\n"
                    "1. **Everyone** - Public (customers + staff)\n"
                    "2. **All Staff** - Admin, Engineer, Support\n"
                    "3. **Technical** - Admin, Engineer only\n"
                    "4. **Admin Only** - Administrators only"
                ),
            )
        else:
            # Unrecognized response - re-prompt
            return StepResult(
                needs_user_input=True,
                user_prompt=(
                    "I didn't understand that. Please reply with:\n"
                    "1. **Approve** - Ingest the document\n"
                    "2. **Reject** - Cancel ingestion\n"
                    "3. **Reclassify** - Change document type\n"
                    "4. **Change Access** - Modify access level"
                ),
            )
    elif not awaiting_approval:
        # First run - show approval summary and ask
        approval_summary = context.get_previous_result("prepare_approval_summary")
        if approval_summary:
            summary = approval_summary.get("approval_summary", "Document ready for approval")
        else:
            summary = "Document processed. Approve ingestion?"

        return StepResult(
            state_updates={"awaiting_approval": True},
            needs_user_input=True,
            user_prompt=summary,
        )

    # User approved - proceed with storage
    await context.send_progress_to_user("Embedding and storing document...")

    content = context.get_state("cleaned_content") or context.get_state("document_content")
    doc_type = context.get_state("detected_doc_type") or "technical"
    metadata = context.get_state("proposed_metadata") or {}
    entities = context.get_state("extracted_entities") or []
    relationships = context.get_state("extracted_relationships") or []

    # Include procedure_ids from state (set during per-chunk matching or reclassification)
    procedure_ids = context.get_state("procedure_ids") or []
    if procedure_ids:
        metadata["procedure_ids"] = procedure_ids
        LOGGER.info(f"Including procedure_ids in metadata: {procedure_ids}")

    # Stamp org_id for cross-tenant filtering on future cross-source dedup checks
    if context.organization_id is not None:
        metadata["organization_id"] = context.organization_id

    # Get duplicate handling mode from detect_duplicates step
    duplicate_mode = context.get_state("duplicate_mode") or "new"
    existing_document_id = context.get_state("existing_document_id")
    # Re-query chunk hashes at storage time (not from state — avoids large JSONB blob)
    existing_hashes_set: Set[str] = set()
    if duplicate_mode == "incorporate" and existing_document_id:
        from orchestrator.experts.handlers.ingestion_expert.detect_duplicates import (
            get_existing_chunk_hashes,
        )

        existing_hashes_set = await get_existing_chunk_hashes(existing_document_id)

    LOGGER.info(f"Storage mode: {duplicate_mode}")
    if duplicate_mode == "incorporate":
        LOGGER.info(
            f"Incorporating into {existing_document_id} with {len(existing_hashes_set)} existing hashes"
        )

    if not content:
        return StepResult.failure("No document content available for storage")

    try:
        # Chunk content
        LOGGER.info(f"Chunking {len(content)} characters")
        chunks = chunk_content(content)
        LOGGER.info(f"Created {len(chunks)} chunks")

        # ======================================================================
        # Per-Chunk Procedure Matching for support_example documents
        # ======================================================================
        chunk_procedure_map_storage: Dict[int, List[str]] = {}

        # Check if we're resuming from chunk matching flow
        if context.get_state("chunk_procedure_map"):
            chunk_procedure_map_storage = context.get_state("chunk_procedure_map")
            LOGGER.info(
                f"Resuming with existing chunk_procedure_map: "
                f"{len(chunk_procedure_map_storage)} entries"
            )

        # For support_example, do per-chunk procedure matching
        elif doc_type == "support_example":
            from orchestrator.services.artifacts_provider import clear_gdoc_cache
            from orchestrator.services.procedure_provider import (
                ProcedureProvider,
                match_content_to_procedures,
            )

            # Re-cache customer support doc to catch latest procedures
            # This is important when ingesting support examples that may reference
            # procedures added since the last cache refresh
            doc_id = os.getenv("CUSTOMER_SUPPORT_DOC_ID", "")
            if doc_id:
                LOGGER.info("Refreshing Customer Support Doc cache for procedure matching")
                clear_gdoc_cache(doc_id)

            provider = ProcedureProvider()
            provider.clear_cache()  # Clear procedure cache
            procedures = provider.get_procedures(force_reload=True)

            if procedures and len(chunks) > 0:
                await context.send_progress_to_user(
                    f"Matching {len(chunks)} chunks to procedures..."
                )

                unmatched_chunks: List[int] = []
                skipped_intro_chunks: List[int] = []

                # Try to auto-match each chunk
                for i, chunk in enumerate(chunks):
                    # Auto-skip intro/header chunks
                    if _is_intro_chunk(chunk, i):
                        chunk_procedure_map_storage[i] = []  # Empty = skip
                        skipped_intro_chunks.append(i)
                        continue

                    match_result = await match_content_to_procedures(
                        chunk.content[:3000], procedures
                    )
                    if match_result:
                        matched_proc, confidence = match_result
                        chunk_procedure_map_storage[i] = [matched_proc.id]
                        LOGGER.info(
                            f"Chunk {i} auto-matched to: {matched_proc.title} "
                            f"(confidence: {confidence:.2f})"
                        )
                    else:
                        unmatched_chunks.append(i)
                        LOGGER.info(f"Chunk {i} no auto-match")

                if skipped_intro_chunks:
                    LOGGER.info(f"Auto-skipped {len(skipped_intro_chunks)} intro chunks")

                # Calculate counts
                matched_count = len(chunks) - len(unmatched_chunks) - len(skipped_intro_chunks)
                LOGGER.info(
                    f"Auto-matched {matched_count}/{len(chunks)} chunks "
                    f"({len(skipped_intro_chunks)} intro skipped, {len(unmatched_chunks)} unmatched)"
                )

                # If there are unmatched chunks, prompt user for each
                if unmatched_chunks:
                    # Save chunks content for later reference
                    chunks_data = [c.content for c in chunks]

                    current_idx = unmatched_chunks[0]
                    chunk_preview = _format_chunk_preview(chunks[current_idx])
                    proc_list = _format_procedure_list_for_chunk(procedures)

                    # Build status message
                    status_parts = [f"✅ Auto-matched {matched_count} of {len(chunks)} chunks."]
                    if skipped_intro_chunks:
                        status_parts.append(
                            f"📝 Skipped {len(skipped_intro_chunks)} intro section(s)."
                        )

                    return StepResult(
                        state_updates={
                            "chunk_matching_in_progress": True,
                            "awaiting_chunk_procedure_selection": True,
                            "awaiting_approval": False,
                            "unmatched_chunk_indices": unmatched_chunks,
                            "current_chunk_index": current_idx,
                            "chunk_procedure_map": chunk_procedure_map_storage,
                            "chunks_for_matching": chunks_data,
                            "total_chunks": len(chunks),
                        },
                        needs_user_input=True,
                        user_prompt=(
                            f"{' '.join(status_parts)}\n\n"
                            f"**Chunk {1} of {len(unmatched_chunks)}** needs procedure:\n"
                            f'> "{chunk_preview}"\n\n'
                            f"**Options:**\n{proc_list}\n"
                            f"  **P.** Add new procedure\n"
                            f"  **0.** Skip this chunk\n\n"
                            "Enter procedure **number**, **P**, or **0**:"
                        ),
                    )

        # Embed chunks
        LOGGER.info(f"Embedding {len(chunks)} chunks with doc_type={doc_type}")
        embedded_chunks = await embed_chunks(chunks, doc_type)
        LOGGER.info(f"Embedded {len(embedded_chunks)} chunks")

        if not embedded_chunks:
            return StepResult.failure("Failed to embed document chunks")

        # Filter out skipped chunks (those with empty procedure_ids for support_example)
        # For support_example with per-chunk matching, chunks marked as "skip" (empty list)
        # are still included but without procedure association
        # NOTE: We include all chunks; skipped chunks just have empty procedure_ids

        # Store to database first (insert new content before removing old)
        LOGGER.info("Storing to database")
        result = await store_to_database(
            content=content,
            embedded_chunks=embedded_chunks,
            entities=entities,
            relationships=relationships,
            metadata=metadata,
            doc_type=doc_type,
            duplicate_mode=duplicate_mode,
            existing_document_id=existing_document_id,
            existing_chunk_hashes=existing_hashes_set,
            chunk_procedure_map=(
                chunk_procedure_map_storage if chunk_procedure_map_storage else None
            ),
        )

        # After successful store: delete any chunks contradicted by detect_contradictions.
        # Done after insert so new content is safe even if delete fails.
        chunks_to_delete = context.get_state("chunks_to_delete") or []
        if chunks_to_delete:
            # Validate IDs against the set originally shown to the LLM (prevents phantom deletes)
            valid_chunk_ids = set(context.get_state("valid_chunk_ids") or [])
            if valid_chunk_ids:
                chunks_to_delete = [cid for cid in chunks_to_delete if cid in valid_chunk_ids]
            if chunks_to_delete:
                _url = os.getenv("CHAT_DB_URL")
                _key = os.getenv("CHAT_DB_SERVICE_KEY")
                if _url and _key:
                    from supabase import (  # type: ignore[attr-defined]
                        create_client as _supabase_client,
                    )

                    _contra_db = _supabase_client(_url, _key)
                    try:
                        _ids = chunks_to_delete
                        await asyncio.to_thread(
                            lambda: _contra_db.table("chunks").delete().in_("id", _ids).execute()
                        )
                        LOGGER.info(
                            f"Deleted {len(chunks_to_delete)} contradicted chunk(s) after ingestion"
                        )
                    except Exception as e:
                        LOGGER.warning(f"Failed to delete contradicted chunks: {e}")

        chunks_skipped = result.get("chunks_skipped", 0)
        relationships_inserted = result.get("relationships_inserted", 0)
        log_msg = f"Stored document {result['document_id']}: {result['chunks_inserted']} chunks"
        if chunks_skipped > 0:
            log_msg += f" ({chunks_skipped} duplicates skipped)"
        log_msg += f", {result['entities_inserted']} entities"
        if relationships_inserted > 0:
            log_msg += f", {relationships_inserted} relationships"
        LOGGER.info(log_msg)

        # Build progress message with deduplication info
        progress_parts = [f"Stored {result['chunks_inserted']} chunks"]
        if chunks_skipped > 0:
            progress_parts.append(f"{chunks_skipped} duplicates skipped")
        progress_parts.append(f"{result['entities_inserted']} entities")
        if relationships_inserted > 0:
            progress_parts.append(f"{relationships_inserted} relationships")
        if chunk_procedure_map_storage:
            matched_count = sum(1 for pids in chunk_procedure_map_storage.values() if pids)
            progress_parts.append(f"{matched_count} chunks linked to procedures")
        progress_msg = ", ".join(progress_parts)

        return StepResult(
            data={
                "stored_document_id": result["document_id"],
                "chunk_count": result["chunks_inserted"],
                "chunks_skipped": chunks_skipped,
                "entity_count": result["entities_inserted"],
                "relationship_count": relationships_inserted,
                "storage_status": "success",
                "duplicate_mode": duplicate_mode,
            },
            state_updates={
                "stored_document_id": result["document_id"],
                "stored_chunk_count": result["chunks_inserted"],
                "stored_chunks_skipped": chunks_skipped,
                "approval_status": "approved",
                "chunk_matching_in_progress": False,
            },
            progress_message=progress_msg,
        )

    except Exception as e:
        LOGGER.exception(f"Storage failed: {e}")
        from shared.utils.error_messages import ErrorCategory, get_user_message

        return StepResult.failure(get_user_message(ErrorCategory.SYSTEM, "internal_error"))
