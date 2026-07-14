"""Procedure Provider - Extracts and manages procedures from Customer Support Doc.

This service parses procedures from the Customer Support Google Doc and provides
matching capabilities for support example ingestion. Procedures follow the format:

    ## Procedure N: Title

    ### Purpose
    Description of what this procedure covers...

    ### Prerequisites
    Required conditions...

    ### Procedure Steps
    1. Step one...
    2. Step two...
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import List, Optional

from orchestrator.config.settings import get_settings
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)


@dataclass
class Procedure:
    """Represents a procedure from the Customer Support Doc."""

    id: str  # "procedure_1"
    number: int  # 1
    title: str  # "Commissioning Failed Troubleshooting"
    purpose: str  # From ### Purpose section
    full_text: str  # Full procedure content


class ProcedureProvider:
    """Extracts and provides access to procedures from Customer Support Doc.

    Procedures are defined in the CUSTOMER_SUPPORT_DOC_ID Google Doc using
    a specific format with ## Procedure N: Title headers.
    """

    def __init__(self) -> None:
        """Initialize the provider."""
        self._cached_procedures: Optional[List[Procedure]] = None

    def get_procedures(self, force_reload: bool = False) -> List[Procedure]:
        """Fetch and parse procedures from CUSTOMER_SUPPORT_DOC_ID.

        Args:
            force_reload: If True, bypass cache and reload from Google Doc

        Returns:
            List of Procedure objects found in the document
        """
        if self._cached_procedures is not None and not force_reload:
            LOGGER.debug(f"Returning {len(self._cached_procedures)} cached procedures")
            return self._cached_procedures

        doc_id = os.getenv("CUSTOMER_SUPPORT_DOC_ID", "").strip()
        if not doc_id:
            LOGGER.warning("CUSTOMER_SUPPORT_DOC_ID not configured")
            return []

        try:
            from shared.utils.gdrive_doc_fetcher import fetch_google_doc_markdown

            content = fetch_google_doc_markdown(doc_id)
            if not content:
                LOGGER.error(f"Failed to fetch Customer Support Doc: {doc_id}")
                return []

            procedures = self._parse_procedures(content)
            self._cached_procedures = procedures

            LOGGER.info(f"Parsed {len(procedures)} procedures from Customer Support Doc")
            return procedures

        except Exception as e:
            LOGGER.exception(f"Error fetching procedures: {e}")
            return []

    def clear_cache(self) -> None:
        """Clear the cached procedures to force reload on next call."""
        self._cached_procedures = None
        LOGGER.info("Cleared procedure cache")

    def _parse_procedures(self, content: str) -> List[Procedure]:
        """Parse procedures from markdown content.

        Looks for pattern:
            ## Procedure N: Title
            ...content...
            ### Purpose
            ...purpose text...

        Args:
            content: Markdown content from Google Doc

        Returns:
            List of Procedure objects
        """
        procedures = []

        # Pattern to match procedure headers: ## Procedure N: Title
        # Handles variations like "## Procedure 1: Title" or "## Procedure 12: Title"
        procedure_pattern = re.compile(
            r"^##\s+Procedure\s+(\d+):\s*(.+?)$",
            re.MULTILINE | re.IGNORECASE,
        )

        # Find all procedure headers with their positions
        matches = list(procedure_pattern.finditer(content))

        for i, match in enumerate(matches):
            number = int(match.group(1))
            title = match.group(2).strip()
            start_pos = match.start()

            # Find end position (start of next procedure or end of content)
            if i + 1 < len(matches):
                end_pos = matches[i + 1].start()
            else:
                end_pos = len(content)

            # Extract full procedure text
            full_text = content[start_pos:end_pos].strip()

            # Extract purpose section
            purpose = self._extract_purpose(full_text)

            procedure = Procedure(
                id=f"procedure_{number}",
                number=number,
                title=title,
                purpose=purpose,
                full_text=full_text,
            )
            procedures.append(procedure)

            LOGGER.debug(f"Parsed procedure {number}: {title[:50]}...")

        return procedures

    def _extract_purpose(self, procedure_text: str) -> str:
        """Extract the Purpose section from procedure text.

        Args:
            procedure_text: Full text of a single procedure

        Returns:
            Content of the Purpose section, or empty string if not found
        """
        # Look for ### Purpose section
        purpose_pattern = re.compile(
            r"###\s+Purpose\s*\n(.*?)(?=###|\Z)",
            re.DOTALL | re.IGNORECASE,
        )

        match = purpose_pattern.search(procedure_text)
        if match:
            return match.group(1).strip()

        return ""


async def generate_suggested_procedure(
    content: str,
    existing_procedures: List[Procedure],
) -> str:
    """Use Gemini to generate a suggested procedure from support example content.

    Args:
        content: The support example document content
        existing_procedures: List of existing procedures for context

    Returns:
        Markdown-formatted suggested procedure text
    """
    from google import genai

    client = genai.Client(
        api_key=os.getenv("GOOGLE_API_KEY"),
        http_options={"timeout": 30_000},
    )
    model = get_settings().gemini.model

    # Build context about existing procedures
    existing_list = "\n".join(f"- Procedure {p.number}: {p.title}" for p in existing_procedures)
    next_number = max((p.number for p in existing_procedures), default=0) + 1

    prompt = f"""Analyze this support conversation example and generate a suggested procedure
