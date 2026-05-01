"""LLM-based cell mapping service.

Uses LLM to intelligently match spreadsheet labels (column A) to data keys
from workflow results. This replaces hardcoded mappings with semantic matching.

Usage:
    from orchestrator.services.llm_cell_mapper import LLMCellMapper

    mapper = LLMCellMapper()
    mapping = await mapper.map_labels_to_keys(
        input_labels=["Site Name", "Total kWp", "Number of Poles"],
        available_keys=["site.site_name", "energy.total_kwp", "meta.pole_count"],
    )
    # Returns: {"Site Name": "site.site_name", "Total kWp": "energy.total_kwp", ...}
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

import httpx

from shared.utils.logging import get_logger

LOGGER = get_logger(__name__)

# Gemini API endpoint template
GEMINI_ENDPOINT_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)

# Prompt for LLM mapping
MAPPING_PROMPT = """Match spreadsheet input labels to data keys ONLY when there's a clear semantic match.

INPUT LABELS (from spreadsheet column A):
{input_labels}

AVAILABLE DATA KEYS:
{available_keys}

Instructions:
1. Match ONLY when the label clearly refers to the same concept as the data key
2. Be CONSERVATIVE - if unsure, DO NOT include the label in your response
3. OMIT labels that are:
   - Formulas/calculations (e.g., "Discount", "Margin", "Rate", "Ratio")
   - Financial terms that don't directly map to raw data (e.g., "$/kWp", "$/kWh", "Cash", "Rental")
   - Configuration constants (e.g., "FX Rate", "Inflation", "Fee")
   - Questions (e.g., "All Cash?", "PBG Exists?")
   - Percentages unless the data key also represents a percentage

4. About BOM (Bill of Materials) cost fields:
   - bom.total_cost = total project cost in dollars
   - bom.main_energy_asset_cost = cost of main equipment
   - bom.bos_cost = balance of system cost
   - bom.metering_cost = metering equipment cost
   - These are RAW DOLLAR AMOUNTS, NOT rates, percentages, or per-unit values
   - Only match to labels asking for the actual cost (e.g., "Total BOM Cost", "Equipment Cost")
   - DO NOT match to: margins, discounts, rates, $/kWp, $/kWh, fees, or calculations

5. Good matches (include these):
   - "Site Name" or "Grid Name" -> site.site_name
   - "Lat"/"Latitude" -> location.lat
   - "Total kWp" or "System kWp" -> energy.total_kwp
   - "Served Buildings" or "Connections" -> meta.served_building_count
   - "Cable Length" -> computed.cable_length_m

6. Respond with ONLY a JSON object mapping label -> key. Fewer matches is better than wrong matches.

Example response:
{{"Site Name": "site.site_name", "Number of Poles": "meta.pole_count", "Total kWp": "energy.total_kwp"}}

Your response (JSON only):"""


class LLMCellMapper:
    """Uses LLM to intelligently map sheet labels to data keys."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
    ):
        """Initialize the mapper.

        Args:
            api_key: Google API key (defaults to GOOGLE_API_KEY env var)
            model: Gemini model (defaults to VERIFICATION_MODEL for fast/cheap matching)
        """
        self._api_key = api_key or os.getenv("GOOGLE_API_KEY", "")
        self._model = model or os.getenv("VERIFICATION_MODEL", "gemini-2.5-flash-lite")
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))
        return self._client

    async def map_labels_to_keys(
        self,
        input_labels: List[str],
        available_keys: List[str],
        available_values: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, str]:
        """Map input labels to data keys using LLM.

        Args:
            input_labels: Labels from column A of spreadsheet
            available_keys: All available data keys (e.g., "site.site_name")
            available_values: Optional dict of key->value for context

        Returns:
            Dict mapping input label -> data key (only for matched pairs)
        """
        if not input_labels or not available_keys:
            return {}

        if not self._api_key:
            LOGGER.error("GOOGLE_API_KEY not set - cannot perform LLM mapping")
            return {}

        # Filter out empty labels
        input_labels = [label for label in input_labels if label and label.strip()]
        if not input_labels:
            return {}

        # Build key info with optional values for better matching
        key_info = available_keys
        if available_values:
            key_info = [
                (
                    f"{key} (e.g., {available_values.get(key)})"
                    if key in available_values and available_values.get(key) is not None
                    else key
                )
                for key in available_keys
            ]

        # Build prompt
        prompt = MAPPING_PROMPT.format(
            input_labels=json.dumps(input_labels, indent=2),
            available_keys="\n".join(f"- {k}" for k in key_info),
        )

        try:
            result = await self._call_gemini(prompt)
            return self._parse_mapping_response(result, available_keys)
        except Exception as e:
            LOGGER.exception(f"LLM mapping failed: {e}")
            return {}

    async def _call_gemini(self, user_message: str) -> str:
        """Make a Gemini API call for mapping."""
        endpoint = GEMINI_ENDPOINT_TEMPLATE.format(model=self._model)

        payload = {
            "contents": [{"role": "user", "parts": [{"text": user_message}]}],
            "generationConfig": {
                "temperature": 0.1,  # Low temperature for consistent matching
                "maxOutputTokens": int(os.getenv("GEMINI_LITE_MAX_OUTPUT_TOKENS", "1024")),
                "candidateCount": 1,
            },
        }

        client = await self._get_client()

        LOGGER.debug(f"Calling LLM mapping endpoint: {endpoint}")

        response = await client.post(
            endpoint,
            params={"key": self._api_key},
            json=payload,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            error_body = response.text
            LOGGER.error(f"LLM mapping API error {response.status_code}: {error_body}")
            response.raise_for_status()

        result = response.json()

        # Extract text from Gemini response
        try:
            candidates = result.get("candidates", [])
            if candidates:
                content = candidates[0].get("content", {})
                parts = content.get("parts", [])
                if parts:
                    return str(parts[0].get("text", ""))
        except (KeyError, IndexError) as e:
            LOGGER.error(f"Failed to extract text from Gemini response: {e}")

        return ""

    def _parse_mapping_response(
        self,
        response_text: str,
        available_keys: List[str],
    ) -> Dict[str, str]:
        """Parse the JSON mapping response from LLM.

        Args:
            response_text: Raw LLM response
            available_keys: Valid keys to validate against

        Returns:
            Validated mapping dict
        """
        if not response_text:
            LOGGER.warning("Empty LLM mapping response")
            return {}

        # Clean up response - remove markdown code blocks if present
        json_text = response_text.strip()
        if json_text.startswith("```"):
            lines = json_text.split("\n")
            json_lines = []
            in_block = False
            for line in lines:
                if line.startswith("```"):
                    in_block = not in_block
                    continue
                if in_block:
                    json_lines.append(line)
            json_text = "\n".join(json_lines).strip()

        try:
            data = json.loads(json_text)

            if not isinstance(data, dict):
                LOGGER.warning(f"LLM mapping returned non-dict: {type(data)}")
                return {}

            # Validate that all values are in available_keys
            validated: Dict[str, str] = {}
            available_keys_set = set(available_keys)

            for label, key in data.items():
                if not isinstance(label, str) or not isinstance(key, str):
                    continue

                if key in available_keys_set:
                    validated[label] = key
                else:
                    LOGGER.debug(f"LLM suggested invalid key '{key}' for '{label}'")

            LOGGER.info(f"LLM mapping: {len(validated)} labels matched to keys")
            return validated

        except json.JSONDecodeError as e:
            LOGGER.warning(f"Failed to parse LLM mapping JSON: {e}")
            return {}

    async def aclose(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "LLMCellMapper":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()


__all__ = ["LLMCellMapper"]
