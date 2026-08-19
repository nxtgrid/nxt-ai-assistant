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

import re
from dataclasses import dataclass
from typing import Any, List, Optional

from orchestrator.config.settings import get_settings
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.prompts import PROMPTS
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
    """Provides access to procedures, read from context modules.

    Procedures used to be parsed out of the customer.system prompt body
    (see _parse_procedures below); they now live as individual
    knowledge_modules rows tagged 'procedure', fetched on demand instead of
    riding in every conversation. See
    docs/superpowers/specs/2026-08-19-procedures-to-context-modules-design.md.
    _parse_procedures is kept -- the migration script that created those
    rows in the first place (scripts/migrate_procedures_to_modules.py)
    calls it, and removing it would break re-running that migration.
    """

    PROCEDURE_TAG = "procedure"

    def __init__(self, store: Any = None) -> None:
        self._store = store

    def _knowledge_store(self):
        if self._store is None:
            from shared.prompts.knowledge import KnowledgeStore

            self._store = KnowledgeStore.from_env()
        return self._store

    def get_procedures(self, force_reload: bool = False) -> List[Procedure]:
        """Procedures, read from the context modules tagged 'procedure'.

        Args:
            force_reload: If True, invalidate the knowledge store's cache
                and re-fetch before returning.

        Returns:
            List of Procedure objects, one per procedure-tagged module.
        """
        store = self._knowledge_store()
        if force_reload and hasattr(store, "invalidate"):
            store.invalidate()

        try:
            modules = store.all_modules()
        except Exception as e:
            LOGGER.exception(f"Error fetching procedure modules: {e}")
            return []

        procedures = [
            Procedure(
                # Slug, not a positional index: chunk_procedure_map persists
                # these ids, so a reordered list must not remap them.
                id=module.slug,
                number=index,
                title=module.title,
                purpose=module.summary,
                full_text=module.body or "",
            )
            for index, module in enumerate(
                sorted(
                    (m for m in modules if self.PROCEDURE_TAG in (m.tags or [])),
                    key=lambda m: m.slug,
                ),
                start=1,
            )
        ]
        LOGGER.info(f"Loaded {len(procedures)} procedures from context modules")
        return procedures

    def clear_cache(self) -> None:
        """Force the next get_procedures() call to re-fetch rather than use
        the knowledge store's cached module list."""
        store = self._knowledge_store()
        if hasattr(store, "invalidate"):
            store.invalidate()
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
    model = get_settings().gemini.model
    gateway = get_default_generation_gateway(default_model=model)

    # Build context about existing procedures
    existing_list = "\n".join(f"- Procedure {p.number}: {p.title}" for p in existing_procedures)
    next_number = max((p.number for p in existing_procedures), default=0) + 1

    prompt = PROMPTS.text(
        "procedure.suggest",
        next_number=next_number,
        existing_list=existing_list,
        content=content[:8000],
    )

    try:
        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(model=model),
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

    model = get_settings().gemini.model
    gateway = get_default_generation_gateway(default_model=model)

    # Build procedure descriptions for matching
    procedure_descriptions = "\n\n".join(
        f"PROCEDURE {p.number}: {p.title}\nPurpose: {p.purpose or 'No purpose specified'}"
        for p in procedures
    )

    prompt = PROMPTS.text(
        "procedure.match",
        procedure_descriptions=procedure_descriptions,
        content=content[:4000],
    )

    try:
        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(model=model),
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
