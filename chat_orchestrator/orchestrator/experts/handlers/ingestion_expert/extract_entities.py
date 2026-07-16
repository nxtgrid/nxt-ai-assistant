"""Extract entities step handler for Document Ingestion Expert.

Uses Gemini LLM to extract entities and relationships from document content
for GraphRAG. This enables semantic search and knowledge graph queries.
"""

import json
import os
import re
from typing import Any, Dict, List

from orchestrator.config.settings import get_settings
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Use env var for max tokens, default to 4096 for entity extraction
# (higher than classification since entity lists can be long)
ENTITY_EXTRACTION_MAX_TOKENS = int(os.getenv("GEMINI_MAX_OUTPUT_TOKENS", "4096"))

EXTRACTION_PROMPT = """Analyze this document and extract key entities and their relationships.

**Entity Types to Extract:**
- person: People mentioned (names, roles)
- organization: Companies, teams, departments
- concept: Business concepts, processes, methodologies
- technology: Software, tools, systems, APIs
- location: Places, regions, grid locations
- product: Products, services, offerings
- metric: KPIs, measurements, statistics

**Relationship Types:**
- uses: Entity uses another entity
- belongs_to: Entity is part of another
- related_to: General relationship
- manages: Person/team manages something
- produces: Entity produces/creates another
- depends_on: Entity depends on another

Document content:
---
{content}
---

Return a JSON object with:
- entities: array of objects with name, type, and description
- relationships: array of objects with source, target, type, and description"""


def repair_json(text: str) -> str:
    """Repair common JSON issues from LLM output.

    Args:
        text: Raw JSON text that may have issues

    Returns:
        Repaired JSON text
    """
    # Remove markdown code blocks if present
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
        text = text.strip()

    # Fix trailing commas before ] or } (common LLM JSON issue)
    text = re.sub(r",\s*([}\]])", r"\1", text)

    # Fix single quotes to double quotes for keys and values
    # Use capturing groups instead of variable-width look-behinds (not supported in Python re)
    text = re.sub(r"([{,]\s*)'([^']+)'(\s*:)", r'\1"\2"\3', text)  # Keys: {'key': -> {"key":
    text = re.sub(r"(:\s*)'([^']*)'(\s*[,}\]])", r'\1"\2"\3', text)  # Values: : 'val', -> : "val",

    # Try to fix truncated JSON by closing open structures
    # Count braces and brackets
    open_braces = text.count("{") - text.count("}")
    open_brackets = text.count("[") - text.count("]")

    # If truncated mid-string, try to close it
    if open_braces > 0 or open_brackets > 0:
        # Check if we're in an unclosed string
        quote_count = text.count('"') - text.count('\\"')
        if quote_count % 2 == 1:
            text += '"'

        # Close any open arrays/objects
        text += "]" * open_brackets
        text += "}" * open_braces

    return text


async def extract_with_gemini(content: str) -> Dict[str, Any]:
    """Extract entities using Gemini LLM.

    Args:
        content: Document content (truncated to 8000 chars)

    Returns:
        Dict with entities and relationships lists
    """
    model = get_settings().gemini.model
    gateway = get_default_generation_gateway(
        default_model=model,
    )

    prompt = EXTRACTION_PROMPT.format(content=content[:8000])

    response = await gateway.generate(
        [LLMMessage(role="user", text=prompt)],
        GenerationOptions(
            model=model,
            temperature=0.1,
            max_output_tokens=ENTITY_EXTRACTION_MAX_TOKENS,
            response_format="json",
        ),
    )

    # Handle empty response (can happen with safety filters or API issues)
    if not response.text:
        LOGGER.warning("Gemini returned empty response for entity extraction")
        return {"entities": [], "relationships": []}

    response_text = response.text.strip()

    # Apply JSON repairs for common LLM issues
    response_text = repair_json(response_text)

    result: Dict[str, Any] = json.loads(response_text)
    return result


