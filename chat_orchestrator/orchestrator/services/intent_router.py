"""Low-cost structured intent routing for natural-language expert requests."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from orchestrator.config.settings import get_settings
from orchestrator.services.command_registry import get_expert_command_mapping
from shared.llm import GeminiGateway, GenerationOptions, LLMMessage
from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

MIN_INTENT_CONFIDENCE = 0.8
_EXPERT_INTENT_PREFILTER_TERMS = (
    "lpp",
    "preliminary package",
    "package",
    "kpi",
    "report",
    "analysis",
    "analyze",
    "analyse",
    "review",
)


def _intent_router_model() -> str:
    return os.getenv("INTENT_ROUTER_MODEL") or get_settings().gemini.fallback_model


def _clean_json_text(text: str) -> str:
    clean = text.strip()
    if clean.startswith("```"):
        lines = clean.splitlines()
        json_lines = [
            line for line in lines if not line.strip().startswith("```") and line.strip() != "json"
        ]
        clean = "\n".join(json_lines).strip()
    return clean


def should_try_intent_router(user_input: str) -> bool:
    input_lower = user_input.lower()
    return any(term in input_lower for term in _EXPERT_INTENT_PREFILTER_TERMS)


def _normalize_route(data: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not data.get("should_route_to_expert"):
        return None

    confidence = float(data.get("confidence") or 0)
    if confidence < MIN_INTENT_CONFIDENCE:
        return None

    packet_type = str(data.get("packet_type") or "").strip()
    command = str(data.get("command") or "").strip()
    if not command.startswith("/"):
        command = f"/{command}" if command else ""

    expert_commands = get_expert_command_mapping()
    if not command or expert_commands.get(command) != packet_type:
        return None

    fields = {
        "command": command,
        "packet_type": packet_type,
        "key_entity": str(data.get("key_entity") or "").strip(),
        "args": str(data.get("args") or "").strip(),
        "raw_request": str(data.get("raw_request") or "").strip(),
    }

    latitude = str(data.get("latitude") or "").strip()
    longitude = str(data.get("longitude") or "").strip()
    if latitude and longitude:
        fields["args"] = f"{latitude},{longitude}"
        fields["key_entity"] = fields["key_entity"] or fields["args"]

    if not fields["args"] and fields["key_entity"]:
        fields["args"] = fields["key_entity"]

    if packet_type == "light_preliminary_package" and not fields["args"]:
        return None

    if not fields["raw_request"]:
        fields.pop("raw_request")

    return fields


async def route_expert_intent(user_input: str) -> Optional[Dict[str, str]]:
    """Return a structured expert route for clear natural-language requests.

    This is intentionally fail-open: API errors, malformed JSON, low confidence,
    or unsupported commands all return None so normal chat handling can continue.
    """
    model = _intent_router_model()
    api_key = os.getenv("GOOGLE_API_KEY")
    if (
        not model
        or not api_key
        or not user_input.strip()
        or not should_try_intent_router(user_input)
    ):
        return None

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    prompt = (
        f"TODAY'S DATE AND TIME: {now_str}\n\n"
        "Decide whether this staff message is clearly asking to start an expert workflow.\n"
        "Only route when the intent is explicit. Do not route ordinary questions, status checks, "
        "or vague requests.\n\n"
        "Supported expert workflows:\n"
        "- /lpp -> light_preliminary_package: create or generate a Light Preliminary Package "
        "for a named site or GPS coordinates.\n\n"
        f"Message: {user_input[:1000]!r}\n\n"
        "Return only JSON with this shape:\n"
        "{"
        '"should_route_to_expert": false, '
        '"confidence": 0.0, '
        '"command": "/lpp", '
        '"packet_type": "light_preliminary_package", '
        '"key_entity": "site name or coordinate pair, if present", '
        '"args": "slash-command arguments, if present", '
        '"latitude": "decimal latitude, if present", '
        '"longitude": "decimal longitude, if present", '
        '"raw_request": "original user message"'
        "}"
    )

    try:
        gateway = GeminiGateway(api_key=api_key, default_model=model)
        response = await gateway.generate(
            [LLMMessage(role="user", text=prompt)],
            GenerationOptions(
                model=model,
                temperature=0.0,
                max_output_tokens=256,
                response_format="json",
            ),
        )
        if not response.text:
            return None
        data = json.loads(_clean_json_text(response.text))
        if not isinstance(data, dict):
            return None
        route = _normalize_route(data)
        if route and not route.get("raw_request"):
            route["raw_request"] = user_input
        return route
    except Exception as e:
        LOGGER.warning(f"Intent router model failed; continuing without expert route: {e}")
        return None


__all__ = ["route_expert_intent", "should_try_intent_router"]