that could be added to a Customer Support documentation.

IMPORTANT: Use standard markdown with ## and ### for headers.
Do NOT use *asterisks* for bold - use proper ## headers instead.

The procedure should follow this exact format:

## Procedure {next_number}: [Title]

### Purpose

[1-2 sentence description of what issue/scenario this procedure addresses]

### Prerequisites

- [List any required access, tools, or conditions needed]

### Procedure Steps

1. [First step]
2. [Second step]
...

EXISTING PROCEDURES (for context, avoid duplicating):
{existing_list}

SUPPORT EXAMPLE CONTENT:
{content[:8000]}

Generate ONLY the procedure markdown using ## headers. No *asterisk bold*. No explanation."""

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
        )

        if response.text:
            return str(response.text).strip()

        LOGGER.warning("Empty response from Gemini for procedure generation")
        return ""

    except Exception as e:
        LOGGER.exception(f"Error generating suggested procedure: {e}")
        return ""


async def match_content_to_procedures(
    content: str,
    procedures: List[Procedure],
) -> Optional[tuple[Procedure, float]]:
    """Use Gemini to match support example content to a procedure.

    Args:
        content: The support example document content
        procedures: List of available procedures to match against

    Returns:
        Tuple of (matched_procedure, confidence) if match found with confidence >= 0.7,
        None otherwise
    """
    if not procedures:
        return None

    from google import genai

    client = genai.Client(
        api_key=os.getenv("GOOGLE_API_KEY"),
        http_options={"timeout": 30_000},
    )
    model = get_settings().gemini.model

    # Build procedure descriptions for matching
    procedure_descriptions = "\n\n".join(
        f"PROCEDURE {p.number}: {p.title}\nPurpose: {p.purpose or 'No purpose specified'}"
        for p in procedures
    )

    prompt = f"""Analyze this support conversation example and determine which procedure it best matches.

AVAILABLE PROCEDURES:
{procedure_descriptions}

SUPPORT EXAMPLE CONTENT (first 4000 chars):
{content[:4000]}

Respond in this exact format:
MATCH: [procedure number, or NONE if no good match]
CONFIDENCE: [0.0 to 1.0]
REASONING: [1 sentence explanation]

Only output MATCH with a procedure number if you are confident the support example
demonstrates or is directly related to that procedure. If the content doesn't clearly
fit any procedure, output MATCH: NONE."""

    try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=prompt,
        )

        if not response.text:
            LOGGER.warning("Empty response from Gemini for procedure matching")
            return None

        # Parse response
        text = response.text.strip()
        match_line = None
        confidence_line = None

        for line in text.split("\n"):
            line = line.strip()
            if line.upper().startswith("MATCH:"):
                match_line = line.split(":", 1)[1].strip()
            elif line.upper().startswith("CONFIDENCE:"):
                confidence_line = line.split(":", 1)[1].strip()

        if not match_line or match_line.upper() == "NONE":
            LOGGER.info("No procedure match found")
            return None

        # Parse procedure number
        try:
            proc_num = int(re.search(r"\d+", match_line).group())
        except (AttributeError, ValueError):
            LOGGER.warning(f"Could not parse procedure number from: {match_line}")
            return None

        # Parse confidence
        try:
            confidence = float(re.search(r"[\d.]+", confidence_line or "0").group())
        except (AttributeError, ValueError):
            confidence = 0.0

        # Find matching procedure
        matched_proc = next((p for p in procedures if p.number == proc_num), None)
        if not matched_proc:
            LOGGER.warning(f"Procedure {proc_num} not found in list")
            return None

        if confidence < 0.7:
            LOGGER.info(f"Procedure {proc_num} match confidence {confidence:.2f} below threshold")
            return None

        LOGGER.info(
            f"Matched to Procedure {proc_num}: {matched_proc.title} (confidence: {confidence:.2f})"
        )
        return (matched_proc, confidence)

    except Exception as e:
        LOGGER.exception(f"Error matching content to procedures: {e}")
        return None


__all__ = [
    "Procedure",
    "ProcedureProvider",
    "generate_suggested_procedure",
    "match_content_to_procedures",
]
