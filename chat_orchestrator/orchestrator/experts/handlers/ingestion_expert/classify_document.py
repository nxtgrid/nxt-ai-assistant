"""Classify document step handler for Document Ingestion Expert.

This handler uses Gemini LLM to classify documents into categories:
- sop: Standard operating procedure with steps
- faq: Frequently asked questions
- support_example: Customer support conversation/ticket
- technical: Technical documentation, specs, API docs
- policy: Company policies, guidelines
"""

import json
import os

from orchestrator.config.settings import get_settings
from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Use higher token limit for thinking models (gemini-flash-latest uses thinking tokens)
CLASSIFICATION_MAX_TOKENS = 1024

CLASSIFICATION_PROMPT = """Classify this document into exactly one category:

Categories:
- **sop**: Standard operating procedure with numbered steps, checklists, or workflows
- **faq**: Frequently asked questions with Q&A format (generic questions, not specific customer interactions)
- **support_example**: Customer support conversations, chat transcripts, ticket exchanges, or documents containing example support interactions. Look for patterns like "User:", "Customer:", "Response:", "Agent:", or actual conversation exchanges between support and customers.
- **technical**: Technical documentation, API specs, system architecture, code docs, implementation guides
- **policy**: Company policies, guidelines, rules, or compliance documents

IMPORTANT: If a document contains EXAMPLES of customer support conversations (even if structured as documentation with headers), classify it as support_example, not technical.

Document (first 4000 characters):
---
{content}
---

Return a JSON object with these fields:
- doc_type: one of the category names above
- confidence: 0.0 to 1.0
- reasoning: brief explanation of why this category fits"""


@register_step("classify_document")
async def classify_document(context: StepContext) -> StepResult:
    """Classify document type using LLM analysis.

    Uses Gemini to analyze document content and determine the most appropriate
    category for preprocessing and embedding strategy.

    Args:
        context: Step execution context

    Returns:
        StepResult with doc_type and confidence
    """
    content = context.get_state("document_content")
    if not content:
        return StepResult.failure("No document content available for classification")

    # Short-circuit: user already selected the type in interactive mode
    if context.get_state("user_selected_doc_type"):
        doc_type = context.get_state("detected_doc_type") or "technical"
        return StepResult(
            data={"doc_type": doc_type, "confidence": 1.0, "reasoning": "User-selected"},
            state_updates={
                "detected_doc_type": doc_type,
                "classification_confidence": 1.0,
                "classification_reasoning": "User-selected document type",
            },
            progress_message=f"Document type: {doc_type} (user-selected)",
        )

    await context.send_progress_to_user("Analyzing document type...")

    # Prepare prompt with first 4000 chars
    prompt = CLASSIFICATION_PROMPT.format(content=content[:4000])

    try:
        model = get_settings().gemini.model
        gateway = GeminiGateway(api_key=os.getenv("GOOGLE_API_KEY"), default_model=model)

        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=model,
                temperature=0.1,
                max_output_tokens=CLASSIFICATION_MAX_TOKENS,
                response_format="json",
            ),
        )

        # Handle empty response (can happen with safety filters or API issues)
        if not response.text:
            LOGGER.warning("Gemini returned empty response for classification")
            return StepResult(
                data={
                    "doc_type": "technical",
                    "confidence": 0.3,
                    "reasoning": "Classification failed: empty API response",
                },
                state_updates={
                    "detected_doc_type": "technical",
                    "classification_confidence": 0.3,
                    "classification_reasoning": "Classification failed: empty API response",
                },
                progress_message="Classification failed (empty response), defaulting to 'technical'",
            )

        response_text = response.text.strip()

        # Remove markdown code blocks if present (e.g., ```json\n{...}\n```)
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            # Remove first line (```json or ```) and last line if it's closing ```
            if lines[-1].strip() == "```":
                response_text = "\n".join(lines[1:-1])
            else:
                response_text = "\n".join(lines[1:])
            response_text = response_text.strip()

        # Parse JSON
        try:
            result = json.loads(response_text)
        except json.JSONDecodeError:
            # Try to extract JSON from response
            import re

            json_match = re.search(r"\{[^}]+\}", response_text)
            if json_match:
                result = json.loads(json_match.group())
            else:
                LOGGER.error(f"Could not parse classification response: {response_text[:200]}")
                # Default to technical with low confidence
                result = {
                    "doc_type": "technical",
                    "confidence": 0.5,
                    "reasoning": "Classification failed, defaulting to technical",
                }

        # Handle Gemini returning a JSON array instead of an object
        if isinstance(result, list):
            result = result[0] if result else {}

        doc_type = result.get("doc_type", "technical")
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "")

        # Validate doc_type
        valid_types = ["sop", "faq", "support_example", "technical", "policy"]
        if doc_type not in valid_types:
            LOGGER.warning(f"Invalid doc_type '{doc_type}', defaulting to 'technical'")
            doc_type = "technical"
            confidence = 0.5

        LOGGER.info(f"Document classified as '{doc_type}' (confidence: {confidence:.2f})")

        return StepResult(
            data={
                "doc_type": doc_type,
                "confidence": confidence,
                "reasoning": reasoning,
            },
            state_updates={
                "detected_doc_type": doc_type,
                "classification_confidence": confidence,
                "classification_reasoning": reasoning,
            },
            progress_message=f"Classified as {doc_type} ({confidence * 100:.0f}% confidence)",
        )

    except Exception as e:
        LOGGER.exception(f"Classification failed: {e}")
        error_reason = f"Classification error: {e}"
        # Don't fail the workflow - default to technical
        return StepResult(
            data={
                "doc_type": "technical",
                "confidence": 0.3,
                "reasoning": error_reason,
            },
            state_updates={
                "detected_doc_type": "technical",
                "classification_confidence": 0.3,
                "classification_reasoning": error_reason,
            },
            progress_message="Classification failed, defaulting to 'technical'",
        )
