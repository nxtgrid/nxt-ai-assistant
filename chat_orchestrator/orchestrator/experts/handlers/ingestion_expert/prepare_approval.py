"""Prepare approval summary step handler for Document Ingestion Expert.

Builds a comprehensive summary of the document processing results
for user review and approval before final ingestion.
"""

from typing import Dict, List, TypedDict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Document type display names
DOC_TYPE_DISPLAY: Dict[str, str] = {
    "sop": "Standard Operating Procedure",
    "faq": "FAQ / Q&A",
    "support_example": "Support Conversation (available in customer mode)",
    "technical": "Technical Documentation",
    "policy": "Policy / Guidelines",
}


class AccessConfig(TypedDict):
    """Type for document access configuration."""

    audience: str
    roles: List[int]


# Default access roles based on doc type
DOC_TYPE_ACCESS: Dict[str, AccessConfig] = {
    "sop": {"audience": "staff", "roles": [1, 2, 3]},  # Admin, Engineer, Support
    "faq": {"audience": "all", "roles": []},  # Public
    "support_example": {"audience": "all", "roles": []},  # Public - used in customer mode RAG
    "technical": {"audience": "staff", "roles": [1, 2]},  # Admin, Engineer
    "policy": {"audience": "staff", "roles": [1, 2, 3]},
}


def format_entity_list(entities: List[Dict], max_items: int = 10) -> str:
    """Format entity list for display.

    Args:
        entities: List of entity dicts
        max_items: Maximum items to show

    Returns:
        Formatted string with entity names and types
    """
    if not entities:
        return "_No entities extracted_"

    lines = []
    for entity in entities[:max_items]:
        name = entity.get("name", "Unknown")
        etype = entity.get("type", "concept")
        lines.append(f"  • {name} ({etype})")

    if len(entities) > max_items:
        lines.append(f"  _...and {len(entities) - max_items} more_")

    return "\n".join(lines)


def estimate_chunk_count(content: str, chunk_size: int = 1000) -> int:
    """Estimate number of chunks that will be created.

    Args:
        content: Document content
        chunk_size: Target chunk size

    Returns:
        Estimated chunk count
    """
    if not content:
        return 0
    # Account for overlap (roughly 20%)
    effective_size = chunk_size * 0.8
    return max(1, int(len(content) / effective_size))


@register_step("prepare_approval_summary")
async def prepare_approval_summary(context: StepContext) -> StepResult:
    """Build approval summary for user review.

    Compiles all processing results into a clear summary that shows:
    - Document classification and confidence
    - Preprocessing results (PII masked, etc.)
    - Extracted entities
    - Proposed metadata and access control
    - Estimated storage impact

    Args:
        context: Step execution context

    Returns:
        StepResult with approval_summary and proposed_metadata
    """
    # Gather all state
    doc_type = context.get_state("detected_doc_type") or "technical"
    confidence = context.get_state("classification_confidence") or 0.5
    reasoning = context.get_state("classification_reasoning") or ""
    pii_count = context.get_state("pii_masked_count") or 0
    entities = context.get_state("extracted_entities") or []
    relationships = context.get_state("extracted_relationships") or []
    extraction_error = context.get_state("extraction_error")
    source_type = context.get_state("source_type") or "unknown"
    source_id = context.get_state("source_id") or ""
    title = context.get_state("document_title") or "Untitled Document"
    content = context.get_state("cleaned_content") or context.get_state("document_content") or ""

    # Get access defaults for doc type
    access = DOC_TYPE_ACCESS.get(doc_type, DOC_TYPE_ACCESS["technical"])

    # Build proposed metadata
    proposed_metadata = {
        "doc_type": doc_type,
        "title": title,
        "source_type": source_type,
        "source_id": source_id,
        "audience": access["audience"],
        "allowed_role_ids": access["roles"],
        "allowed_org_ids": [],  # Will be set based on user's org
        "classification_confidence": confidence,
    }

    # Estimate chunks
    estimated_chunks = estimate_chunk_count(content)

    # Build summary message
    doc_type_display = DOC_TYPE_DISPLAY.get(doc_type, doc_type.title())
    confidence_emoji = "🟢" if confidence >= 0.8 else "🟡" if confidence >= 0.5 else "🔴"

    summary_lines = [
        "**Document Ready for Ingestion**",
        "",
        f"**Title:** {title}",
        f"**Source:** {source_type}" + (f" (`{source_id[:12]}...`)" if source_id else ""),
        "",
        f"**Classification:** {doc_type_display}",
        f"**Confidence:** {confidence_emoji} {confidence * 100:.0f}%",
    ]

    if reasoning:
        summary_lines.append(f"_Reasoning: {reasoning}_")

    summary_lines.extend(
        [
            "",
            "**Processing Summary:**",
            f"  • Content length: {len(content):,} characters",
            f"  • Estimated chunks: {estimated_chunks}",
        ]
    )

    if pii_count > 0:
        summary_lines.append(f"  • PII masked: {pii_count} items")

    # Show matched procedure for support examples
    matched_procedure_title = context.get_state("matched_procedure_title")
    if matched_procedure_title:
        summary_lines.extend(
            [
                "",
                f"**Matched Procedure:** {matched_procedure_title}",
            ]
        )

    summary_lines.extend(
        [
            "",
            "**Entities Found:**",
        ]
    )

    if extraction_error:
        summary_lines.append(f"_⚠️ Extraction failed: {extraction_error}_")
    else:
        summary_lines.append(format_entity_list(entities))

    if relationships:
        summary_lines.append(f"\n**Relationships:** {len(relationships)} connections found")

    summary_lines.extend(
        [
            "",
            "**Proposed Access:**",
            f"  • Audience: {access['audience']}",
        ]
    )

    if access["roles"]:
        role_names = {1: "Admin", 2: "Engineer", 3: "Support"}
        roles_display = ", ".join(role_names.get(r, str(r)) for r in access["roles"])
        summary_lines.append(f"  • Roles: {roles_display}")

    summary_lines.extend(
        [
            "",
            "---",
            "**Reply with:**",
            "1. **Approve** - Ingest as shown above",
            "2. **Reject** - Discard this document",
            "3. **Reclassify** - Change document type",
            "4. **Change Access** - Modify access level",
        ]
    )

    summary = "\n".join(summary_lines)

    LOGGER.info(f"Prepared approval summary for '{title}' ({doc_type})")

    return StepResult(
        data={
            "approval_summary": summary,
            "proposed_metadata": proposed_metadata,
            "estimated_chunks": estimated_chunks,
        },
        state_updates={
            "proposed_metadata": proposed_metadata,
            "estimated_chunks": estimated_chunks,
        },
        progress_message="Ready for approval",
    )
