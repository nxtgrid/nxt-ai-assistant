"""Preprocess document step handler for Document Ingestion Expert.

Applies type-specific preprocessing to documents:
- support_example: PII masking (emails, phone numbers, names)
- sop: Step extraction and formatting
- technical: Table/code block formatting
- all: Whitespace normalization
"""

import re
from typing import Tuple

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# PII patterns for masking
EMAIL_PATTERN = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b")
PHONE_PATTERN = re.compile(
    r"(?:\+?[0-9]{1,3}[-.\s]?)?"  # Country code
    r"(?:\(?[0-9]{2,4}\)?[-.\s]?)?"  # Area code
    r"[0-9]{3,4}[-.\s]?[0-9]{3,4}"  # Local number
)
# Nigerian phone numbers specifically
NG_PHONE_PATTERN = re.compile(
    r"(?:\+?234|0)"  # Nigeria country code or leading 0
    r"[789][01][0-9]{8}"  # Nigerian mobile number
)


def mask_pii(text: str) -> Tuple[str, int]:
    """Mask PII (emails, phone numbers) in text.

    Args:
        text: Document content

    Returns:
        Tuple of (masked_text, pii_count)
    """
    pii_count = 0

    # Mask emails
    emails = EMAIL_PATTERN.findall(text)
    for email in emails:
        text = text.replace(email, "[EMAIL]")
        pii_count += 1

    # Mask Nigerian phone numbers first (more specific)
    ng_phones = NG_PHONE_PATTERN.findall(text)
    for phone in ng_phones:
        text = text.replace(phone, "[PHONE-NG]")
        pii_count += 1

    # Mask general phone numbers
    phones = PHONE_PATTERN.findall(text)
    for phone in phones:
        if "[PHONE" not in phone:  # Don't double-mask
            text = text.replace(phone, "[PHONE]")
            pii_count += 1

    return text, pii_count


def extract_steps(text: str) -> str:
    """Extract and format steps from SOP-style documents.

    Identifies numbered steps, bullet points, and procedure markers,
    ensuring consistent formatting.

    Args:
        text: Document content

    Returns:
        Formatted content with clear step structure
    """
    lines = text.split("\n")
    formatted_lines = []
    step_number = 0

    for line in lines:
        stripped = line.strip()

        # Detect numbered steps: "1.", "1)", "Step 1:", etc.
        step_match = re.match(r"^(?:Step\s*)?(\d+)[.):]\s*(.+)", stripped, re.IGNORECASE)
        if step_match:
            step_number = int(step_match.group(1))
            content = step_match.group(2)
            formatted_lines.append(f"**Step {step_number}:** {content}")
        # Detect bullet points: "- ", "* ", "• "
        elif stripped.startswith(("-", "*", "•")) and len(stripped) > 2:
            content = stripped[1:].strip()
            formatted_lines.append(f"  - {content}")
        else:
            formatted_lines.append(line)

    return "\n".join(formatted_lines)


