"""Propose a context module's identity (slug, title, summary) from its body.

The summary is how an operator recognises this module in the Context picker
and on the Prompts page without opening it -- the body itself is inlined into
every prompt the module is attached to.
"""

import json
import re
from typing import Dict

from orchestrator.experts.step_context import StepContext, StepResult
from orchestrator.experts.step_registry import register_step
from shared.llm import GenerationOptions, LLMMessage, get_default_generation_gateway
from shared.llm.model_tiers import resolve_model
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

PROPOSAL_PROMPT = (
    "You are naming a reusable knowledge module for an off-grid solar operations "
    "assistant. Given the content below, reply with JSON only:\n"
    '{"slug": "kebab-case-identifier", "title": "Human Readable Title", '
    '"summary": "One sentence, max 20 words, naming the specific equipment, '
    'standard or calculation covered."}\n\n'
    "The summary is the only thing an AI sees before deciding whether to load the "
    "full module, so make it specific.\n\nContent:\n{body}"
)


def normalize_slug(text: str) -> str:
    """Kebab-case identifier. Raises if nothing usable survives."""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if not slug:
        raise ValueError(f"empty slug derived from {text!r}")
    return slug


def parse_proposal(raw: str) -> Dict[str, str]:
    """Validate the LLM's JSON proposal and normalize the slug."""
    try:
        data = json.loads(raw.strip().removeprefix("```json").removesuffix("```").strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"proposal is not valid JSON: {raw[:120]!r}") from e
    for field in ("slug", "title", "summary"):
        if not str(data.get(field, "")).strip():
            raise ValueError(f"proposal is missing '{field}'")
    return {
        "slug": normalize_slug(str(data["slug"])),
        "title": str(data["title"]).strip(),
        "summary": str(data["summary"]).strip(),
    }


@register_step("propose_module")
async def propose_module(context: StepContext) -> StepResult:
    """Draft slug/title/summary for the content gathered so far."""
    body = context.get_state("improved_content") or context.get_state("document_content") or ""
    if not body.strip():
        return StepResult.failure("No content to build a context module from.")

    model = resolve_model("fast")
    gateway = get_default_generation_gateway(default_model=model)
    response = await gateway.generate(
        [LLMMessage(role="user", text=PROPOSAL_PROMPT.format(body=body[:8000]))],
        GenerationOptions(model=model, temperature=0.2, response_format="json"),
    )

    try:
        proposal = parse_proposal(response.text or "")
    except ValueError as e:
        LOGGER.warning(f"Module proposal failed, falling back to title heuristic: {e}")
        first_line = body.strip().split("\n")[0].lstrip("#").strip()[:60] or "Untitled module"
        proposal = {
            "slug": normalize_slug(first_line),
            "title": first_line,
            "summary": f"{first_line}.",
        }

    return StepResult(
        data=proposal,
        state_updates={
            "module_slug": proposal["slug"],
            "module_title": proposal["title"],
            "module_summary": proposal["summary"],
            "module_body": body,
        },
        progress_message=f"Proposed module: {proposal['title']}",
    )