def validate_entities(entities: List[Dict]) -> List[Dict]:
    """Validate and clean extracted entities.

    Args:
        entities: Raw extracted entities

    Returns:
        Validated entity list
    """
    valid_types = {
        "person",
        "organization",
        "concept",
        "technology",
        "location",
        "product",
        "metric",
    }
    validated = []

    for entity in entities:
        name = entity.get("name", "").strip()
        etype = entity.get("type", "concept").lower().strip()
        description = entity.get("description", "").strip()

        # Skip empty names
        if not name or len(name) < 2:
            continue

        # Normalize type
        if etype not in valid_types:
            etype = "concept"

        validated.append(
            {
                "name": name,
                "type": etype,
                "description": description or f"{etype}: {name}",
            }
        )

    return validated


def validate_relationships(relationships: List[Dict], entity_names: set) -> List[Dict]:
    """Validate and clean extracted relationships.

    Args:
        relationships: Raw extracted relationships
        entity_names: Set of valid entity names

    Returns:
        Validated relationship list
    """
    valid_types = {
        "uses",
        "belongs_to",
        "related_to",
        "manages",
        "produces",
        "depends_on",
    }
    validated = []

    for rel in relationships:
        source = rel.get("source", "").strip()
        target = rel.get("target", "").strip()
        rtype = rel.get("type", "related_to").lower().strip()
        description = rel.get("description", "").strip()

        # Skip if source or target not in entities
        if source not in entity_names or target not in entity_names:
            continue

        # Skip self-relationships
        if source == target:
            continue

        # Normalize type
        if rtype not in valid_types:
            rtype = "related_to"

        validated.append(
            {
                "source": source,
                "target": target,
                "type": rtype,
                "description": description or f"{source} {rtype} {target}",
            }
        )

    return validated


@register_step("extract_entities")
async def extract_entities(context: StepContext) -> StepResult:
    """Extract entities and relationships from document content.

    Uses Gemini LLM to identify entities (people, orgs, concepts, tech)
    and their relationships for GraphRAG indexing. Retries once if the
    first attempt produces 0 validated entities.

    Args:
        context: Step execution context

    Returns:
        StepResult with entities and relationships lists
    """
    content = context.get_state("cleaned_content")
    if not content:
        content = context.get_state("document_content")

    if not content:
        return StepResult.failure("No document content available for entity extraction")

    await context.send_progress_to_user("Extracting entities and relationships...")

    max_attempts = 2
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            result = await extract_with_gemini(content)

            raw_entities = result.get("entities", [])
            raw_relationships = result.get("relationships", [])

            entities = validate_entities(raw_entities)

            if entities or attempt == max_attempts:
                # Got results, or exhausted retries
                entity_names = {e["name"] for e in entities}
                relationships = validate_relationships(raw_relationships, entity_names)

                if attempt > 1:
                    LOGGER.info(f"Entity extraction succeeded on attempt {attempt}")

                LOGGER.info(
                    f"Extracted {len(entities)} entities and {len(relationships)} relationships"
                )

                return StepResult(
                    data={
                        "entities": entities,
                        "relationships": relationships,
                        "raw_entity_count": len(raw_entities),
                        "raw_relationship_count": len(raw_relationships),
                    },
                    state_updates={
                        "extracted_entities": entities,
                        "extracted_relationships": relationships,
                    },
                    progress_message=f"Found {len(entities)} entities, {len(relationships)} relationships",
                )

            # No entities on first attempt - retry
            LOGGER.warning(f"Attempt {attempt}: 0 entities extracted, retrying...")

        except json.JSONDecodeError as e:
            last_error = str(e)
            if attempt < max_attempts:
                LOGGER.warning(f"Attempt {attempt}: JSON parse failed, retrying...")
                continue
            LOGGER.error(f"Failed to parse entity extraction response: {e}")

        except Exception as e:
            last_error = str(e)
            if attempt < max_attempts:
                LOGGER.warning(f"Attempt {attempt}: {e}, retrying...")
                continue
            LOGGER.exception(f"Entity extraction failed: {e}")

    # All attempts exhausted - return empty results (don't fail workflow)
    return StepResult(
        data={"entities": [], "relationships": []},
        state_updates={
            "extracted_entities": [],
            "extracted_relationships": [],
            "extraction_error": last_error,
        },
        progress_message="Entity extraction produced no results",
    )