def convert_tables_to_markdown(text: str) -> str:
    """Convert table-like structures to markdown tables.

    Detects pipe-separated or tab-separated data and formats as markdown.

    Args:
        text: Document content

    Returns:
        Content with markdown-formatted tables
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    in_table = False
    table_lines: list[str] = []

    for line in lines:
        # Detect table row (has multiple pipes or tabs)
        if line.count("|") >= 2 or line.count("\t") >= 2:
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
        else:
            if in_table:
                # End of table - format it
                result_lines.extend(_format_table(table_lines))
                in_table = False
                table_lines = []
            result_lines.append(line)

    # Handle table at end of document
    if in_table and table_lines:
        result_lines.extend(_format_table(table_lines))

    return "\n".join(result_lines)


def _format_table(lines: list) -> list:
    """Format detected table lines as markdown table."""
    if not lines:
        return []

    result = []
    for i, line in enumerate(lines):
        # Normalize to pipe-separated
        if "\t" in line and "|" not in line:
            line = "|" + line.replace("\t", "|") + "|"
        elif not line.startswith("|"):
            line = "|" + line + "|"

        result.append(line)

        # Add header separator after first row
        if i == 0:
            cols = line.count("|") - 1
            result.append("|" + "|".join(["---"] * cols) + "|")

    return result


def clean_pdf_tables(text: str) -> str:
    """Post-process markdown tables from PDF extraction.

    Large tables (>5 rows AND >4 columns) are converted to labeled record format
    for better embedding/retrieval. Small tables are left as markdown pipe tables.

    Args:
        text: Document content with markdown tables

    Returns:
        Content with large tables converted to record format
    """
    lines = text.split("\n")
    result_lines: list[str] = []
    table_lines: list[str] = []
    in_table = False

    for line in lines:
        is_table_line = line.strip().startswith("|") and line.strip().endswith("|")

        if is_table_line:
            if not in_table:
                in_table = True
                table_lines = []
            table_lines.append(line)
        else:
            if in_table:
                result_lines.extend(_process_pdf_table(table_lines))
                in_table = False
                table_lines = []
            result_lines.append(line)

    if in_table and table_lines:
        result_lines.extend(_process_pdf_table(table_lines))

    return "\n".join(result_lines)


def _process_pdf_table(table_lines: list[str]) -> list[str]:
    """Process a single markdown table block.

    Large tables (>5 data rows AND >4 columns) are converted to labeled records.
    Small tables are returned as-is.

    Args:
        table_lines: Lines of a markdown pipe table

    Returns:
        Processed lines (either original table or record format)
    """
    if not table_lines:
        return []

    # Parse rows: split by | and strip whitespace
    parsed_rows: list[list[str]] = []
    separator_indices: list[int] = []

    for i, line in enumerate(table_lines):
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        # Detect separator rows (e.g. |---|---|---|)
        if all(re.match(r"^[-:]+$", c.strip()) for c in cells if c.strip()):
            separator_indices.append(i)
        else:
            parsed_rows.append(cells)

    if not parsed_rows:
        return table_lines

    headers = parsed_rows[0]
    data_rows = parsed_rows[1:]
    num_columns = len(headers)

    # Small table: return as-is
    if len(data_rows) <= 5 or num_columns <= 4:
        return table_lines

    # Large table: convert to labeled record format
    result: list[str] = []
    for row_idx, row in enumerate(data_rows, start=1):
        # Use first cell value as row identifier if it looks like a label
        first_cell = row[0].strip() if row else ""
        row_label = first_cell if first_cell else f"Row {row_idx}"
        result.append(f"### Row {row_idx}: {row_label}")

        for col_idx, header in enumerate(headers):
            if col_idx >= len(row):
                continue
            value = row[col_idx].strip()
            if not value:
                continue
            # Skip if this is the same as the row label (first column)
            if col_idx == 0:
                continue
            label = header.strip() if header.strip() else f"Col{col_idx + 1}"
            result.append(f"- {label}: {value}")

        result.append("")  # Blank line between records

    return result


def normalize_whitespace(text: str) -> str:
    """Normalize whitespace while preserving structure.

    - Removes trailing whitespace
    - Normalizes multiple blank lines to single blank line
    - Preserves code block formatting

    Args:
        text: Document content

    Returns:
        Normalized content
    """
    lines = text.split("\n")
    result_lines = []
    prev_blank = False
    in_code_block = False

    for line in lines:
        # Track code blocks to preserve their formatting
        if line.strip().startswith("```"):
            in_code_block = not in_code_block

        if in_code_block:
            result_lines.append(line)
            prev_blank = False
        else:
            stripped = line.rstrip()
            is_blank = len(stripped.strip()) == 0

            if is_blank:
                if not prev_blank:
                    result_lines.append("")
                prev_blank = True
            else:
                result_lines.append(stripped)
                prev_blank = False

    return "\n".join(result_lines)


@register_step("preprocess_document")
async def preprocess_document(context: StepContext) -> StepResult:
    """Apply type-specific preprocessing to document content.

    Preprocessing varies by document type:
    - support_example: PII masking (emails, phones)
    - sop: Step extraction and formatting
    - technical: Table and code block formatting
    - pdf: Minimal (already preprocessed by pymupdf4llm)
    - all: Whitespace normalization

    Args:
        context: Step execution context

    Returns:
        StepResult with cleaned_content and preprocessing stats
    """
    content = context.get_state("document_content")
    doc_type = context.get_state("detected_doc_type") or "technical"
    file_type = context.get_state("file_type") or ""

    if not content:
        return StepResult.failure("No document content available for preprocessing")

    LOGGER.info(f"Preprocessing document of type '{doc_type}' (file_type: {file_type})")

    pii_count = 0
    cleaned = content

    # PDFs are already preprocessed by pymupdf4llm (outputs markdown with tables)
    # Only apply minimal cleanup
    if file_type == "pdf":
        LOGGER.info("PDF already preprocessed by pymupdf4llm, applying table cleanup")
        cleaned = clean_pdf_tables(cleaned)
        cleaned = normalize_whitespace(cleaned)
        # Still apply PII masking if it's a support example
        if doc_type == "support_example":
            cleaned, pii_count = mask_pii(cleaned)
    else:
        # Apply type-specific preprocessing for non-PDF files
        if doc_type == "support_example":
            cleaned, pii_count = mask_pii(cleaned)
            LOGGER.info(f"Masked {pii_count} PII items")

        elif doc_type == "sop":
            cleaned = extract_steps(cleaned)

        elif doc_type == "technical":
            cleaned = convert_tables_to_markdown(cleaned)

        # Always normalize whitespace
        cleaned = normalize_whitespace(cleaned)

    # Calculate compression ratio
    original_len = len(content)
    cleaned_len = len(cleaned)
    compression = (1 - cleaned_len / original_len) * 100 if original_len > 0 else 0

    LOGGER.info(
        f"Preprocessing complete: {original_len} -> {cleaned_len} chars "
        f"({compression:.1f}% reduction)"
    )

    return StepResult(
        data={
            "cleaned_content": cleaned,
            "pii_masked_count": pii_count,
            "original_length": original_len,
            "cleaned_length": cleaned_len,
        },
        state_updates={
            "cleaned_content": cleaned,
            "pii_masked_count": pii_count,
        },
        progress_message=f"Preprocessed: {pii_count} PII masked" if pii_count else "Preprocessed",
    )
