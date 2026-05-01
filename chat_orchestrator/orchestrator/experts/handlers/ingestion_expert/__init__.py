"""Document Ingestion Expert step handlers.

Handlers for the ingestion_expert's workflow steps:
- fetch_document: Retrieve document from Google Drive, text input, or Telegram file
- classify_document: LLM-based document type classification
- preprocess_document: Type-specific cleaning (PII masking, formatting)
- detect_duplicates: Check for existing documents/chunks, handle deduplication
- extract_entities: GraphRAG entity and relationship extraction
- detect_contradictions: LLM-based check for conflicts with existing RAG corpus / system instructions
- prepare_approval_summary: Build approval summary for user review
- embed_and_store: Chunk, embed, per-chunk procedure matching, and store to pgvector

NOTE: match_procedures was removed - procedure matching is now done per-chunk
in embed_and_store for support_example documents.
"""

from orchestrator.experts.handlers.ingestion_expert.classify_document import classify_document
from orchestrator.experts.handlers.ingestion_expert.detect_contradictions import (
    detect_contradictions,
)
from orchestrator.experts.handlers.ingestion_expert.detect_duplicates import detect_duplicates
from orchestrator.experts.handlers.ingestion_expert.embed_and_store import embed_and_store
from orchestrator.experts.handlers.ingestion_expert.extract_entities import extract_entities
from orchestrator.experts.handlers.ingestion_expert.fetch_document import fetch_document
from orchestrator.experts.handlers.ingestion_expert.improve_content import improve_content
from orchestrator.experts.handlers.ingestion_expert.prepare_approval import prepare_approval_summary
from orchestrator.experts.handlers.ingestion_expert.preprocess_document import preprocess_document

__all__ = [
    "fetch_document",
    "classify_document",
    "improve_content",
    "preprocess_document",
    "detect_duplicates",
    "extract_entities",
    "detect_contradictions",
    "prepare_approval_summary",
    "embed_and_store",
]
