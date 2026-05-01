"""Entity tagger for message context management.

Extracts known entities from message text using fuzzy matching and regex patterns.
No LLM calls — purely programmatic extraction for speed and reliability.

Entity types:
- Grid names: Fuzzy matched via grid_matcher (80% threshold)
- Ticket keys: Regex pattern [A-Z]+-\\d+
- Meter numbers: Regex pattern \\d{11,13}
- Jira assignees: Case-insensitive substring match

Usage:
    tagger = EntityTagger(grid_names=["ExampleGrid", "ExampleGrid2"], assignee_names=["John"])
    entities = tagger.extract_entities("Check ExampleGrid and ticket OPS-123")
    # Returns: ["grid:ExampleGrid", "ticket:OPS-123"]
"""

from __future__ import annotations

import re
from typing import List, Optional

from shared.utils.grid_matcher import find_best_grid_match
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Regex patterns for entity extraction
TICKET_KEY_PATTERN = re.compile(r"\b([A-Z]{2,10}-\d+)\b")
METER_NUMBER_PATTERN = re.compile(r"\b(\d{11,13})\b")


class EntityTagger:
    """Extracts known entities from message text using programmatic matching."""

    def __init__(
        self,
        grid_names: Optional[List[str]] = None,
        assignee_names: Optional[List[str]] = None,
    ) -> None:
        self._grid_names = grid_names or []
        self._assignee_names = assignee_names or []

    def extract_entities(self, text: str) -> List[str]:
        """Extract entities from message text.

        Returns list of tagged entities like ["grid:ExampleGrid", "ticket:OPS-123"].
        """
        if not text:
            return []

        entities: List[str] = []

        # Extract ticket keys (e.g., OPS-123, PD-456)
        for match in TICKET_KEY_PATTERN.finditer(text):
            entities.append(f"ticket:{match.group(1)}")

        # Extract meter numbers (11-13 digit numbers)
        for match in METER_NUMBER_PATTERN.finditer(text):
            entities.append(f"meter:{match.group(1)}")

        # Extract grid names via fuzzy matching
        entities.extend(self._extract_grid_names(text))

        # Extract assignee names via case-insensitive substring
        entities.extend(self._extract_assignees(text))

        return entities

    def _extract_grid_names(self, text: str) -> List[str]:
        """Extract grid names from text using fuzzy matching."""
        if not self._grid_names:
            return []

        found: List[str] = []
        # Split into potential name tokens (words and word pairs)
        words = text.split()
        candidates = list(words)
        # Also check consecutive word pairs (for multi-word grid names)
        for i in range(len(words) - 1):
            candidates.append(f"{words[i]} {words[i + 1]}")
        # And triples
        for i in range(len(words) - 2):
            candidates.append(f"{words[i]} {words[i + 1]} {words[i + 2]}")

        seen = set()
        for candidate in candidates:
            # Skip very short candidates and common words
            if len(candidate) < 3:
                continue
            matched_name, _was_fuzzy, score = find_best_grid_match(
                candidate, self._grid_names, threshold=80
            )
            if matched_name and matched_name not in seen:
                seen.add(matched_name)
                found.append(f"grid:{matched_name}")

        return found

    def _extract_assignees(self, text: str) -> List[str]:
        """Extract assignee names via case-insensitive substring match."""
        if not self._assignee_names:
            return []

        text_lower = text.lower()
        found: List[str] = []
        for name in self._assignee_names:
            if name.lower() in text_lower:
                found.append(f"assignee:{name}")

        return found


async def create_entity_tagger_from_context(
    organization_ids: List[str],
    is_staff: bool,
) -> EntityTagger:
    """Create an EntityTagger with entity lists from auth service.

    Reuses the same data sources as ContextEnrichmentProvider.
    """
    from shared.auth import get_auth_service

    grid_names: List[str] = []
    try:
        auth_service = get_auth_service()
        if is_staff:
            grid_names = await auth_service.get_grid_names_for_organization(include_all=True)
        elif organization_ids:
            grid_names = await auth_service.get_grid_names_for_organization(
                organization_id=organization_ids[0]
            )
    except Exception as e:
        LOGGER.warning(f"Failed to fetch grid names for entity tagger: {e}")

    return EntityTagger(grid_names=grid_names)


__all__ = ["EntityTagger", "create_entity_tagger_from_context"]
